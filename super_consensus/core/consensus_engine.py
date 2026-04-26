"""
Consensus Engine — AI-driven trading loop.

Flow per cycle:
  1. Fetch current price from MEXC
  2. Run MARKET_GIANTS in parallel (Giant1, Giant3, Giant4, Giant5)
  3. Feed their reports to AIJudge (Giant2) — rate-limited per symbol
  4. AIJudge returns: signal, confidence, reason
  5. Act on signal:
       BUY  (confidence >= threshold, no open position) → market buy
       SELL (confidence >= threshold, open position)    → market sell
       HOLD → do nothing
  6. Log everything to DB

Rate limiting: AIJudge is called at most once per AI_JUDGE_INTERVAL_MINUTES.
Between calls, the last AIJudge decision is reused.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Dict, List, Optional

from config.settings import AI_JUDGE_INTERVAL_MINUTES
from super_consensus.core.giants_engine import (
    MARKET_GIANTS,
    AI_JUDGE,
    GiantReport,
    BaseGiant,
    AIJudgeGiant,
)

logger = logging.getLogger(__name__)

# Minimum confidence for AIJudge decision to trigger a trade
CONFIDENCE_THRESHOLD = 55

# Seconds between full market scans
POLL_INTERVAL = 60

# Seconds between AIJudge LLM calls (rate-limit guard)
AI_JUDGE_INTERVAL_SECONDS = AI_JUDGE_INTERVAL_MINUTES * 60


# ── Position & state ───────────────────────────────────────────────────────────

@dataclass
class Position:
    qty:         float
    entry_price: float
    entry_time:  datetime
    cost_usdt:   float


@dataclass
class BotState:
    symbol:           str
    total_investment: float
    is_active:        bool = True
    is_paused:        bool = False
    position:         Optional[Position] = None
    realized_pnl:     float = 0.0
    total_trades:     int   = 0

    # Last reports from each giant
    last_market_reports: List[GiantReport] = field(default_factory=list)
    last_judge_report:   Optional[GiantReport] = None

    # Rate-limit tracking for AIJudge
    last_judge_call_ts:  float = 0.0   # monotonic timestamp

    _task: Optional[asyncio.Task] = field(default=None, repr=False)


# ── Engine ─────────────────────────────────────────────────────────────────────

class ConsensusEngine:
    def __init__(
        self,
        client,
        notify: Optional[Callable[[str], Coroutine]] = None,
        market_giants: Optional[List[BaseGiant]] = None,
        ai_judge: Optional[AIJudgeGiant] = None,
    ) -> None:
        self._client        = client
        self._notify        = notify
        self._market_giants = market_giants or MARKET_GIANTS
        self._ai_judge      = ai_judge or AI_JUDGE
        self._bots: Dict[str, BotState] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    def active_symbols(self) -> List[str]:
        return [s for s, b in self._bots.items() if b.is_active]

    def get_state(self, symbol: str) -> Optional[BotState]:
        return self._bots.get(symbol.upper())

    def pause(self, symbol: str) -> None:
        state = self._bots.get(symbol.upper())
        if state:
            state.is_paused = True

    def resume(self, symbol: str) -> None:
        state = self._bots.get(symbol.upper())
        if state:
            state.is_paused = False

    async def stop(
        self,
        symbol: str,
        market_sell: bool = True,
        db_fns: Optional[dict] = None,
    ) -> Optional[float]:
        sym   = symbol.upper()
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
        return pnl

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
                    logger.error("Cycle error [%s]: %s", symbol, exc, exc_info=True)
                    await self._send(f"⚠️ SuperConsensus [{symbol}] خطأ: {exc}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _cycle(self, symbol: str, state: BotState, db_fns: Optional[dict]) -> None:
        # 1. Current price
        price = await self._client.get_current_price(symbol)

        # 2. Run market giants concurrently
        tasks = [g.analyze(symbol, price) for g in self._market_giants]
        market_reports: List[GiantReport] = await asyncio.gather(*tasks, return_exceptions=False)
        state.last_market_reports = list(market_reports)

        # 3. AIJudge — rate-limited
        import time
        now = time.monotonic()
        if now - state.last_judge_call_ts >= AI_JUDGE_INTERVAL_SECONDS:
            judge_report = await self._ai_judge.judge(symbol, price, market_reports)
            state.last_judge_report  = judge_report
            state.last_judge_call_ts = now
            logger.info(
                "[%s] AIJudge called: %s (%d%%) — %s",
                symbol, judge_report.signal, judge_report.confidence, judge_report.reason,
            )
        else:
            judge_report = state.last_judge_report
            remaining = int(AI_JUDGE_INTERVAL_SECONDS - (now - state.last_judge_call_ts))
            logger.info("[%s] AIJudge reusing last decision (next call in %ds)", symbol, remaining)

        if judge_report is None:
            logger.info("[%s] No AIJudge decision yet — skipping", symbol)
            return

        # 4. Act on decision
        action = "none"
        sig  = judge_report.signal
        conf = judge_report.confidence

        if sig == "BUY" and conf >= CONFIDENCE_THRESHOLD and state.position is None:
            action = "buy"
            await self._open_position(symbol, state, price, judge_report, db_fns)

        elif sig == "SELL" and conf >= CONFIDENCE_THRESHOLD and state.position is not None:
            action = "sell"
            await self._close_position(symbol, state, reason="AIJudge", db_fns=db_fns)

        # 5. Log cycle to DB
        if db_fns and "log_cycle" in db_fns:
            await db_fns["log_cycle"]({
                "symbol":        symbol,
                "timestamp":     datetime.now(timezone.utc),
                "price":         price,
                "rsi_signal":    market_reports[0].signal if len(market_reports) > 0 else "HOLD",
                "macd_signal":   market_reports[1].signal if len(market_reports) > 1 else "HOLD",
                "ema_signal":    market_reports[2].signal if len(market_reports) > 2 else "HOLD",
                "bb_signal":     market_reports[3].signal if len(market_reports) > 3 else "HOLD",
                "volume_signal": judge_report.signal,
                "buy_votes":     conf if sig == "BUY" else 0,
                "sell_votes":    conf if sig == "SELL" else 0,
                "action_taken":  action,
            })

        logger.info(
            "[%s] price=%.4f judge=%s conf=%d%% action=%s",
            symbol, price, sig, conf, action,
        )

    # ── Trade execution ────────────────────────────────────────────────────────

    async def _open_position(
        self,
        symbol: str,
        state: BotState,
        price: float,
        judge: GiantReport,
        db_fns: Optional[dict],
    ) -> None:
        usdt_to_spend = state.total_investment
        qty = self._client.round_amount(symbol, usdt_to_spend / price)

        if qty <= 0 or qty < self._client.min_amount(symbol):
            logger.warning("Open position: qty too small for %s", symbol)
            return

        order = await self._client.market_buy(symbol, qty)
        if not order:
            return

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
            f"🧠 قرار القاضي: `{judge.signal}` — ثقة `{judge.confidence}%`\n"
            f"💬 السبب: _{judge.reason}_"
        )
        await self._send(msg)

        if db_fns and "record_trade" in db_fns:
            await db_fns["record_trade"]({
                "symbol":          symbol,
                "side":            "buy",
                "entry_price":     filled_price,
                "qty":             filled_qty,
                "pnl":             0.0,
                "consensus_votes": judge.confidence,
            })
        if db_fns and "update_bot" in db_fns:
            await db_fns["update_bot"](state)

    async def _close_position(
        self,
        symbol: str,
        state: BotState,
        reason: str = "AIJudge",
        db_fns: Optional[dict] = None,
    ) -> Optional[float]:
        if not state.position:
            return None

        order = await self._client.market_sell_all(symbol)
        if not order:
            return None

        exit_price = float(order.get("average") or order.get("price") or 0)
        sold_qty   = float(order.get("filled") or state.position.qty)
        if exit_price == 0:
            exit_price = await self._client.get_current_price(symbol)

        pnl = (exit_price - state.position.entry_price) * sold_qty
        state.realized_pnl += pnl
        state.total_trades += 1

        judge = state.last_judge_report
        judge_line = ""
        if judge:
            judge_line = (
                f"\n🧠 قرار القاضي: `{judge.signal}` — ثقة `{judge.confidence}%`"
                f"\n💬 السبب: _{judge.reason}_"
            )

        msg = (
            f"{'✅' if pnl >= 0 else '❌'} *SuperConsensus [{symbol}]* — خروج ({reason})\n"
            f"💰 سعر الخروج: `{exit_price:.4f}`\n"
            f"📥 سعر الدخول: `{state.position.entry_price:.4f}`\n"
            f"📦 الكمية: `{sold_qty:.6f}`\n"
            f"{'💚' if pnl >= 0 else '🔴'} الربح/الخسارة: `{pnl:+.4f} USDT`\n"
            f"📊 إجمالي الربح: `{state.realized_pnl:+.4f} USDT`"
            f"{judge_line}"
        )
        await self._send(msg)

        if db_fns and "record_trade" in db_fns:
            await db_fns["record_trade"]({
                "symbol":          symbol,
                "side":            "sell",
                "entry_price":     exit_price,
                "qty":             sold_qty,
                "pnl":             pnl,
                "consensus_votes": judge.confidence if judge else 0,
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

    async def get_signals_now(self, symbol: str, price: float) -> dict:
        """Run all giants + AIJudge immediately (for /signals command)."""
        tasks = [g.analyze(symbol, price) for g in self._market_giants]
        market_reports = await asyncio.gather(*tasks, return_exceptions=False)
        judge_report   = await self._ai_judge.judge(symbol, price, list(market_reports))
        return {
            "market_reports": market_reports,
            "judge":          judge_report,
        }
