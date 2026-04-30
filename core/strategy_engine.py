"""
Support & Resistance Strategy Engine.

Logic per active strategy:
  - Compute N support levels (S1…SN, all below price) and
    N resistance levels (R1…RN, all above price) via pivot-based detection.
    Supports are sorted nearest-first; resistances nearest-first.
  - Place N limit buy orders, one per support level (equal investment split).
  - Each buy order is paired with the nearest resistance above that support
    level as its sell target.
  - On a BUY fill at Si: place a SELL order at the resistance paired with Si.
  - On a SELL fill: update realized PnL.
    • If price has since closed above that resistance (flip condition), the
      resistance is promoted to a support and a new buy order is placed there.
    • When all positions are cleared and no open orders remain, restart cycle.
  - The run loop also checks for resistance flips every poll cycle: if the
    current price has closed above a resistance level, that level is moved
    from resistances to supports and buy orders are refreshed.
  - S/R levels are refreshed every SR_REFRESH_INTERVAL seconds.
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
from core.sr_engine import LevelMode, SRLevels, fetch_sr_levels
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
    num_levels:       int       = 2       # levels per side
    mode:             LevelMode = "both"  # "current" | "previous" | "both"
    strategy_id:      int   = 0
    # order_id → {side, price, qty, level: "s1"|"s2"|..., target_resistance: float|None}
    open_orders:      dict  = field(default_factory=dict)
    held_qty:         float = 0.0
    avg_buy_price:    float = 0.0
    realized_pnl:     float = 0.0
    buy_count:        int   = 0
    sell_count:       int   = 0
    started_at:       datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    running:          bool  = True
    # qty held per resistance target: resistance_price → qty waiting to sell there
    _sell_qty_map:    dict  = field(default_factory=dict)


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
        num_levels: int = 2,
        mode: LevelMode = "both",
    ) -> StrategyState:
        if symbol in self._states and self._states[symbol].running:
            raise ValueError(f"Strategy already running for {symbol}")

        levels = await fetch_sr_levels(
            self._client, symbol, timeframe, num_levels=num_levels, mode=mode
        )
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
            num_levels=num_levels,
            mode=mode,
        )
        self._states[symbol] = state

        strategy_id = await db.upsert_strategy({
            "symbol":           symbol,
            "timeframe":        timeframe,
            "total_investment": total_investment,
            "support1":         levels.supports[0],
            "support2":         levels.supports[1] if len(levels.supports) > 1 else levels.supports[0],
            "resistance1":      levels.resistances[0],
            "resistance2":      levels.resistances[1] if len(levels.resistances) > 1 else levels.resistances[0],
            "level_mode":       mode,
            "is_active":        True,
        })
        state.strategy_id = strategy_id

        # ── Check existing balance before placing buy orders ───────────────────
        base_currency = symbol.split("/")[0]
        try:
            free_qty = await self._client.get_balance(base_currency)
        except Exception as exc:
            logger.warning("Could not fetch %s balance: %s — assuming 0", base_currency, exc)
            free_qty = 0.0

        current_price = levels.current_price
        min_amt       = self._client.min_amount(symbol)
        # Value of existing balance in USDT
        existing_value = free_qty * current_price

        if free_qty >= min_amt and existing_value >= total_investment * 0.8:
            # Existing balance covers ≥80% of investment — treat it as the position
            state.held_qty      = free_qty
            state.avg_buy_price = current_price
            logger.info(
                "Strategy started with existing balance %s: qty=%.6f @ %.6f (value=%.2f USDT)",
                symbol, free_qty, current_price, existing_value,
            )
            await db.update_strategy_state(
                symbol,
                held_qty      = state.held_qty,
                avg_buy_price = state.avg_buy_price,
                realized_pnl  = 0.0,
                buy_count     = 0,
                sell_count    = 0,
            )
            # Place sell orders at resistance levels immediately
            await self._place_sell_orders(state)
        else:
            # No sufficient existing balance — place buy orders at supports
            await self._place_orders(state)

        self._tasks[symbol] = asyncio.create_task(self._run_loop(state))

        logger.info(
            "Strategy started: %s | tf=%s | S=%s R=%s | inv=%.2f | held=%.6f",
            symbol, timeframe,
            [round(s, 6) for s in levels.supports],
            [round(r, 6) for r in levels.resistances],
            total_investment, state.held_qty,
        )
        return state

    async def restore(
        self,
        symbol: str,
        timeframe: str,
        total_investment: float,
        held_qty: float = 0.0,
        avg_buy_price: float = 0.0,
        realized_pnl: float = 0.0,
        buy_count: int = 0,
        sell_count: int = 0,
        num_levels: int = 2,
        mode: LevelMode = "both",
    ) -> StrategyState:
        """
        Restore a strategy from DB after a bot restart.

        Re-fetches S/R levels (market may have moved), restores position state
        from DB, then re-places orders without resetting PnL or held qty.
        """
        if symbol in self._states and self._states[symbol].running:
            raise ValueError(f"Strategy already running for {symbol}")

        levels = await fetch_sr_levels(
            self._client, symbol, timeframe, num_levels=num_levels, mode=mode
        )
        if not levels:
            raise ValueError(
                f"Could not compute S/R levels for {symbol} on {timeframe} during restore."
            )

        state = StrategyState(
            symbol=symbol,
            timeframe=timeframe,
            total_investment=total_investment,
            levels=levels,
            num_levels=num_levels,
            mode=mode,
            held_qty=held_qty,
            avg_buy_price=avg_buy_price,
            realized_pnl=realized_pnl,
            buy_count=buy_count,
            sell_count=sell_count,
        )

        strategy_id = await db.upsert_strategy({
            "symbol":      symbol,
            "timeframe":   timeframe,
            "support1":    levels.supports[0],
            "support2":    levels.supports[1] if len(levels.supports) > 1 else levels.supports[0],
            "resistance1": levels.resistances[0],
            "resistance2": levels.resistances[1] if len(levels.resistances) > 1 else levels.resistances[0],
            "level_mode":  mode,
        })
        state.strategy_id = strategy_id

        self._states[symbol] = state

        # Cancel any stale exchange orders from before the restart
        await self._client.cancel_all_orders(symbol)

        # Re-place buy orders at current S/R levels
        await self._place_orders(state)

        # If we were holding a position, re-place sell orders too
        if state.held_qty > 0:
            await self._place_sell_orders(state)

        self._tasks[symbol] = asyncio.create_task(self._run_loop(state))

        logger.info(
            "Strategy restored: %s | tf=%s | S=[%.4f,%.4f] R=[%.4f,%.4f] "
            "| held=%.6f avg=%.6f pnl=%.4f",
            symbol, timeframe,
            levels.supports[0], levels.supports[1],
            levels.resistances[0], levels.resistances[1],
            held_qty, avg_buy_price, realized_pnl,
        )
        return state

    async def stop(
        self,
        symbol: str,
        market_sell: bool = True,
        persist: bool = True,
    ) -> float:
        """
        Stop a running strategy.

        market_sell: execute a market sell of any held position.
        persist:     write is_active=FALSE to DB. Pass False during bot
                     shutdown so the strategy is restored on next startup.
        """
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

        if persist:
            await db.deactivate_strategy(symbol)
        del self._states[symbol]
        logger.info(
            "Strategy stopped: %s | sell_value=%.4f | persisted=%s",
            symbol, sell_value, persist,
        )
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
        report = {
            "symbol":           symbol,
            "timeframe":        state.timeframe,
            "total_investment": state.total_investment,
            "num_levels":       state.num_levels,
            "mode":             state.mode,
            "supports":         lv.supports,
            "resistances":      lv.resistances,
            # Annotated level objects for display (is_current flag)
            "support_levels":    lv.support_levels,
            "resistance_levels": lv.resistance_levels,
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
        for i, p in enumerate(lv.supports):
            report[f"support{i+1}"] = p
        for i, p in enumerate(lv.resistances):
            report[f"resistance{i+1}"] = p
        return report

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _nearest_resistance_above(self, state: StrategyState, support_price: float) -> float:
        """
        Return the nearest resistance level strictly above support_price.
        Falls back to the lowest resistance if none is strictly above
        (e.g. price already above all resistances after a flip).

        Note: resistances are already sorted ascending (nearest first) by
        sr_engine, so min(above) always gives the closest one.
        """
        above = [r for r in state.levels.resistances if r > support_price]
        if above:
            return min(above)
        return min(state.levels.resistances)

    def _resistance_for_support_index(self, state: StrategyState, support_index: int) -> float:
        """
        Pair each support with a distinct resistance by index:
          S1 (index 0, nearest) → R1 (index 0, nearest)
          S2 (index 1)          → R2 (index 1)
          ...
        If there are fewer resistances than supports, wrap around to the last one.
        """
        resistances = state.levels.resistances
        idx = min(support_index, len(resistances) - 1)
        return resistances[idx]

    # ── Order placement ────────────────────────────────────────────────────────

    async def _place_orders(self, state: StrategyState) -> None:
        """
        Place one buy order per support level (equal investment split).
        Each buy is tagged with its paired resistance target — the nearest
        resistance strictly above that support.  Sell orders are placed only
        after a buy fills (see _place_sell_for_buy).
        """
        lv     = state.levels
        inv    = state.total_investment
        symbol = state.symbol
        n      = len(lv.supports)
        alloc  = inv / n

        for i, price in enumerate(lv.supports):
            level_name        = f"s{i + 1}"
            # Pair Si with Ri by index: S1→R1, S2→R2, etc.
            target_resistance = self._resistance_for_support_index(state, i)
            qty               = self._client.round_amount(symbol, alloc / price)
            order             = await self._client.place_limit_buy(symbol, price, qty)
            if order:
                state.open_orders[order["id"]] = {
                    "side":              "buy",
                    "price":             price,
                    "qty":               qty,
                    "level":             level_name,
                    "target_resistance": target_resistance,
                }
                logger.info(
                    "Placed BUY @ %.6f (%s) qty=%.6f → target R=%.6f",
                    price, level_name, qty, target_resistance,
                )

    async def _place_sell_for_buy(
        self,
        state: StrategyState,
        qty: float,
        target_resistance: float,
    ) -> None:
        """
        Place (or top-up) a sell order at target_resistance for qty coins.

        If an open sell already exists at that price, cancel it and re-place
        with the combined qty so the order book always shows one clean order
        per resistance level.
        """
        symbol  = state.symbol
        min_amt = self._client.min_amount(symbol)

        if qty < min_amt:
            logger.warning(
                "sell qty %.6f below min %.6f for %s — skipping",
                qty, min_amt, symbol,
            )
            return

        # Find and cancel any existing sell at this resistance
        existing_qty = 0.0
        for oid, meta in list(state.open_orders.items()):
            if meta["side"] == "sell" and abs(meta["price"] - target_resistance) < 1e-12:
                await self._client.cancel_order(symbol, oid)
                existing_qty += meta["qty"]
                state.open_orders.pop(oid, None)

        combined_qty = self._client.round_amount(symbol, existing_qty + qty)
        if combined_qty < min_amt:
            return

        # Find resistance label
        res_label = "r?"
        for i, r in enumerate(state.levels.resistances):
            if abs(r - target_resistance) < 1e-12:
                res_label = f"r{i + 1}"
                break

        order = await self._client.place_limit_sell(symbol, target_resistance, combined_qty)
        if order:
            state.open_orders[order["id"]] = {
                "side":  "sell",
                "price": target_resistance,
                "qty":   combined_qty,
                "level": res_label,
            }
            logger.info(
                "Placed SELL @ %.6f (%s) qty=%.6f (prev=%.6f + new=%.6f)",
                target_resistance, res_label, combined_qty, existing_qty, qty,
            )

    async def _place_sell_orders(self, state: StrategyState) -> None:
        """
        Legacy helper used by sync_balance and restore: cancel all open sells
        and re-place based on current held_qty, pairing each resistance with
        an equal share of the position.
        """
        symbol = state.symbol
        lv     = state.levels

        for oid, meta in list(state.open_orders.items()):
            if meta["side"] == "sell":
                await self._client.cancel_order(symbol, oid)
                state.open_orders.pop(oid, None)

        if state.held_qty <= 0:
            return

        n             = len(lv.resistances)
        sell_qty_each = self._client.round_amount(symbol, state.held_qty / n)
        min_amt       = self._client.min_amount(symbol)

        for i, price in enumerate(lv.resistances):
            level_name = f"r{i + 1}"
            qty        = sell_qty_each

            if qty < min_amt:
                if i == 0:
                    qty = self._client.round_amount(symbol, state.held_qty)
                else:
                    break

            order = await self._client.place_limit_sell(symbol, price, qty)
            if order:
                state.open_orders[order["id"]] = {
                    "side": "sell", "price": price, "qty": qty, "level": level_name,
                }
                logger.info("Placed SELL @ %.6f (%s) qty=%.6f", price, level_name, qty)

    async def sync_balance(self, symbol: str) -> dict:
        """
        Fetch the actual free balance of the base currency from the exchange
        and inject it into the strategy state as if the bot had bought it.

        Use case: user bought the coin manually outside the bot and wants
        the strategy to manage it.

        Returns a dict with before/after values for display.
        """
        state = self._states.get(symbol)
        if not state:
            raise ValueError(f"No active strategy for {symbol}")

        # symbol format: ACNUSDT → base = ACN
        base_currency = symbol.replace("USDT", "").replace("BUSD", "").replace("USDC", "")
        free_qty      = await self._client.get_balance(base_currency)
        current_price = await self._client.get_current_price(symbol)
        min_amt       = self._client.min_amount(symbol)

        if free_qty < min_amt:
            return {
                "symbol":       symbol,
                "base":         base_currency,
                "free_qty":     free_qty,
                "old_held_qty": state.held_qty,
                "new_held_qty": state.held_qty,
                "synced":       False,
                "reason":       f"رصيد {base_currency} أقل من الحد الأدنى ({min_amt})",
            }

        old_held      = state.held_qty
        old_avg_price = state.avg_buy_price

        # Qty already locked in open sell orders is reported as "free" by the
        # exchange only after the sell fills.  Subtract it to avoid counting
        # the same coins twice when merging into held_qty.
        locked_in_sells = sum(
            m["qty"] for m in state.open_orders.values() if m["side"] == "sell"
        )
        already_tracked = max(0.0, state.held_qty - locked_in_sells)
        external_qty    = max(0.0, free_qty - already_tracked)

        if external_qty < min_amt:
            return {
                "symbol":       symbol,
                "base":         base_currency,
                "free_qty":     free_qty,
                "old_held_qty": state.held_qty,
                "new_held_qty": state.held_qty,
                "synced":       False,
                "reason":       f"لا يوجد رصيد خارجي إضافي لـ {base_currency} (الرصيد الحر مُحاسَب مسبقاً)",
            }

        # Merge external qty into state using weighted average price
        if state.held_qty > 0 and state.avg_buy_price > 0:
            total_qty           = state.held_qty + external_qty
            state.avg_buy_price = (
                (state.held_qty * state.avg_buy_price + external_qty * current_price)
                / total_qty
            )
            state.held_qty = total_qty
        else:
            state.held_qty      = external_qty
            state.avg_buy_price = current_price

        # Re-place sell orders with updated qty (_place_sell_orders cancels
        # existing sells internally before placing new ones)
        await self._place_sell_orders(state)

        # Persist updated position to DB
        await db.update_strategy_state(
            symbol,
            held_qty      = state.held_qty,
            avg_buy_price = state.avg_buy_price,
            realized_pnl  = state.realized_pnl,
            buy_count     = state.buy_count,
            sell_count    = state.sell_count,
        )

        logger.info(
            "sync_balance %s: free=%.6f external=%.6f old_held=%.6f "
            "new_held=%.6f avg_price=%.6f",
            symbol, free_qty, external_qty, old_held,
            state.held_qty, state.avg_buy_price,
        )

        return {
            "symbol":         symbol,
            "base":           base_currency,
            "free_qty":       free_qty,
            "external_qty":   external_qty,
            "old_held_qty":   old_held,
            "old_avg_price":  old_avg_price,
            "new_held_qty":   state.held_qty,
            "new_avg_price":  state.avg_buy_price,
            "current_price":  current_price,
            "synced":         True,
        }

    async def _refresh_orders(self, state: StrategyState) -> None:
        """Cancel all open orders, re-compute S/R, re-place orders."""
        await self._client.cancel_all_orders(state.symbol)
        state.open_orders.clear()

        new_levels = await fetch_sr_levels(
            self._client, state.symbol, state.timeframe,
            num_levels=state.num_levels, mode=state.mode,
        )
        if not new_levels:
            logger.warning("S/R refresh failed for %s — keeping old levels", state.symbol)
            new_levels = state.levels

        old_levels = state.levels
        state.levels = new_levels

        upsert_data = {"symbol": state.symbol, "level_mode": state.mode}
        for i, p in enumerate(new_levels.supports):
            upsert_data[f"support{i + 1}"] = p
        for i, p in enumerate(new_levels.resistances):
            upsert_data[f"resistance{i + 1}"] = p
        await db.upsert_strategy(upsert_data)

        await self._place_orders(state)
        if state.held_qty > 0:
            await self._place_sell_orders(state)

        await _fire(_notify_sr_refresh and _notify_sr_refresh(
            state.symbol, state.timeframe,
            old_levels, new_levels,
        ))
        sup_str = ", ".join(f"{p:.4f}" for p in new_levels.supports)
        res_str = ", ".join(f"{p:.4f}" for p in new_levels.resistances)
        logger.info("S/R refreshed for %s: S=[%s] R=[%s]", state.symbol, sup_str, res_str)

    # ── Flip detection ─────────────────────────────────────────────────────────

    async def _check_flips(self, state: StrategyState) -> None:
        """
        Detect resistance levels that price has closed above (flip to support).

        When a resistance is breached:
          1. Remove it from resistances list.
          2. Add it to supports list (nearest-first order maintained).
          3. Cancel all open buy orders and re-place at updated support levels.
          4. Any open sell orders at the flipped resistance are left in place —
             if they fill it means price came back down, which is fine.
        """
        try:
            current_price = await self._client.get_current_price(state.symbol)
        except Exception as exc:
            logger.warning("flip check: get_current_price failed for %s: %s", state.symbol, exc)
            return

        lv = state.levels
        flipped = [r for r in lv.resistances if current_price > r]
        if not flipped:
            return

        for r in flipped:
            logger.info(
                "FLIP %s: resistance %.6f breached (price=%.6f) → promoted to support",
                state.symbol, r, current_price,
            )
            lv.resistances = [x for x in lv.resistances if abs(x - r) > 1e-12]
            # Insert into supports, keep sorted nearest-to-price first (descending)
            lv.supports.append(r)
            lv.supports.sort(reverse=True)

        if not lv.resistances:
            # No resistances left — trigger a full S/R refresh
            logger.info("No resistances left for %s after flip — refreshing S/R", state.symbol)
            await self._refresh_orders(state)
            return

        # Cancel pending buy orders and re-place at updated support levels
        for oid, meta in list(state.open_orders.items()):
            if meta["side"] == "buy":
                await self._client.cancel_order(state.symbol, oid)
                state.open_orders.pop(oid, None)

        await self._place_orders(state)

        await _fire(_notify_sr_refresh and _notify_sr_refresh(
            state.symbol, state.timeframe, lv, lv,
        ))

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
                else:
                    # Check for resistance→support flips every poll cycle
                    await self._check_flips(state)

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
            # Skip if already removed by a concurrent iteration
            if order_id not in state.open_orders:
                continue
            try:
                order = await self._client.fetch_order(state.symbol, order_id)
            except Exception as exc:
                logger.warning("fetch_order failed for %s id=%s: %s", state.symbol, order_id, exc)
                continue
            if not order:
                continue
            status = order.get("status", "")
            if status == "closed":
                state.open_orders.pop(order_id, None)
                await self._handle_fill(state, meta, order)
            elif status == "canceled":
                state.open_orders.pop(order_id, None)

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

            # Determine sell target: use the paired resistance stored in order meta.
            # Fallback: nearest resistance above fill price (e.g. after a flip).
            target_resistance = (
                meta.get("target_resistance")
                or self._nearest_resistance_above(state, fill_price)
            )

            await _fire(_notify_buy_filled and _notify_buy_filled(
                state.symbol, fill_price, qty, level,
            ))
            logger.info(
                "BUY filled %s @ %.6f qty=%.6f level=%s → sell target=%.6f | held=%.6f",
                state.symbol, fill_price, qty, level, target_resistance, state.held_qty,
            )
            # Place sell at the specific resistance paired with this buy
            await self._place_sell_for_buy(state, qty, target_resistance)

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

            # Cycle complete — re-enter with fresh buy orders when:
            #   • no remaining position (within floating-point dust)
            #   • no open sell orders still waiting
            #   • no open buy orders already placed
            dust_qty   = self._client.min_amount(state.symbol)
            open_buys  = [m for m in state.open_orders.values() if m["side"] == "buy"]
            open_sells = [m for m in state.open_orders.values() if m["side"] == "sell"]

            if state.held_qty <= dust_qty and not open_sells and not open_buys:
                state.held_qty      = 0.0
                state.avg_buy_price = 0.0
                logger.info("Cycle complete for %s — re-placing buy orders", state.symbol)
                await self._place_orders(state)

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
