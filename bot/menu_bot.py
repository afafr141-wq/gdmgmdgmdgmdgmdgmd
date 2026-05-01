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
    AWAIT_PA_PAIR,         # PA: trading pair
    AWAIT_PA_CAPITAL,      # PA: capital % per trade
    AWAIT_LS_PAIR,         # LS: trading pair
    AWAIT_LS_CAPITAL,      # LS: capital % per trade
) = range(10)

PA_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]

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
        [InlineKeyboardButton("🚀 شبكة جديدة",           callback_data="menu:grid"),
         InlineKeyboardButton("📊 متابعة وإدارة الشبكات", callback_data="menu:manage")],
        [InlineKeyboardButton("🔄 ترقية الشبكات",        callback_data="settings_upgradeall"),
         InlineKeyboardButton("🎯 Price Action",         callback_data="pa:back")],
        [InlineKeyboardButton("💧 Liquidity Swings",     callback_data="ls:back")],
        [InlineKeyboardButton("❓ مساعدة",                callback_data="help:main")],
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

    if action == "manage":
        engine  = ctx.bot_data.get("engine")
        symbols = engine.active_symbols() if engine else []
        if not symbols:
            await query.answer("لا توجد شبكات نشطة حالياً.", show_alert=True)
            return None
        rows = [[InlineKeyboardButton(f"⚙️ {s}", callback_data=f"detail_{s}")] for s in symbols]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:back")])
        await _edit(query, "📊 *اختر الشبكة للمتابعة والإدارة:*", InlineKeyboardMarkup(rows))
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


# ── PA Menu ────────────────────────────────────────────────────────────────────

def _kb_pa_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 استراتيجية جديدة", callback_data="pa:new"),
         InlineKeyboardButton("📋 الاستراتيجيات",    callback_data="pa:list")],
        [InlineKeyboardButton("⛔ إيقاف استراتيجية", callback_data="pa:stop_menu"),
         InlineKeyboardButton("📖 شرح الاستراتيجية", callback_data="pa:explain")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu:back")],
    ])


def _kb_pa_tf() -> InlineKeyboardMarkup:
    rows, row = [], []
    for tf in PA_TIMEFRAMES:
        row.append(InlineKeyboardButton(tf, callback_data=f"patf:{tf}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="pa:back")])
    return InlineKeyboardMarkup(rows)


def _kb_pa_strategy(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 تفاصيل",          callback_data=f"padetail:{symbol}"),
         InlineKeyboardButton("⛔ إيقاف وبيع",      callback_data=f"pastop:{symbol}")],
        [InlineKeyboardButton("🔙 رجوع للقائمة",    callback_data="pa:list")],
    ])


def _fmt_pa_report(r: dict) -> str:
    direction_ar = {"buy": "شراء 📥", "sell": "بيع 📤", "": "يراقب 🔍"}.get(r.get("direction", ""), "—")
    pnl_sign     = "+" if r.get("realized_pnl", 0) >= 0 else ""
    win_rate     = r.get("win_rate", 0)

    pos_lines = ""
    if r.get("held_qty", 0) > 0:
        pos_lines = (
            f"📌 الاتجاه: {direction_ar}\n"
            f"🪙 الكمية: `{r['held_qty']:.6f}`\n"
            f"💵 سعر الدخول: `{r['avg_entry_price']:.6f}`\n"
            f"🎯 الهدف (TP): `{r['tp_price']:.6f}`\n"
        )
    else:
        pos_lines = f"📌 الوضع: {direction_ar}\n"

    return (
        f"🎯 *PA Strategy — `{r['symbol']}`*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ التايم فريم: `{r['timeframe']}`\n"
        f"💰 رأس المال/صفقة: `{r['capital_pct']:.0f}%`\n"
        f"{pos_lines}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 الصفقات: `{r['trade_count']}` | الرابحة: `{r['win_count']}` ({win_rate:.0f}%)\n"
        f"💹 الربح المحقق: `{pnl_sign}{r['realized_pnl']:.4f}` USDT\n"
        f"⏳ أيام التشغيل: `{r['days_running']:.1f}`"
    )


async def _cb_pa(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "back":
        await _edit(query,
            "🎯 *Price Action Strategy*\n\n"
            "يصطاد السيولة عبر:\n"
            "• تحديد Equal Highs / Equal Lows\n"
            "• كشف Liquidity Sweep\n"
            "• تأكيد الشمعة (Engulfing / Hammer)\n"
            "• دخول Market Order + TP تلقائي",
            _kb_pa_main(),
        )
        return None

    if action == "new":
        await _edit(query,
            "🚀 *استراتيجية PA جديدة*\n\nأرسل اسم العملة (مثال: `BTC` أو `SOLUSDT`):",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="pa:back")]]),
        )
        return AWAIT_PA_PAIR

    if action == "list":
        pa_engine = ctx.bot_data.get("pa_engine")
        symbols   = pa_engine.active_symbols() if pa_engine else []
        if not symbols:
            await query.answer("لا توجد استراتيجيات PA نشطة.", show_alert=True)
            return None
        rows = []
        for sym in symbols:
            r = pa_engine.calc_report(sym)
            status = "🔄" if r and r.get("held_qty", 0) > 0 else "🔍"
            rows.append([InlineKeyboardButton(
                f"{status} {sym}", callback_data=f"padetail:{sym}"
            )])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="pa:back")])
        await _edit(query, "📋 *استراتيجيات PA النشطة:*", InlineKeyboardMarkup(rows))
        return None

    if action == "stop_menu":
        pa_engine = ctx.bot_data.get("pa_engine")
        symbols   = pa_engine.active_symbols() if pa_engine else []
        if not symbols:
            await query.answer("لا توجد استراتيجيات PA نشطة.", show_alert=True)
            return None
        rows = [[InlineKeyboardButton(f"⛔ {s}", callback_data=f"pastop:{s}")] for s in symbols]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="pa:back")])
        await _edit(query, "⛔ *اختر الاستراتيجية للإيقاف:*", InlineKeyboardMarkup(rows))
        return None

    if action == "explain":
        from bot.telegram_bot import cmd_explain_pa
        # إرسال الشرح كرسائل منفصلة
        await query.answer("جاري إرسال الشرح…")
        await cmd_explain_pa(update, ctx)
        return None

    return None


async def _recv_pa_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    raw  = (update.message.text or "").strip().upper()
    pair = raw if "/" in raw else (raw.replace("USDT", "/USDT") if raw.endswith("USDT") else raw + "/USDT")
    ctx.user_data["pa_pair"] = pair
    await update.message.reply_text(
        f"⏱ *التايم فريم — {pair}*\n\nاختر التايم فريم:",
        reply_markup=_kb_pa_tf(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return AWAIT_PA_CAPITAL


async def _cb_patf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tf   = query.data.split(":")[1]
    pair = ctx.user_data.get("pa_pair", "")
    ctx.user_data["pa_tf"] = tf

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("10%",  callback_data=f"pacap:{pair}:{tf}:10"),
         InlineKeyboardButton("20%",  callback_data=f"pacap:{pair}:{tf}:20"),
         InlineKeyboardButton("25%",  callback_data=f"pacap:{pair}:{tf}:25")],
        [InlineKeyboardButton("30%",  callback_data=f"pacap:{pair}:{tf}:30"),
         InlineKeyboardButton("50%",  callback_data=f"pacap:{pair}:{tf}:50")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="pa:new")],
    ])
    await _edit(query,
        f"💰 *رأس المال لكل صفقة — `{pair}` | `{tf}`*\n\n"
        f"اختر النسبة من رصيد USDT الحر لكل صفقة:",
        kb,
    )
    return AWAIT_PA_CAPITAL


async def _cb_pacap(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # callback_data = "pacap:{pair}:{tf}:{pct}"
    parts       = query.data.split(":")
    pair        = parts[1]
    tf          = parts[2]
    capital_pct = float(parts[3])

    pa_engine = ctx.bot_data.get("pa_engine")
    if not pa_engine:
        await query.answer("❌ PA engine غير متاح.", show_alert=True)
        return ConversationHandler.END

    await _edit(query,
        f"⏳ جاري تشغيل استراتيجية PA لـ `{pair}` على `{tf}`…",
        InlineKeyboardMarkup([]),
    )
    try:
        await pa_engine.start(pair, tf, capital_pct)
        await query.edit_message_text(
            f"✅ *PA Strategy مُشغَّلة*\n\n"
            f"🪙 الزوج: `{pair}` | ⏱ `{tf}`\n"
            f"💰 رأس المال/صفقة: `{capital_pct:.0f}%` من الرصيد الحر\n\n"
            f"🔍 البوت يراقب الآن إشارات السيولة…\n"
            f"سيُرسل إشعار فور اكتشاف إشارة.",
            reply_markup=_kb_pa_strategy(pair),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        await query.edit_message_text(
            f"❌ فشل التشغيل:\n`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    return ConversationHandler.END


async def _cb_padetail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol    = query.data.split(":", 1)[1]
    pa_engine = ctx.bot_data.get("pa_engine")
    if not pa_engine:
        await query.answer("❌ PA engine غير متاح.", show_alert=True)
        return
    report = pa_engine.calc_report(symbol)
    if not report:
        await query.answer("لا توجد بيانات لهذه الاستراتيجية.", show_alert=True)
        return
    await _edit(query, _fmt_pa_report(report), _kb_pa_strategy(symbol))


async def _cb_pastop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]
    # Confirm first
    await _edit(query,
        f"⚠️ *تأكيد الإيقاف*\n\n"
        f"هل تريد إيقاف PA Strategy لـ `{symbol}`\nوبيع الرصيد فوراً؟",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم، إيقاف وبيع", callback_data=f"paconfirmstop:{symbol}"),
             InlineKeyboardButton("❌ إلغاء",           callback_data=f"padetail:{symbol}")],
        ]),
    )


async def _cb_paconfirmstop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol    = query.data.split(":", 1)[1]
    pa_engine = ctx.bot_data.get("pa_engine")
    if not pa_engine:
        await query.answer("❌ PA engine غير متاح.", show_alert=True)
        return
    try:
        sell_value = await pa_engine.stop(symbol, market_sell=True)
        await _edit(query,
            f"⛔ *PA Strategy متوقفة — `{symbol}`*\n"
            f"💵 قيمة البيع: `{sell_value:.2f}` USDT",
            _kb_pa_main(),
        )
    except Exception as exc:
        await query.edit_message_text(f"❌ خطأ:\n`{exc}`", parse_mode=ParseMode.MARKDOWN)


# ── Liquidity Swings Menu ──────────────────────────────────────────────────────

LS_TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"]


def _kb_ls_main(ctx=None) -> InlineKeyboardMarkup:
    ls_engine = ctx.bot_data.get("ls_engine") if ctx and hasattr(ctx, "bot_data") else None
    symbols   = ls_engine.active_symbols() if ls_engine else []
    rows = [
        [InlineKeyboardButton("➕ استراتيجية جديدة", callback_data="ls:new")],
    ]
    if symbols:
        rows.append([InlineKeyboardButton("📋 الاستراتيجيات النشطة", callback_data="ls:list")])
        rows.append([InlineKeyboardButton("⛔ إيقاف استراتيجية",     callback_data="ls:stop_menu")])
    rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


def _kb_ls_tf() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(tf, callback_data=f"lstf:{tf}") for tf in LS_TIMEFRAMES[:3]]
    row2 = [InlineKeyboardButton(tf, callback_data=f"lstf:{tf}") for tf in LS_TIMEFRAMES[3:]]
    return InlineKeyboardMarkup([row1, row2, [InlineKeyboardButton("🔙 رجوع", callback_data="ls:back")]])


def _kb_ls_strategy(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ إيقاف وبيع", callback_data=f"lsstop:{symbol}")],
        [InlineKeyboardButton("🔙 رجوع",       callback_data="ls:list")],
    ])


def _fmt_ls_report(r: dict) -> str:
    pnl_sign = "+" if r["realized_pnl"] >= 0 else ""
    pos = (
        f"📌 في صفقة شراء\n"
        f"🪙 الكمية: `{r['held_qty']:.6f}`\n"
        f"💵 سعر الدخول: `{r['avg_entry_price']:.6f}`\n"
        f"🎯 TP: `{r['tp_price']:.6f}`\n"
        f"🛡 SL: `{r['sl_price']:.6f}`\n"
    ) if r["held_qty"] > 0 else "📌 يراقب مناطق السيولة ⏳\n"
    return (
        f"💧 *Liquidity Swings — `{r['symbol']}`*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ التايم فريم: `{r['timeframe']}`\n"
        f"💰 رأس المال/صفقة: `{r['capital_pct']:.0f}%`\n"
        f"🔢 الحد الأدنى للمسات: `{r['min_touches']}`\n"
        f"📐 نوع المنطقة: `{r['swing_area']}`\n"
        f"{pos}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 الصفقات: `{r['trade_count']}` | الرابحة: `{r['win_count']}` ({r['win_rate']:.0f}%)\n"
        f"💹 الربح المحقق: `{pnl_sign}{r['realized_pnl']:.4f}` USDT\n"
        f"⏳ أيام التشغيل: `{r['days_running']:.1f}`"
    )


async def _cb_ls(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "back":
        ls_engine = ctx.bot_data.get("ls_engine")
        symbols   = ls_engine.active_symbols() if ls_engine else []
        count     = len(symbols)
        status    = "🟢 يعمل" if count else "⚪ لا توجد استراتيجيات"
        await _edit(query,
            f"💧 *Liquidity Swings*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 الحالة: {status}\n"
            f"📊 استراتيجيات نشطة: `{count}`\n\n"
            f"اختر من القائمة أدناه:",
            _kb_ls_main(ctx),
        )
        return None

    if action == "new":
        await _edit(query,
            "💧 *Liquidity Swings — استراتيجية جديدة*\n\n"
            "أرسل اسم العملة (مثال: `BTC` أو `SOLUSDT`):",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="ls:back")]]),
        )
        return AWAIT_LS_PAIR

    if action == "list":
        ls_engine = ctx.bot_data.get("ls_engine")
        symbols   = ls_engine.active_symbols() if ls_engine else []
        if not symbols:
            await query.answer("لا توجد استراتيجيات نشطة.", show_alert=True)
            return None
        rows = []
        for sym in symbols:
            r      = ls_engine.calc_report(sym)
            status = "🟢" if r and r["held_qty"] > 0 else "⚪"
            rows.append([InlineKeyboardButton(f"{status} {sym}", callback_data=f"lsdetail:{sym}")])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="ls:back")])
        await _edit(query, "📋 *استراتيجيات Liquidity Swings النشطة:*", InlineKeyboardMarkup(rows))
        return None

    if action == "stop_menu":
        ls_engine = ctx.bot_data.get("ls_engine")
        symbols   = ls_engine.active_symbols() if ls_engine else []
        if not symbols:
            await query.answer("لا توجد استراتيجيات نشطة.", show_alert=True)
            return None
        rows = [[InlineKeyboardButton(f"⛔ {s}", callback_data=f"lsstop:{s}")] for s in symbols]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="ls:back")])
        await _edit(query, "⛔ *اختر الاستراتيجية للإيقاف:*", InlineKeyboardMarkup(rows))
        return None

    return None


async def _recv_ls_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _authorized(update):
        return ConversationHandler.END
    raw  = (update.message.text or "").strip().upper()
    pair = raw if "/" in raw else (raw.replace("USDT", "/USDT") if raw.endswith("USDT") else raw + "/USDT")
    ctx.user_data["ls_pair"] = pair
    await update.message.reply_text(
        f"⏱ *التايم فريم — {pair}*\n\nاختر التايم فريم:",
        reply_markup=_kb_ls_tf(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return AWAIT_LS_CAPITAL


async def _cb_lstf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tf   = query.data.split(":")[1]
    pair = ctx.user_data.get("ls_pair", "")
    ctx.user_data["ls_tf"] = tf

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("10%", callback_data=f"lscap:{pair}:{tf}:10"),
         InlineKeyboardButton("20%", callback_data=f"lscap:{pair}:{tf}:20"),
         InlineKeyboardButton("25%", callback_data=f"lscap:{pair}:{tf}:25")],
        [InlineKeyboardButton("30%", callback_data=f"lscap:{pair}:{tf}:30"),
         InlineKeyboardButton("50%", callback_data=f"lscap:{pair}:{tf}:50")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="ls:new")],
    ])
    await _edit(query,
        f"💰 *رأس المال لكل صفقة — `{pair}` | `{tf}`*\n\n"
        f"اختر النسبة من رصيد USDT الحر لكل صفقة:",
        kb,
    )
    return AWAIT_LS_CAPITAL


async def _cb_lscap(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts       = query.data.split(":")
    pair        = parts[1]
    tf          = parts[2]
    capital_pct = float(parts[3])

    ls_engine = ctx.bot_data.get("ls_engine")
    if not ls_engine:
        await query.answer("❌ LS engine غير متاح.", show_alert=True)
        return ConversationHandler.END

    await _edit(query,
        f"⏳ جاري تشغيل Liquidity Swings لـ `{pair}` على `{tf}`…",
        InlineKeyboardMarkup([]),
    )
    try:
        from core.liquidity_swings_strategy_engine import LSParams
        await ls_engine.start(pair, tf, capital_pct, LSParams())
        await query.edit_message_text(
            f"✅ *Liquidity Swings مُشغَّلة*\n\n"
            f"🪙 الزوج: `{pair}` | ⏱ `{tf}`\n"
            f"💰 رأس المال/صفقة: `{capital_pct:.0f}%` من الرصيد الحر\n\n"
            f"🔍 البوت يراقب الآن مناطق السيولة…\n"
            f"سيُرسل إشعار فور اكتشاف إشارة.",
            reply_markup=_kb_ls_strategy(pair),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        await query.edit_message_text(f"❌ فشل التشغيل:\n`{exc}`", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def _cb_lsdetail(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol    = query.data.split(":", 1)[1]
    ls_engine = ctx.bot_data.get("ls_engine")
    if not ls_engine:
        await query.answer("❌ LS engine غير متاح.", show_alert=True)
        return
    report = ls_engine.calc_report(symbol)
    if not report:
        await query.answer("لا توجد بيانات.", show_alert=True)
        return
    await _edit(query, _fmt_ls_report(report), _kb_ls_strategy(symbol))


async def _cb_lsstop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol    = query.data.split(":", 1)[1]
    ls_engine = ctx.bot_data.get("ls_engine")
    if not ls_engine:
        await query.answer("❌ LS engine غير متاح.", show_alert=True)
        return
    try:
        sell_value = await ls_engine.stop(symbol, market_sell=True)
        await _edit(query,
            f"⛔ *Liquidity Swings متوقفة — `{symbol}`*\n"
            f"💵 قيمة البيع: `{sell_value:.2f}` USDT",
            _kb_ls_main(ctx),
        )
    except Exception as exc:
        await query.edit_message_text(f"❌ خطأ:\n`{exc}`", parse_mode=ParseMode.MARKDOWN)


# ── Help Menu ──────────────────────────────────────────────────────────────────

_HELP_TOPICS = {
    "grid": (
        "🚀 *شبكة Grid — كيف تشتغل؟*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "البوت يضع أوامر شراء وبيع على مستويات سعرية متعددة.\n\n"
        "📌 *الخطوات:*\n"
        "1️⃣ اضغط *شبكة جديدة* واختر العملة\n"
        "2️⃣ حدد مبلغ الاستثمار بـ USDT\n"
        "3️⃣ اختر عدد الشبكات (كل جانب)\n"
        "4️⃣ حدد نسبة الخروج العلوي والسفلي\n"
        "5️⃣ اختر مستوى المخاطرة\n\n"
        "✅ البوت يعيد بناء الشبكة تلقائياً عند كسر الحدود."
    ),
    "pa": (
        "🎯 *Price Action — كيف تشتغل؟*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "الاستراتيجية تصطاد السيولة وتدخل عند الارتداد.\n\n"
        "📌 *آلية العمل:*\n"
        "• يرصد البوت *Equal Highs / Equal Lows*\n"
        "• ينتظر *Liquidity Sweep* (اختراق وهمي)\n"
        "• يتأكد بشمعة *Engulfing أو Hammer*\n"
        "• يدخل بـ Market Order ويضع TP تلقائياً\n\n"
        "📌 *الإعداد:*\n"
        "1️⃣ اضغط *Price Action* ← *استراتيجية جديدة*\n"
        "2️⃣ اختر العملة والتايم فريم ونسبة رأس المال"
    ),
    "status": (
        "📊 *حالة الشبكات — شرح الأرقام*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📈 *محقق* — الربح الفعلي من صفقات البيع المكتملة\n"
        "📉 *غير محقق* — الفرق بين سعر الشراء والسعر الحالي\n"
        "🪙 *الكمية* — ما تحتفظ به البوت حالياً\n"
        "✅ *بيع* — عدد صفقات البيع المنفذة\n"
        "🔓 *مفتوح* — عدد الأوامر المعلقة في السوق\n"
        "⏳ — الشبكة في انتظار إعادة البناء"
    ),
    "upgrade": (
        "🔄 *ترقية الشبكات — متى تستخدمها؟*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "الترقية تلغي الأوامر القديمة وتعيد بناء الشبكة حول السعر الحالي.\n\n"
        "📌 *استخدمها عندما:*\n"
        "• تحرك السعر بعيداً عن نطاق الشبكة\n"
        "• تريد تحديث المستويات بعد تغير السوق\n"
        "• الشبكة توقفت عن التداول\n\n"
        "⚠️ *تنبيه:* الترقية تبيع الكميات المحتجزة بسعر السوق."
    ),
    "risk": (
        "⚖️ *مستويات المخاطرة — الفرق بينها*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 *منخفض* — فوارق شبكة أوسع، أوامر أقل، مناسب للسوق الهادئ\n"
        "🟡 *متوسط* — توازن بين الفوارق وعدد الأوامر\n"
        "🔴 *مرتفع* — فوارق ضيقة، أوامر أكثر، مناسب للسوق المتذبذب\n\n"
        "💡 *نصيحة:* ابدأ بـ *متوسط* إذا كنت جديداً."
    ),
    "commands": (
        "⌨️ *الأوامر المتاحة*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "`/menu` — القائمة التفاعلية الرئيسية\n"
        "`/list` — عرض الشبكات النشطة\n"
        "`/status BTCUSDT` — تفاصيل شبكة محددة\n"
        "`/stop BTCUSDT` — إيقاف شبكة\n"
        "`/upgrade` — ترقية جميع الشبكات\n"
        "`/start_ai BTCUSDT 500 medium` — تشغيل شبكة سريع\n"
        "`/pa_start BTC/USDT 1h 20` — تشغيل Price Action\n"
        "`/ls_start BTC/USDT 1h 20` — تشغيل Liquidity Swings\n"
        "`/help` — هذه المساعدة"
    ),
    "ls": (
        "💧 *Liquidity Swings — كيف تشتغل؟*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "الاستراتيجية تحدد مناطق السيولة وتدخل عند لمسها.\n\n"
        "📌 *آلية العمل:*\n"
        "• يرصد البوت *Swing Highs / Swing Lows* البارزة\n"
        "• يبني منطقة سيولة حول كل قمة أو قاع\n"
        "• يعدّ كم مرة لمس السعر المنطقة\n"
        "• عند لمس منطقة *Swing Low* بعدد لمسات كافٍ → شراء\n"
        "• الهدف (TP) = أقرب *Swing High* نشطة فوق السعر\n"
        "• وقف الخسارة (SL) = أسفل القاع بهامش صغير\n\n"
        "📌 *الإعداد:*\n"
        "1️⃣ اضغط *Liquidity Swings* ← *استراتيجية جديدة*\n"
        "2️⃣ اختر العملة والتايم فريم ونسبة رأس المال"
    ),
}

def _kb_help_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 شبكة Grid",        callback_data="help:grid"),
         InlineKeyboardButton("🎯 Price Action",     callback_data="help:pa")],
        [InlineKeyboardButton("💧 Liquidity Swings", callback_data="help:ls"),
         InlineKeyboardButton("📊 فهم الأرقام",      callback_data="help:status")],
        [InlineKeyboardButton("🔄 الترقية",          callback_data="help:upgrade"),
         InlineKeyboardButton("⚖️ مستويات المخاطرة", callback_data="help:risk")],
        [InlineKeyboardButton("⌨️ الأوامر",          callback_data="help:commands")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu:back")],
    ])

def _kb_help_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 رجوع للمساعدة", callback_data="help:main"),
         InlineKeyboardButton("🏠 القائمة",        callback_data="menu:back")],
    ])

async def _cb_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    topic = query.data.split(":", 1)[1]

    if topic == "main":
        await _edit(query,
            "❓ *مركز المساعدة*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "اختر الموضوع اللي تريد تعرف عنه:",
            _kb_help_main(),
        )
        return

    text = _HELP_TOPICS.get(topic)
    if text:
        await _edit(query, text, _kb_help_back())


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

    # ── PA conversation ────────────────────────────────────────────────────────
    pa_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_cb_pa,    pattern=r"^pa:new$"),
        ],
        states={
            AWAIT_PA_PAIR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_pa_pair),
            ],
            AWAIT_PA_CAPITAL: [
                CallbackQueryHandler(_cb_patf,   pattern=r"^patf:"),
                CallbackQueryHandler(_cb_pacap,  pattern=r"^pacap:"),
            ],
        },
        fallbacks=[
            CommandHandler("menu", cmd_menu),
            CallbackQueryHandler(_cb_menu, pattern=r"^menu:"),
            CallbackQueryHandler(_cb_pa,   pattern=r"^pa:"),
        ],
        per_message=False,
    )

    # ── LS conversation ────────────────────────────────────────────────────────
    ls_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_cb_ls, pattern=r"^ls:new$"),
        ],
        states={
            AWAIT_LS_PAIR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_ls_pair),
            ],
            AWAIT_LS_CAPITAL: [
                CallbackQueryHandler(_cb_lstf,  pattern=r"^lstf:"),
                CallbackQueryHandler(_cb_lscap, pattern=r"^lscap:"),
            ],
        },
        fallbacks=[
            CommandHandler("menu", cmd_menu),
            CallbackQueryHandler(_cb_menu, pattern=r"^menu:"),
            CallbackQueryHandler(_cb_ls,   pattern=r"^ls:"),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(conv)
    app.add_handler(pa_conv)
    app.add_handler(ls_conv)
    app.add_handler(CallbackQueryHandler(_cb_menu,  pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(_cb_grid,  pattern=r"^grid:"))

    app.add_handler(CallbackQueryHandler(_cb_gridstop, pattern=r"^gridstop:"))
    app.add_handler(CallbackQueryHandler(_cb_adjinv,   pattern=r"^adjinv:"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: _show_adjust_inv(u.callback_query, c, u.callback_query.data.split(":", 1)[1]),
        pattern=r"^adjinv_show:",
    ))
    # PA callbacks
    app.add_handler(CallbackQueryHandler(_cb_pa,            pattern=r"^pa:"))
    app.add_handler(CallbackQueryHandler(_cb_padetail,      pattern=r"^padetail:"))
    app.add_handler(CallbackQueryHandler(_cb_pastop,        pattern=r"^pastop:"))
    app.add_handler(CallbackQueryHandler(_cb_paconfirmstop, pattern=r"^paconfirmstop:"))
    # Liquidity Swings callbacks
    app.add_handler(CallbackQueryHandler(_cb_ls,       pattern=r"^ls:"))
    app.add_handler(CallbackQueryHandler(_cb_lsdetail, pattern=r"^lsdetail:"))
    app.add_handler(CallbackQueryHandler(_cb_lsstop,   pattern=r"^lsstop:"))
    # Help
    app.add_handler(CallbackQueryHandler(_cb_help, pattern=r"^help:"))

    logger.info("Grid + S&R + PA + LS + Help menu handlers registered")
