"""
SuperConsensus Telegram Bot — handlers only.

Registered into the main bot's Application via register_super_handlers().
Notifications go through the shared _send() from telegram_bot.py.
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from config.settings import ALLOWED_USER_IDS
from super_consensus.core.consensus_engine import ConsensusEngine, CANDLE_TIMEFRAME, CANDLE_LIMIT
from super_consensus.utils.super_db import (
    upsert_bot,
    deactivate_bot,
    make_db_fns,
)

logger = logging.getLogger(__name__)

# ── Auth guard ─────────────────────────────────────────────────────────────────

def _authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return update.effective_user.id in ALLOWED_USER_IDS


async def _deny(update: Update) -> None:
    await update.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")


# ── Signal emoji helpers ───────────────────────────────────────────────────────

def _signal_emoji(sig: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}.get(sig, "⚪")


def _votes_bar(votes: int, total: int = 5) -> str:
    filled = "█" * votes
    empty  = "░" * (total - votes)
    return f"{filled}{empty} {votes}/{total}"


# ── Main menu keyboard ─────────────────────────────────────────────────────────

def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 بدء بوت جديد", callback_data="super:menu:start"),
            InlineKeyboardButton("🛑 إيقاف بوت",    callback_data="super:menu:stop"),
        ],
        [
            InlineKeyboardButton("📊 الحالة",        callback_data="super:menu:status"),
            InlineKeyboardButton("📡 الإشارات",      callback_data="super:menu:signals"),
        ],
        [
            InlineKeyboardButton("📋 قائمة البوتات", callback_data="super:menu:list"),
        ],
    ])


# ── /super_menu ────────────────────────────────────────────────────────────────

async def cmd_super_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    await update.message.reply_text(
        "🤖 *SuperConsensus Bot* — القائمة الرئيسية\n"
        "اختر أمراً أو استخدم الأوامر مباشرة:",
        reply_markup=_main_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /start_super SYMBOL INVESTMENT ────────────────────────────────────────────

async def cmd_start_super(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "الاستخدام: `/start_super BTCUSDT 1000`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    raw_symbol = args[0].upper()
    # Normalise: BTCUSDT → BTC/USDT for ccxt
    symbol = raw_symbol if "/" in raw_symbol else raw_symbol.replace("USDT", "/USDT")

    try:
        investment = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ رأس المال يجب أن يكون رقماً.")
        return

    if investment <= 0:
        await update.message.reply_text("❌ رأس المال يجب أن يكون أكبر من صفر.")
        return

    engine: ConsensusEngine = context.bot_data["super_engine"]

    try:
        import asyncio
        db_fns = make_db_fns(symbol)

        # Create state manually so we can wire db_fns into the loop from the start
        from super_consensus.core.consensus_engine import BotState
        if symbol in engine._bots and engine._bots[symbol].is_active:
            raise ValueError(f"{symbol} يعمل بالفعل")

        state = BotState(symbol=symbol, total_investment=investment)
        engine._bots[symbol] = state
        await upsert_bot(state)

        task = asyncio.create_task(
            engine._loop(symbol, db_fns), name=f"super_{symbol}"
        )
        state._task = task

        await update.message.reply_text(
            f"✅ *SuperConsensus* بدأ على `{symbol}`\n"
            f"💰 رأس المال: `{investment:.2f} USDT`\n"
            f"🔄 يفحص كل دقيقة — شموع `{CANDLE_TIMEFRAME}` (آخر {CANDLE_LIMIT})\n"
            f"🗳️ يحتاج ≥3 أصوات للتنفيذ",
            parse_mode=ParseMode.MARKDOWN,
        )
    except ValueError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
    except Exception as exc:
        logger.error("start_super error: %s", exc, exc_info=True)
        await update.message.reply_text(f"❌ خطأ: {exc}")


# ── /stop_super SYMBOL ─────────────────────────────────────────────────────────

async def cmd_stop_super(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = context.args
    if not args:
        await update.message.reply_text("الاستخدام: `/stop_super BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return

    symbol = args[0].upper()
    if "/" not in symbol:
        symbol = symbol.replace("USDT", "/USDT")

    engine: ConsensusEngine = context.bot_data["super_engine"]
    db_fns = make_db_fns(symbol)

    pnl = await engine.stop(symbol, market_sell=True, db_fns=db_fns)

    msg = f"🛑 *SuperConsensus* أُوقف على `{symbol}`\n"
    if pnl is not None:
        msg += f"{'💚' if pnl >= 0 else '🔴'} ربح/خسارة الإغلاق: `{pnl:+.4f} USDT`"
    else:
        msg += "ℹ️ لا يوجد مركز مفتوح."

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ── /status_super SYMBOL ──────────────────────────────────────────────────────

async def cmd_status_super(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = context.args
    if not args:
        await update.message.reply_text("الاستخدام: `/status_super BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return

    symbol = args[0].upper()
    if "/" not in symbol:
        symbol = symbol.replace("USDT", "/USDT")

    engine: ConsensusEngine = context.bot_data["super_engine"]
    state = engine.get_state(symbol)

    if not state:
        await update.message.reply_text(f"⚠️ لا يوجد بوت نشط على `{symbol}`.", parse_mode=ParseMode.MARKDOWN)
        return

    client = context.bot_data["client"]
    try:
        price = await client.get_current_price(symbol)
    except Exception:
        price = 0.0

    pos = state.position
    pos_text = "لا يوجد مركز مفتوح"
    unrealized = 0.0
    if pos:
        unrealized = (price - pos.entry_price) * pos.qty
        pos_text = (
            f"📦 الكمية: `{pos.qty:.6f}`\n"
            f"📥 سعر الدخول: `{pos.entry_price:.4f}`\n"
            f"⏱️ منذ: `{pos.entry_time.strftime('%Y-%m-%d %H:%M UTC')}`\n"
            f"{'💚' if unrealized >= 0 else '🔴'} غير محقق: `{unrealized:+.4f} USDT`"
        )

    # Last signals
    sigs = state.last_signals
    sig_lines = "\n".join(
        f"  {_signal_emoji(v)} {k}: `{v}`"
        for k, v in sigs.items()
    ) if sigs else "  لم يتم الفحص بعد"

    status_icon = "▶️" if not state.is_paused else "⏸️"
    msg = (
        f"📊 *SuperConsensus — {symbol}*\n\n"
        f"{status_icon} الحالة: `{'متوقف مؤقتاً' if state.is_paused else 'يعمل'}`\n"
        f"💰 رأس المال: `{state.total_investment:.2f} USDT`\n"
        f"💵 السعر الحالي: `{price:.4f}`\n\n"
        f"📌 *المركز الحالي:*\n{pos_text}\n\n"
        f"📡 *آخر إشارات:*\n{sig_lines}\n"
        f"  🟢 BUY: {_votes_bar(state.last_buy_votes)}\n"
        f"  🔴 SELL: {_votes_bar(state.last_sell_votes)}\n\n"
        f"📈 إجمالي الربح المحقق: `{state.realized_pnl:+.4f} USDT`\n"
        f"🔢 عدد الصفقات: `{state.total_trades}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ── /signals SYMBOL ───────────────────────────────────────────────────────────

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = context.args
    if not args:
        await update.message.reply_text("الاستخدام: `/signals BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return

    symbol = args[0].upper()
    if "/" not in symbol:
        symbol = symbol.replace("USDT", "/USDT")

    client  = context.bot_data["client"]
    engine: ConsensusEngine = context.bot_data["super_engine"]

    await update.message.reply_text(f"⏳ جاري جلب الإشارات لـ `{symbol}`...", parse_mode=ParseMode.MARKDOWN)

    try:
        candles = await client.fetch_ohlcv(symbol, CANDLE_TIMEFRAME, CANDLE_LIMIT)
        price   = float(candles[-1][4]) if candles else 0.0
        signals = engine.get_signals_now(candles)
    except Exception as exc:
        await update.message.reply_text(f"❌ خطأ في جلب البيانات: {exc}")
        return

    buy_votes  = sum(1 for s in signals.values() if s == "BUY")
    sell_votes = sum(1 for s in signals.values() if s == "SELL")

    sig_lines = "\n".join(
        f"  {_signal_emoji(v)} *{k}*: `{v}`"
        for k, v in signals.items()
    )

    consensus = "⚪ لا إجماع"
    if buy_votes >= 3:
        consensus = f"🟢 إجماع شراء ({buy_votes}/5)"
    elif sell_votes >= 3:
        consensus = f"🔴 إجماع بيع ({sell_votes}/5)"

    msg = (
        f"📡 *إشارات لحظية — {symbol}*\n"
        f"💵 السعر: `{price:.4f}`\n\n"
        f"{sig_lines}\n\n"
        f"🗳️ BUY: {_votes_bar(buy_votes)} | SELL: {_votes_bar(sell_votes)}\n"
        f"🏁 الإجماع: {consensus}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ── /list_super ────────────────────────────────────────────────────────────────

async def cmd_list_super(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    engine: ConsensusEngine = context.bot_data["super_engine"]
    symbols = engine.active_symbols()

    if not symbols:
        await update.message.reply_text("ℹ️ لا توجد بوتات نشطة حالياً.")
        return

    lines = []
    for sym in symbols:
        state = engine.get_state(sym)
        icon  = "⏸️" if state and state.is_paused else "▶️"
        pnl   = state.realized_pnl if state else 0.0
        lines.append(f"{icon} `{sym}` — ربح: `{pnl:+.4f} USDT`")

    await update.message.reply_text(
        f"📋 *البوتات النشطة ({len(symbols)}):*\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /pause_super SYMBOL ────────────────────────────────────────────────────────

async def cmd_pause_super(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = context.args
    if not args:
        await update.message.reply_text("الاستخدام: `/pause_super BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return

    symbol = args[0].upper()
    if "/" not in symbol:
        symbol = symbol.replace("USDT", "/USDT")

    engine: ConsensusEngine = context.bot_data["super_engine"]
    engine.pause(symbol)
    await update.message.reply_text(
        f"⏸️ تم إيقاف المراقبة مؤقتاً على `{symbol}` — المركز المفتوح محفوظ.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /resume_super SYMBOL ───────────────────────────────────────────────────────

async def cmd_resume_super(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)

    args = context.args
    if not args:
        await update.message.reply_text("الاستخدام: `/resume_super BTCUSDT`", parse_mode=ParseMode.MARKDOWN)
        return

    symbol = args[0].upper()
    if "/" not in symbol:
        symbol = symbol.replace("USDT", "/USDT")

    engine: ConsensusEngine = context.bot_data["super_engine"]
    engine.resume(symbol)
    await update.message.reply_text(
        f"▶️ استُؤنفت المراقبة على `{symbol}`.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Inline keyboard callbacks ──────────────────────────────────────────────────

async def handle_super_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data  # e.g. "super:menu:start"

    if not data.startswith("super:"):
        return

    parts = data.split(":")
    action = parts[2] if len(parts) > 2 else ""

    if action == "start":
        await query.message.reply_text(
            "لبدء بوت جديد، أرسل:\n`/start_super BTCUSDT 1000`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif action == "stop":
        await query.message.reply_text(
            "لإيقاف بوت، أرسل:\n`/stop_super BTCUSDT`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif action == "status":
        await query.message.reply_text(
            "لعرض الحالة، أرسل:\n`/status_super BTCUSDT`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif action == "signals":
        await query.message.reply_text(
            "لعرض الإشارات، أرسل:\n`/signals BTCUSDT`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif action == "list":
        # Reuse list command logic
        engine: ConsensusEngine = context.bot_data["super_engine"]
        symbols = engine.active_symbols()
        if not symbols:
            await query.message.reply_text("ℹ️ لا توجد بوتات نشطة.")
        else:
            lines = [f"▶️ `{s}`" for s in symbols]
            await query.message.reply_text(
                "📋 *البوتات النشطة:*\n" + "\n".join(lines),
                parse_mode=ParseMode.MARKDOWN,
            )


# ── Handler registration (called from telegram_bot.build_application) ──────────

def register_super_handlers(app: Application, engine: ConsensusEngine) -> None:
    """Add all SuperConsensus commands to an existing Application."""
    app.bot_data["super_engine"] = engine

    app.add_handler(CommandHandler("super_menu",   cmd_super_menu))
    app.add_handler(CommandHandler("start_super",  cmd_start_super))
    app.add_handler(CommandHandler("stop_super",   cmd_stop_super))
    app.add_handler(CommandHandler("status_super", cmd_status_super))
    app.add_handler(CommandHandler("signals",      cmd_signals))
    app.add_handler(CommandHandler("list_super",   cmd_list_super))
    app.add_handler(CommandHandler("pause_super",  cmd_pause_super))
    app.add_handler(CommandHandler("resume_super", cmd_resume_super))
    app.add_handler(CallbackQueryHandler(handle_super_callback, pattern=r"^super:"))

    logger.info("SuperConsensus handlers registered into main bot")
