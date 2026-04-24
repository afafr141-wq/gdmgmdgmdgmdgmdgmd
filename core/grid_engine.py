"""
AI Dynamic Grid Engine — KuCoin AI Plus behaviour (Grid Expansion).

Startup:
  - Places INITIAL_ORDERS_PER_SIDE limit buys below price.
  - Places INITIAL_ORDERS_PER_SIDE limit sells above price.
  - No upfront base-currency purchase; capital is deployed only as orders fill.
  - Each order size = total_investment / grid_count / current_price.

Fill handling:
  - Buy filled  → place new buy one grid lower + sell at the filled level.
  - Sell filled → place new sell one grid higher + buy at the filled level.

This "expansion" means the grid grows organically with price movement
instead of locking all capital into 20+ orders at once.

ATR rebalance (every 5 min):
  - If price escapes range by ≥ 1×ATR, cancel all orders and rebuild.
"""
import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Optional

from config.settings import (
    ATR_REFRESH_SECONDS,
    CANDLE_TIMEFRAME,
    FILL_POLL_INTERVAL,
    RISK_PROFILES,
)
from core.mexc_client import MexcClient
from utils import db_manager as db

logger = logging.getLogger(__name__)

# How many active orders to keep on each side at startup
INITIAL_ORDERS_PER_SIDE = 2

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


# ── ATR (Wilder) ───────────────────────────────────────────────────────────────

def compute_atr(ohlcv: list, period: int = 14) -> float:
    if len(ohlcv) < 2:
        return 0.0
    true_ranges = []
    for i in range(1, len(ohlcv)):
        h, l, pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
        true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not true_ranges:
        return 0.0
    atr = sum(true_ranges[:period]) / min(period, len(true_ranges))
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ── Grid parameters ────────────────────────────────────────────────────────────

@dataclass
class GridParams:
    lower: float          # range lower bound
    upper: float          # range upper bound
    grid_count: int       # total number of grid levels
    grid_spacing: float   # price distance between levels
    qty_per_grid: float   # base-currency qty per order
    atr: float


def derive_grid_params(
    current_price: float,
    atr: float,
    total_investment: float,
    risk: str,
    client: MexcClient,
    symbol: str,
) -> GridParams:
    profile = RISK_PROFILES[risk]
    mult    = profile["atr_multiplier"]
    min_g   = profile["min_grids"]
    max_g   = profile["max_grids"]

    lower = max(current_price - atr * mult, current_price * 0.01)
    upper = current_price + atr * mult

    vol_ratio  = atr / current_price
    raw_grids  = int(min_g + (max_g - min_g) * min(vol_ratio / 0.02, 1.0))
    grid_count = max(min_g, min(raw_grids, max_g))

    grid_spacing = (upper - lower) / grid_count
    # Each grid level gets an equal share of the total investment.
    # qty = (total_investment / grid_count) / current_price
    raw_qty      = total_investment / grid_count / current_price
    # Hard cap: a single grid order must not exceed half the total investment
    max_qty      = (total_investment / 2) / current_price
    qty_per_grid = client.round_amount(symbol, min(raw_qty, max_qty))

    return GridParams(
        lower        = client.round_price(symbol, lower),
        upper        = client.round_price(symbol, upper),
        grid_count   = grid_count,
        grid_spacing = client.round_price(symbol, grid_spacing),
        qty_per_grid = qty_per_grid,
        atr          = atr,
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
        atr    = await self._fetch_atr(symbol, RISK_PROFILES[risk]["atr_period"])
        params = derive_grid_params(price, atr, total_investment, risk, self._client, symbol)

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
            "current_atr":      params.atr,
            "is_active":        True,
        })
        state.grid_id = grid_id

        await self._place_initial_orders(state, price)
        self._tasks[symbol] = asyncio.create_task(self._run_loop(state))

        logger.info(
            "Grid started: %s | range %.4f–%.4f | %d grids | spacing=%.4f | ATR=%.4f",
            symbol, params.lower, params.upper, params.grid_count, params.grid_spacing, params.atr,
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
        Return False and fire an error notification if a single order would cost
        more than 2× the per-grid budget (total_investment / grid_count).
        This catches runaway orders caused by stale params or rounding bugs.
        """
        cost      = price * qty
        max_cost  = (state.total_investment / state.params.grid_count) * 2
        if cost > max_cost:
            logger.error(
                "ORDER BLOCKED %s: cost=%.4f USDT exceeds budget cap=%.4f USDT "
                "(investment=%.2f / grids=%d × 2)",
                state.symbol, cost, max_cost, state.total_investment, state.params.grid_count,
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

    # ── Initial order placement (KuCoin-style) ─────────────────────────────────

    async def _place_initial_orders(self, state: GridState, price: float) -> None:
        """
        Place INITIAL_ORDERS_PER_SIDE limit buys below price and
        INITIAL_ORDERS_PER_SIDE limit sells above price.
        No upfront base-currency purchase — sell orders are placed speculatively
        and will only fill after the corresponding buy orders execute first.
        """
        p = state.params

        # ── Limit buy orders below price ───────────────────────────────────────
        for i in range(1, INITIAL_ORDERS_PER_SIDE + 1):
            level = self._client.round_price(state.symbol, price - i * p.grid_spacing)
            if level < p.lower:
                break
            order = await self._client.place_limit_buy(state.symbol, level, p.qty_per_grid)
            if order:
                state.open_orders[order["id"]] = {"side": "buy", "price": level, "qty": p.qty_per_grid}

        # ── Limit sell orders above price ──────────────────────────────────────
        for i in range(1, INITIAL_ORDERS_PER_SIDE + 1):
            level = self._client.round_price(state.symbol, price + i * p.grid_spacing)
            if level > p.upper:
                break
            order = await self._client.place_limit_sell(state.symbol, level, p.qty_per_grid)
            if order:
                state.open_orders[order["id"]] = {"side": "sell", "price": level, "qty": p.qty_per_grid}

        logger.info(
            "Initial orders placed for %s: %d total (%d buys + %d sells)",
            state.symbol, len(state.open_orders),
            sum(1 for m in state.open_orders.values() if m["side"] == "buy"),
            sum(1 for m in state.open_orders.values() if m["side"] == "sell"),
        )

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _run_loop(self, state: GridState) -> None:
        loop = asyncio.get_event_loop()
        last_atr_refresh  = loop.time()
        last_hourly_report = loop.time()
        HOURLY = 3600

        while state.running:
            try:
                now = loop.time()

                # ATR rebalance every 5 min
                if now - last_atr_refresh >= ATR_REFRESH_SECONDS:
                    await self._maybe_rebalance(state)
                    last_atr_refresh = now

                # Hourly summary report
                if now - last_hourly_report >= HOURLY:
                    await self._send_hourly_report(state)
                    last_hourly_report = now

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
        Cancel all open orders and rebuild the grid at the current price/ATR.
        Used by /upgrade command and called for every active grid on startup.
        """
        state = self._grids.get(symbol)
        if not state or not state.running:
            raise ValueError(f"No active grid for {symbol}")
        profile = RISK_PROFILES[state.risk]
        price   = await self._client.get_current_price(symbol)
        atr     = await self._fetch_atr(symbol, profile["atr_period"])
        await self._rebuild(state, price, atr)
        logger.info("Grid upgraded: %s @ %.4f", symbol, price)

    # ── ATR rebalance ──────────────────────────────────────────────────────────

    async def _maybe_rebalance(self, state: GridState) -> None:
        profile = RISK_PROFILES[state.risk]
        atr     = await self._fetch_atr(state.symbol, profile["atr_period"])
        price   = await self._client.get_current_price(state.symbol)
        trigger = profile["rebalance_trigger"] * atr
        outside = price < state.params.lower - trigger or price > state.params.upper + trigger

        if outside:
            reason = (
                "السعر تجاوز النطاق العلوي"
                if price > state.params.upper + trigger
                else "السعر تجاوز النطاق السفلي"
            )
            old_lower = state.params.lower
            old_upper = state.params.upper
            logger.info("Price %.4f escaped range — rebuilding grid for %s", price, state.symbol)
            await self._rebuild(state, price, atr)
            await _fire(_notify_grid_rebuild and _notify_grid_rebuild(
                state.symbol,
                reason,
                price,
                price,
                state.params.lower,
                state.params.upper,
                state.params.grid_count,
                atr,
            ))
        else:
            state.params.atr = atr
            await db.upsert_grid({"symbol": state.symbol, "current_atr": atr})

    async def _rebuild(self, state: GridState, price: float, atr: float) -> None:
        await self._client.cancel_all_orders(state.symbol)
        state.open_orders.clear()
        params = derive_grid_params(
            price, atr, state.total_investment, state.risk, self._client, state.symbol
        )
        state.params = params
        await db.upsert_grid({
            "symbol":       state.symbol,
            "lower_price":  params.lower,
            "upper_price":  params.upper,
            "grid_count":   params.grid_count,
            "grid_spacing": params.grid_spacing,
            "current_atr":  params.atr,
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

        if side == "buy":
            # Update average cost
            total_cost          = state.avg_buy_price * state.held_qty + fill_price * qty
            state.held_qty     += qty
            state.avg_buy_price = total_cost / state.held_qty if state.held_qty else 0.0

            # Place sell one grid above the fill
            sell_price = self._client.round_price(state.symbol, fill_price + state.params.grid_spacing)
            if self._guard_order_cost(state, sell_price, qty):
                sell_order = await self._client.place_limit_sell(state.symbol, sell_price, qty)
                if sell_order:
                    state.open_orders[sell_order["id"]] = {"side": "sell", "price": sell_price, "qty": qty}

            # Expand: place next buy one grid lower — only if holdings are within budget
            max_holdings = state.total_investment / fill_price
            next_buy_price = self._client.round_price(state.symbol, fill_price - state.params.grid_spacing)
            if (
                next_buy_price >= state.params.lower
                and state.held_qty < max_holdings
                and self._guard_order_cost(state, next_buy_price, state.params.qty_per_grid)
            ):
                buy_order = await self._client.place_limit_buy(state.symbol, next_buy_price, state.params.qty_per_grid)
                if buy_order:
                    state.open_orders[buy_order["id"]] = {"side": "buy", "price": next_buy_price, "qty": state.params.qty_per_grid}
                    await _fire(_notify_grid_expansion and _notify_grid_expansion(
                        state.symbol, "down", next_buy_price, "buy"
                    ))
            elif state.held_qty >= max_holdings:
                logger.warning(
                    "HOLDINGS CAP reached for %s: held=%.4f >= max=%.4f (investment=%.2f) — buy skipped",
                    state.symbol, state.held_qty, max_holdings, state.total_investment,
                )

            # Notify buy fill — grid_level = buys executed so far (held_qty / qty_per_grid)
            buy_level = max(1, round(state.held_qty / state.params.qty_per_grid)) if state.params.qty_per_grid else 1
            await _fire(_notify_buy_filled and _notify_buy_filled(
                state.symbol, fill_price, qty,
                grid_level=buy_level,
                grid_total=state.params.grid_count,
            ))

        else:  # sell
            pnl              = (fill_price - state.avg_buy_price) * qty
            state.realized_pnl += pnl
            state.sell_count += 1
            state.held_qty    = max(0.0, state.held_qty - qty)

            # Place buy one grid below the fill — only if holdings are within budget
            max_holdings = state.total_investment / fill_price
            buy_price = self._client.round_price(state.symbol, fill_price - state.params.grid_spacing)
            if (
                buy_price >= state.params.lower
                and state.held_qty < max_holdings
                and self._guard_order_cost(state, buy_price, qty)
            ):
                buy_order = await self._client.place_limit_buy(state.symbol, buy_price, qty)
                if buy_order:
                    state.open_orders[buy_order["id"]] = {"side": "buy", "price": buy_price, "qty": qty}
            elif state.held_qty >= max_holdings:
                logger.warning(
                    "HOLDINGS CAP reached for %s: held=%.4f >= max=%.4f — buy skipped",
                    state.symbol, state.held_qty, max_holdings,
                )

            # Expand: place next sell one grid higher
            next_sell_price = self._client.round_price(state.symbol, fill_price + state.params.grid_spacing)
            if next_sell_price <= state.params.upper and self._guard_order_cost(state, next_sell_price, state.params.qty_per_grid):
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

    async def _fetch_atr(self, symbol: str, period: int) -> float:
        ohlcv = await self._client.fetch_ohlcv(symbol, CANDLE_TIMEFRAME, limit=period + 5)
        atr   = compute_atr(ohlcv, period)
        if atr <= 0:
            price = await self._client.get_current_price(symbol)
            atr   = price * 0.01
        return atr

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
