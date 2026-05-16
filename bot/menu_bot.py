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
) = range(6)

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
    engine  = ctx.bot_data.get("engine") if ctx and hasattr(ctx, "bot_data") else None
    symbols = engine.active_symbols() if engine else []
    active  = len(symbols)

    if active:
        status_line = f"🟢 *{active}* شبكة نشطة"
        grids_text  = "\n".join(f"  ▸ `{s}`" for s in symbols)
    else:
        status_line = "⚪ لا توجد شبكات نشطة"
        grids_text  = "  _ابدأ شبكة جديدة من القائمة أدناه_"

    return (
        "┌─────────────────────────┐\n"
        "│  🤖  *AI Grid Bot*      │\n"
        "│  📍  *MEXC Spot*        │\n"
        "└─────────────────────────┘\n\n"
        f"📡 *الحالة:* {status_line}\n"
        f"{grids_text}\n\n"
        "─────────────────────────"
    )


def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 شبكة جديدة",            callback_data="menu:grid"),
         InlineKeyboardButton("📊 إدارة الشبكات",          callback_data="menu:manage")],
        [InlineKeyboardButton("🔄 ترقية الشبكات",          callback_data="settings_upgradeall"),
         InlineKeyboardButton("❓ مساعدة",                  callback_data="help:main")],
        [InlineKeyboardButton("⚡ سكالبنج",                callback_data="scalp:menu"),
         InlineKeyboardButton("💼 المحافظ الذكية",          callback_data="portfolio:home")],
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
        pnl_str  = f"`{pnl:+.2f} USDT`" if pnl is not None else "غير محدد"
        pnl_icon = "📈" if (pnl or 0) >= 0 else "📉"
        await _edit(query,
            f"🛑 *تم إيقاف الشبكة*\n\n"
            f"🪙 الزوج: `{symbol}`\n"
            f"{pnl_icon} الربح / الخسارة: {pnl_str}",
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
            f"✅ *شبكة مُشغَّلة بنجاح*\n\n"
            f"🪙 الزوج:       `{pair}`\n"
            f"💵 الاستثمار:   `{amount:.0f} USDT`\n"
            f"🔢 الأوامر:     `{num_grids}` شراء + `{num_grids}` بيع\n"
            f"📈 حد علوي:    `+{upper_pct}%`  |  📉 حد سفلي: `-{lower_pct}%`\n"
            f"⚖️ المخاطرة:    {risk_labels.get(risk, risk)}\n\n"
            "─────────────────────────\n"
            "البوت يراقب السوق ويُنفّذ الأوامر تلقائياً.",
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
            f"✅ *شبكة مُشغَّلة بنجاح*\n\n"
            f"🪙 الزوج:       `{pair}`\n"
            f"💵 الاستثمار:   `{amount:.0f} USDT`\n"
            f"🔢 الأوامر:     `{count}` شراء + `{count}` بيع\n"
            f"📈 حد علوي:    `+{upper_pct}%`  |  📉 حد سفلي: `-{pct}%`\n\n"
            "─────────────────────────\n"
            "البوت يراقب السوق ويُنفّذ الأوامر تلقائياً.",
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
        f"💰 *تعديل الاستثمار — `{symbol}`*\n\n"
        f"📊 الاستثمار الحالي:  `{current:.2f} USDT`\n"
        f"🏦 الرصيد المتاح:     `{usdt_free:.2f} USDT`\n\n"
        f"─────────────────────────\n"
        f"اختر نسبة أو أدخل مبلغاً مخصصاً:",
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
                    price  = await client.get_current_price(sym) if client else 0.0
                    report = engine.calc_profit_report(state, price) if state else {}
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
        "`/help` — هذه المساعدة"
    ),
}

def _kb_help_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 شبكة Grid",        callback_data="help:grid"),
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

    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(_cb_menu,     pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(_cb_grid,     pattern=r"^grid:"))
    app.add_handler(CallbackQueryHandler(_cb_gridstop, pattern=r"^gridstop:"))
    app.add_handler(CallbackQueryHandler(_cb_adjinv,   pattern=r"^adjinv:"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: _show_adjust_inv(u.callback_query, c, u.callback_query.data.split(":", 1)[1]),
        pattern=r"^adjinv_show:",
    ))
    app.add_handler(CallbackQueryHandler(_cb_help, pattern=r"^help:"))

    logger.info("Grid + Help menu handlers registered")
