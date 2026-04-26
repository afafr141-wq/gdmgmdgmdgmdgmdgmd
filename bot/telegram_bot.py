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

# Default quick-select pairs — editable via /addpair and /removepair commands
# or by changing bot_config key "popular_pairs" in Supabase directly.
DEFAULT_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

# In-memory cache; refreshed from DB on each menu open
_popular_pairs: list[str] = list(DEFAULT_PAIRS)


async def _load_popular_pairs() -> list[str]:
    """Load pairs from bot_config table; fall back to DEFAULT_PAIRS."""
    global _popular_pairs
    try:
        from utils import db_manager as db
        raw = await db.get_config("popular_pairs", "")
        if raw:
            _popular_pairs = [p.strip() for p in raw.split(",") if p.strip()]
        else:
            _popular_pairs = list(DEFAULT_PAIRS)
    except Exception:
        _popular_pairs = list(DEFAULT_PAIRS)
    return _popular_pairs


async def _save_popular_pairs(pairs: list[str]) -> None:
    from utils import db_manager as db
    await db.set_config("popular_pairs", ",".join(pairs))


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


def _pair_kb(pairs: list[str] | None = None) -> InlineKeyboardMarkup:
    pairs = pairs or _popular_pairs
    # Two pairs per row for compact display
    rows = []
    for i in range(0, len(pairs), 2):
        row = [InlineKeyboardButton(pairs[i], callback_data=f"pair_{pairs[i]}")]
        if i + 1 < len(pairs):
            row.append(InlineKeyboardButton(pairs[i + 1], callback_data=f"pair_{pairs[i + 1]}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔍 زوج آخر", callback_data="pair_custom")])
    rows.append([InlineKeyboardButton("⚙️ إدارة الأزواج", callback_data="manage_pairs"),
                 InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def _risk_kb(symbol: str) -> InlineKeyboardMarkup:
    labels = {"low": "🟢 منخفض", "medium": "🟡 متوسط", "high": "🔴 مرتفع"}
    rows = [[InlineKeyboardButton(labels[r], callback_data=f"risk_{symbol}_{r}")] for r in RISK_LEVELS]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_start")])
    return InlineKeyboardMarkup(rows)


def _active_grid_kb(symbol: str) -> InlineKeyboardMarkup:
    muted = symbol in _muted_symbols
    mute_label = "🔔 تفعيل الإشعارات" if muted else "🔕 كتم الإشعارات"
    mute_cb    = f"unmute_{symbol}" if muted else f"mute_{symbol}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 تفاصيل الربح", callback_data=f"detail_{symbol}"),
         InlineKeyboardButton("🔄 إعادة تشكيل", callback_data=f"rebuild_{symbol}")],
        [InlineKeyboardButton(mute_label, callback_data=mute_cb)],
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
        f"📊 *{_fmt_symbol(r['symbol'])}* — تقرير الشبكة\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 الاستثمار: `${r['total_investment']:.2f}`\n"
        f"📐 النطاق: `{r['lower']:.4f}` — `{r['upper']:.4f}`\n"
        f"🔢 عدد الشبكات: `{r['grid_count']}`\n"
        f"📏 فارق الشبكة: `{r['grid_spacing']:.4f}`\n"
        f"💲 ميزانية كل شبكة: `{r.get('qty_per_grid_usdt', 0):.2f}` USDT\n"
        f"🏦 الحد الأدنى MEXC: `{r.get('min_order_cost', 1):.2f}` USDT\n"
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
    from super_consensus.bot.menu_bot import _kb_main
    auto = ctx.bot_data.get("auto_engine")
    status = "🟢 مفعّل" if auto and auto.is_active() else "🔴 موقوف"
    open_c = len(auto.open_positions()) if auto else 0
    await update.message.reply_text(
        "🤖 *AI Trading Bot — MEXC*\n\n"
        f"الوضع الآلي: {status}\n"
        f"صفقات مفتوحة: `{open_c}`\n\n"
        "اختر من القائمة:",
        parse_mode="Markdown",
        reply_markup=_kb_main(),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    await update.message.reply_text(
        "📖 *المساعدة*\n\n"
        "*تداول:*\n"
        "`/start_ai BTCUSDT 500 medium` — بدء شبكة ذكية\n"
        "`/status BTCUSDT` — تقرير الأرباح\n"
        "`/stop BTCUSDT` — إيقاف وبيع\n"
        "`/list` — الأزواج النشطة\n\n"
        "*إدارة الأزواج السريعة:*\n"
        "`/pairs` — عرض القائمة الحالية\n"
        "`/addpair ADAUSDT` — إضافة زوج للقائمة\n"
        "`/removepair ADAUSDT` — حذف زوج من القائمة\n\n"
        "*ملاحظة:* أي زوج يدعمه MEXC يعمل مع `/start_ai`\nمثال: `ADAUSDT`, `DOGEUSDT`, `MATICUSDT`\n\n"
        "*مستويات المخاطرة:*\n"
        "🟢 `low` | 🟡 `medium` | 🔴 `high`\n\n"
        "*صيانة:*\n"
        "`/upgrade` — إعادة بناء جميع الشبكات بالسعر الحالي\n"
        "`/upgrade BTCUSDT` — إعادة بناء شبكة محددة\n"
        "`/mute [SYMBOL]` — كتم الإشعارات\n"
        "`/unmute [SYMBOL]` — تفعيل الإشعارات",
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


async def cmd_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current quick-select pairs list."""
    if not _is_allowed(update):
        return await _deny(update)
    pairs = await _load_popular_pairs()
    text = "📋 *الأزواج المحفوظة في القائمة السريعة:*\n" + "\n".join(f"• `{p}`" for p in pairs)
    text += "\n\n`/addpair BTC/USDT` — إضافة زوج\n`/removepair BTC/USDT` — حذف زوج"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_addpair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a pair to the quick-select list: /addpair SOLUSDT"""
    if not _is_allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("❌ الاستخدام: `/addpair SYMBOL`\nمثال: `/addpair SOLUSDT`", parse_mode="Markdown")
        return
    symbol = _normalize_symbol(ctx.args[0])
    # Validate on MEXC
    try:
        await _client.get_current_price(symbol)
    except Exception as exc:
        await update.message.reply_text(f"❌ الزوج `{symbol}` غير موجود على MEXC:\n`{exc}`", parse_mode="Markdown")
        return
    pairs = await _load_popular_pairs()
    if symbol in pairs:
        await update.message.reply_text(f"⚠️ `{symbol}` موجود بالفعل في القائمة.", parse_mode="Markdown")
        return
    pairs.append(symbol)
    await _save_popular_pairs(pairs)
    await update.message.reply_text(f"✅ تمت إضافة `{symbol}` إلى القائمة السريعة.", parse_mode="Markdown")


async def cmd_removepair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a pair from the quick-select list: /removepair SOLUSDT"""
    if not _is_allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text("❌ الاستخدام: `/removepair SYMBOL`", parse_mode="Markdown")
        return
    symbol = _normalize_symbol(ctx.args[0])
    pairs = await _load_popular_pairs()
    if symbol not in pairs:
        await update.message.reply_text(f"⚠️ `{symbol}` غير موجود في القائمة.", parse_mode="Markdown")
        return
    pairs.remove(symbol)
    await _save_popular_pairs(pairs)
    await update.message.reply_text(f"✅ تم حذف `{symbol}` من القائمة السريعة.", parse_mode="Markdown")


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
        from super_consensus.bot.menu_bot import _kb_main
        auto = ctx.bot_data.get("auto_engine")
        status = "🟢 مفعّل" if auto and auto.is_active() else "🔴 موقوف"
        open_c = len(auto.open_positions()) if auto else 0
        await query.edit_message_text(
            "🤖 *AI Trading Bot — MEXC*\n\n"
            f"الوضع الآلي: {status}\n"
            f"صفقات مفتوحة: `{open_c}`\n\n"
            "اختر من القائمة:",
            parse_mode="Markdown",
            reply_markup=_kb_main(),
        )

    elif data == "pair_custom":
        ctx.user_data["awaiting"] = "custom_symbol"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_start")]])
        await query.edit_message_text(
            "🔍 *إدخال زوج مخصص*\n\nاكتب رمز الزوج:\n"
            "مثال: `ADAUSDT` أو `ADA/USDT` أو `DOGEUSDT`\n\n"
            "أي زوج يدعمه MEXC يعمل.",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif data == "manage_pairs":
        pairs = await _load_popular_pairs()
        text = "⚙️ *إدارة الأزواج السريعة*\n\n"
        text += "\n".join(f"`{p}`" for p in pairs)
        text += "\n\n*لإضافة زوج:* `/addpair SYMBOL`\n*لحذف زوج:* `/removepair SYMBOL`\n*لعرض القائمة:* `/pairs`"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_start")]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

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
            price = await _client.get_current_price(symbol)
            await _engine._rebuild(state, price)
            p = state.params
            await query.edit_message_text(
                f"✅ *{symbol}* — تمت إعادة التشكيل!\n"
                f"📐 النطاق الجديد: `{p.lower:.4f}` — `{p.upper:.4f}`\n"
                f"🔢 عدد الأوامر: `{p.grid_count}` ({p.grid_count // 2} شراء + {p.grid_count // 2} بيع)",
                parse_mode="Markdown",
                reply_markup=_active_grid_kb(symbol),
            )
        except Exception as exc:
            await query.edit_message_text(f"❌ خطأ: {exc}")

    elif data.startswith("mute_"):
        symbol = data[5:]
        _muted_symbols.add(symbol)
        await query.answer("🔕 تم كتم الإشعارات", show_alert=False)
        await query.edit_message_reply_markup(reply_markup=_active_grid_kb(symbol))

    elif data.startswith("unmute_"):
        symbol = data[7:]
        _muted_symbols.discard(symbol)
        await query.answer("🔔 تم تفعيل الإشعارات", show_alert=False)
        await query.edit_message_reply_markup(reply_markup=_active_grid_kb(symbol))

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
    awaiting = ctx.user_data.get("awaiting")

    # ── Custom symbol input ────────────────────────────────────────────────────
    if awaiting == "custom_symbol":
        symbol = _normalize_symbol(update.message.text.strip())
        try:
            price = await _client.get_current_price(symbol)
        except Exception as exc:
            await update.message.reply_text(
                f"❌ الزوج `{symbol}` غير موجود على MEXC:\n`{exc}`\n\nحاول مرة أخرى:",
                parse_mode="Markdown",
            )
            return
        ctx.user_data["pending_symbol"] = symbol
        ctx.user_data["awaiting"] = "amount"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_start")]])
        await update.message.reply_text(
            f"✅ *{symbol}* — السعر الحالي: `{price:.4f}`\n\nأدخل المبلغ بالـ USDT:\n(مثال: `500`)",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    # ── Amount input ───────────────────────────────────────────────────────────
    if awaiting != "amount":
        return
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ أدخل رقماً صحيحاً للمبلغ.")
        return
    symbol = ctx.user_data.get("pending_symbol", "BTC/USDT")
    ctx.user_data["pending_amount"] = amount
    ctx.user_data["awaiting"] = None
    await update.message.reply_text(
        f"⚠️ *{symbol}* — اختر مستوى المخاطرة:",
        parse_mode="Markdown",
        reply_markup=_risk_kb(symbol),
    )


# ── Notification system ────────────────────────────────────────────────────────

# Global application reference — set by build_application()
_application = None

# Muted symbols: notifications suppressed per-symbol
_muted_symbols: set[str] = set()

# Dedup cache: (symbol, event_type, key) → last sent timestamp
import time as _time
_notif_cache: dict[tuple, float] = {}
_NOTIF_DEDUP_SECONDS = 5  # ignore duplicate events within this window


def _is_muted(symbol: str) -> bool:
    return symbol in _muted_symbols or "ALL" in _muted_symbols


def _dedup_key(symbol: str, event: str, key: str = "") -> bool:
    """Return True if this event was already sent recently (dedup)."""
    cache_key = (symbol, event, key)
    now = _time.monotonic()
    last = _notif_cache.get(cache_key, 0)
    if now - last < _NOTIF_DEDUP_SECONDS:
        return True
    _notif_cache[cache_key] = now
    return False


async def _send(text: str) -> None:
    """Low-level fire-and-forget send. Never raises."""
    if not _application or not TELEGRAM_CHAT_ID:
        logger.info("NOTIFY: %s", text[:120])
        return
    try:
        await _application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.error("send failed: %s", exc)


def _now_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%H:%M:%S %d-%m-%Y")


def _fmt_symbol(symbol: str) -> str:
    """Normalise exchange symbol to display format: BTCUSDT → BTC/USDT."""
    symbol = symbol.replace("/", "").upper()
    for quote in ("USDT", "USDC", "BTC", "ETH", "BNB"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            base = symbol[: -len(quote)]
            return f"{base}/{quote}"
    return symbol


def _fmt_pnl(value: float) -> str:
    """Format a USDT profit/loss value to 2 decimal places."""
    return f"{value:.2f}"


# ── Public notification functions (called from grid_engine) ────────────────────

async def notify_buy_filled(
    symbol: str,
    price: float,
    qty: float,
    grid_level: int = 0,
    grid_total: int = 0,
) -> None:
    if _is_muted(symbol):
        return
    if _dedup_key(symbol, "buy", f"{price:.4f}"):
        return
    value = price * qty
    grid_info = f"\n🔢 الشبكة: `{grid_level}` من `{grid_total}`" if grid_total else ""
    await _send(
        f"📥 *تنفيذ شراء*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 الزوج: `{_fmt_symbol(symbol)}`\n"
        f"💵 السعر: `{price:,.4f}` USDT\n"
        f"🪙 الكمية: `{qty:.6f}`\n"
        f"💰 القيمة: `{value:.2f}` USDT"
        f"{grid_info}\n"
        f"🕐 الوقت: `{_now_str()}`"
    )


async def notify_sell_filled(
    symbol: str,
    price: float,
    qty: float,
    pnl: float,
) -> None:
    if _is_muted(symbol):
        return
    if _dedup_key(symbol, "sell", f"{price:.4f}"):
        return
    value = price * qty
    pnl_sign = "+" if pnl >= 0 else ""
    await _send(
        f"📤 *تنفيذ بيع*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 الزوج: `{_fmt_symbol(symbol)}`\n"
        f"💵 السعر: `{price:,.4f}` USDT\n"
        f"🪙 الكمية: `{qty:.6f}`\n"
        f"💰 القيمة: `{value:.2f}` USDT\n"
        f"💹 الربح: `{pnl_sign}{_fmt_pnl(pnl)}` USDT\n"
        f"🕐 الوقت: `{_now_str()}`"
    )


async def notify_grid_rebuild(
    symbol: str,
    reason: str,
    old_price: float,
    new_price: float,
    new_lower: float,
    new_upper: float,
    new_grid_count: int,
    new_atr: float,   # kept for signature compatibility, not displayed
) -> None:
    if _is_muted(symbol):
        return
    await _send(
        f"🔄 *نقل الشبكة — إعادة التمركز*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 الزوج: `{_fmt_symbol(symbol)}`\n"
        f"📌 السبب: {reason}\n"
        f"📉 السعر عند الخروج: `{old_price:,.4f}`\n"
        f"📐 النطاق الجديد: `{new_lower:,.4f}` ←→ `{new_upper:,.4f}`\n"
        f"🔢 الأوامر: `{new_grid_count // 2}` شراء + `{new_grid_count // 2}` بيع\n"
        f"🕐 الوقت: `{_now_str()}`"
    )


async def notify_grid_expansion(
    symbol: str,
    direction: str,
    new_price: float,
    order_side: str,
) -> None:
    if _is_muted(symbol):
        return
    if _dedup_key(symbol, "expand", f"{direction}{new_price:.4f}"):
        return
    dir_ar  = "⬆️ أعلى" if direction == "up" else "⬇️ أسفل"
    side_ar = "شراء 🟢" if order_side == "buy" else "بيع 🔴"
    await _send(
        f"➕ *توسيع الشبكة*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 الزوج: `{_fmt_symbol(symbol)}`\n"
        f"🧭 الاتجاه: {dir_ar}\n"
        f"💵 السعر الجديد: `{new_price:,.4f}`\n"
        f"📋 نوع الأمر: {side_ar}"
    )


async def notify_error(
    symbol: str,
    error_type: str,
    details: str,
) -> None:
    if _dedup_key(symbol, "error", error_type):
        return
    await _send(
        f"⚠️ *خطأ في البوت*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 الزوج: `{_fmt_symbol(symbol)}`\n"
        f"🔴 نوع الخطأ: {error_type}\n"
        f"📋 التفاصيل: {details}\n"
        f"🕐 الوقت: `{_now_str()}`"
    )


async def notify_hourly_report(symbol: str, report: dict) -> None:
    if _is_muted(symbol):
        return
    pnl_sign = "+" if report["total_profit"] >= 0 else ""
    await _send(
        f"📊 *تقرير ساعي — {_fmt_symbol(symbol)}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ صفقات بيع منفذة: `{report['sell_count']}`\n"
        f"💹 ربح الشبكة: `+{_fmt_pnl(report['grid_profit'])}` USDT\n"
        f"📈 أرباح غير محققة: `{_fmt_pnl(report['unrealised_pnl'])}` USDT\n"
        f"🏆 إجمالي الربح: `{pnl_sign}{_fmt_pnl(report['total_profit'])}` USDT\n"
        f"📊 APY: `{report['apy']:.2f}%`\n"
        f"🔓 أوامر مفتوحة: `{report['open_orders']}`"
    )


# ── send_notification (legacy / engine rebuild alerts) ─────────────────────────

async def send_notification(text: str, application=None) -> None:
    """Generic notification — used by engine for rebuild alerts."""
    global _application
    if application:
        _application = application
    await _send(text)


# ── Mute command ───────────────────────────────────────────────────────────────

async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/mute [SYMBOL|ALL] — suppress notifications for a symbol or all."""
    if not _is_allowed(update):
        return await _deny(update)
    target = (ctx.args[0].upper() if ctx.args else "ALL")
    if "/" not in target and target != "ALL":
        target = _normalize_symbol(target)
    _muted_symbols.add(target)
    await update.message.reply_text(
        f"🔕 الإشعارات مكتومة لـ `{target}`.\nلإعادة التفعيل: `/unmute {target}`",
        parse_mode="Markdown",
    )


async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/unmute [SYMBOL|ALL] — re-enable notifications."""
    if not _is_allowed(update):
        return await _deny(update)
    target = (ctx.args[0].upper() if ctx.args else "ALL")
    if "/" not in target and target != "ALL":
        target = _normalize_symbol(target)
    _muted_symbols.discard(target)
    await update.message.reply_text(
        f"🔔 الإشعارات مفعّلة لـ `{target}`.",
        parse_mode="Markdown",
    )


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/balance [SYMBOL] — show budget vs actual holdings for one or all grids."""
    if not _is_allowed(update):
        return await _deny(update)

    symbols = (
        [_normalize_symbol(ctx.args[0])]
        if ctx.args
        else _engine.active_symbols()
    )

    if not symbols:
        await update.message.reply_text("📭 لا توجد شبكات نشطة.")
        return

    lines = ["💼 *ميزان المحافظ*\n━━━━━━━━━━━━━━━━━━━━"]
    for sym in symbols:
        state = _engine.get_state(sym)
        if not state:
            continue
        try:
            price        = await _client.get_current_price(sym)
            allocated    = state.total_investment
            held_value   = state.held_qty * price
            max_holdings = allocated          # الحد الأقصى = كامل الاستثمار
            diff         = allocated - held_value
            pct          = (held_value / allocated * 100) if allocated else 0
            status_icon  = "✅" if held_value <= allocated else "🚨"
            lines.append(
                f"\n{status_icon} *{_fmt_symbol(sym)}*\n"
                f"💰 المخصص: `{allocated:.2f}` USDT\n"
                f"📦 القيمة الفعلية: `{held_value:.2f}` USDT ({pct:.1f}%)\n"
                f"📊 الفرق: `{diff:+.2f}` USDT\n"
                f"🪙 الكمية: `{state.held_qty:.4f}` | الحد الأقصى: `{max_holdings/price:.4f}`"
            )
        except Exception as exc:
            lines.append(f"\n⚠️ *{_fmt_symbol(sym)}*: خطأ — {exc}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/upgrade [SYMBOL] — rebuild one or all active grids at current price/ATR."""
    if not _is_allowed(update):
        return await _deny(update)

    symbols = (
        [_normalize_symbol(ctx.args[0])]
        if ctx.args
        else _engine.active_symbols()
    )

    if not symbols:
        await update.message.reply_text("📭 لا توجد شبكات نشطة حالياً.")
        return

    await update.message.reply_text(
        f"⏳ جاري ترقية {len(symbols)} شبكة…",
        parse_mode="Markdown",
    )

    ok, failed = [], []
    for sym in symbols:
        try:
            await _engine.upgrade_grid(sym)
            ok.append(sym)
        except Exception as exc:
            logger.error("upgrade_grid %s failed: %s", sym, exc)
            failed.append(f"`{_fmt_symbol(sym)}`: {exc}")

    lines = []
    if ok:
        lines.append("✅ *تمت الترقية:*\n" + "\n".join(f"• `{_fmt_symbol(s)}`" for s in ok))
    if failed:
        lines.append("❌ *فشلت:*\n" + "\n".join(f"• {e}" for e in failed))

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


def build_application(engine, client, super_engine=None) -> Application:
    global _application
    set_engine(engine, client)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    _application = app

    # ── Grid bot handlers ──────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start_ai", cmd_start_ai))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("addpair", cmd_addpair))
    app.add_handler(CommandHandler("removepair", cmd_removepair))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CommandHandler("balance", cmd_balance))

    # ── SuperConsensus handlers (registered if engine provided) ───────────────
    if super_engine is not None:
        from super_consensus.bot.super_bot import register_super_handlers
        register_super_handlers(app, super_engine)

    # ── Interactive menu handlers (before catch-all) ───────────────────────────
    from super_consensus.bot.menu_bot import register_menu_handlers
    register_menu_handlers(app)

    # ── Catch-all: only legacy grid/super callbacks (new menu patterns handled above)
    app.add_handler(CallbackQueryHandler(
        handle_callback,
        pattern=r"^(?!menu:|auto:|set:|profit:|stop:|grid:|gridrisk:|gridstop:|super:)",
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
