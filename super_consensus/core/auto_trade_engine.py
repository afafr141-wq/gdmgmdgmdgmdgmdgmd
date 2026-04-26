"""
Auto-Trade Engine — fully autonomous trading loop.

Flow every scan_interval_min minutes:
  1. Run SmartScanner on top N coins
  2. Check consensus: all 3 analysts must agree BUY with conf >= min_analyst_conf
  3. Apply risk guards (max open trades, capital cap, cooldown)
  4. Execute market buy, record position
  5. Monitor open positions every MONITOR_INTERVAL seconds:
       - Take-profit hit  → sell
       - Stop-loss hit    → sell
       - 24h timeout      → sell
       - Analyst reversal → sell (checked each scan)
  6. Send periodic 4-hour summary report
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Coroutine, Dict, List, Optional

from config.settings import (
    AUTO_SCAN_INTERVAL_MINUTES,
    AUTO_TAKE_PROFIT_PCT,
    AUTO_STOP_LOSS_PCT,
    AUTO_MAX_OPEN_TRADES,
    AUTO_MAX_CAPITAL_PCT,
    AUTO_MAX_HOLD_HOURS,
    AUTO_MIN_COINS_SCANNED,
    AUTO_MIN_ANALYST_CONF,
    AUTO_COOLDOWN_MINUTES,
    AUTO_REPORT_INTERVAL_HOURS,
)

logger = logging.getLogger(__name__)

# How often to check open positions for TP/SL (seconds)
MONITOR_INTERVAL = 30


# ── Settings dataclass ─────────────────────────────────────────────────────────

@dataclass
class AutoSettings:
    is_active:           bool  = False
    trade_amount_usdt:   Optional[float] = None   # fixed USDT (None = use risk_pct)
    risk_pct:            Optional[float] = None   # % of free capital (None = use fixed)
    take_profit_pct:     float = AUTO_TAKE_PROFIT_PCT
    stop_loss_pct:       float = AUTO_STOP_LOSS_PCT
    max_open_trades:     int   = AUTO_MAX_OPEN_TRADES
    max_capital_pct:     float = AUTO_MAX_CAPITAL_PCT
    max_hold_hours:      int   = AUTO_MAX_HOLD_HOURS
    min_coins_scanned:   int   = AUTO_MIN_COINS_SCANNED
    min_analyst_conf:    int   = AUTO_MIN_ANALYST_CONF
    cooldown_minutes:    int   = AUTO_COOLDOWN_MINUTES
    scan_interval_min:   int   = AUTO_SCAN_INTERVAL_MINUTES
    report_interval_hrs: int   = AUTO_REPORT_INTERVAL_HOURS
    total_capital_usdt:  float = 0.0
    realized_pnl:        float = 0.0
    total_auto_trades:   int   = 0


# ── Open position ──────────────────────────────────────────────────────────────

@dataclass
class AutoPosition:
    symbol:          str
    qty:             float
    entry_price:     float
    entry_time:      datetime
    cost_usdt:       float
    take_profit_pct: float
    stop_loss_pct:   float

    @property
    def tp_price(self) -> float:
        return self.entry_price * (1 + self.take_profit_pct / 100)

    @property
    def sl_price(self) -> float:
        return self.entry_price * (1 - self.stop_loss_pct / 100)

    @property
    def age_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.entry_time).total_seconds() / 3600


# ── Engine ─────────────────────────────────────────────────────────────────────

class AutoTradeEngine:
    """
    Singleton-style engine. One instance per bot process.
    Activated/deactivated via enable() / disable().
    """

    def __init__(
        self,
        client,
        notify: Optional[Callable[[str], Coroutine]] = None,
    ) -> None:
        self._client   = client
        self._notify   = notify
        self.settings  = AutoSettings()
        self._positions: Dict[str, AutoPosition] = {}
        self._scan_task:    Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._report_task:  Optional[asyncio.Task] = None
        self._last_trade_ts: float = 0.0   # monotonic, for cooldown

    # ── Public API ─────────────────────────────────────────────────────────────

    def is_active(self) -> bool:
        return self.settings.is_active

    def open_positions(self) -> List[AutoPosition]:
        return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[AutoPosition]:
        return self._positions.get(symbol.upper())

    async def enable(
        self,
        trade_amount_usdt: Optional[float] = None,
        risk_pct: Optional[float] = None,
        db_save=None,
    ) -> None:
        """Activate auto-trade mode. Starts background tasks."""
        self.settings.is_active         = True
        self.settings.trade_amount_usdt = trade_amount_usdt
        self.settings.risk_pct          = risk_pct

        # Fetch current USDT balance as starting capital
        try:
            bal = await self._client.get_balance("USDT")
            self.settings.total_capital_usdt = float(bal or 0)
        except Exception as exc:
            logger.warning("Could not fetch USDT balance: %s", exc)

        if db_save:
            await db_save(self._settings_dict())

        self._start_tasks()
        logger.info("AutoTradeEngine enabled — amount=%s risk_pct=%s", trade_amount_usdt, risk_pct)

    async def disable(self, db_save=None) -> None:
        """Deactivate auto-trade mode. Stops background tasks (keeps open positions)."""
        self.settings.is_active = False
        self._stop_tasks()
        if db_save:
            await db_save(self._settings_dict())
        logger.info("AutoTradeEngine disabled")

    async def restore_from_db(self, settings_row: dict, positions: list[dict]) -> None:
        """Restore state after bot restart."""
        s = self.settings
        s.is_active           = bool(settings_row.get("is_active", False))
        s.trade_amount_usdt   = settings_row.get("trade_amount_usdt")
        s.risk_pct            = settings_row.get("risk_pct")
        s.take_profit_pct     = float(settings_row.get("take_profit_pct", AUTO_TAKE_PROFIT_PCT))
        s.stop_loss_pct       = float(settings_row.get("stop_loss_pct", AUTO_STOP_LOSS_PCT))
        s.max_open_trades     = int(settings_row.get("max_open_trades", AUTO_MAX_OPEN_TRADES))
        s.max_capital_pct     = float(settings_row.get("max_capital_pct", AUTO_MAX_CAPITAL_PCT))
        s.max_hold_hours      = int(settings_row.get("max_hold_hours", AUTO_MAX_HOLD_HOURS))
        s.min_coins_scanned   = int(settings_row.get("min_coins_scanned", AUTO_MIN_COINS_SCANNED))
        s.min_analyst_conf    = int(settings_row.get("min_analyst_conf", AUTO_MIN_ANALYST_CONF))
        s.cooldown_minutes    = int(settings_row.get("cooldown_minutes", AUTO_COOLDOWN_MINUTES))
        s.scan_interval_min   = int(settings_row.get("scan_interval_min", AUTO_SCAN_INTERVAL_MINUTES))
        s.report_interval_hrs = int(settings_row.get("report_interval_hrs", AUTO_REPORT_INTERVAL_HOURS))
        s.total_capital_usdt  = float(settings_row.get("total_capital_usdt", 0))
        s.realized_pnl        = float(settings_row.get("realized_pnl", 0))
        s.total_auto_trades   = int(settings_row.get("total_auto_trades", 0))

        for row in positions:
            sym = row["symbol"].upper()
            self._positions[sym] = AutoPosition(
                symbol=sym,
                qty=float(row["qty"]),
                entry_price=float(row["entry_price"]),
                entry_time=row["entry_time"].replace(tzinfo=timezone.utc)
                    if row["entry_time"] else datetime.now(timezone.utc),
                cost_usdt=float(row["cost_usdt"]),
                take_profit_pct=float(row["take_profit_pct"]),
                stop_loss_pct=float(row["stop_loss_pct"]),
            )

        if s.is_active:
            self._start_tasks()
            logger.info(
                "AutoTradeEngine restored: active=True, %d open positions",
                len(self._positions),
            )

    # ── Background tasks ───────────────────────────────────────────────────────

    def _start_tasks(self) -> None:
        if not self._scan_task or self._scan_task.done():
            self._scan_task = asyncio.create_task(self._scan_loop(), name="auto_scan")
        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop(), name="auto_monitor")
        if not self._report_task or self._report_task.done():
            self._report_task = asyncio.create_task(self._report_loop(), name="auto_report")

    def _stop_tasks(self) -> None:
        for task in (self._scan_task, self._monitor_task, self._report_task):
            if task and not task.done():
                task.cancel()

    # ── Scan loop ──────────────────────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        """Run SmartScanner every scan_interval_min minutes."""
        from super_consensus.core.smart_scanner import SMART_SCANNER

        logger.info("AutoTrade scan loop started (interval=%dm)", self.settings.scan_interval_min)
        while self.settings.is_active:
            try:
                await self._run_scan(SMART_SCANNER)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("AutoTrade scan error: %s", exc, exc_info=True)
                await self._send(f"⚠️ *Auto-Trade* خطأ في المسح: `{exc}`")

            interval = self.settings.scan_interval_min * 60
            logger.info("AutoTrade: next scan in %d minutes", self.settings.scan_interval_min)
            await asyncio.sleep(interval)

    async def _run_scan(self, scanner) -> None:
        s = self.settings
        coin_count = max(s.min_coins_scanned, 50)

        logger.info("AutoTrade: scanning %d coins…", coin_count)
        result = await scanner.scan(coin_count=coin_count)

        if result.coins_scanned < s.min_coins_scanned:
            logger.info(
                "AutoTrade: only %d coins scanned (need %d) — skipping",
                result.coins_scanned, s.min_coins_scanned,
            )
            return

        # Check each final pick for full consensus
        for pick in result.final_picks:
            if pick.signal.upper() != "BUY":
                continue
            if pick.confidence < s.min_analyst_conf:
                continue

            # Require all 3 analysts to agree (votes == number of analysts)
            analyst_count = len(result.analyst_reports)
            if len(pick.analysts) < analyst_count:
                logger.info(
                    "AutoTrade: %s has %d/%d analysts — need full consensus",
                    pick.symbol, len(pick.analysts), analyst_count,
                )
                continue

            symbol = f"{pick.symbol}/USDT" if "/" not in pick.symbol else pick.symbol
            await self._try_open(symbol, pick.confidence, pick.reason)

        # Also check open positions for analyst reversal (SELL consensus)
        await self._check_reversal(result)

    async def _check_reversal(self, scan_result) -> None:
        """If all analysts agree SELL on an open position, close it."""
        for pos in list(self._positions.values()):
            base = pos.symbol.replace("/USDT", "")
            for pick in scan_result.final_picks:
                if pick.symbol.upper() == base.upper() and pick.signal.upper() == "SELL":
                    analyst_count = len(scan_result.analyst_reports)
                    if len(pick.analysts) >= analyst_count and pick.confidence >= self.settings.min_analyst_conf:
                        logger.info("AutoTrade: analyst reversal SELL for %s", pos.symbol)
                        await self._close_position(pos, reason="analyst_reversal", conf=pick.confidence)

    # ── Monitor loop ───────────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Check TP/SL/timeout for open positions every MONITOR_INTERVAL seconds."""
        logger.info("AutoTrade monitor loop started")
        while self.settings.is_active or self._positions:
            try:
                await self._check_exits()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("AutoTrade monitor error: %s", exc, exc_info=True)
            await asyncio.sleep(MONITOR_INTERVAL)

    async def _check_exits(self) -> None:
        for pos in list(self._positions.values()):
            try:
                price = await self._client.get_current_price(pos.symbol)
            except Exception as exc:
                logger.warning("Price fetch failed for %s: %s", pos.symbol, exc)
                continue

            if price >= pos.tp_price:
                await self._close_position(pos, reason="take_profit", exit_price=price)
            elif price <= pos.sl_price:
                await self._close_position(pos, reason="stop_loss", exit_price=price)
            elif pos.age_hours >= self.settings.max_hold_hours:
                await self._close_position(pos, reason="timeout_24h", exit_price=price)

    # ── Report loop ────────────────────────────────────────────────────────────

    async def _report_loop(self) -> None:
        """Send a summary report every report_interval_hrs hours."""
        logger.info("AutoTrade report loop started")
        while self.settings.is_active:
            await asyncio.sleep(self.settings.report_interval_hrs * 3600)
            if not self.settings.is_active:
                break
            try:
                await self._send_report()
            except Exception as exc:
                logger.error("AutoTrade report error: %s", exc)

    async def _send_report(self) -> None:
        s = self.settings
        open_pos = self.open_positions()

        unrealized = 0.0
        pos_lines = []
        for pos in open_pos:
            try:
                price = await self._client.get_current_price(pos.symbol)
                unr = (price - pos.entry_price) * pos.qty
                unrealized += unr
                pct = (price / pos.entry_price - 1) * 100
                pos_lines.append(
                    f"  • `{pos.symbol}` — {'+' if pct >= 0 else ''}{pct:.2f}% "
                    f"({'%+.2f' % unr} USDT)"
                )
            except Exception:
                pos_lines.append(f"  • `{pos.symbol}` — لا يمكن جلب السعر")

        pos_block = "\n".join(pos_lines) if pos_lines else "  لا توجد صفقات مفتوحة"

        msg = (
            f"📊 *تقرير Auto-Trade — كل {s.report_interval_hrs} ساعات*\n\n"
            f"✅ صفقات منفذة: `{s.total_auto_trades}`\n"
            f"💰 ربح/خسارة محقق: `{s.realized_pnl:+.4f} USDT`\n"
            f"📈 غير محقق (مفتوح): `{unrealized:+.4f} USDT`\n"
            f"🔓 صفقات مفتوحة: `{len(open_pos)}/{s.max_open_trades}`\n\n"
            f"📌 *المراكز المفتوحة:*\n{pos_block}\n\n"
            f"💵 رأس المال الأساسي: `{s.total_capital_usdt:.2f} USDT`"
        )
        await self._send(msg)

    # ── Trade execution ────────────────────────────────────────────────────────

    async def _try_open(self, symbol: str, conf: int, reason: str) -> None:
        s = self.settings

        # Guard: already have a position in this symbol
        if symbol.upper() in self._positions:
            return

        # Guard: max open trades
        if len(self._positions) >= s.max_open_trades:
            logger.info("AutoTrade: max open trades (%d) reached — skip %s", s.max_open_trades, symbol)
            return

        # Guard: cooldown
        elapsed = (time.monotonic() - self._last_trade_ts) / 60
        if self._last_trade_ts > 0 and elapsed < s.cooldown_minutes:
            logger.info(
                "AutoTrade: cooldown active (%.0f/%d min) — skip %s",
                elapsed, s.cooldown_minutes, symbol,
            )
            return

        # Guard: capital cap
        invested = sum(p.cost_usdt for p in self._positions.values())
        if s.total_capital_usdt > 0:
            invested_pct = (invested / s.total_capital_usdt) * 100
            if invested_pct >= s.max_capital_pct:
                logger.info(
                    "AutoTrade: capital cap %.0f%% reached — skip %s",
                    s.max_capital_pct, symbol,
                )
                return

        # Determine trade size
        usdt_to_spend = await self._calc_trade_size(invested)
        if usdt_to_spend <= 0:
            logger.warning("AutoTrade: trade size is 0 — skip %s", symbol)
            return

        # Execute buy
        try:
            price = await self._client.get_current_price(symbol)
            qty   = self._client.round_amount(symbol, usdt_to_spend / price)
            if qty <= 0 or qty < self._client.min_amount(symbol):
                logger.warning("AutoTrade: qty too small for %s", symbol)
                return

            order = await self._client.market_buy(symbol, qty)
            if not order:
                return

            filled_price = float(order.get("average") or order.get("price") or price)
            filled_qty   = float(order.get("filled") or qty)
            cost         = filled_qty * filled_price

            pos = AutoPosition(
                symbol=symbol.upper(),
                qty=filled_qty,
                entry_price=filled_price,
                entry_time=datetime.now(timezone.utc),
                cost_usdt=cost,
                take_profit_pct=s.take_profit_pct,
                stop_loss_pct=s.stop_loss_pct,
            )
            self._positions[symbol.upper()] = pos
            self._last_trade_ts = time.monotonic()

            # Persist
            await self._db_insert_position(pos)
            await self._db_record_trade(symbol, "buy", filled_price, filled_qty, cost, 0.0, "entry", conf)

            msg = (
                f"🤖 *Auto-Trade — شراء تلقائي*\n\n"
                f"🪙 العملة: `{symbol}`\n"
                f"💰 السعر: `{filled_price:.6f} USDT`\n"
                f"📦 الكمية: `{filled_qty:.6f}`\n"
                f"💵 التكلفة: `{cost:.2f} USDT`\n"
                f"🎯 هدف الربح: `{pos.tp_price:.6f}` (+{s.take_profit_pct}%)\n"
                f"🛡️ وقف الخسارة: `{pos.sl_price:.6f}` (-{s.stop_loss_pct}%)\n"
                f"🧠 ثقة المحللين: `{conf}%`\n"
                f"💬 _{reason}_"
            )
            await self._send(msg)
            logger.info("AutoTrade: opened %s @ %.6f (cost=%.2f USDT)", symbol, filled_price, cost)

        except Exception as exc:
            logger.error("AutoTrade buy error [%s]: %s", symbol, exc, exc_info=True)
            await self._send(f"❌ *Auto-Trade* فشل الشراء لـ `{symbol}`: `{exc}`")

    async def _close_position(
        self,
        pos: AutoPosition,
        reason: str,
        exit_price: Optional[float] = None,
        conf: int = 0,
    ) -> None:
        symbol = pos.symbol
        try:
            order = await self._client.market_sell_all(symbol)
            if not order:
                return

            ep    = float(order.get("average") or order.get("price") or exit_price or 0)
            sq    = float(order.get("filled") or pos.qty)
            if ep == 0:
                ep = await self._client.get_current_price(symbol)

            pnl = (ep - pos.entry_price) * sq
            self.settings.realized_pnl    += pnl
            self.settings.total_auto_trades += 1
            # Compound: add profit to capital
            self.settings.total_capital_usdt += pnl

            del self._positions[symbol]

            await self._db_delete_position(symbol)
            await self._db_record_trade(symbol, "sell", ep, sq, pos.cost_usdt, pnl, reason, conf)
            await self._db_save_settings()

            reason_labels = {
                "take_profit":      "🎯 هدف الربح",
                "stop_loss":        "🛡️ وقف الخسارة",
                "timeout_24h":      "⏰ انتهاء المهلة (24h)",
                "analyst_reversal": "🔄 تحول المحللين للبيع",
                "stop":             "🛑 إيقاف يدوي",
            }
            label = reason_labels.get(reason, reason)
            icon  = "✅" if pnl >= 0 else "❌"

            msg = (
                f"{icon} *Auto-Trade — بيع تلقائي*\n\n"
                f"🪙 العملة: `{symbol}`\n"
                f"📤 سعر البيع: `{ep:.6f} USDT`\n"
                f"📥 سعر الدخول: `{pos.entry_price:.6f} USDT`\n"
                f"📦 الكمية: `{sq:.6f}`\n"
                f"{'💚' if pnl >= 0 else '🔴'} الربح/الخسارة: `{pnl:+.4f} USDT`\n"
                f"📊 إجمالي الربح: `{self.settings.realized_pnl:+.4f} USDT`\n"
                f"💵 رأس المال الجديد: `{self.settings.total_capital_usdt:.2f} USDT`\n"
                f"📌 السبب: {label}"
            )
            await self._send(msg)
            logger.info("AutoTrade: closed %s @ %.6f pnl=%.4f reason=%s", symbol, ep, pnl, reason)

        except Exception as exc:
            logger.error("AutoTrade sell error [%s]: %s", symbol, exc, exc_info=True)
            await self._send(f"❌ *Auto-Trade* فشل البيع لـ `{symbol}`: `{exc}`")

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _calc_trade_size(self, already_invested: float) -> float:
        s = self.settings
        if s.trade_amount_usdt is not None:
            return float(s.trade_amount_usdt)
        if s.risk_pct is not None:
            try:
                free = await self._client.get_balance("USDT")
                return float(free) * (s.risk_pct / 100)
            except Exception as exc:
                logger.warning("Balance fetch failed: %s", exc)
                return 0.0
        return 0.0

    async def _send(self, text: str) -> None:
        if self._notify:
            try:
                await self._notify(text)
            except Exception as exc:
                logger.error("AutoTrade notify failed: %s", exc)

    def _settings_dict(self) -> dict:
        s = self.settings
        return {
            "is_active":           s.is_active,
            "trade_amount_usdt":   s.trade_amount_usdt,
            "risk_pct":            s.risk_pct,
            "take_profit_pct":     s.take_profit_pct,
            "stop_loss_pct":       s.stop_loss_pct,
            "max_open_trades":     s.max_open_trades,
            "max_capital_pct":     s.max_capital_pct,
            "max_hold_hours":      s.max_hold_hours,
            "min_coins_scanned":   s.min_coins_scanned,
            "min_analyst_conf":    s.min_analyst_conf,
            "cooldown_minutes":    s.cooldown_minutes,
            "scan_interval_min":   s.scan_interval_min,
            "report_interval_hrs": s.report_interval_hrs,
            "total_capital_usdt":  s.total_capital_usdt,
            "realized_pnl":        s.realized_pnl,
            "total_auto_trades":   s.total_auto_trades,
        }

    # ── DB wrappers (injected lazily to avoid circular imports) ────────────────

    async def _db_insert_position(self, pos: AutoPosition) -> None:
        try:
            from super_consensus.utils.super_db import insert_auto_position
            await insert_auto_position({
                "symbol":          pos.symbol,
                "qty":             pos.qty,
                "entry_price":     pos.entry_price,
                "entry_time":      pos.entry_time,
                "cost_usdt":       pos.cost_usdt,
                "take_profit_pct": pos.take_profit_pct,
                "stop_loss_pct":   pos.stop_loss_pct,
            })
        except Exception as exc:
            logger.error("DB insert_position failed: %s", exc)

    async def _db_delete_position(self, symbol: str) -> None:
        try:
            from super_consensus.utils.super_db import delete_auto_position
            await delete_auto_position(symbol)
        except Exception as exc:
            logger.error("DB delete_position failed: %s", exc)

    async def _db_record_trade(
        self, symbol, side, price, qty, cost, pnl, reason, conf
    ) -> None:
        try:
            from super_consensus.utils.super_db import record_auto_trade
            await record_auto_trade({
                "symbol":       symbol,
                "side":         side,
                "price":        price,
                "qty":          qty,
                "cost_usdt":    cost,
                "pnl":          pnl,
                "exit_reason":  reason,
                "analysts_conf": conf,
            })
        except Exception as exc:
            logger.error("DB record_auto_trade failed: %s", exc)

    async def _db_save_settings(self) -> None:
        try:
            from super_consensus.utils.super_db import upsert_auto_settings
            await upsert_auto_settings(self._settings_dict())
        except Exception as exc:
            logger.error("DB save_settings failed: %s", exc)
