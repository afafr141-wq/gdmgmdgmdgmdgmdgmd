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
    AWAIT_GRID_PAIR,    # grid bot: trading pair (free text)
    AWAIT_GRID_AMOUNT,  # grid bot: investment amount (free text)
) = range(2)

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
    active = len(engine.active_symbols()) if engine else 0
    return (
        "🤖 *AI Grid Bot — MEXC*\n\n"
        f"شبكات نشطة: `{active}`\n\n"
        "اختر من القائمة:"
    )


def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 شبكة AI",          callback_data="menu:grid"),
            InlineKeyboardButton("📋 عرض الشبكات",       callback_data="menu:status"),
        ],
    ])


def _kb_grid_menu(has_last: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🚀 تشغيل شبكة جديدة", callback_data="grid:new")],
    ]
    if has_last:
        rows.append([InlineKeyboardButton("⚡ تشغيل بآخر إعدادات", callback_data="grid:last")])
    rows.append([InlineKeyboardButton("🛑 إيقاف شبكة",             callback_data="grid:stop_menu")])
    rows.append([InlineKeyboardButton("🔙 رجوع",                   callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


def _kb_grid_pairs() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(POPULAR_PAIRS), 2):
        row = []
        for pair in POPULAR_PAIRS[i:i+2]:
            base = pair.replace("/USDT", "")
            row.append(InlineKeyboardButton(base, callback_data=f"grid:{pair}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ زوج مخصص", callback_data="grid:custom")])
    rows.append([InlineKeyboardButton("🔙 رجوع",      callback_data="menu:grid")])
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
        has_last = bool(ctx.user_data.get("grid_last_pair"))
        last_info = ""
        if has_last:
            lp = ctx.user_data.get("grid_last_pair", "")
            la = ctx.user_data.get("grid_last_amount", 0)
            lr = ctx.user_data.get("grid_last_risk", "medium")
            last_info = f"\n⚡ آخر إعدادات: `{lp}` | `{la} USDT` | `{lr}`"
        await _edit(query,
            f"🤖 *شبكة AI الذكية — Grid Bot*{last_info}\n\nاختر ما تريد:",
            _kb_grid_menu(has_last=has_last),
        )
        return None

    if action == "status":
        await _show_status(query, ctx)
        return None

    return None


# ── grid: callbacks ────────────────────────────────────────────────────────────

async def _cb_grid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "new":
        await _edit(query,
            "🤖 *شبكة جديدة — اختر الزوج*\n\nاضغط على زوج أو اختر 'مخصص':",
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
        symbols = engine.active_symbols() if engine else []
        if not symbols:
            await query.answer("لا توجد شبكات نشطة.", show_alert=True)
            return None
        rows = [[InlineKeyboardButton(f"🛑 {s}", callback_data=f"gridstop:{s}")] for s in symbols]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu:grid")])
        await _edit(query, "🛑 *اختر الشبكة للإيقاف:*", InlineKeyboardMarkup(rows))
        return None

    # Pair selected from quick list
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
    query = update.callback_query
    await query.answer()
    risk   = query.data.split(":")[1]
    pair   = ctx.user_data.get("grid_pending_pair", "")
    amount = ctx.user_data.get("grid_pending_amount", 0)
    if not pair or not amount:
        await query.answer("بيانات ناقصة، ابدأ من جديد.", show_alert=True)
        return
    await _launch_grid(query, ctx, pair, amount, risk)


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


async def _launch_grid(query, ctx, pair: str, amount: float, risk: str) -> None:
    engine = ctx.bot_data.get("engine")
    if not engine:
        await _edit(query, "❌ محرك الشبكة غير متاح.", _kb_back())
        return
    risk_labels = {"low": "🟢 منخفض", "medium": "🟡 متوسط", "high": "🔴 مرتفع"}
    try:
        await _edit(query, f"⏳ جاري تشغيل شبكة `{pair}`...", _kb_back())
        await engine.start(symbol=pair, total_investment=amount, risk=risk)
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





# ── status helper ──────────────────────────────────────────────────────────────

async def _show_status(query, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    engine = ctx.bot_data.get("engine")
    client = ctx.bot_data.get("client")

    lines = ["📊 *حالة بوت الشبكة*\n", "━━━━━━━━━━━━━━━━━━━━"]
    lines.append("\n🤖 *شبكة AI الذكية (Grid Bot):*")

    if engine:
        active_symbols = engine.active_symbols()
        if active_symbols:
            lines.append(f"  شبكات نشطة: `{len(active_symbols)}`")
            for sym in active_symbols:
                try:
                    report = engine.calc_profit_report(sym)
                    pnl    = report.get("realized_pnl", 0)
                    held   = report.get("held_qty", 0)
                    lines.append(f"  • `{sym}` — ربح: `{pnl:+.4f}` | كمية: `{held:.4f}`")
                except Exception:
                    lines.append(f"  • `{sym}`")
        else:
            lines.append("  لا توجد شبكات نشطة حالياً")
    else:
        lines.append("  محرك الشبكة غير متاح")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 تحديث",  callback_data="menu:status")],
        [InlineKeyboardButton("🔙 رجوع",   callback_data="menu:back")],
    ])
    await _edit(query, "\n".join(lines), kb)


# ── Registration ───────────────────────────────────────────────────────────────

def register_menu_handlers(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_cb_grid, pattern=r"^grid:(new|custom|[A-Z]+/USDT)$"),
        ],
        states={
            AWAIT_GRID_PAIR:   [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_grid_pair)],
            AWAIT_GRID_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _recv_grid_amount)],
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
    app.add_handler(CallbackQueryHandler(_cb_gridrisk, pattern=r"^gridrisk:"))
    app.add_handler(CallbackQueryHandler(_cb_gridstop, pattern=r"^gridstop:"))

    logger.info("Grid Bot menu handlers registered")
