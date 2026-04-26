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
    AWAIT_CUSTOM_AMOUNT,    # custom USDT amount for auto mode
    AWAIT_STOP_LOSS,        # stop-loss %
    AWAIT_TAKE_PROFIT,      # take-profit %
    AWAIT_MAX_TRADES,       # max open trades
    AWAIT_SCAN_INTERVAL,    # scan interval minutes
    AWAIT_TRADE_AMOUNT,     # fixed trade amount
    AWAIT_COIN_COUNT,       # coin count to scan
    AWAIT_GRID_PAIR,        # grid bot: trading pair
    AWAIT_GRID_AMOUNT,      # grid bot: investment amount
    AWAIT_SCAN_AUTO_MIN,    # super consensus: auto-scan interval minutes
) = range(10)

# Popular pairs for quick selection
POPULAR_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
                 "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "MATIC/USDT"]

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
        [InlineKeyboardButton("🤖 شبكة AI الذكية (Grid Bot)",     callback_data="menu:grid")],
        [InlineKeyboardButton("🧠 إجماع الخارق (SuperConsensus)", callback_data="menu:super")],
        [InlineKeyboardButton("⚡ تداول آلي كامل (Auto-Trade)",   callback_data="menu:auto_mode")],
        [InlineKeyboardButton("📊 حالة البوت والصفقات (Status)",  callback_data="menu:status")],
        [InlineKeyboardButton("⚙️ الإعدادات (Settings)",          callback_data="menu:settings")],
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
        [InlineKeyboardButton(f"🛑 Stop Loss ({s.stop_loss_pct}%)",          callback_data="set:stop_loss")],
        [InlineKeyboardButton(f"🎯 Take Profit ({s.take_profit_pct}%)",      callback_data="set:take_profit")],
        [InlineKeyboardButton(f"🔢 الحد الأقصى للصفقات ({s.max_open_trades})", callback_data="set:max_trades")],
        [InlineKeyboardButton(f"⏱️ فترة مسح السوق ({s.scan_interval_min} دقيقة)", callback_data="set:scan_interval")],
        [InlineKeyboardButton("🔙 رجوع",                                     callback_data="menu:back")],
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


def _kb_grid_pairs() -> InlineKeyboardMarkup:
    """Quick pair selection for Grid Bot."""
    rows = []
    for i in range(0, len(POPULAR_PAIRS), 2):
        row = []
        for pair in POPULAR_PAIRS[i:i+2]:
            base = pair.replace("/USDT", "")
            row.append(InlineKeyboardButton(base, callback_data=f"grid:{pair}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ زوج مخصص", callback_data="grid:custom")])
    rows.append([InlineKeyboardButton("🔙 رجوع",      callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


def _kb_grid_risk() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 منخفض",  callback_data="gridrisk:low"),
            InlineKeyboardButton("🟡 متوسط",  callback_data="gridrisk:medium"),
            InlineKeyboardButton("🔴 مرتفع",  callback_data="gridrisk:high"),
        ],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu:grid")],
    ])


def _kb_grid_menu(has_last: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🚀 تشغيل شبكة جديدة",  callback_data="grid:new")],
    ]
    if has_last:
        rows.append([InlineKeyboardButton("⚡ تشغيل بآخر إعدادات", callback_data="grid:last")])
    rows.append([InlineKeyboardButton("🛑 إيقاف شبكة",            callback_data="grid:stop_menu")])
    rows.append([InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


def _kb_super_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 مسح السوق الآن (Scan Now)",      callback_data="super:scan_now")],
        [InlineKeyboardButton("⏱️ مسح تلقائي كل ساعة",             callback_data="super:scan_auto")],
        [InlineKeyboardButton("🛑 إيقاف المسح التلقائي",            callback_data="super:stop_scan")],
        [InlineKeyboardButton("📋 آخر تقرير",                       callback_data="super:last_report")],
        [InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية",           callback_data="menu:back")],
    ])


def _kb_auto_trade_menu(auto_engine) -> InlineKeyboardMarkup:
    """Auto-Trade menu with current TP/SL shown on buttons."""
    s = auto_engine.settings if auto_engine else None
    tp  = s.take_profit_pct if s else 3.0
    sl  = s.stop_loss_pct   if s else 2.0
    active = auto_engine and auto_engine.is_active()
    rows = [
        [
            InlineKeyboardButton("💵 50",   callback_data="auto:50"),
            InlineKeyboardButton("💵 100",  callback_data="auto:100"),
            InlineKeyboardButton("💵 200",  callback_data="auto:200"),
        ],
        [
            InlineKeyboardButton("💵 500",  callback_data="auto:500"),
            InlineKeyboardButton("💵 1000", callback_data="auto:1000"),
            InlineKeyboardButton("✏️ مخصص", callback_data="auto:custom"),
        ],
        [
            InlineKeyboardButton(f"🎯 TP: {tp}%",  callback_data="set:take_profit"),
            InlineKeyboardButton(f"🛑 SL: {sl}%",  callback_data="set:stop_loss"),
        ],
    ]
    if active:
        rows.append([InlineKeyboardButton("⏹️ إيقاف التداول الآلي", callback_data="menu:auto_stop")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


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

    if action == "grid":
        has_last = bool(ctx.user_data.get("grid_last_pair"))
        last_info = ""
        if has_last:
            lp = ctx.user_data.get("grid_last_pair", "")
            la = ctx.user_data.get("grid_last_amount", 0)
            lr = ctx.user_data.get("grid_last_risk", "medium")
            last_info = f"\n⚡ آخر إعدادات: `{lp}` | `{la} USDT` | `{lr}`"
        await _edit(query,
            f"🤖 *شبكة AI الذكية — Grid Bot*{last_info}\n\n"
            "اختر ما تريد:",
            _kb_grid_menu(has_last=has_last),
        )
        return None

    if action == "super":
        await _edit(query,
            "🧠 *إجماع الخارق — SuperConsensus*\n\n"
            "يحلل السوق بـ 3 نماذج AI ويجد أفضل الفرص.\n\n"
            "اختر وضع التشغيل:",
            _kb_super_menu(),
        )
        return None

    if action == "auto_mode":
        active_str = "🟢 *الوضع الآلي مفعّل حالياً*\n\n" if auto and auto.is_active() else ""
        s = auto.settings if auto else None
        tp = s.take_profit_pct if s else 3.0
        sl = s.stop_loss_pct   if s else 2.0
        await _edit(query,
            f"{active_str}⚡ *تداول آلي كامل — Auto-Trade*\n\n"
            f"🎯 هدف الربح: `+{tp}%` | 🛑 وقف الخسارة: `-{sl}%`\n\n"
            "اختر مبلغ كل صفقة بـ USDT:",
            _kb_auto_trade_menu(auto),
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
        s = auto.settings
        await _edit(query,
            "⚙️ *الإعدادات*\n\n"
            f"🛑 Stop Loss: `{s.stop_loss_pct}%`\n"
            f"🎯 Take Profit: `{s.take_profit_pct}%`\n"
            f"🔢 الحد الأقصى للصفقات: `{s.max_open_trades}`\n"
            f"⏱️ فترة مسح السوق: `{s.scan_interval_min}` دقيقة\n\n"
            "اضغط على أي إعداد لتعديله:",
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
        f"✅ *التداول الآلي مُفعَّل*\n\n"
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
# grid: callbacks — Grid Bot flow
# ══════════════════════════════════════════════════════════════════════════════

async def _cb_grid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "new":
        await _edit(query,
            "🤖 *شبكة جديدة — اختر الزوج*\n\n"
            "اضغط على زوج أو اختر 'مخصص' لإدخال أي زوج:",
            _kb_grid_pairs(),
        )
        return None

    if action == "last":
        pair   = ctx.user_data.get("grid_last_pair")
        amount = ctx.user_data.get("grid_last_amount")
        risk   = ctx.user_data.get("grid_last_risk", "medium")
        if not pair or not amount:
            await query.answer("لا توجد إعدادات سابقة.", show_alert=True)
            return None
        await _launch_grid(query, ctx, pair, amount, risk)
        return None

    if action == "custom":
        await _edit(query,
            "✏️ *زوج مخصص*\n\nأرسل رمز الزوج (مثال: `SOLUSDT` أو `SOL/USDT`):",
            _kb_back(),
        )
        ctx.user_data["grid_step"] = "pair"
        return AWAIT_GRID_PAIR

    if action == "stop_menu":
        engine = ctx.bot_data.get("engine")
        if not engine:
            await query.answer("محرك الشبكة غير متاح.", show_alert=True)
            return None
        symbols = engine.active_symbols() if engine else []
        if not symbols:
            await query.answer("لا توجد شبكات نشطة.", show_alert=True)
            return None
        rows = [[InlineKeyboardButton(f"🛑 {s}", callback_data=f"gridstop:{s}")] for s in symbols]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:grid")])
        await _edit(query, "🛑 *اختر الشبكة للإيقاف:*", InlineKeyboardMarkup(rows))
        return None

    # Pair selected from quick list (e.g. "BTC/USDT")
    if "/" in action or action.endswith("USDT"):
        pair = action if "/" in action else action.replace("USDT", "/USDT")
        ctx.user_data["grid_pending_pair"] = pair
        await _edit(query,
            f"💵 *المبلغ — {pair}*\n\nأرسل مبلغ الاستثمار بـ USDT (مثال: `100`):",
            _kb_back(),
        )
        ctx.user_data["grid_step"] = "amount"
        return AWAIT_GRID_AMOUNT

    return None


async def _cb_gridrisk(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Risk level selected — launch the grid."""
    query = update.callback_query
    await query.answer()
    risk = query.data.split(":")[1]
    pair   = ctx.user_data.get("grid_pending_pair", "")
    amount = ctx.user_data.get("grid_pending_amount", 0)
    if not pair or not amount:
        await query.answer("بيانات ناقصة، ابدأ من جديد.", show_alert=True)
        return
    await _launch_grid(query, ctx, pair, amount, risk)


async def _cb_gridstop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop a specific grid."""
    query = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]
    engine = ctx.bot_data.get("engine")
    if not engine:
        await query.answer("محرك الشبكة غير متاح.", show_alert=True)
        return
    try:
        pnl = await engine.stop(symbol, market_sell=True)
        pnl_str = f"`{pnl:+.2f} USDT`" if pnl is not None else "غير محدد"
        await _edit(query,
            f"🛑 *تم إيقاف شبكة `{symbol}`*\n\n💰 الربح/الخسارة: {pnl_str}",
            _kb_main(),
        )
    except Exception as exc:
        await _edit(query, f"❌ خطأ في الإيقاف: `{exc}`", _kb_back())


async def _launch_grid(query, ctx, pair: str, amount: float, risk: str) -> None:
    """Execute grid start and show result."""
    engine = ctx.bot_data.get("engine")
    if not engine:
        await _edit(query, "❌ محرك الشبكة غير متاح.", _kb_back())
        return
    risk_labels = {"low": "🟢 منخفض", "medium": "🟡 متوسط", "high": "🔴 مرتفع"}
    try:
        await _edit(query, f"⏳ جاري تشغيل شبكة `{pair}`...", _kb_back())
        await engine.start(symbol=pair, total_investment=amount, risk=risk)
        # Save as last settings
        ctx.user_data["grid_last_pair"]   = pair
        ctx.user_data["grid_last_amount"] = amount
        ctx.user_data["grid_last_risk"]   = risk
        await _edit(query,
            f"✅ *شبكة AI مُشغَّلة*\n\n"
            f"🪙 الزوج: `{pair}`\n"
            f"💵 الاستثمار: `{amount:.0f} USDT`\n"
            f"⚖️ المخاطرة: {risk_labels.get(risk, risk)}\n\n"
            "البوت يعمل الآن ويضع أوامر الشراء والبيع تلقائياً.",
            _kb_main(),
        )
    except Exception as exc:
        await _edit(query, f"❌ فشل تشغيل الشبكة: `{exc}`", _kb_back())


async def _recv_grid_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    raw  = (update.message.text or "").strip().upper()
    pair = raw if "/" in raw else raw.replace("USDT", "/USDT")
    if not pair.endswith("/USDT"):
        pair += "/USDT"
    ctx.user_data["grid_pending_pair"] = pair
    ctx.user_data["grid_step"] = "amount"
    await update.message.reply_text(
        f"💵 *المبلغ — {pair}*\n\nأرسل مبلغ الاستثمار بـ USDT (مثال: `100`):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return AWAIT_GRID_AMOUNT


async def _recv_grid_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    try:
        amount = float(text)
        assert amount > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ أدخل رقماً موجباً (مثال: `100`)", parse_mode=ParseMode.MARKDOWN)
        return AWAIT_GRID_AMOUNT

    ctx.user_data["grid_pending_amount"] = amount
    pair = ctx.user_data.get("grid_pending_pair", "")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 منخفض",  callback_data="gridrisk:low"),
        InlineKeyboardButton("🟡 متوسط",  callback_data="gridrisk:medium"),
        InlineKeyboardButton("🔴 مرتفع",  callback_data="gridrisk:high"),
    ]])
    await update.message.reply_text(
        f"⚖️ *مستوى المخاطرة — {pair} | {amount:.0f} USDT*\n\nاختر مستوى المخاطرة:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# super: callbacks — SuperConsensus flow
# ══════════════════════════════════════════════════════════════════════════════

async def _cb_super(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "scan_now":
        await _do_scan(query, ctx)
        return None

    if action == "scan_auto":
        # تفعيل المسح التلقائي كل 60 دقيقة مباشرة
        auto = ctx.bot_data.get("auto_engine")
        if auto:
            auto.settings.scan_interval_min = 60
            try:
                from super_consensus.utils.super_db import upsert_auto_settings
                await upsert_auto_settings(auto._settings_dict())
            except Exception:
                pass
        await _edit(query,
            "✅ *المسح التلقائي مُفعَّل*\n\n"
            "⏱️ سيتم مسح السوق كل `60` دقيقة تلقائياً.\n\n"
            "اضغط '🛑 إيقاف المسح التلقائي' لإيقافه.",
            _kb_super_menu(),
        )
        return None

    if action == "stop_scan":
        auto = ctx.bot_data.get("auto_engine")
        if auto:
            # تعيين الفترة إلى 0 لإيقاف المسح التلقائي
            auto.settings.scan_interval_min = 0
            try:
                from super_consensus.utils.super_db import upsert_auto_settings
                await upsert_auto_settings(auto._settings_dict())
            except Exception:
                pass
        await _edit(query,
            "🛑 *تم إيقاف المسح التلقائي*\n\n"
            "يمكنك تشغيل مسح يدوي في أي وقت.",
            _kb_super_menu(),
        )
        return None

    if action == "last_report":
        last = ctx.bot_data.get("last_scan_report")
        if not last:
            await query.answer("لا يوجد تقرير مسح سابق بعد. اضغط 'مسح السوق الآن' أولاً.", show_alert=True)
            return None
        # Send as new message (may be long)
        await query.message.reply_text(last, parse_mode=ParseMode.MARKDOWN)
        return None

    return None


async def _recv_scan_auto_min(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    try:
        minutes = int(text)
        assert 5 <= minutes <= 1440
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ أدخل عدد دقائق بين 5 و 1440.", parse_mode=ParseMode.MARKDOWN)
        return AWAIT_SCAN_AUTO_MIN

    auto = ctx.bot_data.get("auto_engine")
    if auto:
        auto.settings.scan_interval_min = minutes
        from super_consensus.utils.super_db import upsert_auto_settings
        await upsert_auto_settings(auto._settings_dict())

    await update.message.reply_text(
        f"✅ *المسح التلقائي مُفعَّل*\n\n"
        f"⏱️ سيتم مسح السوق كل `{minutes}` دقيقة\n\n"
        "اكتب /menu للعودة للقائمة.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


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
        # Cache for "آخر تقرير" button
        ctx.bot_data["last_scan_report"] = report
        # Send as new message (too long to edit) then restore menu
        await query.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)
        auto   = ctx.bot_data.get("auto_engine")
        status = "🟢 مفعّل" if auto and auto.is_active() else "🔴 موقوف"
        open_c = len(auto.open_positions()) if auto else 0
        await query.message.reply_text(
            f"🤖 *القائمة الرئيسية*\n\nالوضع الآلي: {status} | صفقات: `{open_c}`\n\nاختر:",
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
    engine = ctx.bot_data.get("engine")
    client = ctx.bot_data.get("client")

    lines = ["📊 *حالة البوت والصفقات*\n", "━━━━━━━━━━━━━━━━━━━━"]

    # ── قسم 1: شبكات Grid Bot ─────────────────────────────────────────────────
    lines.append("\n🤖 *شبكة AI الذكية (Grid Bot):*")
    if engine:
        active_symbols = engine.active_symbols()
        if active_symbols:
            lines.append(f"  شبكات نشطة: `{len(active_symbols)}`")
            for sym in active_symbols:
                lines.append(f"  • `{sym}`")
        else:
            lines.append("  لا توجد شبكات نشطة")
    else:
        lines.append("  محرك الشبكة غير متاح")

    # ── قسم 2: SuperConsensus ─────────────────────────────────────────────────
    lines.append("\n🧠 *إجماع الخارق (SuperConsensus):*")
    last_scan = ctx.bot_data.get("last_scan_report")
    if last_scan:
        lines.append("  آخر مسح: ✅ متاح")
    else:
        lines.append("  آخر مسح: لم يتم بعد")
    if auto:
        scan_min = auto.settings.scan_interval_min
        if scan_min and scan_min > 0:
            lines.append(f"  المسح التلقائي: 🟢 كل `{scan_min}` دقيقة")
        else:
            lines.append("  المسح التلقائي: 🔴 موقوف")
    else:
        lines.append("  المسح التلقائي: غير متاح")

    # ── قسم 3: Auto-Trade ─────────────────────────────────────────────────────
    lines.append("\n⚡ *التداول الآلي (Auto-Trade):*")
    if auto:
        s          = auto.settings
        positions  = auto.open_positions()
        status_str = "🟢 مفعّل" if auto.is_active() else "🔴 موقوف"
        lines.append(f"  الحالة: {status_str}")
        lines.append(f"  صفقات مفتوحة: `{len(positions)}/{s.max_open_trades}`")
        lines.append(f"  🎯 TP: `+{s.take_profit_pct}%` | 🛑 SL: `-{s.stop_loss_pct}%`")

        # حساب الأرباح غير المحققة
        unrealized = 0.0
        if client and positions:
            for pos in positions:
                try:
                    price = await client.get_current_price(pos.symbol)
                    unrealized += (price - pos.entry_price) * pos.qty
                except Exception:
                    pass

        lines.append(f"  ربح محقق: `{s.realized_pnl:+.4f} USDT`")
        lines.append(f"  غير محقق: `{unrealized:+.4f} USDT`")
        lines.append(f"  إجمالي الصفقات: `{s.total_auto_trades}`")
    else:
        lines.append("  محرك التداول غير متاح")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 تحديث", callback_data="menu:status")],
        [InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="menu:back")],
    ])
    await _edit(query, "\n".join(lines), kb)


# ══════════════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════════════

def register_menu_handlers(app: Application) -> None:
    """Register /menu command and all inline-keyboard handlers."""

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_cb_set,   pattern=r"^set:"),
            CallbackQueryHandler(_cb_auto,  pattern=r"^auto:custom$"),
            CallbackQueryHandler(_cb_grid,  pattern=r"^grid:(new|custom|[A-Z]+/USDT)$"),
            CallbackQueryHandler(_cb_super, pattern=r"^super:scan_auto$"),
        ],
        states={
            AWAIT_CUSTOM_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_custom_amount)],
            AWAIT_STOP_LOSS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
            AWAIT_TAKE_PROFIT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
            AWAIT_MAX_TRADES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
            AWAIT_SCAN_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
            AWAIT_TRADE_AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
            AWAIT_COIN_COUNT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_setting)],
            AWAIT_GRID_PAIR:     [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_grid_pair)],
            AWAIT_GRID_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_grid_amount)],
            AWAIT_SCAN_AUTO_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_scan_auto_min)],
        },
        fallbacks=[
            CommandHandler("menu", cmd_menu),
            CallbackQueryHandler(_cb_menu, pattern=r"^menu:"),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(_cb_menu,     pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(_cb_auto,     pattern=r"^auto:"))
    app.add_handler(CallbackQueryHandler(_cb_stop,     pattern=r"^stop:"))
    app.add_handler(CallbackQueryHandler(_cb_profit,   pattern=r"^profit:"))
    app.add_handler(CallbackQueryHandler(_cb_grid,     pattern=r"^grid:"))
    app.add_handler(CallbackQueryHandler(_cb_gridrisk, pattern=r"^gridrisk:"))
    app.add_handler(CallbackQueryHandler(_cb_gridstop, pattern=r"^gridstop:"))
    app.add_handler(CallbackQueryHandler(_cb_super,    pattern=r"^super:"))

    logger.info("Menu handlers registered (/menu + Grid + SuperConsensus + AutoTrade)")

