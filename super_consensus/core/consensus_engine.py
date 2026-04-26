"""
Consensus Engine — orchestrates the five giants, applies voting logic,
and executes trades via the MEXC client.

State per symbol:
  - is_active / is_paused
  - current open position (qty, entry_price, entry_time)
  - realized PnL and trade count

Lifecycle:
  start(symbol, total_investment) → begins the 60-second polling loop
  stop(symbol, market_sell)       → cancels loop, optionally exits position
  pause(symbol) / resume(symbol)  → freeze/unfreeze without closing position
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Dict, List, Optional

from super_consensus.core.giants_engine import GIANTS, Signal, BaseGiant

logger = logging.getLogger(__name__)

# Minimum votes required to act
CONSENSUS_THRESHOLD = 3

# Capital allocation by vote count
VOTE_ALLOCATION: Dict[int, float] = {3: 0.50, 4: 0.75, 5: 1.00}

# Candle settings
CANDLE_TIMEFRAME = "5m"
CANDLE_LIMIT     = 60   # enough for all indicators (EMA50 needs 50+)
POLL_INTERVAL    = 60   # seconds between cycles


# ── Position state ─────────────────────────────────────────────────────────────

@dataclass
class Position:
    qty:        float
    entry_price: float
    entry_time:  datetime
    cost_usdt:   float   # actual USDT spent


@dataclass
class BotState:
    symbol:           str
    total_investment: float
    is_active:        bool = True
    is_paused:        bool = False
    position:         Optional[Position] = None
    realized_pnl:     float = 0.0
    total_trades:     int   = 0
    last_signals:     Dict[str, str] = field(default_factory=dict)
    last_buy_votes:   int = 0
    last_sell_votes:  int = 0
    _task:            Optional[asyncio.Task] = field(default=None, repr=False)


# ── Engine ─────────────────────────────────────────────────────────────────────

class ConsensusEngine:
    """
    Manages one polling loop per symbol.
    Inject a MexcClient and optional async notify callback.
    """

    def __init__(
        self,
        client,                                          # MexcClient instance
        notify: Optional[Callable[[str], Coroutine]] = None,
        giants: Optional[List[BaseGiant]] = None,
    ) -> None:
        self._client  = client
        self._notify  = notify
        self._giants  = giants or GIANTS
        self._bots:   Dict[str, BotState] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(
        self,
        symbol: str,
        total_investment: float,
        db_save_fn: Optional[Callable] = None,
    ) -> None:
        """Start monitoring symbol. Raises if already active."""
        sym = symbol.upper()
        if sym in self._bots and self._bots[sym].is_active:
            raise ValueError(f"{sym} is already running")

        state = BotState(symbol=sym, total_investment=total_investment)
        self._bots[sym] = state

        if db_save_fn:
            await db_save_fn(state)

        task = asyncio.create_task(self._loop(sym, db_save_fn), name=f"super_{sym}")
        state._task = task
        logger.info("SuperConsensus started for %s (investment=%.2f)", sym, total_investment)

    async def stop(
        self,
        symbol: str,
        market_sell: bool = True,
        db_fns: Optional[dict] = None,
    ) -> Optional[float]:
        """
        Stop the bot. If market_sell=True and a position is open, close it.
        Returns realized PnL from the closing trade (or None).
        """
        sym = symbol.upper()
        state = self._bots.get(sym)
        if not state:
            return None

        state.is_active = False
        if state._task and not state._task.done():
            state._task.cancel()
            try:
                await state._task
            except asyncio.CancelledError:
                pass

        pnl = None
        if market_sell and state.position:
            pnl = await self._close_position(sym, state, reason="stop", db_fns=db_fns)

        if db_fns and "deactivate" in db_fns:
            await db_fns["deactivate"](sym)

        del self._bots[sym]
        logger.info("SuperConsensus stopped for %s", sym)
        return pnl

    def pause(self, symbol: str) -> None:
        state = self._bots.get(symbol.upper())
        if state:
            state.is_paused = True
            logger.info("Paused %s", symbol)

    def resume(self, symbol: str) -> None:
        state = self._bots.get(symbol.upper())
        if state:
            state.is_paused = False
            logger.info("Resumed %s", symbol)

    def active_symbols(self) -> List[str]:
        return [s for s, b in self._bots.items() if b.is_active]

    def get_state(self, symbol: str) -> Optional[BotState]:
        return self._bots.get(symbol.upper())

    # ── Polling loop ───────────────────────────────────────────────────────────

    async def _loop(self, symbol: str, db_fns: Optional[dict]) -> None:
        while True:
            state = self._bots.get(symbol)
            if not state or not state.is_active:
                break
            if not state.is_paused:
                try:
                    await self._cycle(symbol, state, db_fns)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Cycle error for %s: %s", symbol, exc, exc_info=True)
                    await self._send(f"⚠️ SuperConsensus [{symbol}] خطأ: {exc}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _cycle(self, symbol: str, state: BotState, db_fns: Optional[dict]) -> None:
        """One full scan: fetch data → compute signals → vote → act."""
        # 1. Fetch candles
        candles = await self._client.fetch_ohlcv(symbol, CANDLE_TIMEFRAME, CANDLE_LIMIT)
        if not candles or len(candles) < 10:
            logger.warning("Not enough candles for %s", symbol)
            return

        price = float(candles[-1][4])   # last close

        # 2. Compute signals from each giant
        signals: Dict[str, Signal] = {}
        for giant in self._giants:
            try:
                signals[giant.name] = giant.compute(candles)
            except Exception as exc:
                logger.error("Giant %s error: %s", giant.name, exc)
                signals[giant.name] = Signal.NEUTRAL

        buy_votes  = sum(1 for s in signals.values() if s == Signal.BUY)
        sell_votes = sum(1 for s in signals.values() if s == Signal.SELL)

        state.last_signals   = {k: v.value for k, v in signals.items()}
        state.last_buy_votes  = buy_votes
        state.last_sell_votes = sell_votes

        action = "none"

        # 3. Consensus logic
        if buy_votes >= CONSENSUS_THRESHOLD and state.position is None:
            action = "buy"
            await self._open_position(symbol, state, price, buy_votes, db_fns)

        elif sell_votes >= CONSENSUS_THRESHOLD and state.position is not None:
            action = "sell"
            await self._close_position(symbol, state, reason="consensus", db_fns=db_fns)

        # 4. Log to DB
        if db_fns and "log_cycle" in db_fns:
            await db_fns["log_cycle"]({
                "symbol":        symbol,
                "timestamp":     datetime.now(timezone.utc),
                "price":         price,
                "rsi_signal":    signals.get("RSI", Signal.NEUTRAL).value,
                "macd_signal":   signals.get("MACD", Signal.NEUTRAL).value,
                "ema_signal":    signals.get("EMA", Signal.NEUTRAL).value,
                "bb_signal":     signals.get("Bollinger", Signal.NEUTRAL).value,
                "volume_signal": signals.get("Volume", Signal.NEUTRAL).value,
                "buy_votes":     buy_votes,
                "sell_votes":    sell_votes,
                "action_taken":  action,
            })

        logger.info(
            "[%s] price=%.4f BUY=%d SELL=%d action=%s",
            symbol, price, buy_votes, sell_votes, action,
        )

    # ── Trade execution ────────────────────────────────────────────────────────

    async def _open_position(
        self,
        symbol: str,
        state: BotState,
        price: float,
        votes: int,
        db_fns: Optional[dict],
    ) -> None:
        allocation = VOTE_ALLOCATION.get(min(votes, 5), 0.50)
        usdt_to_spend = state.total_investment * allocation

        # Calculate qty from USDT budget
        qty = usdt_to_spend / price
        qty = self._client.round_amount(symbol, qty)

        if qty <= 0 or qty < self._client.min_amount(symbol):
            logger.warning("Open position: qty too small for %s", symbol)
            return

        order = await self._client.market_buy(symbol, qty)
        if not order:
            logger.error("market_buy failed for %s", symbol)
            return

        # Use filled price if available, else current price
        filled_price = float(order.get("average") or order.get("price") or price)
        filled_qty   = float(order.get("filled") or qty)

        state.position = Position(
            qty=filled_qty,
            entry_price=filled_price,
            entry_time=datetime.now(timezone.utc),
            cost_usdt=filled_qty * filled_price,
        )

        msg = (
            f"✅ *SuperConsensus [{symbol}]* — دخول\n"
            f"💰 السعر: `{filled_price:.4f}`\n"
            f"📦 الكمية: `{filled_qty:.6f}`\n"
            f"💵 التكلفة: `{filled_qty * filled_price:.2f} USDT`\n"
            f"🗳️ أصوات BUY: `{votes}/5` ({int(allocation*100)}% من رأس المال)"
        )
        await self._send(msg)

        if db_fns and "record_trade" in db_fns:
            await db_fns["record_trade"]({
                "symbol":          symbol,
                "side":            "buy",
                "entry_price":     filled_price,
                "qty":             filled_qty,
                "pnl":             0.0,
                "consensus_votes": votes,
            })
        if db_fns and "update_bot" in db_fns:
            await db_fns["update_bot"](state)

    async def _close_position(
        self,
        symbol: str,
        state: BotState,
        reason: str = "consensus",
        db_fns: Optional[dict] = None,
    ) -> Optional[float]:
        if not state.position:
            return None

        order = await self._client.market_sell_all(symbol)
        if not order:
            logger.error("market_sell_all failed for %s", symbol)
            return None

        exit_price = float(order.get("average") or order.get("price") or 0)
        sold_qty   = float(order.get("filled") or state.position.qty)

        if exit_price == 0:
            # Fallback: fetch current price
            exit_price = await self._client.get_current_price(symbol)

        pnl = (exit_price - state.position.entry_price) * sold_qty
        state.realized_pnl += pnl
        state.total_trades += 1

        msg = (
            f"{'✅' if pnl >= 0 else '❌'} *SuperConsensus [{symbol}]* — خروج ({reason})\n"
            f"💰 سعر الخروج: `{exit_price:.4f}`\n"
            f"📥 سعر الدخول: `{state.position.entry_price:.4f}`\n"
            f"📦 الكمية: `{sold_qty:.6f}`\n"
            f"{'💚' if pnl >= 0 else '🔴'} الربح/الخسارة: `{pnl:+.4f} USDT`\n"
            f"📊 إجمالي الربح: `{state.realized_pnl:+.4f} USDT`"
        )
        await self._send(msg)

        if db_fns and "record_trade" in db_fns:
            await db_fns["record_trade"]({
                "symbol":          symbol,
                "side":            "sell",
                "entry_price":     exit_price,
                "qty":             sold_qty,
                "pnl":             pnl,
                "consensus_votes": state.last_sell_votes,
            })

        state.position = None

        if db_fns and "update_bot" in db_fns:
            await db_fns["update_bot"](state)

        return pnl

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        if self._notify:
            try:
                await self._notify(text)
            except Exception as exc:
                logger.error("Notify failed: %s", exc)

    def get_signals_now(self, candles: list) -> Dict[str, str]:
        """Compute signals synchronously from provided candles (for /signals command)."""
        result = {}
        for giant in self._giants:
            try:
                result[giant.name] = giant.compute(candles).value
            except Exception as exc:
                logger.error("Giant %s error: %s", giant.name, exc)
                result[giant.name] = Signal.NEUTRAL.value
        return result
