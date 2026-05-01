"""
Fixed-Spacing Grid Engine.

Startup:
  - User sets upper_pct% and lower_pct% to define the grid range.
  - User sets num_grids (orders per side); default is MAX_ORDERS_PER_SIDE (3).
  - Places num_grids limit buys below price and num_grids limit sells above price,
    evenly spaced within the user-defined range.
  - Market-buys half the investment upfront as the initial position.
  - Each order size = total_investment / (num_grids * 2) / current_price.

Fill handling:
  - Buy filled  → place sell one grid_spacing above fill price.
  - Sell filled → place buy one grid_spacing below fill price.
  - Holdings cap = (total_investment / 2) / current_price; no new buys beyond this.

Range & re-centering:
  - Breakout triggers when price exceeds upper_pct% above params.upper
    or lower_pct% below params.lower.
  - Rebuild is deferred until the current 1-minute candle closes to avoid
    reacting to wicks that immediately reverse.

Min-order enforcement:
  - Every order is validated against the exchange's min_amount and min_cost
    before placement; rejected orders are logged and skipped silently.

Balance drift detection:
  - Every 60 s the engine compares held_qty against the real exchange balance.
  - If drift exceeds 1% of held_qty, the user is prompted to sync.
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
ORDER_PERCENT       = 0.025  # 2.5% spacing between each order
MAX_ORDERS_PER_SIDE = 3      # 3 buys + 3 sells = 6 total orders per grid

# Notification functions — injected at runtime to avoid circular import
_notify_buy_filled     = None
_notify_sell_filled    = None
_notify_grid_rebuild   = None
_notify_grid_expansion = None
_notify_error          = None
_notify_balance_drift  = None   # called when external purchase detected


def set_notifiers(
    buy_filled,
    sell_filled,
    grid_rebuild,
    grid_expansion,
    error,
    balance_drift=None,
) -> None:
    """Called from main.py after both engine and bot are initialised."""
    global _notify_buy_filled, _notify_sell_filled
    global _notify_grid_rebuild, _notify_grid_expansion, _notify_error
    global _notify_balance_drift
    _notify_buy_filled     = buy_filled
    _notify_sell_filled    = sell_filled
    _notify_grid_rebuild   = grid_rebuild
    _notify_grid_expansion = grid_expansion
    _notify_error          = error
    _notify_balance_drift  = balance_drift


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
    num_grids: int = MAX_ORDERS_PER_SIDE,
    upper_pct: float = 3.0,
    lower_pct: float = 3.0,
) -> GridParams:
    """
    Build fixed-spacing grid params driven by the user's upper/lower percentages.

    - upper_pct: the top sell order sits exactly upper_pct% above current price.
    - lower_pct: the bottom buy order sits exactly lower_pct% below current price.
    - num_grids orders per side are spaced evenly inside those bounds.
    - qty_per_grid = total_investment / (num_grids*2) / current_price,
      capped at (total_investment / 2) / current_price.
    """
    n          = max(1, num_grids)
    grid_count = n * 2

    upper = current_price * (1 + upper_pct / 100)
    lower = current_price * (1 - lower_pct / 100)

    # Spacing = range / num_grids so the outermost order hits the bound exactly
    upper_spacing = (upper - current_price) / n
    lower_spacing = (current_price - lower) / n
    # Use the average spacing as the nominal grid_spacing (for reports/fills)
    grid_spacing  = (upper_spacing + lower_spacing) / 2

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
    upper_pct:        float = 3.0   # % above upper bound that triggers a rebuild
    lower_pct:        float = 3.0   # % below lower bound that triggers a rebuild
    grid_id:          int = 0
    # order_id → {"side": "buy"|"sell", "price": float, "qty": float}
    open_orders:      dict = field(default_factory=dict)
    avg_buy_price:    float = 0.0
    held_qty:         float = 0.0
    realized_pnl:     float = 0.0
    sell_count:       int   = 0
    started_at:       datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    running:          bool  = True
    # set to True while waiting for the 1-min candle to close after a breakout
    _pending_rebuild: bool  = field(default=False, repr=False)
    # tracked task for _wait_and_rebuild so it can be cancelled on grid stop
    _rebuild_task:    object = field(default=None, repr=False)
    # set to True while waiting for user response to a balance-sync prompt
    _pending_sync:    bool  = field(default=False, repr=False)


# ── Engine ─────────────────────────────────────────────────────────────────────

class GridEngine:
    def __init__(self, client: MexcClient, notify: Callable[[str], Coroutine]) -> None:
        self._client  = client
        self._notify  = notify
        self._grids:  dict[str, GridState] = {}
        self._tasks:  dict[str, asyncio.Task] = {}

    # ── Public ─────────────────────────────────────────────────────────────────

    async def start(
        self,
        symbol: str,
        total_investment: float,
        risk: str = "medium",
        num_grids: int = MAX_ORDERS_PER_SIDE,
        upper_pct: float = 3.0,
        lower_pct: float = 3.0,
    ) -> GridState:
        if symbol in self._grids and self._grids[symbol].running:
            raise ValueError(f"Grid already running for {symbol}")

        price  = await self._client.get_current_price(symbol)
        params = derive_grid_params(
            price, total_investment, self._client, symbol,
            num_grids=num_grids, upper_pct=upper_pct, lower_pct=lower_pct,
        )

        state = GridState(
            symbol=symbol, risk=risk, total_investment=total_investment,
            params=params, upper_pct=upper_pct, lower_pct=lower_pct,
        )
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
            "upper_pct":        upper_pct,
            "lower_pct":        lower_pct,
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

        # Cancel pending rebuild task if one is waiting for candle close
        if state._rebuild_task and not state._rebuild_task.done():
            state._rebuild_task.cancel()
            try:
                await state._rebuild_task
            except asyncio.CancelledError:
                pass
        state._rebuild_task = None
        state._pending_rebuild = False

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
        max_cost = (state.total_investment / state.params.grid_count) * 2
        if cost > max_cost:
            logger.error(
                "ORDER BLOCKED %s: cost=%.4f USDT exceeds budget cap=%.4f USDT "
                "(investment=%.2f / %d orders × 2)",
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

    # ── Initial order placement ────────────────────────────────────────────────

    async def _place_initial_orders(self, state: GridState, price: float) -> None:
        """
        1. Check existing free balance of the base currency.
           - If the free balance covers ≥ half the investment, use it as the
             initial position (no market buy needed).
           - Otherwise market-buy the shortfall up to half the total investment.

        2. Place num_grids limit buys below the fill/current price.
        3. Place num_grids limit sells above the fill/current price.
        """
        p          = state.params
        fill_price = price   # fallback

        # ── Step 1: use existing balance before buying ─────────────────────────
        base_currency = state.symbol.split("/")[0]
        try:
            free_qty = await self._client.get_balance(base_currency)
        except Exception as exc:
            logger.warning("Could not fetch %s balance: %s — assuming 0", base_currency, exc)
            free_qty = 0.0

        target_qty = self._client.round_amount(
            state.symbol,
            (state.total_investment / 2) / price,
        )
        min_amt = self._client.min_amount(state.symbol)

        if free_qty >= target_qty - min_amt:
            # Existing balance covers the target — no market buy needed
            state.held_qty      = min(free_qty, target_qty)
            state.avg_buy_price = price
            logger.info(
                "Initial position from existing balance %s: qty=%.6f @ %.6f (free=%.6f)",
                state.symbol, state.held_qty, price, free_qty,
            )
        else:
            # Buy only the shortfall
            shortfall_qty = self._client.round_amount(
                state.symbol, target_qty - max(0.0, free_qty),
            )
            market_order = None
            if shortfall_qty >= min_amt:
                market_order = await self._client.market_buy(state.symbol, shortfall_qty)

            if market_order:
                filled_qty  = float(market_order.get("filled") or shortfall_qty)
                fill_price  = float(market_order.get("average") or market_order.get("price") or price)
                state.held_qty      = free_qty + filled_qty
                state.avg_buy_price = fill_price
                logger.info(
                    "Initial market buy %s: shortfall=%.6f filled=%.6f @ %.6f (pre-existing=%.6f)",
                    state.symbol, shortfall_qty, filled_qty, fill_price, free_qty,
                )
                await db.record_trade(
                    state.symbol, "buy", fill_price, filled_qty,
                    market_order.get("id", ""), state.grid_id, pnl=0.0,
                )
            else:
                # Market buy failed or not needed — use whatever we have
                state.held_qty      = free_qty
                state.avg_buy_price = price
                logger.warning(
                    "Initial market buy skipped for %s — using existing balance %.6f",
                    state.symbol, free_qty,
                )

        # ── Step 2: limit buy orders below fill price ──────────────────────────
        n             = p.grid_count // 2
        buy_spacing   = (fill_price - p.lower) / n   # even spacing down to lower bound
        sell_spacing  = (p.upper - fill_price) / n   # even spacing up to upper bound

        for i in range(1, n + 1):
            level = self._client.round_price(state.symbol, fill_price - i * buy_spacing)
            order = await self._client.place_limit_buy(state.symbol, level, p.qty_per_grid)
            if order:
                state.open_orders[order["id"]] = {"side": "buy", "price": level, "qty": p.qty_per_grid}

        # ── Step 3: limit sell orders above fill price ─────────────────────────
        for i in range(1, n + 1):
            level = self._client.round_price(state.symbol, fill_price + i * sell_spacing)
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
        last_balance_check = loop.time()
        HOURLY = 3600
        BALANCE_CHECK_INTERVAL = 60  # check for external purchases every 60 s

        while state.running:
            try:
                now = loop.time()

                # Hourly summary report
                if now - last_hourly_report >= HOURLY:
                    await self._send_hourly_report(state)
                    last_hourly_report = now

                # Detect external purchases (balance drift)
                if now - last_balance_check >= BALANCE_CHECK_INTERVAL:
                    await self._check_balance_drift(state)
                    last_balance_check = now

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
        Trigger a grid rebuild when price breaks out beyond the user-defined
        threshold (upper_pct above params.upper, or lower_pct below params.lower).
        The rebuild is deferred until the current 1-minute candle closes, so we
        avoid reacting to wicks that immediately reverse.
        """
        price = await self._client.get_current_price(state.symbol)

        breakout_upper = price > state.params.upper * (1 + state.upper_pct / 100)
        breakout_lower = price < state.params.lower * (1 - state.lower_pct / 100)

        if not breakout_upper and not breakout_lower:
            state._pending_rebuild = False   # price came back — cancel any pending wait
            return

        if state._pending_rebuild:
            return  # already waiting for candle close in _wait_and_rebuild

        direction = "العلوي" if breakout_upper else "السفلي"
        logger.info(
            "Breakout detected for %s: price=%.4f | upper_trigger=%.4f | lower_trigger=%.4f",
            state.symbol, price,
            state.params.upper * (1 + state.upper_pct / 100),
            state.params.lower * (1 - state.lower_pct / 100),
        )
        state._pending_rebuild = True
        # Track the task so it can be cancelled if the grid stops
        state._rebuild_task = asyncio.ensure_future(self._wait_and_rebuild(state, direction))

    async def _wait_and_rebuild(self, state: GridState, direction: str) -> None:
        """
        Wait until the current 1-minute candle closes, confirm the breakout is
        still valid on the close price, then rebuild the grid.
        """
        import time as _time
        CANDLE_SECONDS = 60   # 1 minute

        # Seconds remaining until the next 1-min candle boundary
        now_ts   = _time.time()
        wait_sec = CANDLE_SECONDS - (now_ts % CANDLE_SECONDS)
        logger.info(
            "Waiting %.0fs for 1-min candle close before rebuilding %s",
            wait_sec, state.symbol,
        )
        await asyncio.sleep(wait_sec)

        if not state.running or not state._pending_rebuild:
            return  # grid was stopped or breakout cancelled while waiting

        # Re-check on the closed candle's close price (index -2 = last completed candle)
        try:
            candles = await self._client.fetch_ohlcv(state.symbol, timeframe="1m", limit=3)
            if candles and len(candles) >= 2:
                close_price = float(candles[-2][4])  # last *closed* candle
            else:
                close_price = await self._client.get_current_price(state.symbol)
        except Exception:
            close_price = await self._client.get_current_price(state.symbol)

        # Only cancel if price returned fully inside the original grid range
        still_upper = close_price > state.params.upper
        still_lower = close_price < state.params.lower

        if not still_upper and not still_lower:
            logger.info(
                "Breakout cancelled for %s: close_price=%.4f returned inside grid range [%.4f, %.4f]",
                state.symbol, close_price, state.params.lower, state.params.upper,
            )
            state._pending_rebuild = False
            return

        old_lower = state.params.lower
        old_upper = state.params.upper
        state._pending_rebuild = False

        logger.info(
            "Rebuilding grid for %s after candle close: close=%.4f range was [%.4f, %.4f]",
            state.symbol, close_price, old_lower, old_upper,
        )
        await self._rebuild(state, close_price)

        await _fire(_notify_grid_rebuild and _notify_grid_rebuild(
            state.symbol,
            f"اختراق النطاق {direction} — تم نقل الشبكة بعد إغلاق الشمعة",
            close_price,
            close_price,
            state.params.lower,
            state.params.upper,
            state.params.grid_count,
            0.0,
        ))

    async def _check_balance_drift(self, state: GridState) -> None:
        """
        Compare the real exchange balance for the base currency against
        state.held_qty. If the exchange holds significantly more than the bot
        tracks, the user likely bought externally — prompt them to sync.
        """
        if state._pending_sync:
            return  # already waiting for user response

        try:
            base = state.symbol.split("/")[0]
            real_qty = await self._client.get_balance(base)
        except Exception as exc:
            logger.debug("Balance drift check failed for %s: %s", state.symbol, exc)
            return

        # Threshold: ignore differences smaller than 1% of held_qty or dust < 0.0001
        threshold = max(state.held_qty * 0.01, 0.0001)
        drift = real_qty - state.held_qty

        if drift > threshold:
            logger.info(
                "Balance drift detected for %s: exchange=%.6f bot=%.6f drift=%.6f",
                state.symbol, real_qty, state.held_qty, drift,
            )
            state._pending_sync = True
            await _fire(_notify_balance_drift and _notify_balance_drift(
                state.symbol, state.held_qty, real_qty, drift,
            ))

    async def sync_balance(self, symbol: str) -> dict:
        """
        Update held_qty and avg_buy_price from the real exchange balance.
        Called when the user confirms they want to sync after an external purchase.
        Returns a dict with old_qty, new_qty, drift.
        """
        state = self._grids.get(symbol)
        if not state or not state.running:
            raise ValueError(f"No active grid for {symbol}")

        base = symbol.split("/")[0]
        real_qty = await self._client.get_balance(base)
        old_qty  = state.held_qty
        drift    = real_qty - old_qty

        if drift > 0:
            # External buy: update avg cost with current price for the extra qty
            price = await self._client.get_current_price(symbol)
            total_cost       = state.avg_buy_price * old_qty + price * drift
            state.held_qty   = real_qty
            state.avg_buy_price = total_cost / real_qty if real_qty else price
        else:
            state.held_qty = max(0.0, real_qty)

        state._pending_sync = False
        await self._sync_pnl(state)
        logger.info(
            "Balance synced for %s: %.6f → %.6f (drift=%.6f)",
            symbol, old_qty, state.held_qty, drift,
        )
        return {"old_qty": old_qty, "new_qty": state.held_qty, "drift": drift}

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

    async def adjust_investment(
        self,
        symbol: str,
        new_investment: float,
    ) -> dict:
        """
        Change total_investment for a running grid without stopping it.

        - new_investment > current: buys the difference at market, then rebuilds.
        - new_investment < current: sells enough base currency to cover the
          difference, then rebuilds.
        - Rebuilds the grid around the current price with the new budget so
          qty_per_grid and range are recalculated correctly.

        Returns a dict with keys:
            old_investment, new_investment, diff_usdt, action, price
        """
        state = self._grids.get(symbol)
        if not state or not state.running:
            raise ValueError(f"No active grid for {symbol}")

        min_investment = 10.0   # hard floor to avoid dust positions
        if new_investment < min_investment:
            raise ValueError(f"الحد الأدنى للاستثمار هو {min_investment} USDT")

        price      = await self._client.get_current_price(symbol)
        old_inv    = state.total_investment
        diff_usdt  = new_investment - old_inv   # positive = add, negative = reduce

        action = "unchanged"

        if diff_usdt > 1.0:
            # ── Increase: buy the extra allocation at market ───────────────────
            buy_qty = self._client.round_amount(symbol, diff_usdt / price)
            if buy_qty > 0:
                order = await self._client.market_buy(symbol, buy_qty)
                if order:
                    filled_qty  = float(order.get("filled") or buy_qty)
                    fill_price  = float(order.get("average") or order.get("price") or price)
                    # Update average cost
                    total_cost  = state.avg_buy_price * state.held_qty + fill_price * filled_qty
                    state.held_qty      += filled_qty
                    state.avg_buy_price  = total_cost / state.held_qty if state.held_qty else fill_price
                    await db.record_trade(
                        symbol, "buy", fill_price, filled_qty,
                        order.get("id", ""), state.grid_id, pnl=0.0,
                    )
                    logger.info(
                        "adjust_investment BUY %s: +%.4f USDT → qty=%.6f @ %.4f",
                        symbol, diff_usdt, filled_qty, fill_price,
                    )
            action = "increased"

        elif diff_usdt < -1.0:
            # ── Decrease: sell the freed allocation at market ──────────────────
            sell_usdt = abs(diff_usdt)
            sell_qty  = self._client.round_amount(symbol, sell_usdt / price)
            # Cap at actual holdings so we never oversell
            sell_qty  = min(sell_qty, state.held_qty)
            if sell_qty > 0:
                order = await self._client.market_sell_qty(symbol, sell_qty)
                if order:
                    filled_qty = float(order.get("filled") or sell_qty)
                    fill_price = float(order.get("average") or order.get("price") or price)
                    pnl = (fill_price - state.avg_buy_price) * filled_qty
                    state.realized_pnl += pnl
                    state.held_qty     -= filled_qty
                    if state.held_qty < 0:
                        state.held_qty = 0.0
                    await db.record_trade(
                        symbol, "sell", fill_price, filled_qty,
                        order.get("id", ""), state.grid_id, pnl=pnl,
                    )
                    logger.info(
                        "adjust_investment SELL %s: -%.4f USDT → qty=%.6f @ %.4f | pnl=%.4f",
                        symbol, sell_usdt, filled_qty, fill_price, pnl,
                    )
            action = "decreased"

        # ── Update investment and rebuild grid ────────────────────────────────
        state.total_investment = new_investment
        await self._rebuild(state, price)
        await db.upsert_grid({
            "symbol":           symbol,
            "total_investment": new_investment,
        })

        logger.info(
            "adjust_investment done: %s | %.2f → %.2f USDT | action=%s",
            symbol, old_inv, new_investment, action,
        )
        return {
            "old_investment": old_inv,
            "new_investment": new_investment,
            "diff_usdt":      diff_usdt,
            "action":         action,
            "price":          price,
        }

    async def _rebuild(self, state: GridState, price: float) -> None:
        """Cancel all orders and re-place the grid around the current price, preserving num_grids."""
        await self._client.cancel_all_orders(state.symbol)
        state.open_orders.clear()
        n      = state.params.grid_count // 2
        params = derive_grid_params(
            price, state.total_investment, self._client, state.symbol,
            num_grids=n, upper_pct=state.upper_pct, lower_pct=state.lower_pct,
        )
        state.params = params
        await db.upsert_grid({
            "symbol":       state.symbol,
            "lower_price":  params.lower,
            "upper_price":  params.upper,
            "grid_count":   params.grid_count,
            "grid_spacing": params.grid_spacing,
            "current_atr":  0.0,
            "upper_pct":    state.upper_pct,
            "lower_pct":    state.lower_pct,
        })
        await self._place_initial_orders(state, price)

    # ── Fill polling ───────────────────────────────────────────────────────────

    async def _poll_fills(self, state: GridState) -> None:
        if not state.open_orders:
            return
        for order_id, meta in list(state.open_orders.items()):
            # Order may have been removed by a concurrent iteration — skip safely
            if order_id not in state.open_orders:
                continue
            try:
                order = await self._client.fetch_order(state.symbol, order_id)
            except Exception as exc:
                logger.warning("fetch_order failed for %s %s: %s", state.symbol, order_id, exc)
                continue
            if not order:
                continue
            status = order.get("status", "")
            if status == "closed":
                state.open_orders.pop(order_id, None)
                try:
                    await self._handle_fill(state, meta, order)
                except Exception as exc:
                    logger.exception("_handle_fill error for %s order %s: %s", state.symbol, order_id, exc)
            elif status == "canceled":
                state.open_orders.pop(order_id, None)

    async def _handle_fill(self, state: GridState, meta: dict, order: dict) -> None:
        side = meta["side"]
        try:
            fill_price = float(order.get("average") or order.get("price") or meta["price"])
            qty        = float(order.get("filled") or meta["qty"])
        except (TypeError, ValueError) as exc:
            logger.error(
                "_handle_fill: could not parse fill data for %s order %s: %s — skipping",
                state.symbol, order.get("id", "?"), exc,
            )
            return
        if fill_price <= 0 or qty <= 0:
            logger.error(
                "_handle_fill: invalid fill_price=%.6f qty=%.6f for %s — skipping",
                fill_price, qty, state.symbol,
            )
            return
        pnl = 0.0

        # Max allowed holdings = half the total investment at current fill price
        max_allowed_qty = (state.total_investment / 2) / fill_price

        if side == "buy":
            # Update average cost
            total_cost          = state.avg_buy_price * state.held_qty + fill_price * qty
            state.held_qty     += qty
            state.avg_buy_price = total_cost / state.held_qty if state.held_qty else 0.0

            # Place sell one step above the fill using actual grid spacing
            sell_price = self._client.round_price(state.symbol, fill_price + state.params.grid_spacing)
            if self._guard_order_cost(state, sell_price, qty):
                sell_order = await self._client.place_limit_sell(state.symbol, sell_price, qty)
                if sell_order:
                    state.open_orders[sell_order["id"]] = {"side": "sell", "price": sell_price, "qty": qty}

            # Place next buy one step lower — only if under holdings cap
            next_buy_price = self._client.round_price(state.symbol, fill_price - state.params.grid_spacing)
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
            buy_price = self._client.round_price(state.symbol, fill_price - state.params.grid_spacing)
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
            next_sell_price = self._client.round_price(state.symbol, fill_price + state.params.grid_spacing)
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
            "upper_pct":           state.upper_pct,
            "lower_pct":           state.lower_pct,
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
