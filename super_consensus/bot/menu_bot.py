"""
Interactive Menu Bot — Inline keyboard interface for all bot commands.

/menu  → main menu with inline buttons
Navigation uses edit_message (no new messages) except for scan results.
ConversationHandler captures free-text input for settings that need a value.
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from config.settings import ALLOWED_USER_IDS

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
(
    AWAIT_CUSTOM_AMOUNT,   # waiting for custom USDT amount in auto mode
    AWAIT_STOP_LOSS,       # waiting for stop-loss %
    AWAIT_TAKE_PROFIT,     # waiting for take-profit %
    AWAIT_MAX_TRADES,      # waiting for max open trades
    AWAIT_SCAN_INTERVAL,   # waiting for scan interval minutes
    AWAIT_TRADE_AMOUNT,    # waiting for fixed trade amount
    AWAIT_COIN_COUNT,      # waiting for coin count to scan
) = range(7)

# ── Auth ───────────────────────────────────────────────────────────────────────

def _authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_USER_IDS

async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")
    elif update.callback_query:
        await update.callback_query.answer("⛔ غير مصرح.", show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
# Keyboard builders
# ══════════════════════════════════════════════════════════════════════════════

def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 مسح السوق الآن",       callback_data="menu:scan")],
        [InlineKeyboardButton("🤖 تفعيل الوضع الآلي",    callback_data="menu:auto_mode")],
        [InlineKeyboardButton("⏹️ إيقاف الوضع الآلي",    callback_data="menu:auto_stop")],
        [InlineKeyboardButton("📊 حالة البوت والصفقات",  callback_data="menu:status")],
        [InlineKeyboardButton("⚙️ الإعدادات",            callback_data="menu:settings")],
        [InlineKeyboardButton("📈 تقرير الأرباح",        callback_data="menu:profit")],
    ])


def _kb_auto_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💵 50 USDT",   callback_data="auto:50"),
            InlineKeyboardButton("💵 100 USDT",  callback_data="auto:100"),
            InlineKeyboardButton("💵 200 USDT",  callback_data="auto:200"),
        ],
        [
            InlineKeyboardButton("💵 500 USDT",  callback_data="auto:500"),
            InlineKeyboardButton("💵 1000 USDT", callback_data="auto:1000"),
            InlineKeyboardButton("✏️ مبلغ مخصص", callback_data="auto:custom"),
        ],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu:back")],
    ])


def _kb_settings(auto_engine) -> InlineKeyboardMarkup:
    s = auto_engine.settings
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🛑 وقف الخسارة ({s.stop_loss_pct}%)",    callback_data="set:stop_loss"),
            InlineKeyboardButton(f"🎯 هدف الربح ({s.take_profit_pct}%)",    callback_data="set:take_profit"),
        ],
        [
            InlineKeyboardButton("💵 رأس المال/صفقة",                       callback_data="set:trade_amount"),
            InlineKeyboardButton("📊 عدد عملات المسح",                      callback_data="set:coin_count"),
        ],
        [
            InlineKeyboardButton(f"🔢 حد الصفقات ({s.max_open_trades})",    callback_data="set:max_trades"),
            InlineKeyboardButton(f"⏱️ فترة المسح ({s.scan_interval_min}د)", callback_data="set:scan_interval"),
        ],
        [InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="menu:back")],
    ])


def _kb_stop_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ نعم، أوقف الكل", callback_data="stop:confirm"),
        InlineKeyboardButton("❌ إلغاء",           callback_data="menu:back"),
    ]])


def _kb_profit() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 آخر 24 ساعة", callback_data="profit:24h"),
            InlineKeyboardButton("📅 آخر 7 أيام",  callback_data="profit:7d"),
            InlineKeyboardButton("📅 آخر 30 يوم",  callback_data="profit:30d"),
        ],
        [
            InlineKeyboardButton("💰 إجمالي الربح",   callback_data="profit:total"),
            InlineKeyboardButton("📊 تفاصيل الصفقات", callback_data="profit:details"),
        ],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu:back")],
    ])


def _kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="menu:back"),
    ]])


# ══════════════════════════════════════════════════════════════════════════════
# Helper: edit or send
# ══════════════════════════════════════════════════════════════════════════════

async def _edit(query, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit the existing message in place."""
    await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
# /menu command
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    auto = ctx.bot_data.get("auto_engine")
    status = "🟢 مفعّل" if auto and auto.is_active() else "🔴 موقوف"
    open_c = len(auto.open_positions()) if auto else 0
    text = (
        "🤖 *القائمة الرئيسية — SuperConsensus Bot*\n\n"
        f"الوضع الآلي: {status}\n"
        f"صفقات مفتوحة: `{open_c}`\n\n"
        "اختر من القائمة:"
    )
    await update.message.reply_text(text, reply_markup=_kb_main(), parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
# menu: callbacks
# ══════════════════════════════════════════════════════════════════════════════

async def _cb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    auto = ctx.bot_data.get("auto_engine")

    if action == "back":
        status = "🟢 مفعّل" if auto and auto.is_active() else "🔴 موقوف"
        open_c = len(auto.open_positions()) if auto else 0
        await _edit(query,
            "🤖 *القائمة الرئيسية — SuperConsensus Bot*\n\n"
            f"الوضع الآلي: {status}\n"
            f"صفقات مفتوحة: `{open_c}`\n\n"
            "اختر من القائمة:",
            _kb_main(),
        )
        return ConversationHandler.END

    if action == "auto_mode":
        active_str = "🟢 *الوضع الآلي مفعّل حالياً*\n\n" if auto and auto.is_active() else ""
        await _edit(query,
            f"{active_str}🤖 *تفعيل الوضع الآلي*\n\n"
            "اختر مبلغ كل صفقة بـ USDT:",
            _kb_auto_mode(),
        )
        return None

    if action == "auto_stop":
        if not auto or not auto.is_active():
            await query.answer("ℹ️ الوضع الآلي غير مفعّل أصلاً.", show_alert=True)
            return None
        await _edit(query,
            "⏹️ *إيقاف الوضع الآلي*\n\n"
            "هل أنت متأكد؟ الصفقات المفتوحة ستبقى حتى تُغلق يدوياً.",
            _kb_stop_confirm(),
        )
        return None

    if action == "status":
        await _show_status(query, ctx)
        return None

    if action == "settings":
        if not auto:
            await query.answer("⚠️ محرك التداول غير متاح.", show_alert=True)
            return None
        await _edit(query,
            "⚙️ *الإعدادات الحالية*\n\n"
            f"🛑 وقف الخسارة: `{auto.settings.stop_loss_pct}%`\n"
            f"🎯 هدف الربح: `{auto.settings.take_profit_pct}%`\n"
            f"🔢 حد الصفقات: `{auto.settings.max_open_trades}`\n"
            f"⏱️ فترة المسح: `{auto.settings.scan_interval_min}` دقيقة\n\n"
            "اضغط على الإعداد لتعديله:",
            _kb_settings(auto),
        )
        return None

    if action == "profit":
        await _edit(query,
            "📈 *تقرير الأرباح*\n\nاختر الفترة الزمنية:",
            _kb_profit(),
        )
        return None

    if action == "scan":
        await _do_scan(query, ctx)
        return None

    return None


# ══════════════════════════════════════════════════════════════════════════════
# auto: callbacks — fixed amounts + custom
# ══════════════════════════════════════════════════════════════════════════════

async def _cb_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    auto = ctx.bot_data.get("auto_engine")

    if action == "custom":
        await _edit(query,
            "✏️ *مبلغ مخصص*\n\nأرسل المبلغ بـ USDT (مثال: `150`):",
            _kb_back(),
        )
        return AWAIT_CUSTOM_AMOUNT

    # Fixed amount
    try:
        amount = float(action)
    except ValueError:
        await query.answer("قيمة غير صالحة.", show_alert=True)
        return None

    if auto:
        from super_consensus.utils.super_db import upsert_auto_settings
        await auto.enable(trade_amount_usdt=amount, db_save=upsert_auto_settings)

    s = auto.settings if auto else None
    await _edit(query,
        f"✅ *الوضع الآلي مُفعَّل*\n\n"
        f"💵 مبلغ كل صفقة: `{amount:.0f} USDT`\n"
        f"🎯 هدف الربح: `+{s.take_profit_pct if s else 3}%`\n"
        f"🛡️ وقف الخسارة: `-{s.stop_loss_pct if s else 2}%`\n"
        f"📊 حد الصفقات: `{s.max_open_trades if s else 2}`\n"
        f"⏱️ المسح كل: `{s.scan_interval_min if s else 60}` دقيقة\n\n"
        "البوت يمسح السوق الآن ويبحث عن فرص...",
        _kb_main(),
    )
    return ConversationHandler.END


async def _recv_custom_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    try:
        amount = float(text)
        assert amount > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ أدخل رقماً موجباً فقط (مثال: `150`)", parse_mode=ParseMode.MARKDOWN)
        return AWAIT_CUSTOM_AMOUNT

    auto = ctx.bot_data.get("auto_engine")
    if auto:
        from super_consensus.utils.super_db import upsert_auto_settings
        await auto.enable(trade_amount_usdt=amount, db_save=upsert_auto_settings)

    await update.message.reply_text(
        f"✅ *الوضع الآلي مُفعَّل*\n💵 مبلغ كل صفقة: `{amount:.2f} USDT`\n\n"
        "اكتب /menu للعودة للقائمة.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# stop: confirm callback
# ══════════════════════════════════════════════════════════════════════════════

async def _cb_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    auto = ctx.bot_data.get("auto_engine")

    if action == "confirm" and auto:
        from super_consensus.utils.super_db import upsert_auto_settings
        await auto.disable(db_save=upsert_auto_settings)
        open_c = len(auto.open_positions())
        await _edit(query,
            f"⏹️ *الوضع الآلي أُوقف*\n\n"
            f"📌 صفقات مفتوحة متبقية: `{open_c}`\n"
            "_(تُراقَب حتى تُغلق بـ TP/SL/timeout)_",
            _kb_main(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# set: settings callbacks + conversation receivers
# ══════════════════════════════════════════════════════════════════════════════

async def _cb_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    prompts = {
        "stop_loss":    ("AWAIT_STOP_LOSS",    AWAIT_STOP_LOSS,    "🛑 *وقف الخسارة*\n\nأدخل النسبة (0.1 - 50)%:"),
        "take_profit":  ("AWAIT_TAKE_PROFIT",  AWAIT_TAKE_PROFIT,  "🎯 *هدف الربح*\n\nأدخل النسبة (0.1 - 100)%:"),
        "max_trades":   ("AWAIT_MAX_TRADES",   AWAIT_MAX_TRADES,   "🔢 *حد الصفقات المفتوحة*\n\nأدخل العدد (1 - 10):"),
        "scan_interval":("AWAIT_SCAN_INTERVAL",AWAIT_SCAN_INTERVAL,"⏱️ *فترة المسح*\n\nأدخل الدقائق (5 - 1440):"),
        "trade_amount": ("AWAIT_TRADE_AMOUNT", AWAIT_TRADE_AMOUNT, "💵 *رأس المال لكل صفقة*\n\nأدخل المبلغ بـ USDT:"),
        "coin_count":   ("AWAIT_COIN_COUNT",   AWAIT_COIN_COUNT,   "📊 *عدد عملات المسح*\n\nأدخل العدد (10 - 200):"),
    }

    if action not in prompts:
        return None

    _, state, prompt = prompts[action]
    ctx.user_data["setting_action"] = action
    await _edit(query, prompt, _kb_back())
    return state


async def _recv_setting(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END

    action = ctx.user_data.get("setting_action", "")
    text   = (update.message.text or "").strip()
    auto   = ctx.bot_data.get("auto_engine")

    validators = {
        "stop_loss":     (float, 0.1, 50,   "stop_loss_pct",    "🛑 وقف الخسارة"),
        "take_profit":   (float, 0.1, 100,  "take_profit_pct",  "🎯 هدف الربح"),
        "max_trades":    (int,   1,   10,   "max_open_trades",  "🔢 حد الصفقات"),
        "scan_interval": (int,   5,   1440, "scan_interval_min","⏱️ فترة المسح"),
        "trade_amount":  (float, 1,   1e9,  "trade_amount_usdt","💵 رأس المال/صفقة"),
        "coin_count":    (int,   10,  200,  "min_coins_scanned","📊 عدد عملات المسح"),
    }

    if action not in validators:
        return ConversationHandler.END

    cast, lo, hi, attr, label = validators[action]
    try:
        val = cast(text)
        assert lo <= val <= hi
    except (ValueError, AssertionError):
        await update.message.reply_text(
            f"❌ قيمة غير صالحة. المدى المسموح: `{lo}` — `{hi}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return list(validators.keys()).index(action)  # stay in same state

    if auto:
        setattr(auto.settings, attr, val)
        from super_consensus.utils.super_db import upsert_auto_settings
        await upsert_auto_settings(auto._settings_dict())

    await update.message.reply_text(
        f"✅ تم تحديث *{label}* إلى `{val}`\n\nاكتب /menu للعودة للقائمة.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# profit: callbacks
# ══════════════════════════════════════════════════════════════════════════════

async def _cb_profit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    from super_consensus.utils.super_db import get_auto_trades_by_period, get_auto_trades

    if action == "24h":
        trades = await get_auto_trades_by_period(hours=24)
        period = "آخر 24 ساعة"
    elif action == "7d":
        trades = await get_auto_trades_by_period(hours=168)
        period = "آخر 7 أيام"
    elif action == "30d":
        trades = await get_auto_trades_by_period(hours=720)
        period = "آخر 30 يوم"
    elif action == "total":
        trades = await get_auto_trades(limit=1000)
        period = "إجمالي"
    elif action == "details":
        trades = await get_auto_trades(limit=15)
        period = "آخر 15 صفقة"
    else:
        return

    sells   = [t for t in trades if t["side"] == "sell"]
    total   = sum(float(t["pnl"]) for t in sells)
    wins    = [t for t in sells if float(t["pnl"]) > 0]
    losses  = [t for t in sells if float(t["pnl"]) <= 0]
    win_pct = (len(wins) / len(sells) * 100) if sells else 0

    lines = [
        f"📈 *تقرير الأرباح — {period}*\n",
        f"💰 إجمالي الربح/الخسارة: `{total:+.4f} USDT`",
        f"📊 صفقات مكتملة: `{len(sells)}`",
        f"✅ رابحة: `{len(wins)}` | ❌ خاسرة: `{len(losses)}`",
        f"🎯 نسبة النجاح: `{win_pct:.1f}%`",
    ]

    if action == "details" and sells:
        lines.append("\n*آخر الصفقات:*")
        for t in sells[:10]:
            pnl  = float(t["pnl"])
            icon = "✅" if pnl >= 0 else "❌"
            lines.append(
                f"{icon} `{t['symbol']}` {pnl:+.4f} USDT — {t['exit_reason']}"
            )

    await _edit(query, "\n".join(lines), _kb_profit())


# ══════════════════════════════════════════════════════════════════════════════
# Scan helper
# ══════════════════════════════════════════════════════════════════════════════

async def _do_scan(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from super_consensus.core.smart_scanner import SMART_SCANNER
    from super_consensus.bot.super_bot import _build_scan_report

    await _edit(query,
        "🔍 *الماسح الذكي يعمل...*\n\n"
        "🤖 يتم استشارة 3 نماذج AI بالتوازي\n"
        "⏳ _قد يستغرق 30-90 ثانية..._",
        _kb_back(),
    )
    try:
        result = await SMART_SCANNER.scan()
        report = _build_scan_report(result)
        # Send as new message (too long to edit) then restore menu
        await query.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)
        auto = ctx.bot_data.get("auto_engine")
        status = "🟢 مفعّل" if auto and auto.is_active() else "🔴 موقوف"
        open_c = len(auto.open_positions()) if auto else 0
        await query.message.reply_text(
            "🤖 *القائمة الرئيسية — SuperConsensus Bot*\n\n"
            f"الوضع الآلي: {status}\n"
            f"صفقات مفتوحة: `{open_c}`\n\n"
            "اختر من القائمة:",
            reply_markup=_kb_main(),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        await _edit(query, f"❌ خطأ في المسح: `{exc}`", _kb_back())


# ══════════════════════════════════════════════════════════════════════════════
# Status helper
# ══════════════════════════════════════════════════════════════════════════════

async def _show_status(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    auto   = ctx.bot_data.get("auto_engine")
    client = ctx.bot_data.get("client")

    if not auto:
        await _edit(query, "⚠️ محرك التداول غير متاح.", _kb_back())
        return

    s          = auto.settings
    positions  = auto.open_positions()
    status_str = "🟢 مفعّل" if auto.is_active() else "🔴 موقوف"
    mode_str   = (
        f"`{s.trade_amount_usdt:.0f} USDT` ثابت"
        if s.trade_amount_usdt is not None
        else f"`{s.risk_pct:.1f}%` من رأس المال"
        if s.risk_pct is not None
        else "غير محدد"
    )

    unrealized = 0.0
    pos_lines  = []
    for pos in positions:
        try:
            price = await client.get_current_price(pos.symbol)
            unr   = (price - pos.entry_price) * pos.qty
            unrealized += unr
            pct   = (price / pos.entry_price - 1) * 100
            pos_lines.append(
                f"  • `{pos.symbol}` {'+' if pct >= 0 else ''}{pct:.2f}%"
                f" ({'+' if unr >= 0 else ''}{unr:.4f} USDT)"
            )
        except Exception:
            pos_lines.append(f"  • `{pos.symbol}` — لا يمكن جلب السعر")

    pos_block = "\n".join(pos_lines) if pos_lines else "  لا توجد صفقات مفتوحة"

    text = (
        f"📊 *حالة البوت والصفقات*\n\n"
        f"الوضع الآلي: {status_str}\n"
        f"حجم الصفقة: {mode_str}\n"
        f"🎯 TP: `+{s.take_profit_pct}%` | 🛡️ SL: `-{s.stop_loss_pct}%`\n\n"
        f"📈 *الأداء:*\n"
        f"  صفقات منفذة: `{s.total_auto_trades}`\n"
        f"  ربح محقق: `{s.realized_pnl:+.4f} USDT`\n"
        f"  غير محقق: `{unrealized:+.4f} USDT`\n"
        f"  رأس المال: `{s.total_capital_usdt:.2f} USDT`\n\n"
        f"📌 *مراكز مفتوحة ({len(positions)}/{s.max_open_trades}):*\n{pos_block}"
    )
    await _edit(query, text, _kb_back())


# ══════════════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════════════

def register_menu_handlers(app: Application) -> None:
    """Register /menu command and all inline-keyboard handlers."""

    # States map for ConversationHandler
    setting_states = {
        AWAIT_STOP_LOSS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
        AWAIT_TAKE_PROFIT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
        AWAIT_MAX_TRADES:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
        AWAIT_SCAN_INTERVAL:[MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
        AWAIT_TRADE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
        AWAIT_COIN_COUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
        AWAIT_CUSTOM_AMOUNT:[MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_custom_amount)],
    }

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_cb_set,  pattern=r"^set:"),
            CallbackQueryHandler(_cb_auto, pattern=r"^auto:"),
        ],
        states=setting_states,
        fallbacks=[
            CommandHandler("menu", cmd_menu),
            CallbackQueryHandler(_cb_menu, pattern=r"^menu:"),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(_cb_menu,   pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(_cb_auto,   pattern=r"^auto:"))
    app.add_handler(CallbackQueryHandler(_cb_stop,   pattern=r"^stop:"))
    app.add_handler(CallbackQueryHandler(_cb_profit, pattern=r"^profit:"))

    logger.info("Menu handlers registered (/menu + inline keyboards)")

