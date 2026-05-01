"""
Liquidity Swings Strategy Engine.

Per active strategy:
  - Poll for an LS signal every FILL_POLL_INTERVAL seconds.
  - When a signal fires: place a Market Buy order (spot only).
  - Place a Limit Sell at the TP level immediately after fill.
  - Monitor SL price-side (MEXC Spot has no stop orders).
  - Track position, PnL, and persist state to DB.
  - Re-scan for new signals once the trade closes.

One strategy per symbol. The user sets:
  symbol, timeframe, capital_pct, pivot_left/right, min_touches, swing_area.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from config.settings import FILL_POLL_INTERVAL, LS_LOOKBACK_CANDLES
from core.mexc_client import MexcClient
from core.liquidity_swings_engine import (
    LSSignal,
    LSLevels,
    SwingArea,
    compute_ls_signal,
    compute_ls_levels,
)
from utils import db_manager as db

logger = logging.getLogger(__name__)

# ── Notifiers (injected from main.py) ─────────────────────────────────────────
_notify_entry  = None
_notify_tp_hit = None
_notify_sl_hit = None
_notify_error  = None


def set_notifiers(entry, tp_hit, sl_hit, error) -> None:
    global _notify_entry, _notify_tp_hit, _notify_sl_hit, _notify_error
    _notify_entry  = entry
    _notify_tp_hit = tp_hit
    _notify_sl_hit = sl_hit
    _notify_error  = error


async def _fire(coro) -> None:
    if coro is None:
        return
    try:
        await coro
    except Exception as exc:
        logger.error("LS notifier error: %s", exc)


# ── Strategy parameters ────────────────────────────────────────────────────────

@dataclass
class LSParams:
    pivot_left:    int       = 14
    pivot_right:   int       = 14
    swing_area:    SwingArea = "Wick Extremity"  # type: ignore[assignment]
    min_touches:   int       = 2
    sl_buffer_pct: float     = 0.3
    lookback:      int       = 300


# ── State ──────────────────────────────────────────────────────────────────────

@dataclass
class LSState:
    symbol:      str
    timeframe:   str
    capital_pct: float
    params:      LSParams
    strategy_id: int = 0

    # Open position
    held_qty:         float = 0.0
    avg_entry_price:  float = 0.0
    tp_price:         float = 0.0
    sl_price:         float = 0.0
    tp_order_id:      str   = ""

    # Stats
    realized_pnl: float = 0.0
    trade_count:  int   = 0
    win_count:    int   = 0

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    running:    bool     = True

    # Deduplication: skip re-entry on the same candle minute
    last_signal_ts: int = 0


# ── Engine ─────────────────────────────────────────────────────────────────────

class LSStrategyEngine:
    def __init__(self, client: MexcClient) -> None:
        self._client  = client
        self._states: dict[str, LSState]       = {}
        self._tasks:  dict[str, asyncio.Task]  = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(
        self,
        symbol:      str,
        timeframe:   str,
        capital_pct: float,
        params:      Optional[LSParams] = None,
    ) -> LSState:
        if symbol in self._states and self._states[symbol].running:
            raise ValueError(f"LS strategy already running for {symbol}")

        p     = params or LSParams()
        state = LSState(symbol=symbol, timeframe=timeframe,
                        capital_pct=capital_pct, params=p)

        strategy_id = await db.upsert_ls_strategy({
            "symbol":        symbol,
            "timeframe":     timeframe,
            "capital_pct":   capital_pct,
            "pivot_left":    p.pivot_left,
            "pivot_right":   p.pivot_right,
            "swing_area":    p.swing_area,
            "min_touches":   p.min_touches,
            "sl_buffer_pct": p.sl_buffer_pct,
            "is_active":     True,
        })
        state.strategy_id = strategy_id

        self._states[symbol] = state
        self._tasks[symbol]  = asyncio.create_task(self._run_loop(state))

        logger.info(
            "LS strategy started: %s | tf=%s | capital=%.1f%% | touches>=%d",
            symbol, timeframe, capital_pct, p.min_touches,
        )
        return state

    async def restore(
        self,
        symbol:          str,
        timeframe:       str,
        capital_pct:     float,
        params:          Optional[LSParams] = None,
        held_qty:        float = 0.0,
        avg_entry_price: float = 0.0,
        tp_price:        float = 0.0,
        sl_price:        float = 0.0,
        realized_pnl:    float = 0.0,
        trade_count:     int   = 0,
        win_count:       int   = 0,
    ) -> LSState:
        if symbol in self._states and self._states[symbol].running:
            raise ValueError(f"LS strategy already running for {symbol}")

        p     = params or LSParams()
        state = LSState(
            symbol=symbol, timeframe=timeframe, capital_pct=capital_pct, params=p,
            held_qty=held_qty, avg_entry_price=avg_entry_price,
            tp_price=tp_price, sl_price=sl_price,
            realized_pnl=realized_pnl, trade_count=trade_count, win_count=win_count,
        )

        strategy_id = await db.upsert_ls_strategy({
            "symbol": symbol, "timeframe": timeframe, "capital_pct": capital_pct,
        })
        state.strategy_id = strategy_id

        await self._client.cancel_all_orders(symbol)
        if state.held_qty > 0 and state.tp_price > 0:
            await self._place_tp_order(state)

        self._states[symbol] = state
        self._tasks[symbol]  = asyncio.create_task(self._run_loop(state))

        logger.info(
            "LS strategy restored: %s | tf=%s | held=%.6f tp=%.6f sl=%.6f pnl=%.4f",
            symbol, timeframe, held_qty, tp_price, sl_price, realized_pnl,
        )
        return state

    async def stop(self, symbol: str, market_sell: bool = True, persist: bool = True) -> float:
        state = self._states.get(symbol)
        if not state:
            raise ValueError(f"No active LS strategy for {symbol}")

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
        if market_sell and state.held_qty > 0:
            order = await self._client.market_sell_qty(symbol, state.held_qty)
            if order and order.get("cost"):
                sell_value = float(order["cost"])

        if persist:
            await db.deactivate_ls_strategy(symbol)

        del self._states[symbol]
        logger.info("LS strategy stopped: %s | sell_value=%.4f", symbol, sell_value)
        return sell_value

    def get_state(self, symbol: str) -> Optional[LSState]:
        return self._states.get(symbol)

    def active_symbols(self) -> list[str]:
        return [s for s, st in self._states.items() if st.running]

    def calc_report(self, symbol: str) -> Optional[dict]:
        state = self._states.get(symbol)
        if not state:
            return None
        win_rate = (state.win_count / state.trade_count * 100) if state.trade_count else 0.0
        return {
            "symbol":          symbol,
            "timeframe":       state.timeframe,
            "capital_pct":     state.capital_pct,
            "held_qty":        state.held_qty,
            "avg_entry_price": state.avg_entry_price,
            "tp_price":        state.tp_price,
            "sl_price":        state.sl_price,
            "realized_pnl":    state.realized_pnl,
            "trade_count":     state.trade_count,
            "win_count":       state.win_count,
            "win_rate":        round(win_rate, 1),
            "min_touches":     state.params.min_touches,
            "swing_area":      state.params.swing_area,
            "days_running":    max(
                (datetime.now(timezone.utc) - state.started_at).total_seconds() / 86400,
                1 / 1440,
            ),
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _fetch_signal(self, state: LSState) -> Optional[LSSignal]:
        try:
            candles = await self._client.fetch_ohlcv(
                state.symbol, timeframe=state.timeframe, limit=state.params.lookback,
            )
        except Exception as exc:
            logger.error("LS fetch_ohlcv failed for %s: %s", state.symbol, exc)
            return None

        p = state.params
        return compute_ls_signal(
            candles, state.symbol, state.timeframe,
            pivot_left    = p.pivot_left,
            pivot_right   = p.pivot_right,
            swing_area    = p.swing_area,
            min_touches   = p.min_touches,
            sl_buffer_pct = p.sl_buffer_pct,
        )

    async def _calc_qty(self, state: LSState, price: float) -> float:
        try:
            usdt_balance = await self._client.get_balance("USDT")
        except Exception as exc:
            logger.error("LS get_balance failed for %s: %s", state.symbol, exc)
            return 0.0
        capital = usdt_balance * state.capital_pct / 100
        qty     = self._client.round_amount(state.symbol, capital / price)
        min_amt = self._client.min_amount(state.symbol)
        if qty < min_amt:
            logger.warning("LS qty %.6f below min %.6f for %s", qty, min_amt, state.symbol)
            return 0.0
        return qty

    async def _place_tp_order(self, state: LSState) -> None:
        if state.held_qty <= 0 or state.tp_price <= 0:
            return
        qty = self._client.round_amount(state.symbol, state.held_qty)
        if qty < self._client.min_amount(state.symbol):
            return
        order = await self._client.place_limit_sell(state.symbol, state.tp_price, qty)
        if order:
            state.tp_order_id = order["id"]
            logger.info("LS TP order placed: %s @ %.6f qty=%.6f",
                        state.symbol, state.tp_price, qty)

    # ── Entry ──────────────────────────────────────────────────────────────────

    async def _execute_entry(self, state: LSState, signal: LSSignal) -> None:
        if state.held_qty > 0:
            return

        current_price = await self._client.get_current_price(state.symbol)
        qty = await self._calc_qty(state, current_price)
        if qty <= 0:
            return

        order = await self._client.market_buy_qty(state.symbol, qty)
        if not order:
            logger.error("LS market order failed for %s", state.symbol)
            return

        fill_price = float(order.get("average") or order.get("price") or current_price)
        fill_qty   = float(order.get("filled") or qty)

        state.held_qty        = fill_qty
        state.avg_entry_price = fill_price
        state.tp_price        = signal.tp_price
        state.sl_price        = signal.sl_price

        await db.record_ls_trade(
            symbol=state.symbol, side="buy", price=fill_price, qty=fill_qty,
            order_id=order.get("id", ""), strategy_id=state.strategy_id,
            pnl=0.0, reason=f"entry_touches{signal.touch_count}",
        )
        await self._persist_state(state)

        await _fire(_notify_entry and _notify_entry(
            state.symbol, fill_price, fill_qty,
            signal.tp_price, signal.sl_price,
            signal.touch_count, signal.zone,
        ))

        logger.info(
            "LS entry: BUY %s @ %.6f qty=%.6f tp=%.6f sl=%.6f touches=%d",
            state.symbol, fill_price, fill_qty,
            signal.tp_price, signal.sl_price, signal.touch_count,
        )

        await self._place_tp_order(state)

    # ── Position monitoring ────────────────────────────────────────────────────

    async def _poll_position(self, state: LSState) -> None:
        if state.held_qty <= 0:
            return

        symbol = state.symbol

        # Check TP fill
        if state.tp_order_id:
            try:
                order = await self._client.fetch_order(symbol, state.tp_order_id)
            except Exception as exc:
                logger.warning("LS fetch_order failed for %s: %s", symbol, exc)
                order = None

            if order and order.get("status") == "closed":
                fill_price = float(order.get("average") or order.get("price") or state.tp_price)
                fill_qty   = float(order.get("filled") or state.held_qty)
                await self._close_position(state, fill_price, fill_qty, "tp_hit")
                return

            if order and order.get("status") == "canceled":
                state.tp_order_id = ""

        # Check SL breach (price-based — MEXC Spot has no stop orders)
        if state.sl_price > 0:
            try:
                current_price = await self._client.get_current_price(symbol)
            except Exception as exc:
                logger.warning("LS get_current_price failed for %s: %s", symbol, exc)
                return

            if current_price <= state.sl_price:
                logger.info("LS SL triggered: %s price=%.6f sl=%.6f",
                            symbol, current_price, state.sl_price)
                if state.tp_order_id:
                    try:
                        await self._client.cancel_order(symbol, state.tp_order_id)
                    except Exception:
                        pass
                    state.tp_order_id = ""

                order = await self._client.market_sell_qty(symbol, state.held_qty)
                if order:
                    fill_price = float(order.get("average") or order.get("price") or current_price)
                    fill_qty   = float(order.get("filled") or state.held_qty)
                    await self._close_position(state, fill_price, fill_qty, "sl_hit")

    async def _close_position(
        self, state: LSState, fill_price: float, fill_qty: float, reason: str,
    ) -> None:
        pnl = (fill_price - state.avg_entry_price) * fill_qty

        state.realized_pnl += pnl
        state.trade_count  += 1
        if pnl > 0:
            state.win_count += 1

        await db.record_ls_trade(
            symbol=state.symbol, side="sell", price=fill_price, qty=fill_qty,
            order_id=state.tp_order_id, strategy_id=state.strategy_id,
            pnl=pnl, reason=reason,
        )

        if reason == "tp_hit":
            await _fire(_notify_tp_hit and _notify_tp_hit(
                state.symbol, fill_price, fill_qty, pnl,
                state.trade_count, state.win_count,
            ))
        else:
            await _fire(_notify_sl_hit and _notify_sl_hit(
                state.symbol, fill_price, fill_qty, pnl,
                state.trade_count, state.win_count,
            ))

        logger.info(
            "LS %s: %s @ %.6f qty=%.6f pnl=%.4f | total=%.4f wins=%d/%d",
            reason, state.symbol, fill_price, fill_qty, pnl,
            state.realized_pnl, state.win_count, state.trade_count,
        )

        state.held_qty        = 0.0
        state.avg_entry_price = 0.0
        state.tp_price        = 0.0
        state.sl_price        = 0.0
        state.tp_order_id     = ""
        await self._persist_state(state)

    # ── Persist ────────────────────────────────────────────────────────────────

    async def _persist_state(self, state: LSState) -> None:
        await db.update_ls_strategy_state(
            symbol=state.symbol,
            held_qty=state.held_qty,
            avg_entry_price=state.avg_entry_price,
            tp_price=state.tp_price,
            sl_price=state.sl_price,
            realized_pnl=state.realized_pnl,
            trade_count=state.trade_count,
            win_count=state.win_count,
        )

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _run_loop(self, state: LSState) -> None:
        while state.running:
            try:
                if state.held_qty > 0:
                    await self._poll_position(state)
                else:
                    signal = await self._fetch_signal(state)
                    if signal:
                        candle_ts = int(datetime.now(timezone.utc).timestamp() // 60)
                        if candle_ts != state.last_signal_ts:
                            state.last_signal_ts = candle_ts
                            await self._execute_entry(state, signal)

                await asyncio.sleep(FILL_POLL_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("LS loop error for %s: %s", state.symbol, exc)
                await _fire(_notify_error and _notify_error(
                    state.symbol, type(exc).__name__, str(exc)[:200]
                ))
                await asyncio.sleep(30)
