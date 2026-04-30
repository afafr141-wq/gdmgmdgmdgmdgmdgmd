"""
Interactive menu for Grid Bot — /menu command with inline keyboards.
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config.settings import ALLOWED_USER_IDS

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
(
    AWAIT_GRID_PAIR,       # grid bot: trading pair (free text)
    AWAIT_GRID_AMOUNT,     # grid bot: investment amount (free text)
    AWAIT_GRID_COUNT,      # grid bot: number of grids per side (free text)
    AWAIT_GRID_UPPER_PCT,  # grid bot: upper breakout % before rebuild
    AWAIT_GRID_LOWER_PCT,  # grid bot: lower breakout % before rebuild
    AWAIT_ADJUST_INV,      # adjust investment: new amount (free text)
    AWAIT_SNR_PAIR,        # S&R: trading pair
    AWAIT_SNR_AMOUNT,      # S&R: investment amount
    AWAIT_SNR_EDIT_INV,    # S&R: edit investment
    AWAIT_SNR_EDIT_LEVELS, # S&R: edit number of levels
    AWAIT_SNR_MODE,        # S&R: level mode (current/previous/both)
) = range(11)

# Mode labels for display
_MODE_LABELS = {
    "current":  "🟢 الحالية فقط (غير مكسورة)",
    "previous": "🔵 السابقة فقط (مكسورة / retest)",
    "both":     "⚪ الحالية + السابقة",
}

SNR_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

POPULAR_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "MATIC/USDT",
]

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


# ── Keyboard builders ──────────────────────────────────────────────────────────

def _main_menu_text(ctx=None) -> str:
    engine = ctx.bot_data.get("engine") if ctx and hasattr(ctx, "bot_data") else None
    symbols = engine.active_symbols() if engine else []
    active  = len(symbols)
    status_line = "🟢 يعمل" if active else "⚪ لا توجد شبكات"
    grids_text  = "\n".join(f"  • `{s}`" for s in symbols) if symbols else "  _لا توجد شبكات نشطة_"
    return (
        "╔══════════════════════╗\n"
        "║   🤖 *AI Grid Bot*   ║\n"
        "║      *MEXC Spot*     ║\n"
        "╚══════════════════════╝\n\n"
        f"📡 الحالة: {status_line}\n"
        f"📊 شبكات نشطة: `{active}`\n"
        f"{grids_text}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "اختر من القائمة أدناه:"
    )


def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 شبكة جديدة",     callback_data="menu:grid"),
         InlineKeyboardButton("🛑 إيقاف شبكة",     callback_data="menu:grid_stop")],
        [InlineKeyboardButton("🔄 ترقية الشبكات",  callback_data="settings_upgradeall"),
         InlineKeyboardButton("📈 استراتيجية S&R", callback_data="snr:back")],
    ])








def _kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu:back")]])


# ── Helper ─────────────────────────────────────────────────────────────────────

async def _edit(query, text: str, kb: InlineKeyboardMarkup) -> None:
    await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


# ── /menu command ──────────────────────────────────────────────────────────────

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    await update.message.reply_text(
        _main_menu_text(ctx),
        reply_markup=_kb_main(),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── menu: callbacks ────────────────────────────────────────────────────────────

async def _cb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "back":
        await _edit(query, _main_menu_text(ctx), _kb_main())
        return ConversationHandler.END

    if action == "grid":
        await _edit(query,
            "🚀 *شبكة جديدة*\n\nأرسل اسم العملة (مثال: `BTC` أو `SOLUSDT`):",
            _kb_back(),
        )
        ctx.user_data["grid_step"] = "pair"
        return AWAIT_GRID_PAIR

    if action == "status":
        await _show_status(query, ctx)
        return None

    if action == "grid_stop":
        engine = ctx.bot_data.get("engine")
        symbols = engine.active_symbols() if engine else []
        if not symbols:
            await query.answer("لا توجد شبكات نشطة.", show_alert=True)
            return None
        rows = [[InlineKeyboardButton(f"🛑 {s}", callback_data=f"gridstop:{s}")] for s in symbols]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:back")])
        await _edit(query, "🛑 *اختر الشبكة للإيقاف:*", InlineKeyboardMarkup(rows))
        return None

    return None


# ── grid: callbacks ────────────────────────────────────────────────────────────

async def _cb_grid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "new":
        await _edit(query,
            "🚀 *شبكة جديدة*\n\nأرسل اسم العملة (مثال: `BTC` أو `SOLUSDT`):",
            _kb_back(),
        )
        ctx.user_data["grid_step"] = "pair"
        return AWAIT_GRID_PAIR

    return None



async def _cb_gridstop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _launch_grid(
    query, ctx,
    pair: str, amount: float, risk: str,
    num_grids: int = 3,
    upper_pct: float = 3.0,
    lower_pct: float = 3.0,
) -> None:
    engine = ctx.bot_data.get("engine")
    if not engine:
        await _edit(query, "❌ محرك الشبكة غير متاح.", _kb_back())
        return
    risk_labels = {"low": "🟢 منخفض", "medium": "🟡 متوسط", "high": "🔴 مرتفع"}
    try:
        await _edit(query, f"⏳ جاري تشغيل شبكة `{pair}`...", _kb_back())
        await engine.start(
            symbol=pair, total_investment=amount, risk=risk,
            num_grids=num_grids, upper_pct=upper_pct, lower_pct=lower_pct,
        )
        ctx.user_data["grid_last_pair"]       = pair
        ctx.user_data["grid_last_amount"]     = amount
        ctx.user_data["grid_last_risk"]       = risk
        ctx.user_data["grid_last_count"]      = num_grids
        ctx.user_data["grid_last_upper_pct"]  = upper_pct
        ctx.user_data["grid_last_lower_pct"]  = lower_pct
        await _edit(query,
            f"✅ *شبكة AI مُشغَّلة*\n\n"
            f"🪙 الزوج: `{pair}`\n"
            f"💵 الاستثمار: `{amount:.0f} USDT`\n"
            f"🔢 الشبكات: `{num_grids}×2 = {num_grids*2}` أمر\n"
            f"📈 خروج علوي: `+{upper_pct}%` | 📉 خروج سفلي: `-{lower_pct}%`\n"
            f"⚖️ المخاطرة: {risk_labels.get(risk, risk)}\n\n"
            "البوت يعمل الآن ويضع أوامر الشراء والبيع تلقائياً.",
            _kb_main(),
        )
    except Exception as exc:
        await _edit(query, f"❌ فشل تشغيل الشبكة: `{exc}`", _kb_back())


# ── Free-text input handlers ───────────────────────────────────────────────────

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
        await update.message.reply_text(
            "❌ أدخل رقماً موجباً (مثال: `100`)", parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_GRID_AMOUNT

    ctx.user_data["grid_pending_amount"] = amount
    pair = ctx.user_data.get("grid_pending_pair", "")
    await update.message.reply_text(
        f"🔢 *عدد الشبكات — {pair} | {amount:.0f} USDT*\n\n"
        "كم شبكة تريد من كل جانب (شراء + بيع)؟\n"
        "مثال: `3` يعني 3 أوامر شراء + 3 أوامر بيع = 6 أوامر إجمالاً\n\n"
        "_النطاق المسموح: 1 إلى 10_",
        parse_mode=ParseMode.MARKDOWN,
    )
    ctx.user_data["grid_step"] = "count"
    return AWAIT_GRID_COUNT


async def _recv_grid_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    try:
        count = int(text)
        assert 1 <= count <= 10
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ أدخل رقماً صحيحاً بين 1 و 10 (مثال: `3`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return AWAIT_GRID_COUNT

    ctx.user_data["grid_pending_count"] = count
    pair   = ctx.user_data.get("grid_pending_pair", "")
    amount = ctx.user_data.get("grid_pending_amount", 0)
    await update.message.reply_text(
        f"📈 *نسبة الخروج العلوي — {pair}*\n\n"
        "كم % فوق السعر الحالي تريد قبل نقل الشبكة للأعلى؟\n"
        "مثال: `3` يعني لو السعر ارتفع 3% فوق الحد العلوي للشبكة\n\n"
        "_النطاق المسموح: 0.5 إلى 50_",
        parse_mode=ParseMode.MARKDOWN,
    )
    ctx.user_data["grid_step"] = "upper_pct"
    return AWAIT_GRID_UPPER_PCT


async def _recv_grid_upper_pct(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip().replace(",", ".")
    try:
        pct = float(text)
        assert 0.5 <= pct <= 50
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ أدخل رقماً بين 0.5 و 50 (مثال: `3`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return AWAIT_GRID_UPPER_PCT

    ctx.user_data["grid_pending_upper_pct"] = pct
    pair = ctx.user_data.get("grid_pending_pair", "")
    await update.message.reply_text(
        f"📉 *نسبة الخروج السفلي — {pair}*\n\n"
        "كم % تحت السعر الحالي تريد قبل نقل الشبكة للأسفل؟\n"
        "مثال: `3` يعني لو السعر انخفض 3% تحت الحد السفلي للشبكة\n\n"
        "_النطاق المسموح: 0.5 إلى 50_",
        parse_mode=ParseMode.MARKDOWN,
    )
    ctx.user_data["grid_step"] = "lower_pct"
    return AWAIT_GRID_LOWER_PCT


async def _recv_grid_lower_pct(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip().replace(",", ".")
    try:
        pct = float(text)
        assert 0.5 <= pct <= 50
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ أدخل رقماً بين 0.5 و 50 (مثال: `3`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return AWAIT_GRID_LOWER_PCT

    ctx.user_data["grid_pending_lower_pct"] = pct
    pair      = ctx.user_data.get("grid_pending_pair", "")
    amount    = ctx.user_data.get("grid_pending_amount", 0)
    count     = ctx.user_data.get("grid_pending_count", 3)
    upper_pct = ctx.user_data.get("grid_pending_upper_pct", pct)
    # Launch directly with medium risk — no need to ask
    risk = "medium"
    await update.message.reply_text(
        f"⏳ جاري تشغيل شبكة `{pair}`...",
        parse_mode=ParseMode.MARKDOWN,
    )
    engine = ctx.bot_data.get("engine")
    if not engine:
        await update.message.reply_text("❌ محرك الشبكة غير متاح.")
        return ConversationHandler.END
    try:
        await engine.start(
            symbol=pair, total_investment=amount, risk=risk,
            num_grids=count, upper_pct=upper_pct, lower_pct=pct,
        )
        ctx.user_data["grid_last_pair"]       = pair
        ctx.user_data["grid_last_amount"]     = amount
        ctx.user_data["grid_last_risk"]       = risk
        ctx.user_data["grid_last_count"]      = count
        ctx.user_data["grid_last_upper_pct"]  = upper_pct
        ctx.user_data["grid_last_lower_pct"]  = pct
        await update.message.reply_text(
            f"✅ *شبكة AI مُشغَّلة*\n\n"
            f"🪙 الزوج: `{pair}`\n"
            f"💵 الاستثمار: `{amount:.0f} USDT`\n"
            f"🔢 الشبكات: `{count}×2 = {count*2}` أمر\n"
            f"📈 خروج علوي: `+{upper_pct}%` | 📉 خروج سفلي: `-{pct}%`\n\n"
            "البوت يعمل الآن ويضع أوامر الشراء والبيع تلقائياً.",
            reply_markup=_kb_main(),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ فشل تشغيل الشبكة: `{exc}`", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END





# ── Adjust investment ──────────────────────────────────────────────────────────

def _kb_adjust_inv(symbol: str, current: float, usdt_free: float) -> InlineKeyboardMarkup:
    """Quick-action buttons for investment adjustment."""
    add10  = round(current * 0.10, 2)
    add25  = round(current * 0.25, 2)
    cut25  = round(current * 0.75, 2)   # reduce by 25%
    cut50  = round(current * 0.50, 2)   # reduce by 50%
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➕ +10%  (+{add10:.0f}$)",  callback_data=f"adjinv:{symbol}:set:{current + add10:.2f}"),
         InlineKeyboardButton(f"➕ +25%  (+{add25:.0f}$)",  callback_data=f"adjinv:{symbol}:set:{current + add25:.2f}")],
        [InlineKeyboardButton(f"➖ -25%  (-{add25:.0f}$)",  callback_data=f"adjinv:{symbol}:set:{cut25:.2f}"),
         InlineKeyboardButton(f"➖ -50%  (-{cut50:.0f}$)",  callback_data=f"adjinv:{symbol}:set:{cut50:.2f}")],
        [InlineKeyboardButton(f"💰 رصيد كامل  ({usdt_free:.0f}$)", callback_data=f"adjinv:{symbol}:set:{usdt_free:.2f}")],
        [InlineKeyboardButton("✏️ مبلغ مخصص",  callback_data=f"adjinv:{symbol}:custom"),
         InlineKeyboardButton("🔙 رجوع",        callback_data=f"detail_{symbol}")],
    ])


async def _show_adjust_inv(query, ctx, symbol: str) -> None:
    """Show the investment adjustment screen for a running grid."""
    engine = ctx.bot_data.get("engine")
    client = ctx.bot_data.get("client")
    state  = engine.get_state(symbol) if engine else None
    if not state:
        await query.answer("الشبكة غير نشطة.", show_alert=True)
        return
    try:
        usdt_free = await client.get_balance("USDT") if client else 0.0
    except Exception:
        usdt_free = 0.0
    current = state.total_investment
    await _edit(
        query,
        f"💰 *تعديل رصيد شبكة `{symbol}`*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 الاستثمار الحالي: `{current:.2f}` USDT\n"
        f"🏦 رصيد USDT المتاح: `{usdt_free:.2f}` USDT\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"اختر خياراً أو اضغط *مبلغ مخصص* لإدخال رقم:",
        _kb_adjust_inv(symbol, current, usdt_free),
    )


async def _cb_adjinv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Handle adjinv:SYMBOL:set:AMOUNT  and  adjinv:SYMBOL:custom callbacks."""
    query = update.callback_query
    await query.answer()
    parts  = query.data.split(":", 3)   # adjinv : symbol : action : [value]
    symbol = parts[1]
    action = parts[2]

    if action == "custom":
        ctx.user_data["adjinv_symbol"] = symbol
        await _edit(
            query,
            f"✏️ *مبلغ مخصص — `{symbol}`*\n\n"
            f"أرسل المبلغ الجديد بـ USDT:\n"
            f"• رقم موجب = الاستثمار الكلي الجديد\n"
            f"• مثال: `300` يعني الشبكة ستعمل بـ 300 USDT\n\n"
            f"_الحد الأدنى: 10 USDT_",
            _kb_back(),
        )
        return AWAIT_ADJUST_INV

    # action == "set"
    new_inv = float(parts[3])
    await _do_adjust(query, ctx, symbol, new_inv)
    return None


async def _recv_adjust_inv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive free-text investment amount for custom adjustment."""
    if not _authorized(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip().replace(",", ".")
    try:
        new_inv = float(text)
        assert new_inv >= 10
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ أدخل رقماً موجباً لا يقل عن 10 USDT (مثال: `200`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return AWAIT_ADJUST_INV

    symbol = ctx.user_data.get("adjinv_symbol", "")
    if not symbol:
        return ConversationHandler.END

    engine = ctx.bot_data.get("engine")
    if not engine:
        await update.message.reply_text("❌ محرك الشبكة غير متاح.")
        return ConversationHandler.END

    await update.message.reply_text(
        f"⏳ جاري تعديل رصيد `{symbol}` إلى `{new_inv:.2f}` USDT…",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        result = await engine.adjust_investment(symbol, new_inv)
        action_ar = {"increased": "زيادة ✅", "decreased": "تخفيض ✅", "unchanged": "بدون تغيير"}.get(result["action"], "")
        diff_sign = "+" if result["diff_usdt"] >= 0 else ""
        await update.message.reply_text(
            f"✅ *تم تعديل رصيد `{symbol}`*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 القديم: `{result['old_investment']:.2f}` USDT\n"
            f"📊 الجديد: `{result['new_investment']:.2f}` USDT\n"
            f"💱 الفرق: `{diff_sign}{result['diff_usdt']:.2f}` USDT\n"
            f"🔄 الإجراء: {action_ar}\n"
            f"💵 السعر: `{result['price']:.4f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"تمت إعادة بناء الشبكة بالميزانية الجديدة.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ فشل التعديل: `{exc}`", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def _do_adjust(query, ctx, symbol: str, new_inv: float) -> None:
    """Execute investment adjustment from a button press."""
    engine = ctx.bot_data.get("engine")
    if not engine:
        await query.answer("محرك الشبكة غير متاح.", show_alert=True)
        return
    await _edit(query, f"⏳ جاري تعديل رصيد `{symbol}` إلى `{new_inv:.2f}` USDT…", _kb_back())
    try:
        result  = await engine.adjust_investment(symbol, new_inv)
        action_ar = {"increased": "زيادة ✅", "decreased": "تخفيض ✅", "unchanged": "بدون تغيير"}.get(result["action"], "")
        diff_sign = "+" if result["diff_usdt"] >= 0 else ""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 تعديل مرة أخرى", callback_data=f"adjinv_show:{symbol}"),
             InlineKeyboardButton("📊 تفاصيل",          callback_data=f"detail_{symbol}")],
            [InlineKeyboardButton("🏠 القائمة",          callback_data="menu:back")],
        ])
        await _edit(
            query,
            f"✅ *تم تعديل رصيد `{symbol}`*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 القديم: `{result['old_investment']:.2f}` USDT\n"
            f"📊 الجديد: `{result['new_investment']:.2f}` USDT\n"
            f"💱 الفرق: `{diff_sign}{result['diff_usdt']:.2f}` USDT\n"
            f"🔄 الإجراء: {action_ar}\n"
            f"💵 السعر: `{result['price']:.4f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"تمت إعادة بناء الشبكة بالميزانية الجديدة.",
            kb,
        )
    except Exception as exc:
        await _edit(query, f"❌ فشل التعديل: `{exc}`", _kb_back())


# ── status helper ──────────────────────────────────────────────────────────────

async def _show_status(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    engine = ctx.bot_data.get("engine")
    client = ctx.bot_data.get("client")

    lines = [
        "📊 *لوحة حالة الشبكات*",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if engine:
        active_symbols = engine.active_symbols()
        if active_symbols:
            lines.append(f"🟢 *{len(active_symbols)} شبكة نشطة*\n")
            for sym in active_symbols:
                try:
                    state  = engine.get_state(sym)
                    report = engine.calc_profit_report(sym)
                    pnl    = report.get("realized_pnl", 0)
                    upnl   = report.get("unrealised_pnl", 0)
                    held   = report.get("held_qty", 0)
                    sells  = report.get("sell_count", 0)
                    opens  = report.get("open_orders", 0)
                    pnl_icon = "📈" if pnl >= 0 else "📉"
                    pending = " ⏳" if (state and state._pending_rebuild) else ""
                    lines.append(
                        f"🔹 *{sym}*{pending}\n"
                        f"  {pnl_icon} محقق: `{pnl:+.4f}` | غير محقق: `{upnl:+.4f}`\n"
                        f"  🪙 كمية: `{held:.4f}` | ✅ بيع: `{sells}` | 🔓 مفتوح: `{opens}`"
                    )
                except Exception:
                    lines.append(f"🔹 *{sym}*")
        else:
            lines.append("⚪ لا توجد شبكات نشطة حالياً")
    else:
        lines.append("❌ محرك الشبكة غير متاح")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")

    symbols = engine.active_symbols() if engine else []
    detail_rows = [[InlineKeyboardButton(f"🔍 {s}", callback_data=f"detail_{s}")] for s in symbols]
    kb = InlineKeyboardMarkup(
        detail_rows + [
            [InlineKeyboardButton("🔄 تحديث",          callback_data="menu:status"),
             InlineKeyboardButton("🏠 القائمة",         callback_data="menu:back")],
        ]
    )
    await _edit(query, "\n".join(lines), kb)


# ── S&R helpers ────────────────────────────────────────────────────────────────

def _fmt_sr_levels(supports: list, resistances: list) -> str:
    """Plain price list — used when SRLevel objects are unavailable."""
    lines = []
    for i, s in enumerate(supports):
        target = resistances[min(i, len(resistances) - 1)] if resistances else None
        target_str = f" → 🎯 `{target:.6f}`" if target else ""
        lines.append(f"  🟢 S{i+1}: `{s:.6f}`{target_str}")
    lines.append("")
    for i, r in enumerate(resistances):
        lines.append(f"  🔴 R{i+1}: `{r:.6f}`")
    return "\n".join(lines)


def _fmt_sr_levels_annotated(lv) -> str:
    """
    Annotated display using SRLevel objects — shows whether each level is
    current (unbroken) or previous (broken / retest).
    """
    sup_lvls = getattr(lv, "support_levels", [])
    res_lvls = getattr(lv, "resistance_levels", [])

    # Fallback to plain display if annotation data is missing
    if not sup_lvls or not res_lvls:
        return _fmt_sr_levels(lv.supports, lv.resistances)

    lines = []
    for i, sl in enumerate(sup_lvls):
        tag    = "🟢 حالي" if sl.is_current else "🔵 سابق"
        target = res_lvls[min(i, len(res_lvls) - 1)].price if res_lvls else None
        t_str  = f" → 🎯 `{target:.6f}`" if target else ""
        lines.append(f"  {tag} S{i+1}: `{sl.price:.6f}`{t_str}")
    lines.append("")
    for i, rl in enumerate(res_lvls):
        tag = "🔴 حالي" if rl.is_current else "🟠 سابق"
        lines.append(f"  {tag} R{i+1}: `{rl.price:.6f}`")
    return "\n".join(lines)


def _fmt_sr_levels_from_report(report: dict) -> str:
    """Build S/R level text from a calc_report() dict."""
    sup_lvls = report.get("support_levels", [])
    res_lvls = report.get("resistance_levels", [])
    if sup_lvls and res_lvls:
        # Build a minimal object to reuse _fmt_sr_levels_annotated
        class _Lv:
            support_levels    = sup_lvls
            resistance_levels = res_lvls
            supports          = report.get("supports", [])
            resistances       = report.get("resistances", [])
        return _fmt_sr_levels_annotated(_Lv())
    return _fmt_sr_levels(report.get("supports", []), report.get("resistances", []))


# ── S&R Menu ───────────────────────────────────────────────────────────────────

def _kb_snr_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 استراتيجية جديدة",  callback_data="snr:new"),
         InlineKeyboardButton("📋 الاستراتيجيات",     callback_data="snr:list")],
        [InlineKeyboardButton("💼 الرصيد",            callback_data="snr:balance"),
         InlineKeyboardButton("🛑 إيقاف استراتيجية",  callback_data="snr:stop_menu")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية",  callback_data="menu:back")],
    ])

def _kb_snr_tf() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for tf in SNR_TIMEFRAMES:
        row.append(InlineKeyboardButton(tf, callback_data=f"snrtf:{tf}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="snr:back")])
    return InlineKeyboardMarkup(rows)


def _kb_snr_mode(pair: str, tf: str) -> InlineKeyboardMarkup:
    """Keyboard to choose which level type to trade."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 الحالية فقط",        callback_data=f"snrmode:{pair}:{tf}:current")],
        [InlineKeyboardButton("🔵 السابقة فقط (retest)", callback_data=f"snrmode:{pair}:{tf}:previous")],
        [InlineKeyboardButton("⚪ الحالية + السابقة",   callback_data=f"snrmode:{pair}:{tf}:both")],
        [InlineKeyboardButton("🔙 رجوع",               callback_data="snr:back")],
    ])

def _kb_snr_strategy(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 تفاصيل",           callback_data=f"snrdetail:{symbol}"),
         InlineKeyboardButton("🔄 تحديث المستويات",  callback_data=f"snrrefresh:{symbol}")],
        [InlineKeyboardButton("⏱ تغيير التايم فريم", callback_data=f"snredittf:{symbol}"),
         InlineKeyboardButton("💵 تغيير الرصيد",     callback_data=f"snreditinv:{symbol}")],
        [InlineKeyboardButton("🔢 عدد المستويات",    callback_data=f"snreditlevels:{symbol}"),
         InlineKeyboardButton("📊 نوع المستويات",    callback_data=f"snreditmode:{symbol}")],
        [InlineKeyboardButton("🔃 مزامنة الرصيد",   callback_data=f"snrsync:{symbol}"),
         InlineKeyboardButton("⛔ إيقاف وبيع",       callback_data=f"snrstop:{symbol}")],
        [InlineKeyboardButton("🔙 رجوع",             callback_data="snr:list")],
    ])


async def _cb_snr(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "back":
        await _edit(query, "📈 *قائمة S&R*\n\nاختر:", _kb_snr_main())
        return None

    if action == "new":
        await _edit(query,
            "✏️ *استراتيجية S&R جديدة*\n\nأرسل رمز الزوج (مثال: `BTCUSDT`):",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="snr:back")]]),
        )
        ctx.user_data["snr_step"] = "pair"
        return AWAIT_SNR_PAIR

    if action == "list":
        snr_engine = ctx.bot_data.get("snr_engine")
        symbols = snr_engine.active_symbols() if snr_engine else []
        if not symbols:
            await query.answer("لا توجد استراتيجيات نشطة.", show_alert=True)
            return None
        rows = [[InlineKeyboardButton(f"📊 {s}", callback_data=f"snrdetail:{s}")] for s in symbols]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="snr:back")])
        await _edit(query, f"📋 *الاستراتيجيات النشطة ({len(symbols)}):*", InlineKeyboardMarkup(rows))
        return None

    if action == "stop_menu":
        snr_engine = ctx.bot_data.get("snr_engine")
        symbols = snr_engine.active_symbols() if snr_engine else []
        if not symbols:
            await query.answer("لا توجد استراتيجيات نشطة.", show_alert=True)
            return None
        rows = [[InlineKeyboardButton(f"🛑 {s}", callback_data=f"snrstop:{s}")] for s in symbols]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="snr:back")])
        await _edit(query, "🛑 *اختر الاستراتيجية للإيقاف:*", InlineKeyboardMarkup(rows))
        return None

    if action == "balance":
        snr_engine = ctx.bot_data.get("snr_engine")
        client     = ctx.bot_data.get("client")
        symbols    = snr_engine.active_symbols() if snr_engine else []
        if not symbols:
            await query.answer("لا توجد استراتيجيات نشطة.", show_alert=True)
            return None
        lines = ["💼 *رصيد S&R*\n━━━━━━━━━━━━━━━━━━━━"]
        for sym in symbols:
            report = snr_engine.calc_report(sym)
            if not report:
                continue
            try:
                price = await client.get_current_price(sym)
                val   = report["held_qty"] * price
                lines.append(
                    f"\n🔹 *{sym}*\n"
                    f"  💰 مخصص: `{report['total_investment']:.2f}` USDT\n"
                    f"  📦 فعلي: `{val:.2f}` USDT\n"
                    f"  💹 ربح محقق: `{report['realized_pnl']:+.4f}` USDT"
                )
            except Exception as exc:
                lines.append(f"\n⚠️ *{sym}*: {exc}")
        await _edit(query, "\n".join(lines),
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="snr:back")]]))
        return None

    return None


async def _cb_snrtf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tf   = query.data.split(":")[1]
    pair = ctx.user_data.get("snr_pair", "")
    ctx.user_data["snr_tf"] = tf
    await _edit(query,
        f"📊 *نوع المستويات — `{pair}` | `{tf}`*\n\n"
        "اختر أي مستويات تريد التداول عليها:\n\n"
        "🟢 *الحالية* — مستويات لم يكسرها السعر بعد\n"
        "🔵 *السابقة* — مستويات كسرها السعر (retest)\n"
        "⚪ *الكل* — الحالية أولاً ثم السابقة",
        _kb_snr_mode(pair, tf),
    )
    return AWAIT_SNR_MODE


async def _recv_snr_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    raw  = (update.message.text or "").strip().upper()
    pair = raw if "/" in raw else (raw.replace("USDT", "/USDT") if raw.endswith("USDT") else raw + "/USDT")
    ctx.user_data["snr_pair"] = pair
    await update.message.reply_text(
        f"⏱ *التايم فريم — {pair}*\n\nاختر التايم فريم لحساب الدعم والمقاومة:",
        reply_markup=_kb_snr_tf(), parse_mode=ParseMode.MARKDOWN,
    )
    return AWAIT_SNR_MODE  # wait for tf → mode → amount


async def _cb_snrmode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle level-mode selection (current / previous / both)."""
    query = update.callback_query
    await query.answer()
    # callback_data = "snrmode:{pair}:{tf}:{mode}"
    parts = query.data.split(":", 3)
    pair, tf, mode = parts[1], parts[2], parts[3]
    ctx.user_data["snr_pair"] = pair
    ctx.user_data["snr_tf"]   = tf
    ctx.user_data["snr_mode"] = mode
    mode_label = _MODE_LABELS.get(mode, mode)
    await _edit(query,
        f"💵 *المبلغ — `{pair}` | `{tf}`*\n"
        f"📊 النوع: {mode_label}\n\n"
        f"أرسل مبلغ الاستثمار بـ USDT (مثال: `200`):",
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="snr:back")]]),
    )
    return AWAIT_SNR_AMOUNT


async def _recv_snr_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    try:
        amount = float(text)
        assert amount >= 10
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ أدخل رقماً لا يقل عن 10 USDT (مثال: `200`)", parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_SNR_AMOUNT

    pair = ctx.user_data.get("snr_pair", "")
    tf   = ctx.user_data.get("snr_tf", "1h")
    mode = ctx.user_data.get("snr_mode", "both")
    snr_engine = ctx.bot_data.get("snr_engine")
    if not snr_engine:
        await update.message.reply_text("❌ محرك S&R غير متاح.")
        return ConversationHandler.END

    await update.message.reply_text(
        f"⏳ جاري حساب مستويات الدعم والمقاومة لـ `{pair}` على `{tf}`...",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        state = await snr_engine.start(
            symbol=pair, timeframe=tf, total_investment=amount, mode=mode
        )
        lv = state.levels
        await update.message.reply_text(
            f"✅ *استراتيجية S&R مُشغَّلة*\n\n"
            f"🪙 الزوج: `{pair}` | ⏱ `{tf}`\n"
            f"💵 الاستثمار: `{amount:.0f}` USDT | 🔢 مستويات: `{state.num_levels}`\n"
            f"📊 النوع: {_MODE_LABELS.get(mode, mode)}\n"
            f"💵 السعر الحالي: `{lv.current_price:.6f}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📉 *دعوم (شراء) → 🎯 هدف بيع:*\n"
            f"{_fmt_sr_levels_annotated(lv)}",
            reply_markup=_kb_snr_strategy(pair),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ فشل التشغيل: `{exc}`", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def _cb_snrdetail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol     = query.data.split(":", 1)[1]
    snr_engine = ctx.bot_data.get("snr_engine")
    client     = ctx.bot_data.get("client")
    report     = snr_engine.calc_report(symbol) if snr_engine else None
    if not report:
        await query.answer("الاستراتيجية غير نشطة.", show_alert=True)
        return
    try:
        price = await client.get_current_price(symbol)
        upnl  = (price - report["avg_buy_price"]) * report["held_qty"] if report["held_qty"] else 0.0
    except Exception:
        price, upnl = report["current_price"], 0.0
    total    = report["realized_pnl"] + upnl
    sr_lines   = _fmt_sr_levels_from_report(report)
    mode_label = _MODE_LABELS.get(report.get("mode", "both"), "⚪ الكل")
    await _edit(query,
        f"📊 *تفاصيل S&R — `{symbol}`*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ التايم فريم: `{report['timeframe']}` | 🔢 مستويات: `{report['num_levels']}`\n"
        f"📊 النوع: {mode_label}\n"
        f"💵 الاستثمار: `{report['total_investment']:.2f}` USDT\n"
        f"💵 السعر الحالي: `{price:.6f}`\n\n"
        f"📉 *دعوم → 🎯 هدف بيع:*\n"
        f"{sr_lines}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 الكمية: `{report['held_qty']:.6f}` | متوسط: `{report['avg_buy_price']:.6f}`\n"
        f"✅ شراء: `{report['buy_count']}` | بيع: `{report['sell_count']}`\n"
        f"🔓 أوامر مفتوحة: `{report['open_orders']}` "
        f"(شراء: `{report['open_buys']}` | بيع: `{report['open_sells']}`)\n\n"
        f"💹 محقق: `{report['realized_pnl']:+.4f}` | غير محقق: `{upnl:+.4f}`\n"
        f"🏆 الإجمالي: `{total:+.4f}` USDT",
        _kb_snr_strategy(symbol),
    )


async def _cb_snrrefresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol     = query.data.split(":", 1)[1]
    snr_engine = ctx.bot_data.get("snr_engine")
    state      = snr_engine.get_state(symbol) if snr_engine else None
    if not state:
        await query.answer("الاستراتيجية غير نشطة.", show_alert=True)
        return
    await _edit(query, f"⏳ جاري تحديث مستويات `{symbol}`...", _kb_back())
    try:
        await snr_engine._refresh_orders(state)
        lv = state.levels
        await _edit(query,
            f"✅ *تم تحديث المستويات — `{symbol}`*\n\n"
            f"📉 *دعوم → 🎯 هدف بيع:*\n"
            f"{_fmt_sr_levels(lv.supports, lv.resistances)}",
            _kb_snr_strategy(symbol),
        )
    except Exception as exc:
        await _edit(query, f"❌ فشل التحديث: `{exc}`", _kb_snr_strategy(symbol))


async def _cb_snredittf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show timeframe selector to change the active strategy's timeframe."""
    query = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]
    ctx.user_data["snr_edit_symbol"] = symbol
    ctx.user_data["snr_edit_action"] = "tf"

    rows = []
    row  = []
    for tf in SNR_TIMEFRAMES:
        row.append(InlineKeyboardButton(tf, callback_data=f"snredittf_set:{symbol}:{tf}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"snrdetail:{symbol}")])

    await _edit(query,
        f"⏱ *تغيير التايم فريم — `{symbol}`*\n\nاختر التايم فريم الجديد:",
        InlineKeyboardMarkup(rows),
    )


async def _cb_snredittf_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply new timeframe — cancel orders, recompute S/R, re-place orders."""
    query  = update.callback_query
    await query.answer()
    parts  = query.data.split(":", 2)   # snredittf_set : symbol : tf
    symbol = parts[1]
    new_tf = parts[2]

    snr_engine = ctx.bot_data.get("snr_engine")
    state      = snr_engine.get_state(symbol) if snr_engine else None
    if not state:
        await query.answer("الاستراتيجية غير نشطة.", show_alert=True)
        return

    await _edit(query, f"⏳ جاري تغيير التايم فريم إلى `{new_tf}`...", _kb_back())
    try:
        state.timeframe = new_tf
        await snr_engine._refresh_orders(state)
        lv = state.levels
        await _edit(query,
            f"✅ *تم تغيير التايم فريم — `{symbol}`*\n\n"
            f"⏱ التايم فريم الجديد: `{new_tf}`\n\n"
            f"📉 *دعوم → 🎯 هدف بيع:*\n"
            f"{_fmt_sr_levels(lv.supports, lv.resistances)}",
            _kb_snr_strategy(symbol),
        )
    except Exception as exc:
        await _edit(query, f"❌ فشل التغيير: `{exc}`", _kb_snr_strategy(symbol))


async def _cb_snreditinv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask user for new investment amount."""
    query = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]
    ctx.user_data["snr_edit_symbol"] = symbol

    snr_engine = ctx.bot_data.get("snr_engine")
    state      = snr_engine.get_state(symbol) if snr_engine else None
    current    = state.total_investment if state else 0

    await _edit(query,
        f"💵 *تغيير الرصيد — `{symbol}`*\n\n"
        f"الرصيد الحالي: `{current:.2f}` USDT\n\n"
        f"أرسل الرصيد الجديد (لا يقل عن 10 USDT):",
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=f"snrdetail:{symbol}")]]),
    )
    return AWAIT_SNR_EDIT_INV


async def _recv_snr_edit_inv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Apply new investment amount and re-place orders."""
    if not _authorized(update):
        return ConversationHandler.END

    text   = (update.message.text or "").strip()
    symbol = ctx.user_data.get("snr_edit_symbol", "")

    try:
        new_inv = float(text)
        assert new_inv >= 10
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "❌ أدخل رقماً لا يقل عن 10 USDT:", parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_SNR_EDIT_INV

    snr_engine = ctx.bot_data.get("snr_engine")
    state      = snr_engine.get_state(symbol) if snr_engine else None
    if not state:
        await update.message.reply_text("❌ الاستراتيجية غير نشطة.")
        return ConversationHandler.END

    old_inv = state.total_investment
    state.total_investment = new_inv

    # Cancel open buy orders and re-place with new allocation
    client = ctx.bot_data.get("client")
    for oid, meta in list(state.open_orders.items()):
        if meta["side"] == "buy":
            await client.cancel_order(symbol, oid)
            state.open_orders.pop(oid, None)
    await snr_engine._place_orders(state)

    await update.message.reply_text(
        f"✅ *تم تغيير الرصيد — `{symbol}`*\n\n"
        f"الرصيد القديم: `{old_inv:.2f}` USDT\n"
        f"الرصيد الجديد: `{new_inv:.2f}` USDT",
        reply_markup=_kb_snr_strategy(symbol),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def _cb_snreditlevels(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show level count selector (1–5)."""
    query = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]

    snr_engine = ctx.bot_data.get("snr_engine")
    state      = snr_engine.get_state(symbol) if snr_engine else None
    current    = state.num_levels if state else 2

    buttons = [
        InlineKeyboardButton(
            f"{'✅ ' if n == current else ''}{n}",
            callback_data=f"snreditlevels_set:{symbol}:{n}",
        )
        for n in range(1, 6)
    ]
    kb = InlineKeyboardMarkup([
        buttons,
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"snrdetail:{symbol}")],
    ])
    await _edit(query,
        f"🔢 *عدد المستويات — `{symbol}`*\n\n"
        f"الحالي: `{current}` مستوى لكل جانب\n\n"
        f"اختر العدد الجديد (1–5):",
        kb,
    )


async def _cb_snreditlevels_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply new level count — recompute S/R and re-place all orders."""
    query  = update.callback_query
    await query.answer()
    parts      = query.data.split(":", 2)
    symbol     = parts[1]
    new_levels = int(parts[2])

    snr_engine = ctx.bot_data.get("snr_engine")
    state      = snr_engine.get_state(symbol) if snr_engine else None
    if not state:
        await query.answer("الاستراتيجية غير نشطة.", show_alert=True)
        return

    await _edit(query, f"⏳ جاري تغيير عدد المستويات إلى `{new_levels}`...", _kb_back())
    try:
        state.num_levels = new_levels
        await snr_engine._refresh_orders(state)
        lv = state.levels
        await _edit(query,
            f"✅ *تم تغيير عدد المستويات — `{symbol}`*\n\n"
            f"🔢 المستويات الجديدة: `{new_levels}` لكل جانب\n\n"
            f"📉 *دعوم → 🎯 هدف بيع:*\n"
            f"{_fmt_sr_levels(lv.supports, lv.resistances)}",
            _kb_snr_strategy(symbol),
        )
    except Exception as exc:
        await _edit(query, f"❌ فشل التغيير: `{exc}`", _kb_snr_strategy(symbol))


async def _cb_snreditmode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show mode selector for an active strategy."""
    query = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]

    snr_engine = ctx.bot_data.get("snr_engine")
    state      = snr_engine.get_state(symbol) if snr_engine else None
    current    = state.mode if state else "both"

    rows = []
    for mode_key, mode_label in _MODE_LABELS.items():
        tick = "✅ " if mode_key == current else ""
        rows.append([InlineKeyboardButton(
            f"{tick}{mode_label}",
            callback_data=f"snreditmode_set:{symbol}:{mode_key}",
        )])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"snrdetail:{symbol}")])
    await _edit(query,
        f"📊 *نوع المستويات — `{symbol}`*\n\n"
        f"الحالي: {_MODE_LABELS.get(current, current)}\n\n"
        "🟢 *الحالية* — مستويات لم يكسرها السعر بعد\n"
        "🔵 *السابقة* — مستويات كسرها السعر (retest)\n"
        "⚪ *الكل* — الحالية أولاً ثم السابقة\n\n"
        "اختر النوع الجديد:",
        InlineKeyboardMarkup(rows),
    )


async def _cb_snreditmode_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Apply new mode — recompute S/R with the chosen level type."""
    query  = update.callback_query
    await query.answer()
    parts    = query.data.split(":", 2)
    symbol   = parts[1]
    new_mode = parts[2]

    snr_engine = ctx.bot_data.get("snr_engine")
    state      = snr_engine.get_state(symbol) if snr_engine else None
    if not state:
        await query.answer("الاستراتيجية غير نشطة.", show_alert=True)
        return

    await _edit(query,
        f"⏳ جاري تغيير نوع المستويات إلى {_MODE_LABELS.get(new_mode, new_mode)}...",
        _kb_back(),
    )
    try:
        state.mode = new_mode
        await snr_engine._refresh_orders(state)
        lv = state.levels
        await _edit(query,
            f"✅ *تم تغيير نوع المستويات — `{symbol}`*\n\n"
            f"📊 النوع الجديد: {_MODE_LABELS.get(new_mode, new_mode)}\n\n"
            f"📉 *دعوم → 🎯 هدف بيع:*\n"
            f"{_fmt_sr_levels_annotated(lv)}",
            _kb_snr_strategy(symbol),
        )
    except Exception as exc:
        await _edit(query, f"❌ فشل التغيير: `{exc}`", _kb_snr_strategy(symbol))


async def _cb_snrsync(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sync external balance: fetch free coin balance from exchange and
    inject it into the active strategy as held position.
    """
    query  = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]

    snr_engine = ctx.bot_data.get("snr_engine")
    if not snr_engine or not snr_engine.get_state(symbol):
        await query.answer("الاستراتيجية غير نشطة.", show_alert=True)
        return

    await _edit(query, f"⏳ جاري مزامنة رصيد `{symbol}`...", _kb_back())

    try:
        r = await snr_engine.sync_balance(symbol)
    except Exception as exc:
        await _edit(query, f"❌ فشلت المزامنة: `{exc}`", _kb_snr_strategy(symbol))
        return

    if not r["synced"]:
        await _edit(query,
            f"⚠️ *مزامنة الرصيد — `{symbol}`*\n\n"
            f"لم يتم المزامنة:\n`{r['reason']}`",
            _kb_snr_strategy(symbol),
        )
        return

    pnl_pct = ((r["current_price"] - r["new_avg_price"]) / r["new_avg_price"] * 100) if r["new_avg_price"] else 0

    await _edit(query,
        f"✅ *تمت المزامنة — `{symbol}`*\n\n"
        f"💰 رصيد {r['base']} المكتشف: `{r['free_qty']:.6f}`\n\n"
        f"📦 الكمية قبل: `{r['old_held_qty']:.6f}`\n"
        f"📦 الكمية بعد: `{r['new_held_qty']:.6f}`\n\n"
        f"💲 متوسط سعر الشراء: `{r['new_avg_price']:.6f}`\n"
        f"💲 السعر الحالي:      `{r['current_price']:.6f}`\n"
        f"📊 الفرق: `{pnl_pct:+.2f}%`\n\n"
        f"_تم وضع أوامر البيع عند مستويات المقاومة تلقائياً._",
        _kb_snr_strategy(symbol),
    )


async def _cb_snrstop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، أوقف وبيع", callback_data=f"snrconfirmstop:{symbol}"),
         InlineKeyboardButton("❌ إلغاء",           callback_data=f"snrdetail:{symbol}")],
    ])
    await _edit(query, f"⚠️ هل تريد إيقاف استراتيجية *`{symbol}`* وبيع كل الكميات؟", kb)


async def _cb_snrconfirmstop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol     = query.data.split(":", 1)[1]
    snr_engine = ctx.bot_data.get("snr_engine")
    await _edit(query, f"⏳ جاري إيقاف `{symbol}`...", _kb_back())
    try:
        val = await snr_engine.stop(symbol, market_sell=True)
        await _edit(query,
            f"🛑 *تم إيقاف `{symbol}`*\n💵 قيمة البيع: `{val:.4f}` USDT",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة", callback_data="menu:back")]]),
        )
    except Exception as exc:
        await _edit(query, f"❌ خطأ: `{exc}`", _kb_back())


# ── Registration ───────────────────────────────────────────────────────────────

def register_menu_handlers(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_cb_menu, pattern=r"^menu:grid$"),
            CallbackQueryHandler(_cb_grid, pattern=r"^grid:(new|custom|[A-Z]+/USDT)$"),
        ],
        states={
            AWAIT_GRID_PAIR:      [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_grid_pair)],
            AWAIT_GRID_AMOUNT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_grid_amount)],
            AWAIT_GRID_COUNT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_grid_count)],
            AWAIT_GRID_UPPER_PCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_grid_upper_pct)],
            AWAIT_GRID_LOWER_PCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_grid_lower_pct)],
            AWAIT_ADJUST_INV:     [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_adjust_inv)],
        },
        fallbacks=[
            CommandHandler("menu", cmd_menu),
            CallbackQueryHandler(_cb_menu, pattern=r"^menu:"),
        ],
        per_message=False,
    )

    # ── S&R conversation ───────────────────────────────────────────────────────
    snr_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_cb_snr,         pattern=r"^snr:new$"),
            CallbackQueryHandler(_cb_snreditinv,  pattern=r"^snreditinv:"),
        ],
        states={
            AWAIT_SNR_PAIR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_snr_pair),
            ],
            AWAIT_SNR_MODE: [
                # tf selection leads here → mode selection
                CallbackQueryHandler(_cb_snrtf,    pattern=r"^snrtf:"),
                CallbackQueryHandler(_cb_snrmode,  pattern=r"^snrmode:"),
            ],
            AWAIT_SNR_AMOUNT: [
                # mode selection leads here → amount text
                CallbackQueryHandler(_cb_snrmode,  pattern=r"^snrmode:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_snr_amount),
            ],
            AWAIT_SNR_EDIT_INV: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_snr_edit_inv),
            ],
        },
        fallbacks=[
            CommandHandler("menu", cmd_menu),
            CallbackQueryHandler(_cb_menu, pattern=r"^menu:"),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(conv)
    app.add_handler(snr_conv)
    app.add_handler(CallbackQueryHandler(_cb_menu,  pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(_cb_grid,  pattern=r"^grid:"))

    app.add_handler(CallbackQueryHandler(_cb_snredittf,          pattern=r"^snredittf:[^:]+$"))
    app.add_handler(CallbackQueryHandler(_cb_snredittf_set,      pattern=r"^snredittf_set:"))
    app.add_handler(CallbackQueryHandler(_cb_snreditlevels,      pattern=r"^snreditlevels:[^:]+$"))
    app.add_handler(CallbackQueryHandler(_cb_snreditlevels_set,  pattern=r"^snreditlevels_set:"))
    app.add_handler(CallbackQueryHandler(_cb_snreditmode,        pattern=r"^snreditmode:[^:]+$"))
    app.add_handler(CallbackQueryHandler(_cb_snreditmode_set,    pattern=r"^snreditmode_set:"))
    app.add_handler(CallbackQueryHandler(_cb_snrsync,            pattern=r"^snrsync:"))
    app.add_handler(CallbackQueryHandler(_cb_gridstop,           pattern=r"^gridstop:"))
    app.add_handler(CallbackQueryHandler(_cb_adjinv,             pattern=r"^adjinv:"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: _show_adjust_inv(u.callback_query, c, u.callback_query.data.split(":", 1)[1]),
        pattern=r"^adjinv_show:",
    ))
    # S&R callbacks (broad patterns last)
    app.add_handler(CallbackQueryHandler(_cb_snr,            pattern=r"^snr:"))
    app.add_handler(CallbackQueryHandler(_cb_snrdetail,      pattern=r"^snrdetail:"))
    app.add_handler(CallbackQueryHandler(_cb_snrrefresh,     pattern=r"^snrrefresh:"))
    app.add_handler(CallbackQueryHandler(_cb_snrstop,        pattern=r"^snrstop:"))
    app.add_handler(CallbackQueryHandler(_cb_snrconfirmstop, pattern=r"^snrconfirmstop:"))

    logger.info("Grid + S&R menu handlers registered")
