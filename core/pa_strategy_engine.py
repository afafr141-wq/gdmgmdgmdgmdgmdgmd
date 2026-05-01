"""
Price Action Strategy Engine.

Per active strategy:
  - Poll for a PA signal every FILL_POLL_INTERVAL seconds.
  - When a signal fires: place a Market Order (buy or sell).
  - Place a Limit Sell/Buy at the TP level immediately after fill.
  - Track position, PnL, and persist state to DB.
  - Re-scan for new signals once the TP fills or the position is closed.

One strategy per symbol. The user sets:
  symbol, timeframe, capital_pct (% of USDT balance per trade).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from config.settings import FILL_POLL_INTERVAL, PA_LOOKBACK_CANDLES
from core.mexc_client import MexcClient
from core.pa_engine import (
    PASignal,
    compute_pa_signal,
    compute_pa_levels,
    PALevels,
)
from utils import db_manager as db

logger = logging.getLogger(__name__)

# ── Notifiers (injected from main.py) ─────────────────────────────────────────
_notify_entry   = None
_notify_tp_hit  = None
_notify_error   = None


def set_notifiers(entry, tp_hit, error) -> None:
    global _notify_entry, _notify_tp_hit, _notify_error
    _notify_entry  = entry
    _notify_tp_hit = tp_hit
    _notify_error  = error


async def _fire(coro) -> None:
    if coro is None:
        return
    try:
        await coro
    except Exception as exc:
        logger.error("PA notifier error: %s", exc)


# ── State ──────────────────────────────────────────────────────────────────────

@dataclass
class PAStrategyState:
    symbol:       str
    timeframe:    str
    capital_pct:  float          # % of USDT balance to use per trade
    strategy_id:  int   = 0

    # Position tracking
    held_qty:         float = 0.0
    avg_entry_price:  float = 0.0
    direction:        str   = ""   # "buy" | "sell" | ""
    tp_price:         float = 0.0
    tp_order_id:      str   = ""

    # Stats
    realized_pnl:  float = 0.0
    trade_count:   int   = 0
    win_count:     int   = 0

    started_at:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    running:       bool     = True

    # Last signal seen (to avoid re-entering on the same candle)
    last_signal_ts: int = 0


# ── Engine ─────────────────────────────────────────────────────────────────────

class PAStrategyEngine:
    def __init__(self, client: MexcClient) -> None:
        self._client  = client
        self._states: dict[str, PAStrategyState] = {}
        self._tasks:  dict[str, asyncio.Task]    = {}

    # ── Public ─────────────────────────────────────────────────────────────────

    async def start(
        self,
        symbol:      str,
        timeframe:   str,
        capital_pct: float,
    ) -> PAStrategyState:
        if symbol in self._states and self._states[symbol].running:
            raise ValueError(f"PA strategy already running for {symbol}")

        state = PAStrategyState(
            symbol      = symbol,
            timeframe   = timeframe,
            capital_pct = capital_pct,
        )

        strategy_id = await db.upsert_pa_strategy({
            "symbol":      symbol,
            "timeframe":   timeframe,
            "capital_pct": capital_pct,
            "is_active":   True,
        })
        state.strategy_id = strategy_id

        self._states[symbol] = state
        self._tasks[symbol]  = asyncio.create_task(self._run_loop(state))

        logger.info(
            "PA strategy started: %s | tf=%s | capital_pct=%.1f%%",
            symbol, timeframe, capital_pct,
        )
        return state

    async def restore(
        self,
        symbol:          str,
        timeframe:       str,
        capital_pct:     float,
        held_qty:        float = 0.0,
        avg_entry_price: float = 0.0,
        direction:       str   = "",
        tp_price:        float = 0.0,
        realized_pnl:    float = 0.0,
        trade_count:     int   = 0,
        win_count:       int   = 0,
    ) -> PAStrategyState:
        if symbol in self._states and self._states[symbol].running:
            raise ValueError(f"PA strategy already running for {symbol}")

        state = PAStrategyState(
            symbol          = symbol,
            timeframe       = timeframe,
            capital_pct     = capital_pct,
            held_qty        = held_qty,
            avg_entry_price = avg_entry_price,
            direction       = direction,
            tp_price        = tp_price,
            realized_pnl    = realized_pnl,
            trade_count     = trade_count,
            win_count       = win_count,
        )

        strategy_id = await db.upsert_pa_strategy({
            "symbol":      symbol,
            "timeframe":   timeframe,
            "capital_pct": capital_pct,
        })
        state.strategy_id = strategy_id

        # Cancel any stale exchange orders
        await self._client.cancel_all_orders(symbol)

        # If we were holding a position, re-place the TP order
        if state.held_qty > 0 and state.tp_price > 0 and state.direction:
            await self._place_tp_order(state)

        self._states[symbol] = state
        self._tasks[symbol]  = asyncio.create_task(self._run_loop(state))

        logger.info(
            "PA strategy restored: %s | tf=%s | held=%.6f dir=%s tp=%.6f pnl=%.4f",
            symbol, timeframe, held_qty, direction, tp_price, realized_pnl,
        )
        return state

    async def stop(self, symbol: str, market_sell: bool = True, persist: bool = True) -> float:
        state = self._states.get(symbol)
        if not state:
            raise ValueError(f"No active PA strategy for {symbol}")

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
        if market_sell and state.held_qty > 0 and state.direction == "buy":
            order = await self._client.market_sell_qty(symbol, state.held_qty)
            if order and order.get("cost"):
                sell_value = float(order["cost"])

        if persist:
            await db.deactivate_pa_strategy(symbol)

        del self._states[symbol]
        logger.info("PA strategy stopped: %s | sell_value=%.4f", symbol, sell_value)
        return sell_value

    def get_state(self, symbol: str) -> Optional[PAStrategyState]:
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
            "direction":       state.direction,
            "held_qty":        state.held_qty,
            "avg_entry_price": state.avg_entry_price,
            "tp_price":        state.tp_price,
            "realized_pnl":    state.realized_pnl,
            "trade_count":     state.trade_count,
            "win_count":       state.win_count,
            "win_rate":        round(win_rate, 1),
            "days_running":    max(
                (datetime.now(timezone.utc) - state.started_at).total_seconds() / 86400,
                1 / 1440,
            ),
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _fetch_signal(self, state: PAStrategyState) -> Optional[PASignal]:
        try:
            candles = await self._client.fetch_ohlcv(
                state.symbol, timeframe=state.timeframe, limit=PA_LOOKBACK_CANDLES
            )
        except Exception as exc:
            logger.error("PA fetch_ohlcv failed for %s: %s", state.symbol, exc)
            return None

        return compute_pa_signal(
            candles, state.symbol, state.timeframe,
        )

    async def _calc_qty(self, state: PAStrategyState, price: float) -> float:
        """Calculate order qty based on capital_pct of free USDT balance."""
        try:
            usdt_balance = await self._client.get_balance("USDT")
        except Exception as exc:
            logger.error("PA get_balance failed for %s: %s", state.symbol, exc)
            return 0.0

        capital = usdt_balance * state.capital_pct / 100
        qty     = self._client.round_amount(state.symbol, capital / price)
        min_amt = self._client.min_amount(state.symbol)
        if qty < min_amt:
            logger.warning(
                "PA qty %.6f below min %.6f for %s — skipping",
                qty, min_amt, state.symbol,
            )
            return 0.0
        return qty

    async def _place_tp_order(self, state: PAStrategyState) -> None:
        """Place a limit TP order based on current state."""
        if state.held_qty <= 0 or state.tp_price <= 0:
            return

        symbol  = state.symbol
        min_amt = self._client.min_amount(symbol)
        qty     = self._client.round_amount(symbol, state.held_qty)

        if qty < min_amt:
            return

        if state.direction == "buy":
            order = await self._client.place_limit_sell(symbol, state.tp_price, qty)
        else:
            order = await self._client.place_limit_buy(symbol, state.tp_price, qty)

        if order:
            state.tp_order_id = order["id"]
            logger.info(
                "PA TP order placed: %s %s @ %.6f qty=%.6f",
                state.direction, symbol, state.tp_price, qty,
            )

    # ── Entry execution ────────────────────────────────────────────────────────

    async def _execute_entry(self, state: PAStrategyState, signal: PASignal) -> None:
        symbol = state.symbol

        # Avoid re-entering if already in a position
        if state.held_qty > 0:
            return

        current_price = await self._client.get_current_price(symbol)
        qty = await self._calc_qty(state, current_price)
        if qty <= 0:
            return

        if signal.direction == "buy":
            order = await self._client.market_buy_qty(symbol, qty)
        else:
            order = await self._client.market_sell_qty(symbol, qty)

        if not order:
            logger.error("PA market order failed for %s", symbol)
            return

        fill_price = float(order.get("average") or order.get("price") or current_price)
        fill_qty   = float(order.get("filled") or qty)

        state.held_qty        = fill_qty
        state.avg_entry_price = fill_price
        state.direction       = signal.direction
        state.tp_price        = signal.tp_price

        await db.record_pa_trade(
            symbol      = symbol,
            side        = signal.direction,
            price       = fill_price,
            qty         = fill_qty,
            order_id    = order.get("id", ""),
            strategy_id = state.strategy_id,
            pnl         = 0.0,
            pattern     = signal.candle_pattern,
            trend       = signal.trend,
        )
        await self._persist_state(state)

        await _fire(_notify_entry and _notify_entry(
            symbol, signal.direction, fill_price, fill_qty,
            signal.candle_pattern, signal.trend, signal.tp_price,
        ))

        logger.info(
            "PA entry: %s %s @ %.6f qty=%.6f tp=%.6f pattern=%s trend=%s",
            signal.direction.upper(), symbol, fill_price, fill_qty,
            signal.tp_price, signal.candle_pattern, signal.trend,
        )

        # Place TP limit order immediately
        await self._place_tp_order(state)

    # ── TP polling ─────────────────────────────────────────────────────────────

    async def _poll_tp(self, state: PAStrategyState) -> None:
        """Check if the TP order has filled."""
        if not state.tp_order_id or state.held_qty <= 0:
            return

        try:
            order = await self._client.fetch_order(state.symbol, state.tp_order_id)
        except Exception as exc:
            logger.warning("PA fetch_order failed for %s: %s", state.symbol, exc)
            return

        if not order:
            return

        status = order.get("status", "")

        if status == "closed":
            fill_price = float(order.get("average") or order.get("price") or state.tp_price)
            fill_qty   = float(order.get("filled") or state.held_qty)

            if state.direction == "buy":
                pnl = (fill_price - state.avg_entry_price) * fill_qty
            else:
                pnl = (state.avg_entry_price - fill_price) * fill_qty

            state.realized_pnl += pnl
            state.trade_count  += 1
            if pnl > 0:
                state.win_count += 1

            await db.record_pa_trade(
                symbol      = state.symbol,
                side        = "sell" if state.direction == "buy" else "buy",
                price       = fill_price,
                qty         = fill_qty,
                order_id    = order.get("id", ""),
                strategy_id = state.strategy_id,
                pnl         = pnl,
                pattern     = "tp_hit",
                trend       = "",
            )

            await _fire(_notify_tp_hit and _notify_tp_hit(
                state.symbol, fill_price, fill_qty, pnl,
                state.trade_count, state.win_count,
            ))

            logger.info(
                "PA TP hit: %s @ %.6f qty=%.6f pnl=%.4f | total_pnl=%.4f wins=%d/%d",
                state.symbol, fill_price, fill_qty, pnl,
                state.realized_pnl, state.win_count, state.trade_count,
            )

            # Reset position — ready for next signal
            state.held_qty        = 0.0
            state.avg_entry_price = 0.0
            state.direction       = ""
            state.tp_price        = 0.0
            state.tp_order_id     = ""

            await self._persist_state(state)

        elif status == "canceled":
            # TP order was cancelled externally — clear it so we re-place
            state.tp_order_id = ""

    # ── Persist ────────────────────────────────────────────────────────────────

    async def _persist_state(self, state: PAStrategyState) -> None:
        await db.update_pa_strategy_state(
            symbol          = state.symbol,
            held_qty        = state.held_qty,
            avg_entry_price = state.avg_entry_price,
            direction       = state.direction,
            tp_price        = state.tp_price,
            realized_pnl    = state.realized_pnl,
            trade_count     = state.trade_count,
            win_count       = state.win_count,
        )

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _run_loop(self, state: PAStrategyState) -> None:
        while state.running:
            try:
                if state.held_qty > 0:
                    # In position — just poll the TP order
                    await self._poll_tp(state)
                else:
                    # No position — look for a new signal
                    signal = await self._fetch_signal(state)
                    if signal:
                        # Deduplicate: don't re-enter on the same candle
                        candle_ts = int(datetime.now(timezone.utc).timestamp() // 60)
                        if candle_ts != state.last_signal_ts:
                            state.last_signal_ts = candle_ts
                            await self._execute_entry(state, signal)

                await asyncio.sleep(FILL_POLL_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("PA strategy loop error for %s: %s", state.symbol, exc)
                await _fire(_notify_error and _notify_error(
                    state.symbol, type(exc).__name__, str(exc)[:200]
                ))
                await asyncio.sleep(30)
