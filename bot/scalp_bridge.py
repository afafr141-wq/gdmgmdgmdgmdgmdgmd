"""
Bridge v2 — Scalp Bot (menu-driven with inline keyboards).

كل التفاعل عبر أزرار — لا حاجة لكتابة أوامر نصية.

Commands:
  /scalp_menu   — فتح القائمة الرئيسية
  /scalp_status — اختصار: حالة العملات النشطة (زر سريع)

ConversationHandler states:
  WAIT_CAPITAL   — انتظار رقم المبلغ من المستخدم

Callback prefix: "scalp:"
Handler groups:  callbacks=6, conv=7
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import core.scalp_engine as engine
import core.scalp_scanner as scanner

log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_CAPITAL_USDT   = 20.0
DEFAULT_TIMEFRAME      = "5m"
AUTO_SCAN_INTERVAL_MIN = 30
MAX_AUTO_SYMBOLS       = 2

# ── Conversation states ────────────────────────────────────────────────────────
WAIT_CAPITAL = 1

# ── Runtime state ──────────────────────────────────────────────────────────────
_client    = None
_app_ref: dict = {}
_capital_usdt: float = DEFAULT_CAPITAL_USDT
_auto_enabled:  bool = False
_auto_task: Optional[asyncio.Task] = None
_paper_mode: bool = True   # True=وهمي | False=حقيقي


# ── Init ───────────────────────────────────────────────────────────────────────

def init_scalp(client) -> None:
    global _client
    _client = client
    engine.set_notifiers(entry=_notify_entry, exit_=_notify_exit, error=_notify_error)
    log.info("Scalp bridge v2 initialised")


def set_app(app: Application) -> None:
    _app_ref["app"] = app


async def _send(text: str) -> None:
    app = _app_ref.get("app")
    if not app:
        return
    from bot.telegram_bot import send_notification
    await send_notification(text, application=app)


# ── Keyboards ──────────────────────────────────────────────────────────────────

def _main_menu_kb() -> InlineKeyboardMarkup:
    auto_icon = "🟢 إيقاف التلقائي" if _auto_enabled else "🤖 تشغيل التلقائي"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 مسح السوق",       callback_data="scalp:scan"),
            InlineKeyboardButton("📊 الحالة",           callback_data="scalp:status"),
        ],
        [
            InlineKeyboardButton(auto_icon,              callback_data="scalp:toggle_auto"),
            InlineKeyboardButton("💼 ضبط المبلغ",        callback_data="scalp:set_capital"),
        ],
        [
            InlineKeyboardButton("⛔ إيقاف الكل",        callback_data="scalp:stop_all"),
            InlineKeyboardButton("🔄 تحديث القائمة",     callback_data="scalp:refresh_menu"),
        ],
        [
            InlineKeyboardButton(
                "💰 تفعيل الحقيقي 🔓" if _paper_mode else "📄 العودة للوهمي 🔒",
                callback_data="scalp:toggle_paper",
            ),
        ],
    ])


def _scan_result_kb(top_symbol: str | None) -> InlineKeyboardMarkup:
    rows = []
    if top_symbol:
        rows.append([InlineKeyboardButton(
            f"🚀 ابدأ {'وهمي' if _paper_mode else 'حقيقي'} على {top_symbol}",
            callback_data=f"scalp:autostart:{top_symbol}",
        )])
    rows += [
        [InlineKeyboardButton("🔄 مسح مجدداً",   callback_data="scalp:scan"),
         InlineKeyboardButton("🏠 القائمة",       callback_data="scalp:menu")],
    ]
    return InlineKeyboardMarkup(rows)


def _symbol_kb(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⛔ إيقاف",     callback_data=f"scalp:stop:{symbol}"),
        InlineKeyboardButton("📊 تفاصيل",   callback_data=f"scalp:detail:{symbol}"),
        InlineKeyboardButton("🏠 القائمة",  callback_data="scalp:menu"),
    ]])


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="scalp:menu"),
    ]])


# ── Menu text builder ──────────────────────────────────────────────────────────

def _menu_text() -> str:
    active   = engine.active_symbols()
    auto_txt = "🟢 نشط" if _auto_enabled else "🔴 متوقف"
    lines    = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🤖 *Scalp Bot — القائمة الرئيسية*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💼 مبلغ الصفقة:    `{_capital_usdt:.2f} USDT`",
        f"🤖 الوضع التلقائي: {auto_txt}",
        f"🔄 وضع التداول:    {'📄 وهمي' if _paper_mode else '💰 حقيقي ⚡'}",
        f"📊 عملات نشطة:     `{len(active)}`",
    ]
    if active:
        lines.append("")
        for sym in active:
            s = engine.status(sym)
            if not s:
                continue
            pos = "✅ صفقة مفتوحة" if s["in_position"] else "⏳ ينتظر"
            lines.append(f"  • `{sym}` — {pos} | ربح: `{s['realized_pnl']:+.4f}`")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ── Notification senders ───────────────────────────────────────────────────────

async def _notify_entry(
    *, symbol, price, qty, capital, rsi, adx, atr,
    tp_price, sl_price, supertrend, paper, **_
) -> None:
    mode   = "📄 وهمي" if paper else "💰 حقيقي"
    st_ico = "🟢 صاعد" if supertrend == 1 else "🔴 هابط"
    tp_pct = (tp_price - price) / price * 100
    sl_pct = (price - sl_price) / price * 100
    await _send(
        f"🟢 *دخول Scalp* — {mode}\n"
        f"─────────────────────────\n"
        f"🪙 `{symbol}` @ `{price:.6f}`\n"
        f"📦 الكمية: `{qty:.6f}` | 💼 `{capital:.2f} USDT`\n"
        f"─────────────────────────\n"
        f"🎯 TP: `{tp_price:.6f}` (+{tp_pct:.2f}%)\n"
        f"🛡️ SL: `{sl_price:.6f}` (-{sl_pct:.2f}%)\n"
        f"─────────────────────────\n"
        f"📊 RSI: `{rsi:.1f}` | ADX: `{adx:.1f}` | ST: {st_ico}"
    )


async def _notify_exit(
    *, symbol, price, pnl, reason, paper,
    total_pnl, trade_count, win_rate, **_
) -> None:
    mode   = "📄 وهمي" if paper else "💰 حقيقي"
    icon = "📈" if pnl >= 0 else "📉"
    await _send(
        f"{icon} *خروج Scalp* — {mode}\n"
        f"─────────────────────────\n"
        f"🪙 `{symbol}` @ `{price:.6f}`\n"
        f"💰 ربح/خسارة: `{pnl:+.4f} USDT`\n"
        f"📋 السبب: `{reason}`\n"
        f"─────────────────────────\n"
        f"🔢 صفقات: `{trade_count}` | 🏆 فوز: `{win_rate:.1f}%`\n"
        f"💹 إجمالي: `{total_pnl:+.4f} USDT`"
    )


async def _notify_error(*, symbol, error, **_) -> None:
    await _send(f"❌ *خطأ Scalp* — `{symbol}`\n`{error[:200]}`")


# ── Auto-scan loop ─────────────────────────────────────────────────────────────

async def _auto_scan_loop() -> None:
    log.info("Auto-scan loop started")
    await _send(
        f"🤖 *الوضع التلقائي نشط*\n"
        f"💼 مبلغ الصفقة: `{_capital_usdt:.2f} USDT`\n"
        f"⏱️ مسح كل `{AUTO_SCAN_INTERVAL_MIN}` دقيقة\n"
        ("📄 وضع وهمي — لا صفقات حقيقية" if _paper_mode else "💰 وضع حقيقي ⚡ — صفقات فعلية") + "\n"
        f"🔍 جاري أول مسح الآن…"
    )
    while _auto_enabled:
        try:
            result = await scanner.scan()
            active = engine.active_symbols()
            started = []

            for pick in result.final_picks:
                if pick.signal != "BUY":
                    continue
                if len(active) + len(started) >= MAX_AUTO_SYMBOLS:
                    break
                from bot.telegram_bot import _normalize_symbol
                symbol = _normalize_symbol(pick.symbol + "USDT")
                if symbol in active:
                    continue
                # تحقق أن الرمز موجود فعلاً على MEXC قبل الدخول
                try:
                    test_price = await _client.get_current_price(symbol)
                    if not test_price or test_price <= 0:
                        log.warning("Auto-scan: %s not available on MEXC — skipped", symbol)
                        continue
                except Exception as _sym_exc:
                    log.warning("Auto-scan: %s validation failed (%s) — skipped", symbol, _sym_exc)
                    continue
                try:
                    await engine.start(
                        _client, symbol,
                        capital_usdt=_capital_usdt,
                        timeframe=DEFAULT_TIMEFRAME,
                        paper=_paper_mode,
                    )
                    started.append(symbol)
                    _mode_tag = '📄 وهمي' if _paper_mode else '💰 حقيقي'
                    await _send(
                        f"🚀 *دخول تلقائي — {_mode_tag}*\n"
                        f"─────────────────────────\n"
                        f"🪙 `{symbol}` | 💼 `{_capital_usdt:.2f} USDT`\n"
                        f"🤖 `{pick.analysts[0] if pick.analysts else 'AI'}` | ثقة: `{pick.confidence}%`\n"
                        f"💬 _{pick.reason[:80]}_\n"
                        f"⏳ جاري المراقبة…"
                    )
                except Exception as exc:
                    log.error("Auto start error %s: %s", symbol, exc)

            if not started:
                log.info("Auto-scan: no new entries")

        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("Auto-scan error: %s", exc)
            await _send(f"⚠️ *خطأ مسح تلقائي:* `{str(exc)[:150]}`")

        try:
            await asyncio.sleep(AUTO_SCAN_INTERVAL_MIN * 60)
        except asyncio.CancelledError:
            return
    log.info("Auto-scan loop ended")


def _start_auto_loop() -> None:
    global _auto_task
    if _auto_task and not _auto_task.done():
        return
    _auto_task = asyncio.create_task(_auto_scan_loop())


def _stop_auto_loop() -> None:
    global _auto_task, _auto_enabled
    _auto_enabled = False
    if _auto_task and not _auto_task.done():
        _auto_task.cancel()
    _auto_task = None


# ── Scan report ────────────────────────────────────────────────────────────────

def _conf_bar(c: int) -> str:
    return "█" * round(c / 10) + "░" * (10 - round(c / 10))


def _safe(t: str) -> str:
    return t.replace("_", " ").replace("*", "").replace("`", "")


def _build_scan_report(result: scanner.ScanResult) -> str:
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines  = [
        "🔍 *Scalp Scanner — نتائج المسح*",
        f"📊 `{result.coins_scanned}` عملة | ⏱️ `{result.scan_duration_s:.0f}` ثانية",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for pick in result.final_picks:
        medal    = medals[pick.rank - 1] if pick.rank <= 5 else f"{pick.rank}."
        sig_icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(pick.signal, "⚪")
        analysts = " & ".join(pick.analysts) if pick.analysts else "—"
        lines += [
            f"{medal} *{_safe(pick.name)}* (`{pick.symbol}`)",
            f"  {sig_icon} `{pick.signal}` | {_conf_bar(pick.confidence)} `{pick.confidence}%`",
            f"  💬 _{_safe(pick.reason[:70])}_",
            f"  👥 `{analysts}`",
            "",
        ]
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ _تحليل AI فقط — ليس نصيحة مالية._",
    ]
    return "\n".join(lines)


def _status_text(s: dict) -> str:
    mode = "📄 وهمي" if s["paper"] else "💰 حقيقي"
    if s["in_position"]:
        pos = (
            f"✅ صفقة مفتوحة @ `{s['entry_price']:.6f}`\n"
            f"   🎯 `{s['tp_price']:.6f}` | 🛡️ `{s['sl_price']:.6f}` | 🔄 `{s['trailing_stop']:.6f}`"
        )
    else:
        b, mb = s.get("bars_since_exit", 99), engine.MIN_BARS_BETWEEN_TRADES
        pos   = f"⏳ Cooldown `{b}/{mb}`" if b < mb else "⏳ ينتظر إشارة"
    return (
        f"🪙 *{s['symbol']}* — {mode} `{s['timeframe']}`\n"
        f"{pos}\n"
        f"💹 `{s['realized_pnl']:+.4f} USDT` | 🔢 `{s['trade_count']}` صفقة | 🏆 `{s['win_rate_pct']:.1f}%`"
    )


# ── Command: /scalp_menu ───────────────────────────────────────────────────────

async def cmd_scalp_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.telegram_bot import _is_allowed, _deny
    if not _is_allowed(update):
        return await _deny(update)
    await update.message.reply_text(
        _menu_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_menu_kb(),
    )


async def cmd_scalp_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.telegram_bot import _is_allowed, _deny
    if not _is_allowed(update):
        return await _deny(update)
    symbols = engine.active_symbols()
    if not symbols:
        await update.message.reply_text(
            "ℹ️ لا توجد صفقات نشطة.",
            reply_markup=_back_kb(),
        )
        return
    lines = []
    for sym in symbols:
        s = engine.status(sym)
        if s:
            lines.append(_status_text(s))
            lines.append("")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_back_kb(),
    )


# ── ConversationHandler: ضبط المبلغ ───────────────────────────────────────────

async def _conv_set_capital_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """يُستدعى من زر 'ضبط المبلغ' — يطلب الرقم من المستخدم."""
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        f"💼 *ضبط مبلغ الصفقة*\n"
        f"─────────────────────────\n"
        f"المبلغ الحالي: `{_capital_usdt:.2f} USDT`\n\n"
        f"أرسل المبلغ الجديد بالـ USDT (مثال: `50`):\n"
        f"_(أرسل /cancel للإلغاء)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAIT_CAPITAL


async def _conv_receive_capital(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """يستقبل الرقم المُرسل من المستخدم."""
    global _capital_usdt
    text = (update.message.text or "").strip()
    try:
        amount = float(text)
        if amount < 1:
            await update.message.reply_text("❌ المبلغ يجب أن يكون أكبر من 1 USDT. أرسل رقماً آخر:")
            return WAIT_CAPITAL
        _capital_usdt = amount
        await update.message.reply_text(
            f"✅ *تم ضبط مبلغ الصفقة: `{_capital_usdt:.2f} USDT`*\n"
            f"سيُستخدم في كل الصفقات القادمة.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_back_kb(),
        )
    except ValueError:
        await update.message.reply_text("❌ أرسل رقماً فقط، مثال: `50`", parse_mode=ParseMode.MARKDOWN)
        return WAIT_CAPITAL
    return ConversationHandler.END


async def _conv_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ تم الإلغاء.", reply_markup=_back_kb())
    return ConversationHandler.END


# ── Callback handler ───────────────────────────────────────────────────────────

async def _scalp_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global _auto_enabled, _paper_mode
    from bot.telegram_bot import _is_allowed, _deny, _normalize_symbol
    q = update.callback_query
    await q.answer()
    if not _is_allowed(update):
        return await _deny(update)

    parts  = q.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    arg    = parts[2] if len(parts) > 2 else ""

    # ── القائمة الرئيسية ────────────────────────────────────────────────────
    if action in ("menu", "refresh_menu"):
        await q.message.edit_text(
            _menu_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_main_menu_kb(),
        )

    # ── مسح السوق ──────────────────────────────────────────────────────────
    elif action == "scan":
        await q.message.edit_text("🔍 جاري مسح السوق… قد يستغرق 30-60 ثانية.")
        try:
            result = await scanner.scan()
            report = _build_scan_report(result)
            if len(report) > 4000:
                report = report[:4000] + "\n…"
            await q.message.edit_text(
                report,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_scan_result_kb(result.top_symbol),
            )
        except Exception as exc:
            await q.message.edit_text(
                f"❌ خطأ في المسح: `{exc}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_back_kb(),
            )

    # ── الحالة ─────────────────────────────────────────────────────────────
    elif action == "status":
        symbols = engine.active_symbols()
        if not symbols:
            await q.message.edit_text(
                "ℹ️ لا توجد صفقات نشطة حالياً.",
                reply_markup=_back_kb(),
            )
            return
        lines = ["📊 *العملات النشطة*\n"]
        for sym in symbols:
            s = engine.status(sym)
            if s:
                lines.append(_status_text(s))
                lines.append("")
        await q.message.edit_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_back_kb(),
        )

    # ── تفاصيل عملة ────────────────────────────────────────────────────────
    elif action == "detail":
        s = engine.status(arg)
        if not s:
            await q.message.edit_text(f"ℹ️ `{arg}` غير نشط.", parse_mode=ParseMode.MARKDOWN, reply_markup=_back_kb())
            return
        await q.message.edit_text(
            _status_text(s),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_symbol_kb(arg),
        )

    # ── تشغيل/إيقاف التلقائي ───────────────────────────────────────────────
    elif action == "toggle_auto":
        if _auto_enabled:
            _stop_auto_loop()
            active = engine.active_symbols()
            await q.message.reply_text(
                f"⛔ *الوضع التلقائي أُوقف*\n"
                f"الصفقات المفتوحة: `{len(active)}`\n"
                f"_(تستمر حتى اكتمالها)_",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            if not _client:
                await q.message.reply_text("❌ الـ client غير جاهز.")
                return
            _auto_enabled = True
            _start_auto_loop()
            await q.message.reply_text(
                f"✅ *الوضع التلقائي شُغِّل*\n"
                f"💼 مبلغ الصفقة: `{_capital_usdt:.2f} USDT`\n"
                f"⏱️ مسح كل `{AUTO_SCAN_INTERVAL_MIN}` دقيقة\n"
                ("📄 وضع وهمي" if _paper_mode else "💰 وضع حقيقي ⚡") + "\n"
                f"🔍 جاري أول مسح الآن…",
                parse_mode=ParseMode.MARKDOWN,
            )
        # تحديث القائمة الرئيسية
        await q.message.edit_text(
            _menu_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_main_menu_kb(),
        )

    # ── إيقاف الكل ─────────────────────────────────────────────────────────
    elif action == "stop_all":
        symbols = engine.active_symbols()
        if not symbols:
            await q.message.reply_text("ℹ️ لا توجد صفقات نشطة.")
            return
        stopped_lines = []
        for sym in list(symbols):
            state = await engine.stop(sym)
            if state:
                wr = round(state.win_count / state.trade_count * 100, 1) if state.trade_count else 0
                stopped_lines.append(
                    f"⛔ `{sym}` | ربح: `{state.realized_pnl:+.4f}` | فوز: `{wr:.1f}%`"
                )
        await q.message.reply_text(
            f"⛔ *تم إيقاف كل الصفقات*\n"
            f"─────────────────────────\n"
            + "\n".join(stopped_lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_back_kb(),
        )
        await q.message.edit_text(
            _menu_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_main_menu_kb(),
        )

    # ── إيقاف عملة محددة ───────────────────────────────────────────────────
    elif action == "stop":
        state = await engine.stop(arg)
        if not state:
            await q.message.edit_text(f"ℹ️ `{arg}` لم يكن يعمل.", parse_mode=ParseMode.MARKDOWN, reply_markup=_back_kb())
            return
        wr = round(state.win_count / state.trade_count * 100, 1) if state.trade_count else 0
        await q.message.edit_text(
            f"⛔ *توقف* — `{arg}`\n"
            f"💹 ربح: `{state.realized_pnl:+.4f} USDT` | صفقات: `{state.trade_count}` | فوز: `{wr:.1f}%`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_back_kb(),
        )

    # ── ضبط المبلغ (callback يبدأ المحادثة) ────────────────────────────────
    elif action == "set_capital":
        # يُعالَج بواسطة ConversationHandler — هنا فقط تأكيد
        pass  # ConversationHandler يمسك هذا الـ callback

    # ── تبديل وضع التداول (وهمي ↔ حقيقي) ─────────────────────────────────────
    elif action == "toggle_paper":
        if _paper_mode:
            await q.message.edit_text(
                "⚠️ *تحذير — تفعيل التداول الحقيقي*\n\n"
                "سيتم تنفيذ صفقات *حقيقية* بأموال حقيقية بدلاً من الوهمي.\n"
                "─────────────────────────\n"
                "✅ تأكد أن مفاتيح MEXC API نشطة وتملك صلاحية Spot.\n"
                "✅ تأكد من وجود USDT كافٍ في حسابك.\n"
                f"✅ مبلغ كل صفقة: `{_capital_usdt:.2f} USDT`\n"
                "─────────────────────────\n"
                "⚡ هل تريد التحويل من *وهمي* إلى *حقيقي*؟",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ نعم، تداول حقيقي", callback_data="scalp:confirm_real")],
                    [InlineKeyboardButton("❌ لا، أبقِ وهمياً",  callback_data="scalp:menu")],
                ]),
            )
        else:
            _paper_mode = True
            await q.message.edit_text(
                "📄 *تم التحويل للوضع الوهمي*\n_لا توجد صفقات حقيقية._",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_main_menu_kb(),
            )

    # ── تأكيد الوضع الحقيقي ─────────────────────────────────────────────────
    elif action == "confirm_real":
        _paper_mode = False
        await q.message.edit_text(
            "💰 *تم تفعيل التداول الحقيقي* ⚡\n\n"
            "⚠️ الصفقات القادمة *حقيقية* بأموال حقيقية.\n"
            f"💼 مبلغ الصفقة: `{_capital_usdt:.2f} USDT`\n\n"
            "_للعودة للوهمي: افتح السكالبنج ← اضغط زر الوهمي_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_back_kb(),
        )

    # ── دخول تلقائي من نتيجة المسح ─────────────────────────────────────────
    elif action == "autostart":
        symbol = _normalize_symbol(arg + "USDT")
        if not _client:
            await q.message.reply_text("❌ الـ client غير جاهز.")
            return
        # تحقق أن الرمز موجود على MEXC
        try:
            _test = await _client.get_current_price(symbol)
            if not _test or _test <= 0:
                await q.message.reply_text(f"❌ الرمز `{symbol}` غير موجود على MEXC.", parse_mode=ParseMode.MARKDOWN, reply_markup=_back_kb())
                return
        except Exception as _ve:
            await q.message.reply_text(f"❌ `{symbol}` غير متاح: `{str(_ve)[:100]}`", parse_mode=ParseMode.MARKDOWN, reply_markup=_back_kb())
            return
        try:
            state = await engine.start(
                _client, symbol,
                capital_usdt=_capital_usdt,
                timeframe=DEFAULT_TIMEFRAME,
                paper=_paper_mode,
            )
            _mode_lbl = '📄 وهمي' if _paper_mode else '💰 حقيقي'
            await q.message.reply_text(
                f"✅ *Scalp بدأ — {_mode_lbl}*\n"
                f"🪙 `{symbol}` | 💼 `{_capital_usdt:.2f} USDT` | ⏱️ `{state.timeframe}`\n"
                f"📋 ستصلك إشعارات عند كل دخول وخروج.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_symbol_kb(symbol),
            )
        except ValueError as exc:
            await q.message.reply_text(f"⚠️ {exc}")
        except Exception as exc:
            await q.message.reply_text(f"❌ خطأ: `{exc}`", parse_mode=ParseMode.MARKDOWN)


# ── Registration ───────────────────────────────────────────────────────────────

def register_scalp_handlers(app: Application) -> None:
    # ConversationHandler لضبط المبلغ
    capital_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(_conv_set_capital_entry, pattern=r"^scalp:set_capital$")],
        states={
            WAIT_CAPITAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _conv_receive_capital),
            ],
        },
        fallbacks=[CommandHandler("cancel", _conv_cancel)],
        per_message=False,
    )

    app.add_handler(capital_conv,                                               group=7)
    app.add_handler(CommandHandler("scalp_menu",   cmd_scalp_menu),             group=6)
    app.add_handler(CommandHandler("scalp_status", cmd_scalp_status),           group=6)
    app.add_handler(CallbackQueryHandler(_scalp_callback, pattern=r"^scalp:"), group=6)
    log.info("Scalp v2 handlers (menu-driven) registered")
