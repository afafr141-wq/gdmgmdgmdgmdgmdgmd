"""
Support & Resistance Strategy Engine.

Logic per active strategy:
  - Compute 2 support levels (S1 < S2, both below price) and
    2 resistance levels (R1 < R2, both above price) via KDE clustering.
  - Place 4 limit orders:
      BUY  @ S1  (25% of investment)
      BUY  @ S2  (25% of investment)
      SELL @ R1  (25% of investment qty)
      SELL @ R2  (25% of investment qty)
  - Poll fills every FILL_POLL_INTERVAL seconds.
  - On a BUY fill: record trade, update held_qty / avg_buy_price.
  - On a SELL fill: record trade, update realized_pnl.
  - S/R levels are refreshed every SR_REFRESH_INTERVAL seconds so the
    strategy adapts to new market structure without manual intervention.
  - Notifiers are injected at runtime to avoid circular imports.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Optional

from config.settings import FILL_POLL_INTERVAL
from core.mexc_client import MexcClient
from core.sr_engine import SRLevels, fetch_sr_levels
from utils import db_manager as db

# S&R-specific DB calls are prefixed snr_ in db_manager

logger = logging.getLogger(__name__)

SR_REFRESH_INTERVAL = 3600   # re-compute S/R every hour

# ── Notifiers (injected from main.py) ─────────────────────────────────────────
_notify_buy_filled   = None
_notify_sell_filled  = None
_notify_sr_refresh   = None
_notify_error        = None


def set_notifiers(buy_filled, sell_filled, sr_refresh, error) -> None:
    global _notify_buy_filled, _notify_sell_filled, _notify_sr_refresh, _notify_error
    _notify_buy_filled  = buy_filled
    _notify_sell_filled = sell_filled
    _notify_sr_refresh  = sr_refresh
    _notify_error       = error


async def _fire(coro) -> None:
    if coro is None:
        return
    try:
        await coro
    except Exception as exc:
        logger.error("Notifier error: %s", exc)


# ── State ──────────────────────────────────────────────────────────────────────

@dataclass
class StrategyState:
    symbol:           str
    timeframe:        str
    total_investment: float
    levels:           SRLevels
    strategy_id:      int   = 0
    # order_id → {side, price, qty, level: "s1"|"s2"|"r1"|"r2"}
    open_orders:      dict  = field(default_factory=dict)
    held_qty:         float = 0.0
    avg_buy_price:    float = 0.0
    realized_pnl:     float = 0.0
    buy_count:        int   = 0
    sell_count:       int   = 0
    started_at:       datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    running:          bool  = True


# ── Engine ─────────────────────────────────────────────────────────────────────

class StrategyEngine:
    def __init__(self, client: MexcClient) -> None:
        self._client   = client
        self._states:  dict[str, StrategyState] = {}
        self._tasks:   dict[str, asyncio.Task]  = {}

    # ── Public ─────────────────────────────────────────────────────────────────

    async def start(
        self,
        symbol: str,
        timeframe: str,
        total_investment: float,
    ) -> StrategyState:
        if symbol in self._states and self._states[symbol].running:
            raise ValueError(f"Strategy already running for {symbol}")

        levels = await fetch_sr_levels(self._client, symbol, timeframe)
        if not levels:
            raise ValueError(
                f"Could not compute S/R levels for {symbol} on {timeframe}. "
                "Try a different timeframe or pair."
            )

        state = StrategyState(
            symbol=symbol,
            timeframe=timeframe,
            total_investment=total_investment,
            levels=levels,
        )
        self._states[symbol] = state

        strategy_id = await db.upsert_strategy({
            "symbol":           symbol,
            "timeframe":        timeframe,
            "total_investment": total_investment,
            "support1":         levels.supports[0],
            "support2":         levels.supports[1],
            "resistance1":      levels.resistances[0],
            "resistance2":      levels.resistances[1],
            "is_active":        True,
        })
        state.strategy_id = strategy_id

        await self._place_orders(state)
        self._tasks[symbol] = asyncio.create_task(self._run_loop(state))

        logger.info(
            "Strategy started: %s | tf=%s | S=[%.4f, %.4f] R=[%.4f, %.4f] | inv=%.2f",
            symbol, timeframe,
            levels.supports[0], levels.supports[1],
            levels.resistances[0], levels.resistances[1],
            total_investment,
        )
        return state

    async def stop(self, symbol: str, market_sell: bool = True) -> float:
        state = self._states.get(symbol)
        if not state:
            raise ValueError(f"No active strategy for {symbol}")

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

        await db.deactivate_strategy(symbol)
        del self._states[symbol]
        logger.info("Strategy stopped: %s | sell_value=%.4f", symbol, sell_value)
        return sell_value

    def get_state(self, symbol: str) -> Optional[StrategyState]:
        return self._states.get(symbol)

    def active_symbols(self) -> list[str]:
        return [s for s, st in self._states.items() if st.running]

    def calc_report(self, symbol: str) -> Optional[dict]:
        state = self._states.get(symbol)
        if not state:
            return None
        lv = state.levels
        return {
            "symbol":           symbol,
            "timeframe":        state.timeframe,
            "total_investment": state.total_investment,
            "support1":         lv.supports[0],
            "support2":         lv.supports[1],
            "resistance1":      lv.resistances[0],
            "resistance2":      lv.resistances[1],
            "current_price":    lv.current_price,
            "held_qty":         state.held_qty,
            "avg_buy_price":    state.avg_buy_price,
            "realized_pnl":     state.realized_pnl,
            "buy_count":        state.buy_count,
            "sell_count":       state.sell_count,
            "open_orders":      len(state.open_orders),
            "open_buys":        sum(1 for m in state.open_orders.values() if m["side"] == "buy"),
            "open_sells":       sum(1 for m in state.open_orders.values() if m["side"] == "sell"),
            "days_running":     max(
                (datetime.now(timezone.utc) - state.started_at).total_seconds() / 86400,
                1 / 1440,
            ),
        }

    # ── Order placement ────────────────────────────────────────────────────────

    async def _place_orders(self, state: StrategyState) -> None:
        """
        Place buy orders at both supports (25% each).
        Sell orders are placed only after a buy fills — see _place_sell_orders().
        """
        lv     = state.levels
        inv    = state.total_investment
        symbol = state.symbol
        alloc  = inv * 0.25   # 25% per buy order

        for level_name, price in [("s1", lv.supports[0]), ("s2", lv.supports[1])]:
            qty = self._client.round_amount(symbol, alloc / price)
            order = await self._client.place_limit_buy(symbol, price, qty)
            if order:
                state.open_orders[order["id"]] = {
                    "side": "buy", "price": price, "qty": qty, "level": level_name,
                }
                logger.info("Placed BUY @ %.6f (%s) qty=%.6f", price, level_name, qty)

    async def _place_sell_orders(self, state: StrategyState) -> None:
        """
        Place sell orders at R1 and R2 based on actual held_qty.
        Called after every buy fill. Cancels existing sell orders first
        so qty stays accurate as more buys fill.
        """
        symbol = state.symbol
        lv     = state.levels

        # Cancel any existing open sell orders before re-placing
        for oid, meta in list(state.open_orders.items()):
            if meta["side"] == "sell":
                await self._client.cancel_order(symbol, oid)
                del state.open_orders[oid]

        if state.held_qty <= 0:
            return

        # Split held_qty equally between R1 and R2
        sell_qty_each = self._client.round_amount(symbol, state.held_qty / 2)

        for level_name, price in [("r1", lv.resistances[0]), ("r2", lv.resistances[1])]:
            if sell_qty_each < self._client.min_amount(symbol):
                # Not enough qty for two orders — put everything at R1
                if level_name == "r1":
                    sell_qty_each = self._client.round_amount(symbol, state.held_qty)
                else:
                    break
            order = await self._client.place_limit_sell(symbol, price, sell_qty_each)
            if order:
                state.open_orders[order["id"]] = {
                    "side": "sell", "price": price, "qty": sell_qty_each, "level": level_name,
                }
                logger.info("Placed SELL @ %.6f (%s) qty=%.6f", price, level_name, sell_qty_each)

    async def _refresh_orders(self, state: StrategyState) -> None:
        """Cancel all open orders, re-compute S/R, re-place orders."""
        await self._client.cancel_all_orders(state.symbol)
        state.open_orders.clear()

        new_levels = await fetch_sr_levels(self._client, state.symbol, state.timeframe)
        if not new_levels:
            logger.warning("S/R refresh failed for %s — keeping old levels", state.symbol)
            new_levels = state.levels

        old_levels = state.levels
        state.levels = new_levels

        await db.upsert_strategy({
            "symbol":      state.symbol,
            "support1":    new_levels.supports[0],
            "support2":    new_levels.supports[1],
            "resistance1": new_levels.resistances[0],
            "resistance2": new_levels.resistances[1],
        })

        await self._place_orders(state)
        if state.held_qty > 0:
            await self._place_sell_orders(state)

        await _fire(_notify_sr_refresh and _notify_sr_refresh(
            state.symbol, state.timeframe,
            old_levels, new_levels,
        ))
        logger.info(
            "S/R refreshed for %s: S=[%.4f,%.4f] R=[%.4f,%.4f]",
            state.symbol,
            new_levels.supports[0], new_levels.supports[1],
            new_levels.resistances[0], new_levels.resistances[1],
        )

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _run_loop(self, state: StrategyState) -> None:
        loop = asyncio.get_event_loop()
        last_refresh = loop.time()

        while state.running:
            try:
                now = loop.time()

                # Periodic S/R refresh
                if now - last_refresh >= SR_REFRESH_INTERVAL:
                    await self._refresh_orders(state)
                    last_refresh = now

                await self._poll_fills(state)
                await asyncio.sleep(FILL_POLL_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Strategy loop error for %s: %s", state.symbol, exc)
                await _fire(_notify_error and _notify_error(
                    state.symbol, type(exc).__name__, str(exc)[:200]
                ))
                await asyncio.sleep(30)

    # ── Fill polling ───────────────────────────────────────────────────────────

    async def _poll_fills(self, state: StrategyState) -> None:
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

    async def _handle_fill(self, state: StrategyState, meta: dict, order: dict) -> None:
        side       = meta["side"]
        fill_price = float(order.get("average") or order.get("price") or meta["price"])
        qty        = float(order.get("filled") or meta["qty"])
        level      = meta.get("level", "")
        pnl        = 0.0

        if side == "buy":
            total_cost          = state.avg_buy_price * state.held_qty + fill_price * qty
            state.held_qty     += qty
            state.avg_buy_price = total_cost / state.held_qty if state.held_qty else fill_price
            state.buy_count    += 1

            await _fire(_notify_buy_filled and _notify_buy_filled(
                state.symbol, fill_price, qty, level,
            ))
            logger.info(
                "BUY filled %s @ %.6f qty=%.6f level=%s | held=%.6f avg=%.6f",
                state.symbol, fill_price, qty, level, state.held_qty, state.avg_buy_price,
            )
            # Place/update sell orders based on actual held qty
            await self._place_sell_orders(state)

        else:  # sell
            pnl                 = (fill_price - state.avg_buy_price) * qty
            state.realized_pnl += pnl
            state.held_qty      = max(0.0, state.held_qty - qty)
            state.sell_count   += 1

            await _fire(_notify_sell_filled and _notify_sell_filled(
                state.symbol, fill_price, qty, pnl, level,
            ))
            logger.info(
                "SELL filled %s @ %.6f qty=%.6f level=%s | pnl=%.4f realized=%.4f",
                state.symbol, fill_price, qty, level, pnl, state.realized_pnl,
            )

        await db.record_snr_trade(
            state.symbol, side, fill_price, qty,
            order.get("id", ""), state.strategy_id, pnl, level,
        )
        await db.update_strategy_state(
            state.symbol,
            held_qty      = state.held_qty,
            avg_buy_price = state.avg_buy_price,
            realized_pnl  = state.realized_pnl,
            buy_count     = state.buy_count,
            sell_count    = state.sell_count,
        )
