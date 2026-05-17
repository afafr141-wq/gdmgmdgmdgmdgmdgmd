"""
Smart Scalp Bridge — قائمة تيليجرام لاستراتيجية السكالبينج السريع.

الميزات:
  - اختيار العملة من قائمة أو كتابتها يدوياً
  - اختيار التايم فريم: 1m / 3m / 5m / 15m
  - اختيار المبلغ من أزرار أو إدخال يدوي
  - تبديل Paper / Real
  - إشعارات دخول وخروج فورية

/quick_menu  — القائمة الرئيسية
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

import core.smart_scalp_engine as engine

log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

POPULAR_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "BNBUSDT", "ADAUSDT", "TRXUSDT",
]
TIMEFRAMES   = ["1m", "3m", "5m", "15m"]
AMOUNTS      = [10, 20, 50, 100]

# ── Conv states ────────────────────────────────────────────────────────────────
WAIT_SYMBOL, WAIT_AMOUNT = range(2)

# ── Runtime ────────────────────────────────────────────────────────────────────
_client                = None
_app_ref: dict         = {}
_paper_mode: bool      = True
_pending: dict         = {}   # chat_id → {"symbol", "timeframe", "amount"}


def init_smart_scalp(client) -> None:
    global _client
    _client = client
    engine.set_notifiers(entry=_notify_entry, exit_=_notify_exit, error=_notify_error)
    log.info("SmartScalp bridge initialised")


def set_app(app: Application) -> None:
    _app_ref["app"] = app


async def _send(text: str) -> None:
    app = _app_ref.get("app")
    if not app:
        return
    from bot.telegram_bot import send_notification
    await send_notification(text, application=app)


# ── Notifications ──────────────────────────────────────────────────────────────

async def _notify_entry(
    *, symbol, price, qty, capital, rsi, atr,
    tp_price, sl_price, tp_pct, sl_pct, paper, timeframe, **_
) -> None:
    mode = "📄 وهمي" if paper else "💰 حقيقي"
    await _send(
        f"🟢 *دخول SmartScalp* — {mode}\n"
        f"─────────────────────────\n"
        f"🪙 `{symbol}` @ `{price:.6f}` | ⏱️ `{timeframe}`\n"
        f"📦 الكمية: `{qty:.4f}` | 💼 `{capital:.2f} USDT`\n"
        f"─────────────────────────\n"
        f"🎯 TP: `{tp_price:.6f}` (+{tp_pct:.2f}%)\n"
        f"🛡️ SL: `{sl_price:.6f}` (-{sl_pct:.2f}%)\n"
        f"📊 RSI: `{rsi:.1f}` | ATR: `{atr:.6f}`"
    )


async def _notify_exit(
    *, symbol, price, pnl, reason, paper,
    total_pnl, trade_count, win_rate, **_
) -> None:
    mode = "📄 وهمي" if paper else "💰 حقيقي"
    icon = "📈" if pnl >= 0 else "📉"
    await _send(
        f"{icon} *خروج SmartScalp* — {mode}\n"
        f"─────────────────────────\n"
        f"🪙 `{symbol}` @ `{price:.6f}`\n"
        f"💰 ربح/خسارة: `{pnl:+.4f} USDT`\n"
        f"📋 السبب: `{reason}`\n"
        f"─────────────────────────\n"
        f"🔢 صفقات: `{trade_count}` | 🏆 فوز: `{win_rate:.1f}%`\n"
        f"💹 الإجمالي: `{total_pnl:+.4f} USDT`"
    )


async def _notify_error(*, symbol, error, **_) -> None:
    await _send(f"❌ *خطأ SmartScalp* — `{symbol}`\n`{error[:200]}`")


# ── Keyboards ──────────────────────────────────────────────────────────────────

def _main_kb() -> InlineKeyboardMarkup:
    active = engine.active_symbols()
    paper_lbl = "💰 تفعيل حقيقي 🔓" if _paper_mode else "📄 العودة لوهمي 🔒"
    rows = [
        [InlineKeyboardButton("🚀 صفقة جديدة",      callback_data="ss:new"),
         InlineKeyboardButton("📊 الحالة",           callback_data="ss:status")],
        [InlineKeyboardButton("⛔ إيقاف الكل",       callback_data="ss:stop_all"),
         InlineKeyboardButton("🔄 تحديث",            callback_data="ss:menu")],
        [InlineKeyboardButton(paper_lbl,              callback_data="ss:toggle_paper")],
    ]
    return InlineKeyboardMarkup(rows)


def _symbol_kb() -> InlineKeyboardMarkup:
    rows = []
    row  = []
    for sym in POPULAR_SYMBOLS:
        row.append(InlineKeyboardButton(sym.replace("USDT", ""), callback_data=f"ss:sym:{sym}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ اكتب رمز يدوياً", callback_data="ss:sym:manual")])
    rows.append([InlineKeyboardButton("🏠 رجوع",             callback_data="ss:menu")])
    return InlineKeyboardMarkup(rows)


def _tf_kb(symbol: str) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(tf, callback_data=f"ss:tf:{symbol}:{tf}")
        for tf in TIMEFRAMES
    ]]
    rows.append([InlineKeyboardButton("🏠 رجوع", callback_data="ss:new")])
    return InlineKeyboardMarkup(rows)


def _amount_kb(symbol: str, tf: str) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(f"{a} USDT", callback_data=f"ss:amt:{symbol}:{tf}:{a}")
        for a in AMOUNTS
    ]]
    rows.append([InlineKeyboardButton("✏️ مبلغ يدوي", callback_data=f"ss:amt:{symbol}:{tf}:manual")])
    rows.append([InlineKeyboardButton("🏠 رجوع",       callback_data=f"ss:tf_back:{symbol}")])
    return InlineKeyboardMarkup(rows)


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="ss:menu")
    ]])


def _stop_kb(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⛔ إيقاف {symbol}", callback_data=f"ss:stop:{symbol}"),
        InlineKeyboardButton("🏠 القائمة",          callback_data="ss:menu"),
    ]])


# ── Menu text ──────────────────────────────────────────────────────────────────

def _menu_text() -> str:
    active = engine.active_symbols()
    lines  = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚡ *Smart Scalp — القائمة الرئيسية*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🔄 الوضع:  {'📄 وهمي' if _paper_mode else '💰 حقيقي ⚡'}",
        f"📊 نشطة:   `{len(active)}` عملة",
    ]
    for sym in active:
        s = status_line(sym)
        if s:
            lines.append(s)
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def status_line(sym: str) -> str:
    s = engine.status(sym)
    if not s:
        return ""
    pos = "✅ مفتوحة" if s["in_position"] else "⏳ تنتظر"
    return f"  • `{sym}` [{s['timeframe']}] {pos} | `{s['realized_pnl']:+.4f}`"


# ── Commands ───────────────────────────────────────────────────────────────────

async def cmd_quick_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.telegram_bot import _is_allowed, _deny
    if not _is_allowed(update):
        return await _deny(update)
    await update.message.reply_text(
        _menu_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=_main_kb()
    )


# ── Conv: مبلغ يدوي ────────────────────────────────────────────────────────────

async def _conv_manual_symbol_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    _pending[chat_id] = {}
    await q.message.reply_text(
        "✏️ اكتب رمز العملة (مثال: `BTCUSDT` أو `ETHUSDT`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAIT_SYMBOL


async def _conv_receive_symbol(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.message.chat_id
    symbol  = update.message.text.strip().upper().replace("/", "").replace("-", "")
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    _pending[chat_id] = {"symbol": symbol}
    await update.message.reply_text(
        f"✅ العملة: `{symbol}`\n\nاختر التايم فريم:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_tf_kb(symbol),
    )
    return ConversationHandler.END


async def _conv_manual_amount_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    symbol, tf = parts[2], parts[3]
    chat_id = q.message.chat_id
    _pending[chat_id] = {"symbol": symbol, "tf": tf}
    await q.message.reply_text(
        f"✏️ اكتب المبلغ بالـ USDT (مثال: `25`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAIT_AMOUNT


async def _conv_receive_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    global _paper_mode
    chat_id = update.message.chat_id
    data    = _pending.pop(chat_id, {})
    symbol  = data.get("symbol", "BTCUSDT")
    tf      = data.get("tf", "5m")
    try:
        amount = float(update.message.text.strip())
        if amount < 1:
            await update.message.reply_text("❌ المبلغ يجب أن يكون أكبر من 1 USDT.")
            return WAIT_AMOUNT
    except ValueError:
        await update.message.reply_text("❌ أرسل رقماً فقط، مثال: `25`", parse_mode=ParseMode.MARKDOWN)
        return WAIT_AMOUNT

    await _launch(update.message, symbol, tf, amount)
    return ConversationHandler.END


async def _conv_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ تم الإلغاء.", reply_markup=_back_kb())
    return ConversationHandler.END


# ── Launch helper ──────────────────────────────────────────────────────────────

async def _launch(msg_obj, symbol: str, tf: str, amount: float) -> None:
    mode_lbl = "📄 وهمي" if _paper_mode else "💰 حقيقي ⚡"
    try:
        price = await _client.get_current_price(symbol)
        if not price or price <= 0:
            await msg_obj.reply_text(f"❌ `{symbol}` غير متاح على MEXC.", parse_mode=ParseMode.MARKDOWN)
            return
    except Exception as exc:
        await msg_obj.reply_text(f"❌ خطأ في التحقق من `{symbol}`: `{str(exc)[:80]}`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        await engine.start(_client, symbol, capital_usdt=amount, timeframe=tf, paper=_paper_mode)
        await msg_obj.reply_text(
            f"✅ *SmartScalp بدأ — {mode_lbl}*\n"
            f"─────────────────────────\n"
            f"🪙 `{symbol}` | ⏱️ `{tf}` | 💼 `{amount:.2f} USDT`\n"
            f"📡 السعر الحالي: `{price:.6f}`\n"
            f"📊 الاستراتيجية: EMA9/21 + RSI + شمعة خضرا\n"
            f"ستصلك إشعارات عند كل دخول وخروج.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_stop_kb(symbol),
        )
    except ValueError as exc:
        await msg_obj.reply_text(f"⚠️ {exc}", reply_markup=_back_kb())
    except Exception as exc:
        await msg_obj.reply_text(f"❌ خطأ: `{exc}`", parse_mode=ParseMode.MARKDOWN)


# ── Callback handler ───────────────────────────────────────────────────────────

async def _callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global _paper_mode
    from bot.telegram_bot import _is_allowed, _deny
    q = update.callback_query
    await q.answer()
    if not _is_allowed(update):
        return await _deny(update)

    parts  = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action in ("menu", "refresh"):
        await q.message.edit_text(_menu_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=_main_kb())

    elif action == "toggle_paper":
        _paper_mode = not _paper_mode
        await q.message.edit_text(_menu_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=_main_kb())

    elif action == "new":
        await q.message.edit_text(
            "🪙 *اختر العملة:*", parse_mode=ParseMode.MARKDOWN, reply_markup=_symbol_kb()
        )

    elif action == "sym":
        symbol = parts[2] if len(parts) > 2 else ""
        if symbol == "manual":
            await q.message.reply_text(
                "✏️ اكتب رمز العملة (مثال: `BTCUSDT`):", parse_mode=ParseMode.MARKDOWN
            )
        else:
            await q.message.edit_text(
                f"🪙 `{symbol}`\n\n⏱️ *اختر التايم فريم:*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_tf_kb(symbol),
            )

    elif action == "tf":
        symbol, tf = parts[2], parts[3]
        await q.message.edit_text(
            f"🪙 `{symbol}` | ⏱️ `{tf}`\n\n💼 *اختر مبلغ الصفقة:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_amount_kb(symbol, tf),
        )

    elif action == "tf_back":
        symbol = parts[2]
        await q.message.edit_text(
            f"🪙 `{symbol}`\n\n⏱️ *اختر التايم فريم:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_tf_kb(symbol),
        )

    elif action == "amt":
        symbol, tf, amt_str = parts[2], parts[3], parts[4]
        if amt_str == "manual":
            _pending[q.message.chat_id] = {"symbol": symbol, "tf": tf}
            await q.message.reply_text(
                "✏️ اكتب المبلغ بالـ USDT (مثال: `25`):", parse_mode=ParseMode.MARKDOWN
            )
        else:
            await _launch(q.message, symbol, tf, float(amt_str))

    elif action == "stop":
        symbol = parts[2]
        engine.stop(symbol)
        await q.message.edit_text(
            f"⛔ *تم إيقاف `{symbol}`*", parse_mode=ParseMode.MARKDOWN, reply_markup=_back_kb()
        )

    elif action == "stop_all":
        for sym in engine.active_symbols():
            engine.stop(sym)
        await q.message.edit_text(
            "⛔ *تم إيقاف جميع الصفقات.*", parse_mode=ParseMode.MARKDOWN, reply_markup=_back_kb()
        )

    elif action == "status":
        active = engine.active_symbols()
        if not active:
            await q.message.edit_text(
                "ℹ️ لا توجد صفقات نشطة.", reply_markup=_back_kb()
            )
            return
        lines = []
        for sym in active:
            s = engine.status(sym)
            if not s:
                continue
            mode = "📄" if s["paper"] else "💰"
            pos  = "✅ مفتوحة" if s["in_position"] else "⏳ تنتظر"
            lines.append(
                f"🪙 *{sym}* {mode} `{s['timeframe']}`\n"
                f"  {pos} | ربح: `{s['realized_pnl']:+.4f}` | صفقات: `{s['trade_count']}`"
            )
        await q.message.edit_text(
            "\n\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=_back_kb()
        )


# ── Registration ───────────────────────────────────────────────────────────────

def register_smart_scalp_handlers(app: Application) -> None:
    symbol_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(_conv_manual_symbol_entry, pattern=r"^ss:sym:manual$")],
        states={WAIT_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, _conv_receive_symbol)]},
        fallbacks=[CommandHandler("cancel", _conv_cancel)],
        per_message=False,
    )
    amount_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(_conv_manual_amount_entry, pattern=r"^ss:amt:[^:]+:[^:]+:manual$")],
        states={WAIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _conv_receive_amount)]},
        fallbacks=[CommandHandler("cancel", _conv_cancel)],
        per_message=False,
    )

    app.add_handler(symbol_conv,                                                   group=8)
    app.add_handler(amount_conv,                                                   group=8)
    app.add_handler(CommandHandler("quick_menu",   cmd_quick_menu),                group=8)
    app.add_handler(CallbackQueryHandler(_callback, pattern=r"^ss:"),              group=8)
    log.info("SmartScalp handlers registered — /quick_menu")
