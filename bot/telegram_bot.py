"""
Telegram bot: commands + interactive inline-keyboard menus.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALLOWED_USER_IDS

logger = logging.getLogger(__name__)

POPULAR_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]


def _normalize_symbol(symbol: str) -> str:
    """Convert BTCUSDT → BTC/USDT for ccxt compatibility."""
    symbol = symbol.upper().replace("-", "").replace("_", "")
    if "/" not in symbol:
        # Try common quote currencies
        for quote in ("USDT", "USDC", "BTC", "ETH", "BNB"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                base = symbol[: -len(quote)]
                return f"{base}/{quote}"
    return symbol
RISK_LEVELS = ["low", "medium", "high"]

_engine = None
_client = None


def set_engine(engine, client) -> None:
    global _engine, _client
    _engine = engine
    _client = client


def _is_allowed(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
        return False
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return False
    return True


async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text("⛔ Unauthorized.")
    elif update.callback_query:
        await update.callback_query.answer("⛔ Unauthorized.", show_alert=True)


def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 بدء شبكة جديدة", callback_data="menu_start")],
        [InlineKeyboardButton("📊 حالة البوتات", callback_data="menu_status"),
         InlineKeyboardButton("📋 قائمة الأزواج", callback_data="menu_list")],
        [InlineKeyboardButton("⛔ إيقاف الكل", callback_data="menu_stopall"),
         InlineKeyboardButton("⚙️ إعدادات", callback_data="menu_settings")],
        [InlineKeyboardButton("📈 تقارير الأرباح", callback_data="menu_reports")],
    ])


def _pair_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(p, callback_data=f"pair_{p}")] for p in POPULAR_PAIRS]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _risk_kb(symbol: str) -> InlineKeyboardMarkup:
    labels = {"low": "🟢 منخفض", "medium": "🟡 متوسط", "high": "🔴 مرتفع"}
    rows = [[InlineKeyboardButton(labels[r], callback_data=f"risk_{symbol}_{r}")] for r in RISK_LEVELS]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_start")])
    return InlineKeyboardMarkup(rows)


def _active_grid_kb(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 تفاصيل الربح", callback_data=f"detail_{symbol}"),
         InlineKeyboardButton("🔄 إعادة تشكيل", callback_data=f"rebuild_{symbol}")],
        [InlineKeyboardButton("⛔ إيقاف", callback_data=f"stop_{symbol}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_list")],
    ])


def _confirm_stop_kb(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، أوقف وبيع", callback_data=f"confirmstop_{symbol}"),
         InlineKeyboardButton("❌ إلغاء", callback_data=f"detail_{symbol}")],
    ])


def _reports_kb(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 يومي", callback_data=f"report_1_{symbol}"),
         InlineKeyboardButton("📅 أسبوعي", callback_data=f"report_7_{symbol}"),
         InlineKeyboardButton("📅 شهري", callback_data=f"report_30_{symbol}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data=f"detail_{symbol}")],
    ])


def _fmt_report(r: dict) -> str:
    return (
        f"📊 *{r['symbol']}* — تقرير الشبكة\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 الاستثمار: `${r['total_investment']:.2f}`\n"
        f"📐 النطاق: `{r['lower']:.4f}` — `{r['upper']:.4f}`\n"
        f"🔢 عدد الشبكات: `{r['grid_count']}`\n"
        f"📏 فارق الشبكة: `{r['grid_spacing']:.4f}`\n"
        f"📡 ATR: `{r['atr']:.4f}`\n"
        f"💵 السعر الحالي: `{r['current_price']:.4f}`\n"
        f"📦 متوسط سعر الشراء: `{r['avg_buy_price']:.4f}`\n"
        f"🪙 الكمية المحتجزة: `{r['held_qty']:.6f}`\n"
        f"✅ أوامر بيع منفذة: `{r['sell_count']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 أوامر شراء نشطة: `{r.get('active_buys', 0)}`\n"
        f"🔴 أوامر بيع نشطة: `{r.get('active_sells', 0)}`\n"
        f"🔓 إجمالي الأوامر المفتوحة: `{r['open_orders']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💹 ربح الشبكة: `${r['grid_profit']:.4f}`\n"
        f"📈 أرباح غير محققة: `${r['unrealised_pnl']:.4f}`\n"
        f"✅ أرباح محققة: `${r['realized_pnl']:.4f}`\n"
        f"🏆 إجمالي الربح: `${r['total_profit']:.4f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 أيام التشغيل: `{r['days_running']:.1f}`\n"
        f"📊 APY الكلي: `{r['apy']:.2f}%`\n"
        f"📊 APY الشبكة: `{r['grid_apy']:.2f}%`\n"
        f"⚠️ المخاطرة: `{r['risk']}`"
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    await update.message.reply_text(
        "🤖 *AI Grid Bot — MEXC*\nاختر من القائمة:",
        parse_mode="Markdown",
        reply_markup=_main_menu_kb(),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    await update.message.reply_text(
        "📖 *المساعدة*\n\n"
        "`/start_ai BTCUSDT 500 medium` — بدء شبكة ذكية\n"
        "`/status BTCUSDT` — تقرير الأرباح\n"
        "`/stop BTCUSDT` — إيقاف وبيع\n"
        "`/list` — الأزواج النشطة\n"
        "`/help` — هذه الرسالة\n\n"
        "*مستويات المخاطرة:*\n"
        "🟢 `low` | 🟡 `medium` | 🔴 `high`",
        parse_mode="Markdown",
    )


async def cmd_start_ai(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    args = ctx.args
    if len(args) < 3:
        await update.message.reply_text(
            "❌ الاستخدام: `/start_ai SYMBOL AMOUNT RISK`\nمثال: `/start_ai BTCUSDT 500 medium`",
            parse_mode="Markdown",
        )
        return
    symbol, amount_str, risk = _normalize_symbol(args[0]), args[1], args[2].lower()
    if risk not in RISK_LEVELS:
        await update.message.reply_text(f"❌ المخاطرة يجب أن تكون: {', '.join(RISK_LEVELS)}")
        return
    try:
        amount = float(amount_str)
    except ValueError:
        await update.message.reply_text("❌ المبلغ غير صحيح.")
        return
    await _launch_grid(update, symbol, amount, risk)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("❌ الاستخدام: `/status SYMBOL`", parse_mode="Markdown")
        return
    await _send_status(update, _normalize_symbol(ctx.args[0]))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("❌ الاستخدام: `/stop SYMBOL`", parse_mode="Markdown")
        return
    await _do_stop(update, _normalize_symbol(ctx.args[0]))


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    await _send_list(update)


async def _launch_grid(update: Update, symbol: str, amount: float, risk: str) -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)

    # ── Pre-flight checks ──────────────────────────────────────────────────────
    # 1. Validate symbol exists on MEXC
    try:
        await _client.load_markets()
        price = await _client.get_current_price(symbol)
    except Exception as exc:
        logger.exception("pre-flight price fetch failed for %s: %s", symbol, exc)
        await msg.reply_text(
            f"❌ *تعذّر الاتصال بـ MEXC*\n`{type(exc).__name__}: {exc}`\n\n"
            "تحقق من:\n• صحة `MEXC_API_KEY` و `MEXC_API_SECRET`\n• أن الزوج `{symbol}` موجود على MEXC",
            parse_mode="Markdown",
        )
        return

    # 2. Check USDT balance
    try:
        usdt_balance = await _client.get_balance("USDT")
    except Exception as exc:
        logger.exception("balance fetch failed: %s", exc)
        usdt_balance = 0.0

    if usdt_balance < amount:
        await msg.reply_text(
            f"❌ *رصيد غير كافٍ*\n"
            f"المطلوب: `${amount:.2f}` USDT\n"
            f"المتاح: `${usdt_balance:.2f}` USDT",
            parse_mode="Markdown",
        )
        return

    await msg.reply_text(
        f"⏳ جاري بدء شبكة *{symbol}* بمبلغ `${amount}` ومخاطرة `{risk}`…\n"
        f"💵 السعر الحالي: `{price:.4f}`\n"
        f"💰 الرصيد المتاح: `${usdt_balance:.2f}` USDT",
        parse_mode="Markdown",
    )

    try:
        state = await _engine.start(symbol, amount, risk)
        p = state.params
        await msg.reply_text(
            f"✅ *شبكة {symbol} تعمل الآن!*\n"
            f"📐 النطاق: `{p.lower:.4f}` — `{p.upper:.4f}`\n"
            f"🔢 عدد الشبكات: `{p.grid_count}`\n"
            f"📏 فارق الشبكة: `{p.grid_spacing:.4f}`\n"
            f"📡 ATR: `{p.atr:.4f}`\n"
            f"⚠️ المخاطرة: `{risk}`",
            parse_mode="Markdown",
            reply_markup=_active_grid_kb(symbol),
        )
    except ValueError as exc:
        logger.warning("start grid ValueError for %s: %s", symbol, exc)
        await msg.reply_text(f"⚠️ {exc}")
    except Exception as exc:
        logger.exception("start grid error for %s: %s", symbol, exc)
        await msg.reply_text(
            f"❌ *خطأ أثناء بدء الشبكة*\n`{type(exc).__name__}: {exc}`",
            parse_mode="Markdown",
        )


async def _send_status(update: Update, symbol: str) -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    state = _engine.get_state(symbol)
    if not state:
        await msg.reply_text(f"❌ لا توجد شبكة نشطة لـ *{symbol}*", parse_mode="Markdown")
        return
    try:
        price = await _client.get_current_price(symbol)
        report = _engine.calc_profit_report(state, price)
        await msg.reply_text(_fmt_report(report), parse_mode="Markdown", reply_markup=_active_grid_kb(symbol))
    except Exception as exc:
        logger.exception("status error: %s", exc)
        await msg.reply_text(f"❌ خطأ: {exc}")


async def _send_list(update: Update) -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    symbols = _engine.active_symbols()
    if not symbols:
        await msg.reply_text("📭 لا توجد شبكات نشطة حالياً.")
        return
    rows = [[InlineKeyboardButton(f"📊 {s}", callback_data=f"detail_{s}")] for s in symbols]
    rows.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu_main")])
    await msg.reply_text(
        f"📋 *الأزواج النشطة ({len(symbols)}):*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _do_stop(update: Update, symbol: str) -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    await msg.reply_text(f"⏳ جاري إيقاف *{symbol}* وبيع الكميات…", parse_mode="Markdown")
    try:
        sell_value = await _engine.stop(symbol, market_sell=True)
        await msg.reply_text(
            f"✅ *{symbol}* تم إيقافه.\n💵 قيمة البيع: `${sell_value:.2f}`",
            parse_mode="Markdown",
        )
    except ValueError as exc:
        await msg.reply_text(f"⚠️ {exc}")
    except Exception as exc:
        logger.exception("stop error: %s", exc)
        await msg.reply_text(f"❌ خطأ: {exc}")


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return await _deny(update)
    data = query.data

    if data == "menu_main":
        await query.edit_message_text("🤖 *AI Grid Bot — MEXC*\nاختر من القائمة:", parse_mode="Markdown", reply_markup=_main_menu_kb())

    elif data == "menu_start":
        await query.edit_message_text("🚀 *بدء شبكة جديدة*\nاختر الزوج:", parse_mode="Markdown", reply_markup=_pair_kb())

    elif data in ("menu_status", "menu_list"):
        symbols = _engine.active_symbols()
        if not symbols:
            await query.edit_message_text("📭 لا توجد شبكات نشطة.", reply_markup=_main_menu_kb())
        else:
            rows = [[InlineKeyboardButton(f"📊 {s}", callback_data=f"detail_{s}")] for s in symbols]
            rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")])
            await query.edit_message_text(f"📋 *الأزواج النشطة ({len(symbols)}):*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))

    elif data == "menu_stopall":
        symbols = _engine.active_symbols()
        if not symbols:
            await query.edit_message_text("📭 لا توجد شبكات لإيقافها.", reply_markup=_main_menu_kb())
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"⛔ تأكيد إيقاف الكل ({len(symbols)} أزواج)", callback_data="confirmstopall")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="menu_main")],
            ])
            await query.edit_message_text(f"⚠️ هل تريد إيقاف *{len(symbols)}* شبكة وبيع كل الكميات؟", parse_mode="Markdown", reply_markup=kb)

    elif data == "confirmstopall":
        symbols = _engine.active_symbols()
        await query.edit_message_text("⏳ جاري إيقاف كل الشبكات…")
        results = []
        for sym in symbols:
            try:
                val = await _engine.stop(sym, market_sell=True)
                results.append(f"✅ {sym}: ${val:.2f}")
            except Exception as exc:
                results.append(f"❌ {sym}: {exc}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu_main")]])
        await query.edit_message_text("🏁 *تم إيقاف الكل:*\n" + "\n".join(results), parse_mode="Markdown", reply_markup=kb)

    elif data == "menu_settings":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")]])
        await query.edit_message_text("⚙️ *الإعدادات*\n\nمثال: `/start_ai BTCUSDT 500 medium`", parse_mode="Markdown", reply_markup=kb)

    elif data == "menu_reports":
        symbols = _engine.active_symbols()
        if not symbols:
            await query.edit_message_text("📭 لا توجد شبكات نشطة.", reply_markup=_main_menu_kb())
        else:
            rows = [[InlineKeyboardButton(f"📈 {s}", callback_data=f"reports_{s}")] for s in symbols]
            rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")])
            await query.edit_message_text("📈 *اختر الزوج لعرض تقاريره:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("pair_"):
        symbol = _normalize_symbol(data[5:])
        ctx.user_data["pending_symbol"] = symbol
        ctx.user_data["awaiting"] = "amount"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_start")]])
        await query.edit_message_text(f"💰 *{symbol}* — أدخل المبلغ بالـ USDT:\n(مثال: `500`)", parse_mode="Markdown", reply_markup=kb)

    elif data.startswith("risk_"):
        parts = data.split("_", 2)
        symbol, risk = parts[1], parts[2]
        amount = ctx.user_data.get("pending_amount", 100.0)
        await query.edit_message_text(f"⏳ جاري بدء شبكة *{symbol}* بمبلغ `${amount}` ومخاطرة `{risk}`…", parse_mode="Markdown")
        try:
            state = await _engine.start(symbol, amount, risk)
            p = state.params
            await query.edit_message_text(
                f"✅ *شبكة {symbol} تعمل الآن!*\n"
                f"📐 النطاق: `{p.lower:.4f}` — `{p.upper:.4f}`\n"
                f"🔢 عدد الشبكات: `{p.grid_count}`\n"
                f"📏 فارق الشبكة: `{p.grid_spacing:.4f}`\n"
                f"📡 ATR: `{p.atr:.4f}`\n"
                f"⚠️ المخاطرة: `{risk}`",
                parse_mode="Markdown",
                reply_markup=_active_grid_kb(symbol),
            )
        except Exception as exc:
            await query.edit_message_text(f"❌ خطأ: {exc}", reply_markup=_main_menu_kb())

    elif data.startswith("detail_"):
        symbol = data[7:]
        state = _engine.get_state(symbol)
        if not state:
            await query.edit_message_text(f"❌ لا توجد شبكة نشطة لـ *{symbol}*", parse_mode="Markdown", reply_markup=_main_menu_kb())
            return
        try:
            price = await _client.get_current_price(symbol)
            report = _engine.calc_profit_report(state, price)
            await query.edit_message_text(_fmt_report(report), parse_mode="Markdown", reply_markup=_active_grid_kb(symbol))
        except Exception as exc:
            await query.edit_message_text(f"❌ خطأ: {exc}")

    elif data.startswith("rebuild_"):
        symbol = data[8:]
        state = _engine.get_state(symbol)
        if not state:
            await query.edit_message_text(f"❌ لا توجد شبكة لـ {symbol}")
            return
        await query.edit_message_text(f"⏳ جاري إعادة تشكيل شبكة *{symbol}*…", parse_mode="Markdown")
        try:
            from core.grid_engine import compute_atr
            from config.settings import RISK_PROFILES, CANDLE_TIMEFRAME
            price = await _client.get_current_price(symbol)
            ohlcv = await _client.fetch_ohlcv(symbol, CANDLE_TIMEFRAME, limit=20)
            atr = compute_atr(ohlcv, RISK_PROFILES[state.risk]["atr_period"])
            await _engine._rebuild(state, price, atr)
            p = state.params
            await query.edit_message_text(
                f"✅ *{symbol}* — تمت إعادة التشكيل!\n"
                f"📐 النطاق الجديد: `{p.lower:.4f}` — `{p.upper:.4f}`\n"
                f"🔢 عدد الشبكات: `{p.grid_count}`\n"
                f"📡 ATR: `{p.atr:.4f}`",
                parse_mode="Markdown",
                reply_markup=_active_grid_kb(symbol),
            )
        except Exception as exc:
            await query.edit_message_text(f"❌ خطأ: {exc}")

    elif data.startswith("stop_"):
        symbol = data[5:]
        await query.edit_message_text(f"⚠️ هل تريد إيقاف *{symbol}* وبيع كل الكميات؟", parse_mode="Markdown", reply_markup=_confirm_stop_kb(symbol))

    elif data.startswith("confirmstop_"):
        symbol = data[12:]
        await query.edit_message_text(f"⏳ جاري إيقاف *{symbol}*…", parse_mode="Markdown")
        try:
            val = await _engine.stop(symbol, market_sell=True)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu_main")]])
            await query.edit_message_text(f"✅ *{symbol}* تم إيقافه.\n💵 قيمة البيع: `${val:.2f}`", parse_mode="Markdown", reply_markup=kb)
        except Exception as exc:
            await query.edit_message_text(f"❌ خطأ: {exc}")

    elif data.startswith("reports_"):
        symbol = data[8:]
        await query.edit_message_text(f"📈 *تقارير {symbol}* — اختر الفترة:", parse_mode="Markdown", reply_markup=_reports_kb(symbol))

    elif data.startswith("report_"):
        parts = data.split("_", 2)
        days, symbol = int(parts[1]), parts[2]
        from utils import db_manager as db
        trades = await db.get_trade_history(symbol, days)
        sells = [t for t in trades if t["side"] == "sell"]
        total_pnl = sum(float(t["pnl"]) for t in sells)
        label = {1: "اليوم", 7: "الأسبوع", 30: "الشهر"}.get(days, f"{days} يوم")
        await query.edit_message_text(
            f"📈 *{symbol}* — تقرير {label}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 عدد الصفقات: `{len(trades)}`\n"
            f"✅ صفقات بيع: `{len(sells)}`\n"
            f"💹 إجمالي الربح المحقق: `${total_pnl:.4f}`",
            parse_mode="Markdown",
            reply_markup=_reports_kb(symbol),
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    if ctx.user_data.get("awaiting") != "amount":
        return
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ أدخل رقماً صحيحاً للمبلغ.")
        return
    symbol = ctx.user_data.get("pending_symbol", "BTCUSDT")
    ctx.user_data["pending_amount"] = amount
    ctx.user_data["awaiting"] = None
    await update.message.reply_text(
        f"⚠️ *{symbol}* — اختر مستوى المخاطرة:",
        parse_mode="Markdown",
        reply_markup=_risk_kb(symbol),
    )


async def send_notification(text: str, application=None) -> None:
    if not application or not TELEGRAM_CHAT_ID:
        logger.info("NOTIFY: %s", text)
        return
    try:
        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as exc:
        logger.error("send_notification failed: %s", exc)


def build_application(engine, client) -> Application:
    set_engine(engine, client)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start_ai", cmd_start_ai))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
