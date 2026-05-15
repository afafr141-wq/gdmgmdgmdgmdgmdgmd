"""
Bridge: registers scalping bot handlers inside the grid bot's Application.

Callback prefix: "scalp:"
Handler groups:  callbacks=6, messages=11

Commands added:
  /scalp_scan              — scan market, show top picks, auto-start paper scalp
  /scalp_start SYMBOL      — start paper scalp on a specific symbol
  /scalp_stop  SYMBOL      — stop scalp loop
  /scalp_status            — show all running scalp bots
"""
from __future__ import annotations

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import core.scalp_engine as engine
import core.scalp_scanner as scanner

log = logging.getLogger(__name__)

_client = None   # injected from main.py via init_scalp(client)


def init_scalp(client) -> None:
    global _client
    _client = client
    engine.set_notifiers(
        entry=_notify_entry,
        exit_=_notify_exit,
        error=_notify_error,
    )
    log.info("Scalp bridge initialised")


# ── Notification senders ───────────────────────────────────────────────────────

_app_ref: dict = {}   # holds {"app": Application} set from main.py


def set_app(app: Application) -> None:
    _app_ref["app"] = app


async def _send(text: str) -> None:
    app = _app_ref.get("app")
    if not app:
        return
    from bot.telegram_bot import send_notification
    await send_notification(text, application=app)


async def _notify_entry(*, symbol, price, qty, capital, rsi, paper, **_) -> None:
    mode = "📄 ورقي" if paper else "💰 حقيقي"
    await _send(
        f"🟢 *دخول Scalp* — {mode}\n"
        f"─────────────────────────\n"
        f"🪙 العملة:  `{symbol}`\n"
        f"💵 السعر:   `{price:.6f}`\n"
        f"📦 الكمية:  `{qty:.6f}`\n"
        f"💼 رأس المال: `{capital:.2f} USDT`\n"
        f"📊 RSI:     `{rsi:.1f}`"
    )


async def _notify_exit(*, symbol, price, pnl, reason, paper, total_pnl, trade_count, **_) -> None:
    mode  = "📄 ورقي" if paper else "💰 حقيقي"
    icon  = "📈" if pnl >= 0 else "📉"
    await _send(
        f"{icon} *خروج Scalp* — {mode}\n"
        f"─────────────────────────\n"
        f"🪙 العملة:    `{symbol}`\n"
        f"💵 السعر:     `{price:.6f}`\n"
        f"💰 ربح/خسارة: `{pnl:+.4f} USDT`\n"
        f"📋 السبب:     `{reason}`\n"
        f"─────────────────────────\n"
        f"📊 إجمالي الصفقات: `{trade_count}`\n"
        f"💹 إجمالي الربح:   `{total_pnl:+.4f} USDT`"
    )


async def _notify_error(*, symbol, error, **_) -> None:
    await _send(f"❌ *خطأ Scalp* — `{symbol}`\n`{error[:200]}`")


# ── Report formatter ───────────────────────────────────────────────────────────

def _conf_bar(c: int) -> str:
    filled = round(c / 10)
    return "█" * filled + "░" * (10 - filled)


def _safe(text: str) -> str:
    return text.replace("_", " ").replace("*", "").replace("`", "")


def _build_scan_report(result: scanner.ScanResult) -> str:
    lines = [
        "🔍 *ماسح السوق الذكي — Scalp Scanner*",
        f"📊 عملات تم فحصها: `{result.coins_scanned}`",
        f"⏱️ وقت التحليل: `{result.scan_duration_s:.0f}` ثانية",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🏆 *أفضل الفرص*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for pick in result.final_picks:
        medal = medals[pick.rank - 1] if pick.rank <= len(medals) else f"{pick.rank}."
        sig_icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(pick.signal, "⚪")
        analysts_str = " & ".join(pick.analysts) if pick.analysts else "—"
        lines += [
            "",
            f"{medal} *{pick.name}* (`{pick.symbol}`)",
            f"  {sig_icon} الإشارة: `{pick.signal}` | الثقة: `{_conf_bar(pick.confidence)}` {pick.confidence}%",
            f"  💬 _{_safe(pick.reason)}_",
            f"  👥 اتفق عليها: `{analysts_str}`",
        ]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🧠 *تقارير المحللين*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for r in result.analyst_reports:
        if r.error:
            lines.append(f"  ⚠️ *{r.analyst_name}*: خطأ — `{r.error[:60]}`")
        else:
            picks_str = ", ".join(f"{p.symbol}({p.confidence}%)" for p in r.picks)
            lines.append(f"  🤖 *{r.analyst_name}*: {picks_str}")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ _هذا تحليل AI للمعلومات فقط — ليس نصيحة مالية._",
    ]
    return "\n".join(lines)


def _scalp_status_kb(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⛔ إيقاف", callback_data=f"scalp:stop:{symbol}"),
        InlineKeyboardButton("📊 حالة",  callback_data=f"scalp:status:{symbol}"),
    ]])


def _scan_result_kb(top_symbol: str | None) -> InlineKeyboardMarkup:
    rows = []
    if top_symbol:
        rows.append([InlineKeyboardButton(
            f"🚀 ابدأ Scalp على {top_symbol} (ورقي)",
            callback_data=f"scalp:autostart:{top_symbol}",
        )])
    rows.append([InlineKeyboardButton("🔄 مسح مجدداً", callback_data="scalp:rescan")])
    return InlineKeyboardMarkup(rows)


# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_scalp_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.telegram_bot import _is_allowed, _deny
    if not _is_allowed(update):
        return await _deny(update)

    wait = await update.message.reply_text("🔍 جاري مسح السوق… قد يستغرق 30-60 ثانية.")
    try:
        result = await scanner.scan()
        await wait.delete()
        report = _build_scan_report(result)
        # Split if too long for Telegram (4096 char limit)
        if len(report) > 4000:
            report = report[:4000] + "\n…"
        await update.message.reply_text(
            report,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_scan_result_kb(result.top_symbol),
        )
    except Exception as exc:
        await wait.delete()
        await update.message.reply_text(f"❌ خطأ في المسح: `{exc}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_scalp_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.telegram_bot import _is_allowed, _deny, _normalize_symbol
    if not _is_allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text(
            "❌ الاستخدام: `/scalp_start SYMBOL`\nمثال: `/scalp_start BTCUSDT`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    symbol = _normalize_symbol(ctx.args[0])
    capital = float(ctx.args[1]) if len(ctx.args) > 1 else 20.0
    await _do_start(update.message, symbol, capital)


async def cmd_scalp_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.telegram_bot import _is_allowed, _deny, _normalize_symbol
    if not _is_allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("❌ الاستخدام: `/scalp_stop SYMBOL`", parse_mode=ParseMode.MARKDOWN)
        return
    symbol = _normalize_symbol(ctx.args[0])
    await _do_stop(update.message, symbol)


async def cmd_scalp_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.telegram_bot import _is_allowed, _deny
    if not _is_allowed(update):
        return await _deny(update)
    symbols = engine.active_symbols()
    if not symbols:
        await update.message.reply_text("ℹ️ لا يوجد Scalp بوت يعمل حالياً.")
        return
    lines = ["📊 *بوتات Scalp النشطة:*", ""]
    for sym in symbols:
        s = engine.status(sym)
        if not s:
            continue
        mode = "📄 ورقي" if s["paper"] else "💰 حقيقي"
        pos  = f"✅ في صفقة @ `{s['entry_price']:.6f}`" if s["in_position"] else "⏳ ينتظر إشارة"
        lines += [
            f"🪙 *{sym}* — {mode}",
            f"  {pos}",
            f"  💹 ربح محقق: `{s['realized_pnl']:+.4f} USDT`",
            f"  🔢 صفقات: `{s['trade_count']}`",
            "",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Callback handler ───────────────────────────────────────────────────────────

async def _scalp_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.telegram_bot import _is_allowed, _deny, _normalize_symbol
    q = update.callback_query
    await q.answer()
    if not _is_allowed(update):
        return await _deny(update)

    data = q.data  # e.g. "scalp:stop:BTC/USDT"
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    symbol = parts[2] if len(parts) > 2 else ""

    if action == "menu":
        symbols = engine.active_symbols()
        active_text = (
            "\n".join(f"  ▸ `{s}`" for s in symbols)
            if symbols else "  _لا يوجد Scalp نشط حالياً_"
        )
        await q.message.reply_text(
            f"🎯 *Scalp Bot*\n"
            f"─────────────────────────\n"
            f"📡 النشط: {len(symbols)} بوت\n"
            f"{active_text}\n"
            f"─────────────────────────",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 مسح السوق (AI)", callback_data="scalp:rescan")],
                [InlineKeyboardButton("📊 حالة البوتات",   callback_data="scalp:statusall")],
                [InlineKeyboardButton("⛔ إيقاف الكل",     callback_data="scalp:stopall")],
            ]),
        )

    elif action == "statusall":
        symbols = engine.active_symbols()
        if not symbols:
            await q.message.reply_text("ℹ️ لا يوجد Scalp بوت يعمل حالياً.")
            return
        lines = ["📊 *بوتات Scalp النشطة:*", ""]
        for sym in symbols:
            s = engine.status(sym)
            if not s:
                continue
            pos = f"✅ في صفقة @ `{s['entry_price']:.6f}`" if s["in_position"] else "⏳ ينتظر إشارة"
            lines += [f"🪙 *{sym}*", f"  {pos}", f"  💹 ربح: `{s['realized_pnl']:+.4f} USDT`", ""]
        await q.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    elif action == "stopall":
        symbols = engine.active_symbols()
        if not symbols:
            await q.message.reply_text("ℹ️ لا يوجد شيء يعمل.")
            return
        for sym in list(symbols):
            await engine.stop(sym)
        await q.message.reply_text(f"⛔ تم إيقاف {len(symbols)} بوت.")

    elif action == "stop":
        await _do_stop(q.message, symbol)

    elif action == "status":
        s = engine.status(symbol)
        if not s:
            await q.message.reply_text(f"ℹ️ `{symbol}` غير نشط.", parse_mode=ParseMode.MARKDOWN)
            return
        mode = "📄 ورقي" if s["paper"] else "💰 حقيقي"
        pos  = f"✅ في صفقة @ `{s['entry_price']:.6f}`" if s["in_position"] else "⏳ ينتظر إشارة"
        await q.message.reply_text(
            f"📊 *Scalp {symbol}* — {mode}\n{pos}\n"
            f"💹 ربح: `{s['realized_pnl']:+.4f} USDT` | صفقات: `{s['trade_count']}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "autostart":
        await _do_start(q.message, _normalize_symbol(symbol), capital=20.0)

    elif action == "rescan":
        await q.message.reply_text("🔍 جاري إعادة المسح…")
        try:
            result = await scanner.scan()
            report = _build_scan_report(result)
            if len(report) > 4000:
                report = report[:4000] + "\n…"
            await q.message.reply_text(
                report,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_scan_result_kb(result.top_symbol),
            )
        except Exception as exc:
            await q.message.reply_text(f"❌ خطأ: `{exc}`", parse_mode=ParseMode.MARKDOWN)


# ── Shared helpers ─────────────────────────────────────────────────────────────

async def _do_start(msg, symbol: str, capital: float = 20.0) -> None:
    if not _client:
        await msg.reply_text("❌ الـ client غير جاهز.")
        return
    try:
        state = await engine.start(_client, symbol, capital_usdt=capital, paper=True)
        await msg.reply_text(
            f"✅ *Scalp بدأ* — 📄 ورقي\n"
            f"🪙 العملة: `{symbol}`\n"
            f"💼 رأس المال: `{capital:.2f} USDT`\n"
            f"⏱️ التايم فريم: `{state.timeframe}`\n"
            f"📋 سيبعت إشعار عند كل دخول وخروج.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_scalp_status_kb(symbol),
        )
    except ValueError as exc:
        await msg.reply_text(f"⚠️ {exc}")
    except Exception as exc:
        await msg.reply_text(f"❌ خطأ: `{exc}`", parse_mode=ParseMode.MARKDOWN)


async def _do_stop(msg, symbol: str) -> None:
    state = await engine.stop(symbol)
    if not state:
        await msg.reply_text(f"ℹ️ `{symbol}` لم يكن يعمل.", parse_mode=ParseMode.MARKDOWN)
        return
    await msg.reply_text(
        f"⛔ *Scalp متوقف* — `{symbol}`\n"
        f"💹 إجمالي الربح: `{state.realized_pnl:+.4f} USDT`\n"
        f"🔢 صفقات منفذة: `{state.trade_count}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Registration ───────────────────────────────────────────────────────────────

def register_scalp_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("scalp_scan",   cmd_scalp_scan),   group=6)
    app.add_handler(CommandHandler("scalp_start",  cmd_scalp_start),  group=6)
    app.add_handler(CommandHandler("scalp_stop",   cmd_scalp_stop),   group=6)
    app.add_handler(CommandHandler("scalp_status", cmd_scalp_status), group=6)
    app.add_handler(CallbackQueryHandler(_scalp_callback, pattern=r"^scalp:"), group=6)
    log.info("Scalp handlers registered")
