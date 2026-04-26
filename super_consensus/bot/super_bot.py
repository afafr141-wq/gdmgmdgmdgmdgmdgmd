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
from super_consensus.core.smart_scanner import SMART_SCANNER, ScanResult, SCAN_COIN_COUNT
from super_consensus.core.auto_trade_engine import AutoTradeEngine
from super_consensus.utils.super_db import (
    upsert_bot, deactivate_bot, make_db_fns,
    upsert_auto_settings, get_auto_trades,
)

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
        [
            InlineKeyboardButton("🔍 مسح السوق /scan", callback_data="super:scan"),
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
        "scan":    None,
    }

    if action == "scan":
        # Trigger the scan directly from the inline button
        await query.message.reply_text(
            "🔍 استخدم الأمر `/scan` لبدء مسح السوق الذكي.\n"
            "يمكنك تحديد عدد العملات: `/scan 50`",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif action == "list":
        engine: ConsensusEngine = ctx.bot_data["super_engine"]
        symbols = engine.active_symbols()
        text = (
            "📋 *البوتات النشطة:*\n" + "\n".join(f"▶️ `{s}`" for s in symbols)
            if symbols else "ℹ️ لا توجد بوتات نشطة."
        )
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    elif action in hints:
        await query.message.reply_text(hints[action], parse_mode=ParseMode.MARKDOWN)


# ── /scan — Smart Market Scanner ──────────────────────────────────────────────

def _signal_emoji(sig: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(sig.upper(), "⚪")


def _build_scan_report(result: ScanResult) -> str:
    """Format ScanResult into a Telegram-ready Markdown message."""
    lines = [
        "🔍 *تقرير الماسح الذكي — Smart Scanner*",
        f"📊 عملات تم فحصها: `{result.coins_scanned}`",
        f"⏱️ وقت التحليل: `{result.scan_duration_s:.0f}` ثانية",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🏆 *أفضل الفرص (قرار القاضي النهائي)*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for pick in result.final_picks:
        medal = medals[pick.rank - 1] if pick.rank <= len(medals) else f"{pick.rank}."
        sig_e = _signal_emoji(pick.signal)
        analysts_str = " & ".join(pick.analysts) if pick.analysts else "—"
        conf_bar = "█" * round(pick.confidence / 10) + "░" * (10 - round(pick.confidence / 10))
        lines += [
            "",
            f"{medal} *{pick.name}* (`{pick.symbol}`)",
            f"  {sig_e} الإشارة: `{pick.signal}` | الثقة: `{conf_bar}` {pick.confidence}%",
            f"  💬 _{pick.reason}_",
            f"  👥 اتفق عليها: `{analysts_str}`",
        ]

    # Analyst summaries
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🧠 *تقارير المحللين الثلاثة*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for report in result.analyst_reports:
        if report.error:
            lines.append(f"  ⚠️ *{report.analyst_name}*: خطأ — `{report.error[:60]}`")
            continue
        picks_str = ", ".join(
            f"{p.symbol}({p.confidence}%)" for p in report.picks
        )
        lines.append(f"  🤖 *{report.analyst_name}*: {picks_str}")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ _هذا تحليل AI للمعلومات فقط — ليس نصيحة مالية._",
    ]
    return "\n".join(lines)


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /scan [count]
    Runs the Smart Scanner: fetches top coins, runs 3 AI analysts + judge,
    and returns the best opportunities.
    """
    if not _authorized(update):
        return await _deny(update)

    # Optional coin count argument
    count = SCAN_COIN_COUNT
    if ctx.args:
        try:
            count = max(10, min(100, int(ctx.args[0])))
        except ValueError:
            pass

    wait_msg = await update.message.reply_text(
        f"🔍 *الماسح الذكي يعمل...*\n\n"
        f"📊 جاري فحص أكبر `{count}` عملة من حيث القيمة السوقية\n"
        f"🤖 يتم استشارة 3 نماذج AI بالتوازي\n"
        f"⏳ _قد يستغرق 30-90 ثانية..._",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        result = await SMART_SCANNER.scan(coin_count=count)
        report = _build_scan_report(result)
        await wait_msg.delete()
        await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)

    except Exception as exc:
        logger.error("cmd_scan error: %s", exc, exc_info=True)
        await wait_msg.delete()
        await update.message.reply_text(
            f"❌ *خطأ في الماسح الذكي*\n`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Auto-Trade Mode commands
# ══════════════════════════════════════════════════════════════════════════════

def _get_auto_engine(ctx: ContextTypes.DEFAULT_TYPE) -> AutoTradeEngine:
    return ctx.bot_data["auto_engine"]


async def cmd_auto_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /auto_mode 100
    Activate auto-trade with a fixed USDT amount per trade.
    """
    if not _authorized(update):
        return await _deny(update)

    if not ctx.args:
        await update.message.reply_text(
            "الاستخدام: `/auto_mode 100`\n"
            "يفعّل الوضع الآلي بمبلغ ثابت 100 USDT لكل صفقة.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        amount = float(ctx.args[0])
        assert amount > 0
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ المبلغ يجب أن يكون رقماً موجباً.")
        return

    auto: AutoTradeEngine = _get_auto_engine(ctx)
    s = auto.settings

    await auto.enable(trade_amount_usdt=amount, db_save=upsert_auto_settings)

    await update.message.reply_text(
        f"🤖 *الوضع الآلي — مُفعَّل*\n\n"
        f"💵 مبلغ كل صفقة: `{amount:.2f} USDT`\n"
        f"🎯 هدف الربح: `+{s.take_profit_pct}%`\n"
        f"🛡️ وقف الخسارة: `-{s.stop_loss_pct}%`\n"
        f"📊 حد الصفقات المفتوحة: `{s.max_open_trades}`\n"
        f"⏱️ فترة المسح: كل `{s.scan_interval_min}` دقيقة\n"
        f"🔒 فترة التهدئة بين الصفقات: `{s.cooldown_minutes}` دقيقة\n\n"
        f"البوت يمسح السوق الآن ويبحث عن إجماع 3/3 محللين ≥ {s.min_analyst_conf}%\n"
        f"أوقف الوضع الآلي بـ `/auto_off`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_auto_mode_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /auto_mode_risk 80
    Activate auto-trade using 80% of free capital per trade.
    """
    if not _authorized(update):
        return await _deny(update)

    if not ctx.args:
        await update.message.reply_text(
            "الاستخدام: `/auto_mode_risk 80`\n"
            "يفعّل الوضع الآلي بنسبة 80% من رأس المال المتاح لكل صفقة.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        pct = float(ctx.args[0])
        assert 1 <= pct <= 100
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ النسبة يجب أن تكون بين 1 و 100.")
        return

    auto: AutoTradeEngine = _get_auto_engine(ctx)
    s = auto.settings

    await auto.enable(risk_pct=pct, db_save=upsert_auto_settings)

    await update.message.reply_text(
        f"🤖 *الوضع الآلي (نسبة المخاطرة) — مُفعَّل*\n\n"
        f"📊 نسبة كل صفقة من رأس المال الحر: `{pct:.1f}%`\n"
        f"🎯 هدف الربح: `+{s.take_profit_pct}%`\n"
        f"🛡️ وقف الخسارة: `-{s.stop_loss_pct}%`\n"
        f"📊 حد الصفقات المفتوحة: `{s.max_open_trades}`\n"
        f"⏱️ فترة المسح: كل `{s.scan_interval_min}` دقيقة\n\n"
        f"أوقف الوضع الآلي بـ `/auto_off`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_auto_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /auto_off
    Deactivate auto-trade mode. Open positions are kept.
    """
    if not _authorized(update):
        return await _deny(update)

    auto: AutoTradeEngine = _get_auto_engine(ctx)

    if not auto.is_active():
        await update.message.reply_text("ℹ️ الوضع الآلي غير مفعّل أصلاً.")
        return

    open_count = len(auto.open_positions())
    await auto.disable(db_save=upsert_auto_settings)

    await update.message.reply_text(
        f"🛑 *الوضع الآلي — أُوقف*\n\n"
        f"📌 الصفقات المفتوحة المتبقية: `{open_count}`\n"
        f"_(الصفقات المفتوحة لا تُغلق تلقائياً — استخدم /auto_status لمراقبتها)_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_auto_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /auto_status
    Show current auto-trade state, open positions, and P&L.
    """
    if not _authorized(update):
        return await _deny(update)

    auto: AutoTradeEngine = _get_auto_engine(ctx)
    s = auto.settings
    client = ctx.bot_data["client"]

    status_icon = "🟢 مفعّل" if auto.is_active() else "🔴 موقوف"
    mode_str = (
        f"`{s.trade_amount_usdt:.2f} USDT` ثابت"
        if s.trade_amount_usdt is not None
        else f"`{s.risk_pct:.1f}%` من رأس المال"
        if s.risk_pct is not None
        else "غير محدد"
    )

    positions = auto.open_positions()
    pos_lines = []
    unrealized = 0.0
    for pos in positions:
        try:
            price = await client.get_current_price(pos.symbol)
            unr   = (price - pos.entry_price) * pos.qty
            unrealized += unr
            pct   = (price / pos.entry_price - 1) * 100
            age_h = pos.age_hours
            pos_lines.append(
                f"  • `{pos.symbol}` | دخول: `{pos.entry_price:.6f}`\n"
                f"    السعر: `{price:.6f}` | {'+' if pct >= 0 else ''}{pct:.2f}%"
                f" | {'+' if unr >= 0 else ''}{unr:.4f} USDT\n"
                f"    TP: `{pos.tp_price:.6f}` | SL: `{pos.sl_price:.6f}`"
                f" | منذ: `{age_h:.1f}h`"
            )
        except Exception:
            pos_lines.append(f"  • `{pos.symbol}` — لا يمكن جلب السعر")

    pos_block = "\n".join(pos_lines) if pos_lines else "  لا توجد صفقات مفتوحة"

    msg = (
        f"🤖 *حالة الوضع الآلي*\n\n"
        f"الحالة: {status_icon}\n"
        f"💵 حجم الصفقة: {mode_str}\n"
        f"🎯 هدف الربح: `+{s.take_profit_pct}%`\n"
        f"🛡️ وقف الخسارة: `-{s.stop_loss_pct}%`\n"
        f"📊 حد الصفقات: `{s.max_open_trades}`\n"
        f"⏱️ فترة المسح: `{s.scan_interval_min}` دقيقة\n"
        f"🔒 فترة التهدئة: `{s.cooldown_minutes}` دقيقة\n"
        f"⏰ حد الاحتفاظ: `{s.max_hold_hours}` ساعة\n\n"
        f"📈 *الأداء:*\n"
        f"  صفقات منفذة: `{s.total_auto_trades}`\n"
        f"  ربح/خسارة محقق: `{s.realized_pnl:+.4f} USDT`\n"
        f"  غير محقق: `{unrealized:+.4f} USDT`\n"
        f"  رأس المال: `{s.total_capital_usdt:.2f} USDT`\n\n"
        f"📌 *المراكز المفتوحة ({len(positions)}/{s.max_open_trades}):*\n{pos_block}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_auto_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /auto_history [limit]
    Show last N completed auto-trades.
    """
    if not _authorized(update):
        return await _deny(update)

    limit = 10
    if ctx.args:
        try:
            limit = max(1, min(50, int(ctx.args[0])))
        except ValueError:
            pass

    try:
        trades = await get_auto_trades(limit=limit)
    except Exception as exc:
        await update.message.reply_text(f"❌ خطأ في قراءة السجل: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        return

    if not trades:
        await update.message.reply_text("ℹ️ لا توجد صفقات آلية مسجلة بعد.")
        return

    lines = []
    for t in trades:
        icon = "✅" if float(t["pnl"]) >= 0 else "❌"
        side_ar = "شراء" if t["side"] == "buy" else "بيع"
        lines.append(
            f"{icon} `{t['symbol']}` — {side_ar} @ `{float(t['price']):.6f}`"
            f" | ربح: `{float(t['pnl']):+.4f}` | {t['exit_reason']}"
        )

    await update.message.reply_text(
        f"📋 *آخر {len(trades)} صفقة آلية:*\n\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /set_* — runtime settings ──────────────────────────────────────────────────

async def cmd_set_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_risk 2  — set stop-loss percentage"""
    if not _authorized(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("الاستخدام: `/set_risk 2` (وقف الخسارة 2%)", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        val = float(ctx.args[0])
        assert 0.1 <= val <= 50
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ القيمة يجب أن تكون بين 0.1 و 50.")
        return
    auto: AutoTradeEngine = _get_auto_engine(ctx)
    auto.settings.stop_loss_pct = val
    await upsert_auto_settings(auto._settings_dict())
    await update.message.reply_text(f"✅ وقف الخسارة: `-{val}%`", parse_mode=ParseMode.MARKDOWN)


async def cmd_set_profit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_profit 4  — set take-profit percentage"""
    if not _authorized(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("الاستخدام: `/set_profit 4` (هدف ربح 4%)", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        val = float(ctx.args[0])
        assert 0.1 <= val <= 100
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ القيمة يجب أن تكون بين 0.1 و 100.")
        return
    auto: AutoTradeEngine = _get_auto_engine(ctx)
    auto.settings.take_profit_pct = val
    await upsert_auto_settings(auto._settings_dict())
    await update.message.reply_text(f"✅ هدف الربح: `+{val}%`", parse_mode=ParseMode.MARKDOWN)


async def cmd_set_max_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_max_trades 3  — set max concurrent open trades"""
    if not _authorized(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("الاستخدام: `/set_max_trades 3`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        val = int(ctx.args[0])
        assert 1 <= val <= 10
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ القيمة يجب أن تكون بين 1 و 10.")
        return
    auto: AutoTradeEngine = _get_auto_engine(ctx)
    auto.settings.max_open_trades = val
    await upsert_auto_settings(auto._settings_dict())
    await update.message.reply_text(f"✅ حد الصفقات المفتوحة: `{val}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_set_scan_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_scan_interval 30  — set scan interval in minutes"""
    if not _authorized(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("الاستخدام: `/set_scan_interval 30` (بالدقائق)", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        val = int(ctx.args[0])
        assert 5 <= val <= 1440
    except (ValueError, AssertionError):
        await update.message.reply_text("❌ القيمة يجب أن تكون بين 5 و 1440 دقيقة.")
        return
    auto: AutoTradeEngine = _get_auto_engine(ctx)
    auto.settings.scan_interval_min = val
    await upsert_auto_settings(auto._settings_dict())
    await update.message.reply_text(
        f"✅ فترة المسح: كل `{val}` دقيقة\n"
        f"_(سيُطبَّق في الدورة القادمة)_",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Registration ───────────────────────────────────────────────────────────────

def register_super_handlers(
    app: Application,
    engine: ConsensusEngine,
    auto_engine: Optional[AutoTradeEngine] = None,
) -> None:
    """Add all SuperConsensus commands to an existing Application."""
    app.bot_data["super_engine"] = engine
    if auto_engine is not None:
        app.bot_data["auto_engine"] = auto_engine

    # Manual consensus bot commands
    app.add_handler(CommandHandler("super_menu",   cmd_super_menu))
    app.add_handler(CommandHandler("start_super",  cmd_start_super))
    app.add_handler(CommandHandler("stop_super",   cmd_stop_super))
    app.add_handler(CommandHandler("status_super", cmd_status_super))
    app.add_handler(CommandHandler("signals",      cmd_signals))
    app.add_handler(CommandHandler("giants",       cmd_giants))
    app.add_handler(CommandHandler("list_super",   cmd_list_super))
    app.add_handler(CommandHandler("pause_super",  cmd_pause_super))
    app.add_handler(CommandHandler("resume_super", cmd_resume_super))
    app.add_handler(CommandHandler("scan",         cmd_scan))

    # Auto-trade mode commands
    app.add_handler(CommandHandler("auto_mode",         cmd_auto_mode))
    app.add_handler(CommandHandler("auto_mode_risk",    cmd_auto_mode_risk))
    app.add_handler(CommandHandler("auto_off",          cmd_auto_off))
    app.add_handler(CommandHandler("auto_status",       cmd_auto_status))
    app.add_handler(CommandHandler("auto_history",      cmd_auto_history))
    app.add_handler(CommandHandler("set_risk",          cmd_set_risk))
    app.add_handler(CommandHandler("set_profit",        cmd_set_profit))
    app.add_handler(CommandHandler("set_max_trades",    cmd_set_max_trades))
    app.add_handler(CommandHandler("set_scan_interval", cmd_set_scan_interval))

    app.add_handler(CallbackQueryHandler(handle_super_callback, pattern=r"^super:"))

    logger.info("SuperConsensus handlers registered (AI-powered + SmartScanner + AutoTrade)")


# ── Application factory ────────────────────────────────────────────────────────

def build_super_application(
    engine: ConsensusEngine,
    client,
    auto_engine: Optional[AutoTradeEngine] = None,
) -> Application:
    """Build and return the PTB Application with all handlers registered."""
    from config.settings import TELEGRAM_BOT_TOKEN
    from telegram.ext import ApplicationBuilder

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    register_super_handlers(app, engine, auto_engine=auto_engine)
    return app


# ── Notification helper ────────────────────────────────────────────────────────

async def send_super_notification(
    text: str,
    application: Optional[Application] = None,
) -> None:
    """Send a Markdown message to TELEGRAM_CHAT_ID via the bot."""
    from config.settings import TELEGRAM_CHAT_ID

    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID not set — notification skipped")
        return
    if application is None:
        logger.warning("send_super_notification: no application — skipped")
        return
    try:
        await application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.error("send_super_notification failed: %s", exc)
