"""
SuperConsensus Telegram Bot — handlers only.

Registered into the main bot's Application via register_super_handlers().
Notifications go through the shared notify() callback from main.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from config.settings import ALLOWED_USER_IDS
from super_consensus.core.consensus_engine import ConsensusEngine, BotState, CONFIDENCE_THRESHOLD
from super_consensus.utils.super_db import upsert_bot, deactivate_bot, make_db_fns

logger = logging.getLogger(__name__)


# ── Auth ───────────────────────────────────────────────────────────────────────

def _authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return update.effective_user.id in ALLOWED_USER_IDS

async def _deny(update: Update) -> None:
    await update.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")


# ── Symbol normalisation ───────────────────────────────────────────────────────

def _norm(raw: str) -> str:
    """BTCUSDT → BTC/USDT"""
    s = raw.upper()
    return s if "/" in s else s.replace("USDT", "/USDT")


# ── Signal emoji helpers ───────────────────────────────────────────────────────

def _sig_emoji(sig: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(sig, "⚪")

def _conf_bar(conf: int) -> str:
    filled = round(conf / 10)
    return "█" * filled + "░" * (10 - filled) + f" {conf}%"


# ── Main menu ──────────────────────────────────────────────────────────────────

def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 بدء بوت",      callback_data="super:start"),
            InlineKeyboardButton("🛑 إيقاف بوت",    callback_data="super:stop"),
        ],
        [
            InlineKeyboardButton("📊 الحالة",        callback_data="super:status"),
            InlineKeyboardButton("📡 الإشارات",      callback_data="super:signals"),
        ],
        [
            InlineKeyboardButton("👑 حالة العمالقة", callback_data="super:giants"),
            InlineKeyboardButton("📋 قائمة البوتات", callback_data="super:list"),
        ],
    ])


# ── /super_menu ────────────────────────────────────────────────────────────────

async def cmd_super_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    await update.message.reply_text(
        "🤖 *SuperConsensus Bot* — القائمة الرئيسية\n"
        "يعمل بالذكاء الاصطناعي عبر OpenRouter\n\n"
        "اختر أمراً أو استخدم الأوامر مباشرة:",
        reply_markup=_main_menu_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /start_super SYMBOL INVESTMENT ────────────────────────────────────────────

async def cmd_start_super(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "الاستخدام: `/start_super BTCUSDT 1000`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    symbol = _norm(args[0])
    try:
        investment = float(args[1])
        assert investment > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ رأس المال يجب أن يكون رقماً موجباً.")
        return

    engine: ConsensusEngine = ctx.bot_data["super_engine"]

    if symbol in engine._bots and engine._bots[symbol].is_active:
        await update.message.reply_text(f"⚠️ البوت يعمل بالفعل على `{symbol}`.", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        db_fns = make_db_fns(symbol)
        state  = BotState(symbol=symbol, total_investment=investment)
        engine._bots[symbol] = state
        await upsert_bot(state)

        task = asyncio.create_task(
            engine._loop(symbol, db_fns), name=f"super_{symbol}"
        )
        state._task = task

        await update.message.reply_text(
            f"✅ *SuperConsensus* بدأ على `{symbol}`\n"
            f"💰 رأس المال: `{investment:.2f} USDT`\n\n"
            f"🧠 *آلية العمل:*\n"
            f"  • Giant1 يحلل مشاعر السوق (CoinGecko + Fear & Greed)\n"
            f"  • Giant3/4/5 جاهزون للتوسع\n"
            f"  • AIJudge يصدر القرار النهائي كل 10 دقائق\n\n"
            f"🎯 حد الثقة للتنفيذ: `{CONFIDENCE_THRESHOLD}%`\n"
            f"🔄 دورة الفحص: كل دقيقة",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.error("start_super error: %s", exc, exc_info=True)
        await update.message.reply_text(f"❌ خطأ: {exc}")


# ── /stop_super SYMBOL ─────────────────────────────────────────────────────────

async def cmd_stop_super(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = ctx.args
    if not args:
        await update.message.reply_text("الاستخدام: `/stop_super BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return

    symbol = _norm(args[0])
    engine: ConsensusEngine = ctx.bot_data["super_engine"]
    db_fns = make_db_fns(symbol)

    pnl = await engine.stop(symbol, market_sell=True, db_fns=db_fns)

    msg = f"🛑 *SuperConsensus* أُوقف على `{symbol}`\n"
    if pnl is not None:
        msg += f"{'💚' if pnl >= 0 else '🔴'} ربح/خسارة الإغلاق: `{pnl:+.4f} USDT`"
    else:
        msg += "ℹ️ لا يوجد مركز مفتوح."

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ── /status_super SYMBOL ──────────────────────────────────────────────────────

async def cmd_status_super(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = ctx.args
    if not args:
        await update.message.reply_text("الاستخدام: `/status_super BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return

    symbol = _norm(args[0])
    engine: ConsensusEngine = ctx.bot_data["super_engine"]
    state  = engine.get_state(symbol)

    if not state:
        await update.message.reply_text(f"⚠️ لا يوجد بوت نشط على `{symbol}`.", parse_mode=ParseMode.MARKDOWN)
        return

    client = ctx.bot_data["client"]
    try:
        price = await client.get_current_price(symbol)
    except Exception:
        price = 0.0

    # Position block
    pos = state.position
    pos_text = "لا يوجد مركز مفتوح"
    if pos:
        unrealized = (price - pos.entry_price) * pos.qty
        pos_text = (
            f"📦 الكمية: `{pos.qty:.6f}`\n"
            f"📥 سعر الدخول: `{pos.entry_price:.4f}`\n"
            f"⏱️ منذ: `{pos.entry_time.strftime('%Y-%m-%d %H:%M UTC')}`\n"
            f"{'💚' if unrealized >= 0 else '🔴'} غير محقق: `{unrealized:+.4f} USDT`"
        )

    # Last judge decision
    judge = state.last_judge_report
    judge_text = "لم يتم استشارة القاضي بعد"
    if judge:
        judge_text = (
            f"{_sig_emoji(judge.signal)} القرار: `{judge.signal}`\n"
            f"📊 الثقة: {_conf_bar(judge.confidence)}\n"
            f"💬 السبب: _{judge.reason}_"
        )

    status_icon = "⏸️" if state.is_paused else "▶️"
    msg = (
        f"📊 *SuperConsensus — {symbol}*\n\n"
        f"{status_icon} الحالة: `{'متوقف مؤقتاً' if state.is_paused else 'يعمل'}`\n"
        f"💰 رأس المال: `{state.total_investment:.2f} USDT`\n"
        f"💵 السعر الحالي: `{price:.4f}`\n\n"
        f"📌 *المركز الحالي:*\n{pos_text}\n\n"
        f"🧠 *آخر قرار للقاضي:*\n{judge_text}\n\n"
        f"📈 إجمالي الربح المحقق: `{state.realized_pnl:+.4f} USDT`\n"
        f"🔢 عدد الصفقات: `{state.total_trades}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ── /signals SYMBOL ───────────────────────────────────────────────────────────

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = ctx.args
    if not args:
        await update.message.reply_text("الاستخدام: `/signals BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return

    symbol = _norm(args[0])
    client  = ctx.bot_data["client"]
    engine: ConsensusEngine = ctx.bot_data["super_engine"]

    wait_msg = await update.message.reply_text(
        f"⏳ جاري استشارة العمالقة والقاضي لـ `{symbol}`...\n"
        f"_(قد يستغرق 10-20 ثانية)_",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        price  = await client.get_current_price(symbol)
        result = await engine.get_signals_now(symbol, price)
    except Exception as exc:
        await wait_msg.delete()
        await update.message.reply_text(f"❌ خطأ: {exc}")
        return

    market_reports = result["market_reports"]
    judge          = result["judge"]

    # Market giants block
    giants_lines = []
    for r in market_reports:
        giants_lines.append(
            f"  {_sig_emoji(r.signal)} *{r.name}*: `{r.signal}` ({r.confidence}%)\n"
            f"    _{r.reason}_"
        )

    msg = (
        f"📡 *تقرير العمالقة — {symbol}*\n"
        f"💵 السعر: `{price:.4f} USDT`\n\n"
        f"👑 *تقارير العمالقة:*\n"
        + "\n".join(giants_lines) +
        f"\n\n🧠 *قرار القاضي (AIJudge):*\n"
        f"  {_sig_emoji(judge.signal)} القرار: `{judge.signal}`\n"
        f"  📊 الثقة: {_conf_bar(judge.confidence)}\n"
        f"  💬 السبب: _{judge.reason}_\n\n"
        f"{'✅ سيتم التنفيذ عند الإجماع' if judge.confidence >= CONFIDENCE_THRESHOLD else '⏸️ الثقة أقل من الحد — لن يتم التنفيذ'}"
    )

    await wait_msg.delete()
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ── /giants SYMBOL — حالة العمالقة منفصلة ────────────────────────────────────

async def cmd_giants(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = ctx.args
    if not args:
        await update.message.reply_text("الاستخدام: `/giants BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return

    symbol = _norm(args[0])
    engine: ConsensusEngine = ctx.bot_data["super_engine"]
    state  = engine.get_state(symbol)

    if not state or not state.last_market_reports:
        await update.message.reply_text(
            f"⚠️ لا توجد بيانات بعد لـ `{symbol}` — انتظر أول دورة فحص.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = []
    for r in state.last_market_reports:
        lines.append(
            f"{_sig_emoji(r.signal)} *{r.name}*\n"
            f"  الإشارة: `{r.signal}` | الثقة: `{r.confidence}%`\n"
            f"  _{r.reason}_"
        )

    judge = state.last_judge_report
    judge_block = "لم يتم استشارة القاضي بعد"
    if judge:
        judge_block = (
            f"{_sig_emoji(judge.signal)} القرار: `{judge.signal}`\n"
            f"  الثقة: {_conf_bar(judge.confidence)}\n"
            f"  _{judge.reason}_"
        )

    msg = (
        f"👑 *حالة العمالقة — {symbol}*\n\n"
        + "\n\n".join(lines) +
        f"\n\n🧠 *القاضي (AIJudge):*\n{judge_block}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ── /list_super ────────────────────────────────────────────────────────────────

async def cmd_list_super(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    engine: ConsensusEngine = ctx.bot_data["super_engine"]
    symbols = engine.active_symbols()

    if not symbols:
        await update.message.reply_text("ℹ️ لا توجد بوتات نشطة حالياً.")
        return

    lines = []
    for sym in symbols:
        state = engine.get_state(sym)
        icon  = "⏸️" if state and state.is_paused else "▶️"
        judge = state.last_judge_report if state else None
        judge_str = f"`{judge.signal}` {judge.confidence}%" if judge else "لم يُستشر بعد"
        lines.append(f"{icon} `{sym}` — القاضي: {judge_str} | ربح: `{state.realized_pnl:+.4f}`")

    await update.message.reply_text(
        f"📋 *البوتات النشطة ({len(symbols)}):*\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /pause_super / /resume_super ──────────────────────────────────────────────

async def cmd_pause_super(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("الاستخدام: `/pause_super BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return
    symbol = _norm(ctx.args[0])
    ctx.bot_data["super_engine"].pause(symbol)
    await update.message.reply_text(
        f"⏸️ تم إيقاف المراقبة مؤقتاً على `{symbol}` — المركز محفوظ.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_resume_super(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("الاستخدام: `/resume_super BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return
    symbol = _norm(ctx.args[0])
    ctx.bot_data["super_engine"].resume(symbol)
    await update.message.reply_text(
        f"▶️ استُؤنفت المراقبة على `{symbol}`.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Inline keyboard callbacks ──────────────────────────────────────────────────

async def handle_super_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1] if ":" in query.data else ""

    hints = {
        "start":   "لبدء بوت جديد:\n`/start_super BTCUSDT 1000`",
        "stop":    "لإيقاف بوت:\n`/stop_super BTCUSDT`",
        "status":  "لعرض الحالة:\n`/status_super BTCUSDT`",
        "signals": "لعرض الإشارات:\n`/signals BTCUSDT`",
        "giants":  "لعرض حالة العمالقة:\n`/giants BTCUSDT`",
        "list":    None,
    }

    if action == "list":
        engine: ConsensusEngine = ctx.bot_data["super_engine"]
        symbols = engine.active_symbols()
        text = (
            "📋 *البوتات النشطة:*\n" + "\n".join(f"▶️ `{s}`" for s in symbols)
            if symbols else "ℹ️ لا توجد بوتات نشطة."
        )
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    elif action in hints:
        await query.message.reply_text(hints[action], parse_mode=ParseMode.MARKDOWN)


# ── Registration ───────────────────────────────────────────────────────────────

def register_super_handlers(app: Application, engine: ConsensusEngine) -> None:
    """Add all SuperConsensus commands to an existing Application."""
    app.bot_data["super_engine"] = engine

    app.add_handler(CommandHandler("super_menu",   cmd_super_menu))
    app.add_handler(CommandHandler("start_super",  cmd_start_super))
    app.add_handler(CommandHandler("stop_super",   cmd_stop_super))
    app.add_handler(CommandHandler("status_super", cmd_status_super))
    app.add_handler(CommandHandler("signals",      cmd_signals))
    app.add_handler(CommandHandler("giants",       cmd_giants))
    app.add_handler(CommandHandler("list_super",   cmd_list_super))
    app.add_handler(CommandHandler("pause_super",  cmd_pause_super))
    app.add_handler(CommandHandler("resume_super", cmd_resume_super))
    app.add_handler(CallbackQueryHandler(handle_super_callback, pattern=r"^super:"))

    logger.info("SuperConsensus handlers registered (AI-powered)")
