"""
Telegram bot interface — inline-button dashboard edition.

Public functions used by main.py:
    build_application() -> Application
    recover_state(app)  -> None
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config.settings import ALLOWED_USER_IDS, TELEGRAM_BOT_TOKEN, get_precision
from core import trade_exec
from core.analyzer import MarketAnalyzer
from core.mexc_ws import MexcWebSocket
from utils.db_manager import get_db

if TYPE_CHECKING:
    from core.trade_exec import VirtualStopLossWatcher

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
SETUP_SYMBOL, SETUP_CAPITAL, SETUP_STRATEGY = range(3)

# ── Callback data constants ────────────────────────────────────────────────────
CB_STATUS        = "cb_status"
CB_SETTINGS      = "cb_settings"
CB_ESTOP         = "cb_estop"
CB_ESTOP_CONFIRM = "cb_estop_confirm"
CB_ESTOP_CANCEL  = "cb_estop_cancel"
CB_REFRESH       = "cb_refresh"
CB_SETUP         = "cb_setup"
CB_BACK_MENU     = "cb_back_menu"
CB_STRAT_NORMAL  = "cb_strat_normal"
CB_STRAT_AGGR    = "cb_strat_aggr"
CB_STRAT_CONS    = "cb_strat_cons"
CB_TF_5M         = "cb_tf_5m"
CB_TF_15M        = "cb_tf_15m"
CB_TF_1H         = "cb_tf_1h"
CB_TF_4H         = "cb_tf_4h"

# Valid MEXC kline intervals mapped from callback data
TIMEFRAME_MAP: dict[str, str] = {
    CB_TF_5M:  "5m",
    CB_TF_15M: "15m",
    CB_TF_1H:  "1h",
    CB_TF_4H:  "4h",
}

# ── In-memory runtime state ────────────────────────────────────────────────────

class BotState:
    def __init__(self) -> None:
        self.ws = MexcWebSocket()
        self.analyzer = MarketAnalyzer()
        self.ws_task: asyncio.Task | None = None
        self.sl_watcher: "VirtualStopLossWatcher | None" = None
        self.sl_task: asyncio.Task | None = None
        self.fill_poll_task: asyncio.Task | None = None
        self.running: bool = False
        self.config: dict = {}
        # For live-edit status message
        self.status_chat_id: int | None = None
        self.status_message_id: int | None = None
        self.monitor_task: asyncio.Task | None = None


_state = BotState()


# ── Auth guard ─────────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


async def _deny(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer("⛔ Unauthorized.", show_alert=True)
    elif update.message:
        await update.message.reply_text("⛔ Unauthorized.")


# ── Keyboard builders ──────────────────────────────────────────────────────────

def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Live Status",    callback_data=CB_STATUS),
            InlineKeyboardButton("⚙️ Quick Settings", callback_data=CB_SETTINGS),
        ],
        [
            InlineKeyboardButton("🛑 EMERGENCY STOP", callback_data=CB_ESTOP),
            InlineKeyboardButton("🔄 Refresh Stats",  callback_data=CB_REFRESH),
        ],
    ])


def _estop_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, stop now", callback_data=CB_ESTOP_CONFIRM),
            InlineKeyboardButton("❌ Cancel",         callback_data=CB_ESTOP_CANCEL),
        ],
    ])


def _timeframe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ 5m",  callback_data=CB_TF_5M),
            InlineKeyboardButton("⏱ 15m", callback_data=CB_TF_15M),
        ],
        [
            InlineKeyboardButton("⏱ 1h",  callback_data=CB_TF_1H),
            InlineKeyboardButton("⏱ 4h",  callback_data=CB_TF_4H),
        ],
        [InlineKeyboardButton("« Back to menu", callback_data=CB_BACK_MENU)],
    ])


def _strategy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Normal      (impulse ×2, OB vol ×1.5)", callback_data=CB_STRAT_NORMAL)],
        [InlineKeyboardButton("⚡ Aggressive  (impulse ×1.5, OB vol ×1)", callback_data=CB_STRAT_AGGR)],
        [InlineKeyboardButton("🛡️ Conservative (impulse ×3, OB vol ×2)",  callback_data=CB_STRAT_CONS)],
        [InlineKeyboardButton("« Back to menu",                            callback_data=CB_BACK_MENU)],
    ])


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Main Menu", callback_data=CB_BACK_MENU)],
    ])


# ── Dashboard text builders ────────────────────────────────────────────────────

def _idle_dashboard_text() -> str:
    return (
        "🤖 *MEXC Market Maker Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔴 Status: *IDLE*\n\n"
        "No active trade. Use ⚙️ Quick Settings to configure\n"
        "a symbol and start the strategy."
    )


def _searching_dashboard_text(symbol: str, sma: float | None, price: float) -> str:
    sma_str = f"{sma:.8f}" if sma else "warming up…"
    price_str = f"{price:.8f}" if price else "—"
    return (
        "🤖 *MEXC Market Maker Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟡 Status: *SEARCHING*\n\n"
        f"🔍 Symbol: `{symbol}`\n"
        f"💹 Price:  `{price_str}`\n"
        f"📉 SMA-20: `{sma_str}`\n\n"
        "⏳ Scanning for bid wall entry signal…"
    )


def _pending_dashboard_text(symbol: str, entry_price: float, order_id: str) -> str:
    return (
        "🤖 *MEXC Market Maker Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟠 Status: *PENDING FILL*\n\n"
        f"🔍 Symbol:    `{symbol}`\n"
        f"📋 Entry:     `{entry_price}`\n"
        f"🆔 Order ID:  `{order_id}`\n\n"
        "⏳ Waiting for limit buy to fill…"
    )


def _open_dashboard_text(
    symbol: str,
    side: str,
    entry_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
) -> str:
    if entry_price and current_price:
        pnl_pct = (current_price - entry_price) / entry_price * 100
        pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
        pnl_str = f"{pnl_icon} `{pnl_pct:+.3f}%`"
    else:
        pnl_str = "—"

    price_str = f"{current_price:.8f}" if current_price else "—"
    return (
        "🤖 *MEXC Market Maker Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 Status: *TRADE OPEN*\n\n"
        f"🔍 Symbol:  `{symbol}` | Side: `{side.upper()}`\n"
        f"📥 Entry:   `{entry_price}`\n"
        f"💹 Current: `{price_str}`\n"
        f"💰 P&L:     {pnl_str}\n\n"
        f"⚠️  Stop-Loss (Virtual): `{sl_price}`\n"
        f"🎯 Take-Profit:          `{tp_price}`"
    )


# ── Telegram notifier ──────────────────────────────────────────────────────────

async def send_alert(text: str, application: Application | None = None) -> None:
    """Send a Markdown message to all ALLOWED_USER_IDS."""
    if not ALLOWED_USER_IDS:
        logger.warning("No ALLOWED_USER_IDS configured — cannot send alert")
        return
    if application is None:
        logger.info("Alert (no app): %s", text)
        return
    for uid in ALLOWED_USER_IDS:
        try:
            await application.bot.send_message(
                chat_id=uid, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as exc:
            logger.error("Failed to send alert to %s: %s", uid, exc)


async def _edit_or_send(
    application: Application,
    chat_id: int,
    message_id: int | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int | None:
    """Edit an existing message if possible, otherwise send a new one. Returns message_id."""
    if message_id:
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
            )
            return message_id
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return message_id
            logger.debug("edit_message_text failed (%s), sending new message", exc)
    msg = await application.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )
    return msg.message_id


# ── WebSocket handlers ─────────────────────────────────────────────────────────

async def _on_ticker(msg: dict) -> None:
    data = msg.get("d", {})
    price_str = data.get("c") or data.get("p")
    if not price_str:
        return
    try:
        price = float(price_str)
    except ValueError:
        return
    _state.analyzer.update_price(price)
    if _state.sl_watcher:
        _state.sl_watcher.update_price(price)


async def _on_depth(msg: dict) -> None:
    data = msg.get("d", {})
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    if bids or asks:
        _state.analyzer.update_depth(bids, asks)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _start_ws(symbol: str) -> None:
    """Subscribe to WS streams and start the connection task if not running."""
    _state.ws.subscribe(f"spot@public.miniTicker.v3.api@{symbol}", _on_ticker)
    _state.ws.subscribe(f"spot@public.increase.depth.v3.api@{symbol}@5", _on_depth)
    if _state.ws_task is None or _state.ws_task.done():
        _state.ws_task = asyncio.create_task(_state.ws.run())
        logger.info("WebSocket task started for %s", symbol)


def _arm_sl_watcher(
    symbol: str,
    entry_price: float,
    entry_qty: float,
    exit_order_id: str | None,
    sl_pct: float,
    application: Application,
) -> None:
    async def _notifier(text: str) -> None:
        await send_alert(text, application)

    async def _on_sl_triggered() -> None:
        _state.running = False
        _state.sl_watcher = None

    watcher = trade_exec.VirtualStopLossWatcher(
        symbol=symbol,
        entry_price=entry_price,
        qty=entry_qty,
        exit_order_id=exit_order_id,
        stop_loss_pct=sl_pct,
        notifier=_notifier,
        on_triggered=_on_sl_triggered,
    )
    _state.sl_watcher = watcher
    _state.sl_task = asyncio.create_task(watcher.run())
    logger.info(
        "Virtual SL watcher armed: entry=%.8f sl=%.8f",
        entry_price, entry_price * sl_pct,
    )


# ── Real-time monitor task ─────────────────────────────────────────────────────

async def _monitor_loop(application: Application) -> None:
    """
    Runs while a trade is active. Every 10 seconds it edits the pinned
    status message with fresh price / P&L data.
    """
    while _state.running:
        try:
            row = await get_db().fetch_state()
            cfg = row.get("config", {}) if row else {}
            trade_status = cfg.get("trade_status", "idle")
            symbol = (row.get("symbol") if row else None) or _state.config.get("symbol", "—")

            current_price = _state.analyzer._current_price
            sma = _state.analyzer.sma()

            if trade_status == "searching":
                text = _searching_dashboard_text(symbol, sma, current_price)
            elif trade_status == "pending_fill":
                entry_price = row.get("entry_price", 0) if row else 0
                order_id = cfg.get("entry_order_id", "—")
                text = _pending_dashboard_text(symbol, entry_price, order_id)
            elif trade_status == "open":
                entry_price = float(row.get("entry_price", 0) or 0) if row else 0
                sl_price = float(row.get("stop_loss", 0) or 0) if row else 0
                tp_price = cfg.get("take_profit_price", "—")
                text = _open_dashboard_text(
                    symbol=symbol,
                    side=row.get("side", "buy") if row else "buy",
                    entry_price=entry_price,
                    current_price=current_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                )
            else:
                text = _idle_dashboard_text()

            if _state.status_chat_id and _state.status_message_id:
                new_id = await _edit_or_send(
                    application,
                    _state.status_chat_id,
                    _state.status_message_id,
                    text,
                    reply_markup=_main_menu_keyboard(),
                )
                _state.status_message_id = new_id

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("Monitor loop error: %s", exc)

        await asyncio.sleep(10)


# ── Strategy orchestration ─────────────────────────────────────────────────────

async def _on_order_filled(order: dict, application: Application) -> None:
    db = get_db()
    symbol: str = _state.config["symbol"]
    entry_price = float(order.get("price", 0))
    entry_qty = float(order.get("executedQty", 0))
    prec = get_precision(symbol)

    # Use OB-derived TP/SL written to config by _start_strategy.
    # Fall back to fixed percentages only when recovering an older trade record
    # that pre-dates OB-based logic.
    sl_price: float = float(
        _state.config.get("sl_price") or
        round(entry_price * (1 - _state.config.get("impulse_multiplier", 2.0) * 0.005), 8)
    )
    tp_price: float = float(
        _state.config.get("take_profit_price") or
        round(entry_price * _state.config.get("tp_fallback_pct", 1.02), prec["price_precision"])
    )

    try:
        tp_resp = await trade_exec.place_limit_sell(symbol, tp_price, entry_qty)
        exit_order_id = tp_resp.get("orderId")
    except Exception as exc:
        logger.error("Failed to place TP sell: %s", exc)
        exit_order_id = None

    await db.upsert_state({
        "symbol": symbol,
        "is_active": True,
        "entry_price": entry_price,
        "side": "buy",
        "qty": entry_qty,
        "stop_loss": sl_price,
        "config": {
            **_state.config,
            "exit_order_id": exit_order_id,
            "take_profit_price": tp_price,
            "filled_at": int(time.time()),
            "trade_status": "open",
        },
    })

    await send_alert(
        f"✅ *Order Filled*\n\n"
        f"Symbol: `{symbol}`\n"
        f"Entry: `{entry_price}`\n"
        f"Qty: `{entry_qty}`\n"
        f"🎯 TP: `{tp_price}`\n"
        f"⚠️ SL: `{sl_price}`",
        application,
    )

    # VirtualStopLossWatcher expects stop_loss_pct as a ratio (e.g. 0.985).
    # Derive it from the absolute OB-based sl_price.
    sl_ratio = sl_price / entry_price if entry_price else 0.985
    _arm_sl_watcher(symbol, entry_price, entry_qty, exit_order_id, sl_ratio, application)


async def _start_strategy(application: Application) -> None:
    """
    Subscribe to WS, fetch klines to detect Order Blocks, then loop until
    an OB-confirmed entry signal is found. TP and SL are set dynamically
    from the detected OB zones — no fixed percentages.
    """
    db = get_db()
    symbol: str = _state.config["symbol"]
    quote_amount: float = _state.config["quote_amount"]
    timeframe: str = _state.config.get("timeframe", "15m")
    tick_size: float = float(get_precision(symbol)["tick_size"])

    _start_ws(symbol)
    await asyncio.sleep(3)  # allow initial market data to arrive

    _state.analyzer = MarketAnalyzer(
        wall_multiplier=_state.config.get("wall_multiplier", 3.0),
        sma_period=_state.config.get("sma_period", 20),
        price_precision=get_precision(symbol)["price_precision"],
        impulse_multiplier=_state.config.get("impulse_multiplier", 2.0),
        ob_volume_threshold=_state.config.get("ob_volume_threshold", 1.5),
        tp_fallback_pct=_state.config.get("tp_fallback_pct", 1.02),
    )

    # Fetch candles and detect Order Blocks (refreshed every 5 minutes)
    _OB_REFRESH_INTERVAL = 300  # seconds
    last_ob_refresh: float = 0.0

    attempt = 0
    signal = None
    while _state.running and signal is None:
        now = time.time()

        # Periodically refresh OB detection from fresh klines
        if now - last_ob_refresh >= _OB_REFRESH_INTERVAL or last_ob_refresh == 0:
            try:
                candles = await trade_exec.fetch_klines(symbol, timeframe, limit=100)
                _state.analyzer.detect_order_blocks(candles)
                last_ob_refresh = now
                logger.info(
                    "OBs refreshed for %s [%s]: %d bullish, %d bearish",
                    symbol, timeframe,
                    len(_state.analyzer._bullish_obs),
                    len(_state.analyzer._bearish_obs),
                )
            except Exception as exc:
                logger.error("Failed to fetch klines: %s", exc)

        signal = _state.analyzer.find_entry_signal(tick_size)
        if signal:
            break

        attempt += 1
        if attempt % 12 == 0:  # log every ~60s
            sma = _state.analyzer.sma()
            price = _state.analyzer._current_price
            logger.info(
                "Searching for OB entry signal (attempt %d) price=%.8f sma=%s",
                attempt, price, f"{sma:.8f}" if sma else "warming",
            )
        await asyncio.sleep(5)

    if not _state.running:
        logger.info("Strategy stopped while searching (emergency stop)")
        return

    # Dynamic TP/SL from the OB signal
    sl_price = signal.sl_price
    tp_price = signal.tp_price
    tp_source = "OB" if signal.tp_ob else "fallback"

    try:
        buy_resp = await trade_exec.place_limit_buy(symbol, signal.entry_price, quote_amount)
    except Exception as exc:
        await send_alert(f"❌ *Limit buy failed:* `{exc}`", application)
        _state.running = False
        await db.reset_state()
        return

    order_id = buy_resp.get("orderId")

    # Persist sl_price and tp_price into config so _on_order_filled can read
    # them back without recalculating from percentages.
    _state.config["sl_price"] = sl_price
    _state.config["take_profit_price"] = tp_price

    await db.upsert_state({
        "symbol": symbol,
        "is_active": True,
        "entry_price": signal.entry_price,
        "side": "buy",
        "qty": None,
        "stop_loss": sl_price,
        "config": {
            **_state.config,
            "entry_order_id": order_id,
            "take_profit_price": tp_price,
            "sl_price": sl_price,
            "tp_source": tp_source,
            "trade_status": "pending_fill",
        },
    })

    ob_info = (
        f"Entry OB: `{signal.entry_ob.low:.8f}` – `{signal.entry_ob.high:.8f}`\n"
        f"TP OB:    `{signal.tp_ob.low:.8f}` – `{signal.tp_ob.high:.8f}`\n"
        if signal.tp_ob else
        f"Entry OB: `{signal.entry_ob.low:.8f}` – `{signal.entry_ob.high:.8f}`\n"
        f"TP source: fallback ({_state.config.get('tp_fallback_pct', 1.02)}×)\n"
    )

    await send_alert(
        f"📋 *Limit Buy Placed*\n\n"
        f"Symbol: `{symbol}` | TF: `{timeframe}`\n"
        f"Entry:  `{signal.entry_price}`\n"
        f"⚠️ SL:  `{sl_price}` _(below entry OB)_\n"
        f"🎯 TP:  `{tp_price}` _{('(next bearish OB)' if signal.tp_ob else '(fixed fallback)')}_\n\n"
        f"{ob_info}"
        f"Wall: `{signal.wall_price}` (vol `{signal.wall_volume:.2f}`)\n"
        f"SMA-20: `{signal.sma:.8f}`\n"
        f"Order ID: `{order_id}`",
        application,
    )

    async def _filled_cb(order: dict) -> None:
        await _on_order_filled(order, application)

    _state.fill_poll_task = asyncio.create_task(
        trade_exec.poll_order_fill(symbol, order_id, _filled_cb)
    )


# ── State recovery ─────────────────────────────────────────────────────────────

async def recover_state(application: Application) -> None:
    db = get_db()
    await db.ensure_row_exists()
    row = await db.fetch_state()

    if not row or not row.get("is_active"):
        for uid in ALLOWED_USER_IDS:
            try:
                msg = await application.bot.send_message(
                    chat_id=uid,
                    text=_idle_dashboard_text(),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_main_menu_keyboard(),
                )
                _state.status_chat_id = uid
                _state.status_message_id = msg.message_id
            except Exception as exc:
                logger.error("Startup message failed for %s: %s", uid, exc)
        return

    symbol: str = row.get("symbol", "")
    config: dict = row.get("config", {})
    trade_status: str = config.get("trade_status", "")

    logger.info("Recovery: is_active=True symbol=%s trade_status=%s", symbol, trade_status)

    if not symbol:
        await db.reset_state()
        await send_alert("⚠️ *Bot restarted.* Inconsistent state — reset. Use /start.", application)
        return

    try:
        await trade_exec.fetch_precision(symbol)
    except Exception as exc:
        await send_alert(
            f"⚠️ *Bot restarted* but failed to fetch precision for `{symbol}`.\n"
            f"Error: `{exc}`\n\nUse /emergency\\_stop then /start to restart.",
            application,
        )
        return

    _state.config = config
    _state.running = True

    if trade_status == "pending_fill":
        order_id = config.get("entry_order_id")
        if not order_id:
            await db.reset_state()
            _state.running = False
            await send_alert("⚠️ *Bot restarted.* Inconsistent pending_fill — reset.", application)
            return

        _start_ws(symbol)

        async def _filled_cb(order: dict) -> None:
            await _on_order_filled(order, application)

        _state.fill_poll_task = asyncio.create_task(
            trade_exec.poll_order_fill(symbol, order_id, _filled_cb)
        )
        await send_alert(
            f"🚀 *Bot restarted.*\n\nSymbol: `{symbol}`\nStatus: Waiting for fill on `{order_id}`",
            application,
        )
        return

    if trade_status == "open":
        entry_price = row.get("entry_price")
        entry_qty = row.get("qty")
        exit_order_id = config.get("exit_order_id")
        sl_pct: float = config.get("stop_loss_pct", 0.985)

        if not entry_price or not entry_qty:
            await db.reset_state()
            _state.running = False
            await send_alert("⚠️ *Bot restarted.* Open trade state incomplete — reset.", application)
            return

        _start_ws(symbol)
        _arm_sl_watcher(
            symbol=symbol,
            entry_price=float(entry_price),
            entry_qty=float(entry_qty),
            exit_order_id=exit_order_id,
            sl_pct=sl_pct,
            application=application,
        )
        sl_price = row.get("stop_loss") or round(float(entry_price) * sl_pct, 8)
        tp_price = config.get("take_profit_price", "—")
        await send_alert(
            f"🚀 *Bot restarted.*\n\n"
            f"Symbol: `{symbol}`\nEntry: `{entry_price}`\n"
            f"SL: `{sl_price}`\nTP: `{tp_price}`\n\nSL watcher re-armed.",
            application,
        )
        return

    await db.reset_state()
    _state.running = False
    await send_alert(f"⚠️ Unknown trade status `{trade_status}` — reset. Use /start.", application)


# ── /start command ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — show the main inline-button dashboard."""
    if not _is_allowed(update):
        await _deny(update)
        return

    row = None
    try:
        row = await get_db().fetch_state()
    except Exception:
        pass

    cfg = row.get("config", {}) if row else {}
    trade_status = cfg.get("trade_status", "idle")
    symbol = (row.get("symbol") if row else None) or "—"
    current_price = _state.analyzer._current_price
    sma = _state.analyzer.sma()

    if trade_status == "searching":
        text = _searching_dashboard_text(symbol, sma, current_price)
    elif trade_status == "pending_fill":
        text = _pending_dashboard_text(symbol, row.get("entry_price", 0), cfg.get("entry_order_id", "—"))
    elif trade_status == "open":
        entry_price = float(row.get("entry_price", 0) or 0) if row else 0
        text = _open_dashboard_text(
            symbol=symbol,
            side=row.get("side", "buy") if row else "buy",
            entry_price=entry_price,
            current_price=current_price,
            sl_price=float(row.get("stop_loss", 0) or 0) if row else 0,
            tp_price=cfg.get("take_profit_price", "—"),
        )
    else:
        text = _idle_dashboard_text()

    msg = await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_menu_keyboard(),
    )
    _state.status_chat_id = update.effective_chat.id
    _state.status_message_id = msg.message_id

    # Start monitor loop if running and not already active
    if _state.running and (
        _state.monitor_task is None or _state.monitor_task.done()
    ):
        _state.monitor_task = asyncio.create_task(_monitor_loop(context.application))


# ── Callback query handlers ────────────────────────────────────────────────────

async def _cb_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        await _deny(update)
        return

    row = None
    try:
        row = await get_db().fetch_state()
    except Exception:
        pass

    cfg = row.get("config", {}) if row else {}
    trade_status = cfg.get("trade_status", "idle")
    symbol = (row.get("symbol") if row else None) or "—"
    current_price = _state.analyzer._current_price
    sma = _state.analyzer.sma()

    if trade_status == "searching":
        text = _searching_dashboard_text(symbol, sma, current_price)
    elif trade_status == "pending_fill":
        text = _pending_dashboard_text(symbol, row.get("entry_price", 0), cfg.get("entry_order_id", "—"))
    elif trade_status == "open":
        entry_price = float(row.get("entry_price", 0) or 0) if row else 0
        text = _open_dashboard_text(
            symbol=symbol,
            side=row.get("side", "buy") if row else "buy",
            entry_price=entry_price,
            current_price=current_price,
            sl_price=float(row.get("stop_loss", 0) or 0) if row else 0,
            tp_price=cfg.get("take_profit_price", "—"),
        )
    else:
        text = _idle_dashboard_text()

    new_id = await _edit_or_send(
        context.application,
        query.message.chat_id,
        query.message.message_id,
        text,
        reply_markup=_main_menu_keyboard(),
    )
    _state.status_chat_id = query.message.chat_id
    _state.status_message_id = new_id

    if _state.running and (
        _state.monitor_task is None or _state.monitor_task.done()
    ):
        _state.monitor_task = asyncio.create_task(_monitor_loop(context.application))


async def _cb_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Same as status but shows a brief 'Refreshed' toast."""
    query = update.callback_query
    await query.answer("🔄 Refreshed!")
    if not _is_allowed(update):
        await _deny(update)
        return
    # Reuse status logic
    update.callback_query.data = CB_STATUS
    await _cb_status(update, context)


async def _cb_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        await _deny(update)
        return

    if _state.running:
        await query.edit_message_text(
            "⚠️ *Bot is already running.*\n\nUse 🛑 EMERGENCY STOP first to reconfigure.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_back_keyboard(),
        )
        return

    context.user_data.clear()
    await query.edit_message_text(
        "⚙️ *Quick Setup — Step 1/3*\n\n"
        "Please type the trading symbol:\n"
        "_(e.g. `BTCUSDT`, `ETHUSDT`, `SOLUSDT`)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_back_keyboard(),
    )
    context.user_data["setup_step"] = "symbol"
    context.user_data["setup_chat_id"] = query.message.chat_id
    context.user_data["setup_message_id"] = query.message.message_id


async def _cb_estop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        await _deny(update)
        return

    await query.edit_message_text(
        "🛑 *EMERGENCY STOP*\n\n"
        "This will:\n"
        "• Cancel all open orders\n"
        "• Market-sell any open position\n"
        "• Reset the bot to idle\n\n"
        "Are you sure?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_estop_confirm_keyboard(),
    )


async def _cb_estop_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Cancelled.")
    if not _is_allowed(update):
        await _deny(update)
        return
    await query.edit_message_text(
        _idle_dashboard_text() if not _state.running else "✅ Emergency stop cancelled.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_menu_keyboard(),
    )


async def _cb_back_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        await _deny(update)
        return
    await query.edit_message_text(
        _idle_dashboard_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_menu_keyboard(),
    )


async def _cb_estop_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("🛑 Stopping…")
    if not _is_allowed(update):
        await _deny(update)
        return

    await query.edit_message_text(
        "🛑 *Emergency stop in progress…*",
        parse_mode=ParseMode.MARKDOWN,
    )

    db = get_db()
    try:
        row = await db.fetch_state()
    except Exception:
        row = None

    cfg = row.get("config", {}) if row else {}
    symbol = (row.get("symbol") if row else None) or _state.config.get("symbol")
    trade_status = cfg.get("trade_status", "idle")

    steps: list[str] = []

    # 1. Disarm SL watcher
    if _state.sl_watcher:
        _state.sl_watcher.cancel()
        _state.sl_watcher = None
        steps.append("✅ SL watcher disarmed")

    # 2. Cancel fill poller
    if _state.fill_poll_task and not _state.fill_poll_task.done():
        _state.fill_poll_task.cancel()
        steps.append("✅ Fill poller cancelled")

    # 3. Cancel monitor task
    if _state.monitor_task and not _state.monitor_task.done():
        _state.monitor_task.cancel()

    if symbol:
        # 4. Cancel pending entry order
        entry_order_id = cfg.get("entry_order_id")
        if entry_order_id and trade_status == "pending_fill":
            try:
                await trade_exec.cancel_order(symbol, entry_order_id)
                steps.append(f"✅ Entry order `{entry_order_id}` cancelled")
            except Exception as exc:
                steps.append(f"⚠️ Could not cancel entry order: `{exc}`")

        # 5. Cancel TP order
        exit_order_id = cfg.get("exit_order_id")
        if exit_order_id and trade_status == "open":
            try:
                await trade_exec.cancel_order(symbol, exit_order_id)
                steps.append(f"✅ TP order `{exit_order_id}` cancelled")
            except Exception as exc:
                steps.append(f"⚠️ Could not cancel TP order: `{exc}`")

        # 6. Market sell
        qty = row.get("qty") if row else None
        if qty and trade_status == "open":
            try:
                resp = await trade_exec.market_sell(symbol, float(qty))
                steps.append(f"✅ Market sell executed (`{resp.get('orderId')}`)")
            except Exception as exc:
                steps.append(f"❌ Market sell failed: `{exc}`")

    # 7. Stop WebSocket
    _state.ws.stop()
    if _state.ws_task and not _state.ws_task.done():
        _state.ws_task.cancel()
    steps.append("✅ WebSocket stopped")

    # 8. Reset DB
    try:
        await db.reset_state()
        steps.append("✅ Database reset")
    except Exception as exc:
        steps.append(f"⚠️ DB reset failed: `{exc}`")

    _state.running = False
    _state.config = {}
    _state.status_message_id = None

    summary = "\n".join(steps)
    await query.edit_message_text(
        f"🔴 *Bot stopped.*\n\n{summary}\n\nUse /start to return to the dashboard.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Setup conversation (text message steps) ────────────────────────────────────

async def _handle_setup_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the multi-step setup flow driven by inline buttons + free-text replies.
    Steps: symbol → capital → strategy (via buttons).
    """
    if not _is_allowed(update):
        await _deny(update)
        return

    step = context.user_data.get("setup_step")
    if not step:
        return  # not in a setup flow

    text_in = (update.message.text or "").strip()

    if step == "symbol":
        symbol = text_in.upper()
        if not symbol.isalnum():
            await update.message.reply_text("❌ Invalid symbol. Try again (e.g. `BTCUSDT`):", parse_mode=ParseMode.MARKDOWN)
            return
        context.user_data["setup_symbol"] = symbol
        context.user_data["setup_step"] = "capital"
        await update.message.reply_text(
            f"⚙️ *Quick Setup — Step 2/3*\n\n"
            f"Symbol: `{symbol}` ✅\n\n"
            "How much capital (in quote currency, e.g. USDT) do you want to use?\n"
            "_(e.g. `100` for 100 USDT)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if step == "capital":
        try:
            capital = float(text_in)
            if capital <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Enter a positive number (e.g. `100`):", parse_mode=ParseMode.MARKDOWN)
            return
        context.user_data["setup_capital"] = capital
        context.user_data["setup_step"] = "timeframe"
        symbol = context.user_data.get("setup_symbol", "—")
        await update.message.reply_text(
            f"⚙️ *Quick Setup — Step 3/4*\n\n"
            f"Symbol: `{symbol}` ✅\n"
            f"Capital: `{capital}` ✅\n\n"
            "Choose the analysis timeframe for Order Block detection:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_timeframe_keyboard(),
        )
        return


async def _cb_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle timeframe selection button — advances to strategy step."""
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        await _deny(update)
        return

    tf_interval = TIMEFRAME_MAP.get(query.data, "15m")
    context.user_data["setup_timeframe"] = tf_interval
    context.user_data["setup_step"] = "strategy"

    symbol = context.user_data.get("setup_symbol", "—")
    capital = context.user_data.get("setup_capital", "—")

    await query.edit_message_text(
        f"⚙️ *Quick Setup — Step 4/4*\n\n"
        f"Symbol:    `{symbol}` ✅\n"
        f"Capital:   `{capital}` ✅\n"
        f"Timeframe: `{tf_interval}` ✅\n\n"
        "Choose a strategy sensitivity:\n"
        "_(Controls how strong an impulse must be to qualify as an Order Block)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_strategy_keyboard(),
    )


async def _apply_strategy(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    impulse_multiplier: float,
    ob_volume_threshold: float,
    tp_fallback_pct: float,
) -> None:
    """Finalise setup after strategy sensitivity selection."""
    query = update.callback_query
    await query.answer()

    symbol    = context.user_data.get("setup_symbol")
    capital   = context.user_data.get("setup_capital")
    timeframe = context.user_data.get("setup_timeframe", "15m")

    if not symbol or not capital:
        await query.edit_message_text(
            "⚠️ Setup data lost. Please use /start and try again.",
            reply_markup=_main_menu_keyboard(),
        )
        context.user_data.clear()
        return

    await query.edit_message_text(
        f"🔍 Fetching precision for `{symbol}`…",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        await trade_exec.fetch_precision(symbol)
    except Exception as exc:
        await query.edit_message_text(
            f"❌ Failed to fetch symbol info: `{exc}`\n\nUse /start to try again.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_back_keyboard(),
        )
        context.user_data.clear()
        return

    prec = get_precision(symbol)
    _state.config = {
        "symbol": symbol,
        "quote_amount": capital,
        "timeframe": timeframe,
        "wall_multiplier": 3.0,
        "sma_period": 20,
        "impulse_multiplier": impulse_multiplier,
        "ob_volume_threshold": ob_volume_threshold,
        "tp_fallback_pct": tp_fallback_pct,
        "price_precision": prec["price_precision"],
        "qty_precision": prec["qty_precision"],
        "tick_size": prec["tick_size"],
        "started_at": int(time.time()),
        "trade_status": "searching",
    }
    _state.running = True

    await get_db().upsert_state({
        "symbol": symbol,
        "is_active": True,
        "entry_price": None,
        "side": "buy",
        "qty": None,
        "stop_loss": None,
        "config": _state.config,
    })

    msg = await query.edit_message_text(
        f"✅ *Setup complete — Strategy started!*\n\n"
        f"🔍 Symbol:    `{symbol}`\n"
        f"💰 Capital:   `{capital}`\n"
        f"⏱ Timeframe: `{timeframe}`\n"
        f"📐 Tick:      `{prec['tick_size']}`\n\n"
        "🟡 Scanning for Order Block entry signal…\n"
        "_(TP/SL will be set dynamically from detected OB zones)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_menu_keyboard(),
    )
    _state.status_chat_id = query.message.chat_id
    _state.status_message_id = msg.message_id

    context.user_data.clear()

    asyncio.create_task(_start_strategy(context.application))

    if _state.monitor_task is None or _state.monitor_task.done():
        _state.monitor_task = asyncio.create_task(_monitor_loop(context.application))


async def _cb_strat_normal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _apply_strategy(update, context, impulse_multiplier=2.0, ob_volume_threshold=1.5, tp_fallback_pct=1.02)


async def _cb_strat_aggr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _apply_strategy(update, context, impulse_multiplier=1.5, ob_volume_threshold=1.0, tp_fallback_pct=1.03)


async def _cb_strat_cons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _apply_strategy(update, context, impulse_multiplier=3.0, ob_volume_threshold=2.0, tp_fallback_pct=1.015)


# ── Legacy text commands (kept for backward compatibility) ─────────────────────

async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setup <symbol> <quote_amount> [timeframe]  — TP/SL set dynamically from OBs."""
    if not _is_allowed(update):
        await _deny(update)
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/setup <symbol> <quote_amount> [timeframe]`\n"
            "Example: `/setup BTCUSDT 100 15m`\n\n"
            "Timeframes: `1m` `5m` `15m` `30m` `1h` `4h`\n"
            "TP and SL are set automatically from detected Order Block zones.\n\n"
            "Or use /start for the interactive dashboard.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    symbol = args[0].upper()
    try:
        quote_amount = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid quote amount.")
        return

    timeframe = args[2] if len(args) > 2 else "15m"
    valid_tfs = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    if timeframe not in valid_tfs:
        await update.message.reply_text(
            f"❌ Invalid timeframe `{timeframe}`. Choose from: {', '.join(sorted(valid_tfs))}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if _state.running:
        await update.message.reply_text("⚠️ Bot is already running. Use /emergency_stop first.")
        return

    await update.message.reply_text(f"🔍 Fetching precision for `{symbol}`…", parse_mode=ParseMode.MARKDOWN)
    try:
        await trade_exec.fetch_precision(symbol)
    except Exception as exc:
        await update.message.reply_text(f"❌ Failed to fetch symbol info: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        return

    prec = get_precision(symbol)
    _state.config = {
        "symbol": symbol,
        "quote_amount": quote_amount,
        "timeframe": timeframe,
        "wall_multiplier": 3.0,
        "sma_period": 20,
        "impulse_multiplier": 2.0,
        "ob_volume_threshold": 1.5,
        "tp_fallback_pct": 1.02,
        "price_precision": prec["price_precision"],
        "qty_precision": prec["qty_precision"],
        "tick_size": prec["tick_size"],
        "started_at": int(time.time()),
        "trade_status": "searching",
    }
    _state.running = True

    await get_db().upsert_state({
        "symbol": symbol,
        "is_active": True,
        "entry_price": None,
        "side": "buy",
        "qty": None,
        "stop_loss": None,
        "config": _state.config,
    })

    msg = await update.message.reply_text(
        f"✅ *Setup complete*\n\n"
        f"Symbol:    `{symbol}`\n"
        f"Capital:   `{quote_amount}`\n"
        f"Timeframe: `{timeframe}`\n"
        f"Tick:      `{prec['tick_size']}`\n\n"
        "🟡 Scanning for Order Block entry signal…\n"
        "_(TP/SL will be set dynamically from OB zones)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_menu_keyboard(),
    )
    _state.status_chat_id = update.effective_chat.id
    _state.status_message_id = msg.message_id

    asyncio.create_task(_start_strategy(context.application))
    if _state.monitor_task is None or _state.monitor_task.done():
        _state.monitor_task = asyncio.create_task(_monitor_loop(context.application))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — alias for the dashboard status view."""
    if not _is_allowed(update):
        await _deny(update)
        return
    await cmd_start(update, context)


async def cmd_emergency_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/emergency_stop — text-command version of the emergency stop."""
    if not _is_allowed(update):
        await _deny(update)
        return

    await update.message.reply_text("🛑 *Emergency stop initiated…*", parse_mode=ParseMode.MARKDOWN)

    db = get_db()
    try:
        row = await db.fetch_state()
    except Exception:
        row = None

    cfg = row.get("config", {}) if row else {}
    symbol = (row.get("symbol") if row else None) or _state.config.get("symbol")
    trade_status = cfg.get("trade_status", "idle")

    if _state.sl_watcher:
        _state.sl_watcher.cancel()
        _state.sl_watcher = None
    if _state.fill_poll_task and not _state.fill_poll_task.done():
        _state.fill_poll_task.cancel()
    if _state.monitor_task and not _state.monitor_task.done():
        _state.monitor_task.cancel()

    if symbol:
        entry_order_id = cfg.get("entry_order_id")
        if entry_order_id and trade_status == "pending_fill":
            try:
                await trade_exec.cancel_order(symbol, entry_order_id)
            except Exception as exc:
                await update.message.reply_text(f"⚠️ Could not cancel entry order: `{exc}`", parse_mode=ParseMode.MARKDOWN)

        exit_order_id = cfg.get("exit_order_id")
        if exit_order_id and trade_status == "open":
            try:
                await trade_exec.cancel_order(symbol, exit_order_id)
            except Exception as exc:
                await update.message.reply_text(f"⚠️ Could not cancel TP order: `{exc}`", parse_mode=ParseMode.MARKDOWN)

        qty = row.get("qty") if row else None
        if qty and trade_status == "open":
            try:
                resp = await trade_exec.market_sell(symbol, float(qty))
                await update.message.reply_text(f"✅ Market sell: `{resp.get('orderId')}`", parse_mode=ParseMode.MARKDOWN)
            except Exception as exc:
                await update.message.reply_text(f"❌ Market sell failed: `{exc}`", parse_mode=ParseMode.MARKDOWN)

    _state.ws.stop()
    if _state.ws_task and not _state.ws_task.done():
        _state.ws_task.cancel()

    try:
        await db.reset_state()
    except Exception as exc:
        await update.message.reply_text(f"⚠️ DB reset failed: `{exc}`", parse_mode=ParseMode.MARKDOWN)

    _state.running = False
    _state.config = {}
    _state.status_message_id = None

    await update.message.reply_text(
        "🔴 *Bot stopped. All positions closed.*\n\nUse /start to return to the dashboard.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Application factory ────────────────────────────────────────────────────────

def build_application() -> Application:
    """Build and return the configured Telegram Application."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Core commands
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("setup",          cmd_setup))
    app.add_handler(CommandHandler("emergency_stop", cmd_emergency_stop))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(_cb_status,        pattern=f"^{CB_STATUS}$"))
    app.add_handler(CallbackQueryHandler(_cb_refresh,       pattern=f"^{CB_REFRESH}$"))
    app.add_handler(CallbackQueryHandler(_cb_settings,      pattern=f"^{CB_SETTINGS}$"))
    app.add_handler(CallbackQueryHandler(_cb_estop,         pattern=f"^{CB_ESTOP}$"))
    app.add_handler(CallbackQueryHandler(_cb_estop_confirm, pattern=f"^{CB_ESTOP_CONFIRM}$"))
    app.add_handler(CallbackQueryHandler(_cb_estop_cancel,  pattern=f"^{CB_ESTOP_CANCEL}$"))
    app.add_handler(CallbackQueryHandler(_cb_back_menu,     pattern=f"^{CB_BACK_MENU}$"))
    # Timeframe selection
    app.add_handler(CallbackQueryHandler(_cb_timeframe, pattern=f"^({CB_TF_5M}|{CB_TF_15M}|{CB_TF_1H}|{CB_TF_4H})$"))

    # Strategy sensitivity selection
    app.add_handler(CallbackQueryHandler(_cb_strat_normal,  pattern=f"^{CB_STRAT_NORMAL}$"))
    app.add_handler(CallbackQueryHandler(_cb_strat_aggr,    pattern=f"^{CB_STRAT_AGGR}$"))
    app.add_handler(CallbackQueryHandler(_cb_strat_cons,    pattern=f"^{CB_STRAT_CONS}$"))

    # Free-text handler for the setup conversation flow
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_setup_text))

    return app
