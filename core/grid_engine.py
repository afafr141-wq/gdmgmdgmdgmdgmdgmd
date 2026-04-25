"""
Fixed-Spacing Grid Engine.

Startup:
  - Places MAX_ORDERS_PER_SIDE (3) limit buys below price, each 1% apart.
  - Places MAX_ORDERS_PER_SIDE (3) limit sells above price, each 1% apart.
  - No upfront base-currency purchase; capital is deployed only as orders fill.
  - Each order size = total_investment / (MAX_ORDERS_PER_SIDE * 2) / current_price.

Fill handling:
  - Buy filled  → place sell at fill_price + ORDER_PERCENT, then next buy lower (if under holdings cap).
  - Sell filled → place buy at fill_price - ORDER_PERCENT (if under holdings cap), then next sell higher.

Holdings cap:
  - Max allowed qty = (total_investment / 2) / current_price.
  - No new buy orders are placed once this cap is reached.

Range & re-centering:
  - Fixed: lower = price × (1 - MAX_ORDERS_PER_SIDE × ORDER_PERCENT)
           upper = price × (1 + MAX_ORDERS_PER_SIDE × ORDER_PERCENT)
  - If the live price escapes the range (price < lower or price > upper),
    all orders are cancelled and the grid is rebuilt around the new price.
  - Checked every FILL_POLL_INTERVAL seconds (same cycle as fill polling).
"""
import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Optional

from config.settings import (
    FILL_POLL_INTERVAL,
)
from core.mexc_client import MexcClient
from utils import db_manager as db

logger = logging.getLogger(__name__)

# ── Grid constants ─────────────────────────────────────────────────────────────
ORDER_PERCENT       = 0.01   # 1% spacing between each order
MAX_ORDERS_PER_SIDE = 3      # 3 buys + 3 sells = 6 total orders per grid

# Notification functions — injected at runtime to avoid circular import
_notify_buy_filled    = None
_notify_sell_filled   = None
_notify_grid_rebuild  = None
_notify_grid_expansion = None
_notify_error         = None


def set_notifiers(
    buy_filled,
    sell_filled,
    grid_rebuild,
    grid_expansion,
    error,
) -> None:
    """Called from main.py after both engine and bot are initialised."""
    global _notify_buy_filled, _notify_sell_filled
    global _notify_grid_rebuild, _notify_grid_expansion, _notify_error
    _notify_buy_filled    = buy_filled
    _notify_sell_filled   = sell_filled
    _notify_grid_rebuild  = grid_rebuild
    _notify_grid_expansion = grid_expansion
    _notify_error         = error


async def _fire(coro_or_none) -> None:
    """Await a notification coroutine safely — never blocks the engine."""
    if coro_or_none is None:
        return
    try:
        await coro_or_none
    except Exception as exc:
        logger.error("Notification error: %s", exc)


# ── Grid parameters ────────────────────────────────────────────────────────────

@dataclass
class GridParams:
    lower: float          # range lower bound  = price × (1 - N × ORDER_PERCENT)
    upper: float          # range upper bound  = price × (1 + N × ORDER_PERCENT)
    grid_count: int       # total order slots  = MAX_ORDERS_PER_SIDE × 2
    grid_spacing: float   # price step between levels (ORDER_PERCENT of entry price)
    qty_per_grid: float   # base-currency qty per order
    atr: float            # kept for DB/report compatibility (always 0.0)


def derive_grid_params(
    current_price: float,
    total_investment: float,
    client: MexcClient,
    symbol: str,
) -> GridParams:
    """
    Build fixed-spacing grid params.
    - Range: ±(MAX_ORDERS_PER_SIDE × ORDER_PERCENT) around current_price.
    - Each order is ORDER_PERCENT apart (compound, so level i = price × (1 ± i × ORDER_PERCENT)).
    - qty_per_grid = total_investment / grid_count / current_price,
      capped at (total_investment / 2) / current_price.
    """
    grid_count   = MAX_ORDERS_PER_SIDE * 2
    lower        = current_price * (1 - MAX_ORDERS_PER_SIDE * ORDER_PERCENT)
    upper        = current_price * (1 + MAX_ORDERS_PER_SIDE * ORDER_PERCENT)
    grid_spacing = current_price * ORDER_PERCENT   # nominal step for reference

    raw_qty      = total_investment / grid_count / current_price
    max_qty      = (total_investment / 2) / current_price
    qty_per_grid = client.round_amount(symbol, min(raw_qty, max_qty))

    return GridParams(
        lower        = client.round_price(symbol, lower),
        upper        = client.round_price(symbol, upper),
        grid_count   = grid_count,
        grid_spacing = client.round_price(symbol, grid_spacing),
        qty_per_grid = qty_per_grid,
        atr          = 0.0,
    )


# ── Grid state ─────────────────────────────────────────────────────────────────

@dataclass
class GridState:
    symbol:           str
    risk:             str
    total_investment: float
    params:           GridParams
    grid_id:          int = 0
    # order_id → {"side": "buy"|"sell", "price": float, "qty": float}
    open_orders:      dict = field(default_factory=dict)
    avg_buy_price:    float = 0.0
    held_qty:         float = 0.0
    realized_pnl:     float = 0.0
    sell_count:       int   = 0
    started_at:       datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    running:          bool  = True


# ── Engine ─────────────────────────────────────────────────────────────────────

class GridEngine:
    def __init__(self, client: MexcClient, notify: Callable[[str], Coroutine]) -> None:
        self._client  = client
        self._notify  = notify
        self._grids:  dict[str, GridState] = {}
        self._tasks:  dict[str, asyncio.Task] = {}

    # ── Public ─────────────────────────────────────────────────────────────────

    async def start(self, symbol: str, total_investment: float, risk: str = "medium") -> GridState:
        if symbol in self._grids and self._grids[symbol].running:
            raise ValueError(f"Grid already running for {symbol}")

        price  = await self._client.get_current_price(symbol)
        params = derive_grid_params(price, total_investment, self._client, symbol)

        state = GridState(symbol=symbol, risk=risk, total_investment=total_investment, params=params)
        self._grids[symbol] = state

        grid_id = await db.upsert_grid({
            "symbol":           symbol,
            "risk_level":       risk,
            "total_investment": total_investment,
            "lower_price":      params.lower,
            "upper_price":      params.upper,
            "grid_count":       params.grid_count,
            "grid_spacing":     params.grid_spacing,
            "current_atr":      0.0,
            "is_active":        True,
        })
        state.grid_id = grid_id

        await self._place_initial_orders(state, price)
        # Persist the initial market-buy position (held_qty, avg_buy_price) to DB
        await self._sync_pnl(state)
        self._tasks[symbol] = asyncio.create_task(self._run_loop(state))

        logger.info(
            "Grid started: %s | range %.4f–%.4f | %d grids | spacing=%.4f%% | held=%.6f @ %.4f",
            symbol, params.lower, params.upper, params.grid_count, ORDER_PERCENT * 100,
            state.held_qty, state.avg_buy_price,
        )
        return state

    async def stop(self, symbol: str, market_sell: bool = True) -> float:
        state = self._grids.get(symbol)
        if not state:
            raise ValueError(f"No active grid for {symbol}")

        state.running = False
        task = self._tasks.pop(symbol, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self._client.cancel_all_orders(symbol)

        sell_value = 0.0
        if market_sell:
            order = await self._client.market_sell_all(symbol)
            if order and order.get("cost"):
                sell_value = float(order["cost"])

        await db.deactivate_grid(symbol)
        await db.save_snapshot(symbol, self._snapshot(state))
        del self._grids[symbol]
        logger.info("Grid stopped: %s", symbol)
        return sell_value

    def get_state(self, symbol: str) -> Optional[GridState]:
        return self._grids.get(symbol)

    def active_symbols(self) -> list[str]:
        return [s for s, g in self._grids.items() if g.running]

    # ── Budget guard ───────────────────────────────────────────────────────────

    def _guard_order_cost(self, state: GridState, price: float, qty: float) -> bool:
        """
        Return False if a single order would cost more than 2× the per-grid budget.
        Per-grid budget = total_investment / (MAX_ORDERS_PER_SIDE * 2).
        """
        cost     = price * qty
        max_cost = (state.total_investment / (MAX_ORDERS_PER_SIDE * 2)) * 2
        if cost > max_cost:
            logger.error(
                "ORDER BLOCKED %s: cost=%.4f USDT exceeds budget cap=%.4f USDT "
                "(investment=%.2f / %d orders × 2)",
                state.symbol, cost, max_cost, state.total_investment, MAX_ORDERS_PER_SIDE * 2,
            )
            asyncio.ensure_future(_fire(
                _notify_error and _notify_error(
                    state.symbol,
                    "تجاوز ميزانية الأمر",
                    f"تم رفض أمر بقيمة {cost:.2f} USDT — الحد المسموح {max_cost:.2f} USDT",
                )
            ))
            return False
        return True

    # ── Initial order placement ────────────────────────────────────────────────

    async def _place_initial_orders(self, state: GridState, price: float) -> None:
        """
        1. Market-buy half the total buy-side allocation immediately so the bot
           holds a position from the start and profits if price rises right away.
           initial_buy_qty = (MAX_ORDERS_PER_SIDE × qty_per_grid) / 2

        2. Place MAX_ORDERS_PER_SIDE limit buys below the execution price.
        3. Place MAX_ORDERS_PER_SIDE limit sells above the execution price.

        The pending orders are centred on the actual market-fill price, not the
        pre-fetch price, so slippage is absorbed correctly.
        """
        p = state.params

        # ── Step 1: immediate market buy (half of buy-side allocation) ─────────
        initial_buy_qty = self._client.round_amount(
            state.symbol,
            (MAX_ORDERS_PER_SIDE * p.qty_per_grid) / 2,
        )
        fill_price = price   # fallback if order response lacks average
        market_order = await self._client.market_buy(state.symbol, initial_buy_qty)
        if market_order:
            filled_qty  = float(market_order.get("filled") or initial_buy_qty)
            fill_price  = float(market_order.get("average") or market_order.get("price") or price)
            state.held_qty     += filled_qty
            state.avg_buy_price = fill_price
            logger.info(
                "Initial market buy %s: qty=%.6f @ %.6f",
                state.symbol, filled_qty, fill_price,
            )
            await db.record_trade(
                state.symbol, "buy", fill_price, filled_qty,
                market_order.get("id", ""), state.grid_id, pnl=0.0,
            )
        else:
            logger.warning(
                "Initial market buy skipped for %s — proceeding with limit orders only",
                state.symbol,
            )

        # ── Step 2: limit buy orders below fill price ──────────────────────────
        for i in range(1, MAX_ORDERS_PER_SIDE + 1):
            level = self._client.round_price(state.symbol, fill_price * (1 - i * ORDER_PERCENT))
            order = await self._client.place_limit_buy(state.symbol, level, p.qty_per_grid)
            if order:
                state.open_orders[order["id"]] = {"side": "buy", "price": level, "qty": p.qty_per_grid}

        # ── Step 3: limit sell orders above fill price ─────────────────────────
        for i in range(1, MAX_ORDERS_PER_SIDE + 1):
            level = self._client.round_price(state.symbol, fill_price * (1 + i * ORDER_PERCENT))
            order = await self._client.place_limit_sell(state.symbol, level, p.qty_per_grid)
            if order:
                state.open_orders[order["id"]] = {"side": "sell", "price": level, "qty": p.qty_per_grid}

        logger.info(
            "Initial orders placed for %s: market_buy=%.6f @ %.6f | %d limit orders (%d buys + %d sells)",
            state.symbol, state.held_qty, fill_price, len(state.open_orders),
            sum(1 for m in state.open_orders.values() if m["side"] == "buy"),
            sum(1 for m in state.open_orders.values() if m["side"] == "sell"),
        )

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _run_loop(self, state: GridState) -> None:
        loop = asyncio.get_event_loop()
        last_hourly_report = loop.time()
        HOURLY = 3600

        while state.running:
            try:
                now = loop.time()

                # Hourly summary report
                if now - last_hourly_report >= HOURLY:
                    await self._send_hourly_report(state)
                    last_hourly_report = now

                # Re-center grid if price escaped the range
                await self._check_recentering(state)

                await self._poll_fills(state)
                await asyncio.sleep(FILL_POLL_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Grid loop error for %s: %s", state.symbol, exc)
                await _fire(_notify_error and _notify_error(
                    state.symbol, type(exc).__name__, str(exc)[:200]
                ))
                await asyncio.sleep(30)

    async def _check_recentering(self, state: GridState) -> None:
        """
        Rebuild the grid around the current price if it has escaped the range.
        Triggered when: price < params.lower  OR  price > params.upper
        """
        price = await self._client.get_current_price(state.symbol)
        if state.params.lower <= price <= state.params.upper:
            return  # still inside range — nothing to do

        direction = "العلوي" if price > state.params.upper else "السفلي"
        old_lower = state.params.lower
        old_upper = state.params.upper

        logger.info(
            "Re-centering grid for %s: price=%.4f escaped range [%.4f, %.4f]",
            state.symbol, price, old_lower, old_upper,
        )

        await self._rebuild(state, price)

        await _fire(_notify_grid_rebuild and _notify_grid_rebuild(
            state.symbol,
            f"السعر تجاوز النطاق {direction}",
            price,
            price,
            state.params.lower,
            state.params.upper,
            state.params.grid_count,
            0.0,
        ))

    async def _send_hourly_report(self, state: GridState) -> None:
        try:
            from bot import telegram_bot as tgbot
            price  = await self._client.get_current_price(state.symbol)
            report = self.calc_profit_report(state, price)
            await _fire(tgbot.notify_hourly_report(state.symbol, report))
        except Exception as exc:
            logger.error("Hourly report error for %s: %s", state.symbol, exc)

    # ── Manual upgrade ─────────────────────────────────────────────────────────

    async def upgrade_grid(self, symbol: str) -> None:
        """
        Cancel all open orders and rebuild the grid at the current price.
        Used by /upgrade command and called for every active grid on startup.
        """
        state = self._grids.get(symbol)
        if not state or not state.running:
            raise ValueError(f"No active grid for {symbol}")
        price = await self._client.get_current_price(symbol)
        await self._rebuild(state, price)
        logger.info("Grid upgraded: %s @ %.4f", symbol, price)

    async def _rebuild(self, state: GridState, price: float) -> None:
        """Cancel all orders and re-place the fixed 3+3 grid around the current price."""
        await self._client.cancel_all_orders(state.symbol)
        state.open_orders.clear()
        params = derive_grid_params(price, state.total_investment, self._client, state.symbol)
        state.params = params
        await db.upsert_grid({
            "symbol":       state.symbol,
            "lower_price":  params.lower,
            "upper_price":  params.upper,
            "grid_count":   params.grid_count,
            "grid_spacing": params.grid_spacing,
            "current_atr":  0.0,
        })
        await self._place_initial_orders(state, price)

    # ── Fill polling ───────────────────────────────────────────────────────────

    async def _poll_fills(self, state: GridState) -> None:
        if not state.open_orders:
            return
        for order_id, meta in list(state.open_orders.items()):
            order = await self._client.fetch_order(state.symbol, order_id)
            if not order:
                continue
            status = order.get("status", "")
            if status == "closed":
                del state.open_orders[order_id]
                await self._handle_fill(state, meta, order)
            elif status == "canceled":
                del state.open_orders[order_id]

    async def _handle_fill(self, state: GridState, meta: dict, order: dict) -> None:
        side        = meta["side"]
        fill_price  = float(order.get("average") or order.get("price") or meta["price"])
        qty         = float(order.get("filled") or meta["qty"])
        pnl = 0.0

        # Max allowed holdings = half the total investment at current fill price
        max_allowed_qty = (state.total_investment / 2) / fill_price

        if side == "buy":
            # Update average cost
            total_cost          = state.avg_buy_price * state.held_qty + fill_price * qty
            state.held_qty     += qty
            state.avg_buy_price = total_cost / state.held_qty if state.held_qty else 0.0

            # Place sell one step above the fill (1% higher)
            sell_price = self._client.round_price(state.symbol, fill_price * (1 + ORDER_PERCENT))
            if self._guard_order_cost(state, sell_price, qty):
                sell_order = await self._client.place_limit_sell(state.symbol, sell_price, qty)
                if sell_order:
                    state.open_orders[sell_order["id"]] = {"side": "sell", "price": sell_price, "qty": qty}

            # Place next buy one step lower — only if under holdings cap
            next_buy_price = self._client.round_price(state.symbol, fill_price * (1 - ORDER_PERCENT))
            if state.held_qty >= max_allowed_qty:
                logger.warning(
                    "HOLDINGS CAP reached for %s: held=%.6f >= max=%.6f (investment/2=%.2f) — buy skipped",
                    state.symbol, state.held_qty, max_allowed_qty, state.total_investment / 2,
                )
            elif (
                next_buy_price >= state.params.lower
                and self._guard_order_cost(state, next_buy_price, state.params.qty_per_grid)
            ):
                buy_order = await self._client.place_limit_buy(state.symbol, next_buy_price, state.params.qty_per_grid)
                if buy_order:
                    state.open_orders[buy_order["id"]] = {"side": "buy", "price": next_buy_price, "qty": state.params.qty_per_grid}
                    await _fire(_notify_grid_expansion and _notify_grid_expansion(
                        state.symbol, "down", next_buy_price, "buy"
                    ))

            # Notify buy fill
            buy_level = max(1, round(state.held_qty / state.params.qty_per_grid)) if state.params.qty_per_grid else 1
            await _fire(_notify_buy_filled and _notify_buy_filled(
                state.symbol, fill_price, qty,
                grid_level=buy_level,
                grid_total=state.params.grid_count,
            ))

        else:  # sell
            pnl                = (fill_price - state.avg_buy_price) * qty
            state.realized_pnl += pnl
            state.sell_count   += 1
            state.held_qty      = max(0.0, state.held_qty - qty)

            # Place buy one step below the fill — only if under holdings cap
            buy_price = self._client.round_price(state.symbol, fill_price * (1 - ORDER_PERCENT))
            if state.held_qty >= max_allowed_qty:
                logger.warning(
                    "HOLDINGS CAP reached for %s: held=%.6f >= max=%.6f — buy skipped",
                    state.symbol, state.held_qty, max_allowed_qty,
                )
            elif (
                buy_price >= state.params.lower
                and self._guard_order_cost(state, buy_price, state.params.qty_per_grid)
            ):
                buy_order = await self._client.place_limit_buy(state.symbol, buy_price, state.params.qty_per_grid)
                if buy_order:
                    state.open_orders[buy_order["id"]] = {"side": "buy", "price": buy_price, "qty": state.params.qty_per_grid}

            # Place next sell one step higher
            next_sell_price = self._client.round_price(state.symbol, fill_price * (1 + ORDER_PERCENT))
            if (
                next_sell_price <= state.params.upper
                and self._guard_order_cost(state, next_sell_price, state.params.qty_per_grid)
            ):
                sell_order = await self._client.place_limit_sell(state.symbol, next_sell_price, state.params.qty_per_grid)
                if sell_order:
                    state.open_orders[sell_order["id"]] = {"side": "sell", "price": next_sell_price, "qty": state.params.qty_per_grid}
                    await _fire(_notify_grid_expansion and _notify_grid_expansion(
                        state.symbol, "up", next_sell_price, "sell"
                    ))

            # Notify sell fill
            await _fire(_notify_sell_filled and _notify_sell_filled(
                state.symbol, fill_price, qty, pnl
            ))

        await db.record_trade(state.symbol, side, fill_price, qty, order.get("id",""), state.grid_id, pnl)
        await self._sync_pnl(state)
        logger.info("Fill: %s %s qty=%.6f @ %.4f | pnl=%.4f | realized=%.4f",
                    side.upper(), state.symbol, qty, fill_price, pnl, state.realized_pnl)

    async def _sync_pnl(self, state: GridState) -> None:
        await db.update_grid_pnl(
            state.symbol, state.realized_pnl,
            state.avg_buy_price, state.held_qty, state.sell_count,
        )

    # ── Profit report ──────────────────────────────────────────────────────────

    def calc_profit_report(self, state: GridState, current_price: float) -> dict:
        p = state.params
        days = max(
            (datetime.now(timezone.utc) - state.started_at).total_seconds() / 86400,
            1 / 1440,
        )
        # grid_profit: each completed sell cycle earns grid_spacing × qty_per_grid
        grid_profit    = p.grid_spacing * p.qty_per_grid * state.sell_count
        unrealised_pnl = (current_price - state.avg_buy_price) * state.held_qty
        total_profit        = state.realized_pnl + unrealised_pnl
        apy                 = (total_profit / state.total_investment) / days * 365 * 100
        grid_apy            = (grid_profit  / state.total_investment) / days * 365 * 100

        qty_per_grid_usdt = p.qty_per_grid * current_price
        min_cost          = self._client.min_cost(state.symbol)

        return {
            "symbol":              state.symbol,
            "risk":                state.risk,
            "total_investment":    state.total_investment,
            "lower":               p.lower,
            "upper":               p.upper,
            "grid_count":          p.grid_count,
            "grid_spacing":        p.grid_spacing,
            "atr":                 p.atr,
            "current_price":       current_price,
            "avg_buy_price":       state.avg_buy_price,
            "held_qty":            state.held_qty,
            "sell_count":          state.sell_count,
            "realized_pnl":        state.realized_pnl,
            "unrealised_pnl":      unrealised_pnl,
            "grid_profit":         grid_profit,
            "total_profit":        total_profit,
            "apy":                 apy,
            "grid_apy":            grid_apy,
            "days_running":        days,
            "open_orders":         len(state.open_orders),
            "active_buys":         sum(1 for m in state.open_orders.values() if m["side"] == "buy"),
            "active_sells":        sum(1 for m in state.open_orders.values() if m["side"] == "sell"),
            "qty_per_grid_usdt":   qty_per_grid_usdt,
            "min_order_cost":      min_cost,
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _snapshot(self, state: GridState) -> dict:
        return {
            "symbol":           state.symbol,
            "risk":             state.risk,
            "total_investment": state.total_investment,
            "params": {
                "lower":        state.params.lower,
                "upper":        state.params.upper,
                "grid_count":   state.params.grid_count,
                "grid_spacing": state.params.grid_spacing,
                "qty_per_grid": state.params.qty_per_grid,
                "atr":          state.params.atr,
            },
            "avg_buy_price":  state.avg_buy_price,
            "held_qty":       state.held_qty,
            "realized_pnl":   state.realized_pnl,
            "sell_count":     state.sell_count,
            "open_orders":    state.open_orders,
        }
