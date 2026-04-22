"""
Telegram bot interface.

Commands:
    /setup  <symbol> <quote_amount> [sl_pct] [tp_pct]
        Configure and start the market-maker strategy.
        Example: /setup BTCUSDT 100 1.5 2.0

    /status
        Show current trade state, SMA, and session P&L.

    /emergency_stop
        Immediately cancel all open orders and market-sell any open position.

All commands are restricted to ALLOWED_USER_IDS from .env.
The bot uses python-telegram-bot v20+ (Application / async handlers).

Public functions used by main.py:
    build_application() -> Application
    recover_state(app)  -> None   (called from post_init hook on every startup)
"""

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config.settings import (
    ALLOWED_USER_IDS,
    STATE_PATH,
    TELEGRAM_BOT_TOKEN,
    get_precision,
)
from core import trade_exec
from core.analyzer import MarketAnalyzer
from core.mexc_ws import MexcWebSocket

if TYPE_CHECKING:
    from core.trade_exec import VirtualStopLossWatcher

logger = logging.getLogger(__name__)


# ── Shared bot state ───────────────────────────────────────────────────────────

class BotState:
    """Mutable runtime state shared across command handlers."""

    def __init__(self) -> None:
        self.ws = MexcWebSocket()
        self.analyzer = MarketAnalyzer()
        self.ws_task: asyncio.Task | None = None
        self.sl_watcher: "VirtualStopLossWatcher | None" = None
        self.sl_task: asyncio.Task | None = None
        self.fill_poll_task: asyncio.Task | None = None
        self.running: bool = False

        self.config: dict = {}
        self.trade: dict = {}
        self.session: dict = {}

        self._load()

    def _load(self) -> None:
        """Load persisted state from disk. Safe to call at any time."""
        try:
            os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
            with open(STATE_PATH) as f:
                data = json.load(f)
            self.config = data.get("config", {})
            self.trade = data.get("trade", {})
            self.session = data.get("session", {})
            logger.info(
                "State loaded: trade_status=%s symbol=%s",
                self.trade.get("status", "idle"),
                self.config.get("symbol", "—"),
            )
        except (FileNotFoundError, json.JSONDecodeError):
            self.config = {}
            self.trade = {"status": "idle"}
            self.session = {"total_trades": 0, "total_pnl": 0.0, "wins": 0, "losses": 0}
            logger.info("No existing state found — starting fresh")

    def save(self) -> None:
        os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(
                {"config": self.config, "trade": self.trade, "session": self.session},
                f, indent=2,
            )

    def reset_trade(self) -> None:
        self.trade = {
            "status": "idle",
            "entry_order_id": None,
            "exit_order_id": None,
            "entry_price": None,
            "entry_qty": None,
            "stop_loss_price": None,
            "take_profit_price": None,
            "filled_at": None,
            "closed_at": None,
            "pnl": None,
            "exit_reason": None,
        }
        self.running = False
        self.save()


_state = BotState()


# ── Auth guard ─────────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in ALLOWED_USER_IDS


async def _deny(update: Update) -> None:
    await update.message.reply_text("⛔ Unauthorized.")


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


# ── WebSocket handlers ─────────────────────────────────────────────────────────

async def _on_ticker(msg: dict) -> None:
    """Handle miniTicker messages: update analyzer price and SL watcher."""
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
    """Handle order-book depth messages."""
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
    """Create and start the Virtual Stop-Loss watcher task."""

    async def _notifier(text: str) -> None:
        await send_alert(text, application)

    async def _on_sl_triggered() -> None:
        _state.reset_trade()
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
        "Virtual SL watcher armed: entry=%.8f sl=%.8f", entry_price, entry_price * sl_pct
    )


# ── Strategy orchestration ─────────────────────────────────────────────────────

async def _on_order_filled(order: dict, application: Application) -> None:
    """
    Called when the limit buy order is confirmed FILLED.
    Places the take-profit limit sell and arms the Virtual SL watcher.
    """
    symbol: str = _state.config["symbol"]
    entry_price = float(order.get("price", 0))
    entry_qty = float(order.get("executedQty", 0))
    sl_pct: float = _state.config.get("stop_loss_pct", 0.985)
    tp_pct: float = _state.config.get("take_profit_pct", 1.02)
    tp_price = round(entry_price * tp_pct, get_precision(symbol)["price_precision"])

    try:
        tp_resp = await trade_exec.place_limit_sell(symbol, tp_price, entry_qty)
        exit_order_id = tp_resp.get("orderId")
    except Exception as exc:
        logger.error("Failed to place TP sell: %s", exc)
        exit_order_id = None

    _state.trade.update({
        "status": "open",
        "entry_price": entry_price,
        "entry_qty": entry_qty,
        "exit_order_id": exit_order_id,
        "stop_loss_price": round(entry_price * sl_pct, 8),
        "take_profit_price": tp_price,
        "filled_at": int(time.time()),
    })
    _state.save()

    await send_alert(
        f"✅ *Order Filled*\n\n"
        f"Symbol: `{symbol}`\n"
        f"Entry: `{entry_price}`\n"
        f"Qty: `{entry_qty}`\n"
        f"TP: `{tp_price}`\n"
        f"SL: `{_state.trade['stop_loss_price']}`",
        application,
    )

    _arm_sl_watcher(symbol, entry_price, entry_qty, exit_order_id, sl_pct, application)


async def _start_strategy(application: Application) -> None:
    """
    Subscribe to WS streams, start the WS task, and place the initial
    limit buy order based on the analyzer's entry signal.
    """
    symbol: str = _state.config["symbol"]
    quote_amount: float = _state.config["quote_amount"]
    tick_size: float = float(get_precision(symbol)["tick_size"])

    _start_ws(symbol)
    await asyncio.sleep(3)  # allow initial market data to arrive

    _state.analyzer = MarketAnalyzer(
        wall_multiplier=_state.config.get("wall_multiplier", 3.0),
        sma_period=_state.config.get("sma_period", 20),
        price_precision=get_precision(symbol)["price_precision"],
    )

    # Retry loop — waits for SMA-20 to warm up (up to 60 s)
    signal = None
    for attempt in range(30):
        signal = _state.analyzer.find_entry_signal(tick_size)
        if signal:
            break
        logger.debug("No signal yet (attempt %d/30) — waiting 2s", attempt + 1)
        await asyncio.sleep(2)

    if not signal:
        await send_alert(
            "⚠️ *No entry signal found after 60s.*\n"
            "Check market conditions or adjust wall multiplier.",
            application,
        )
        _state.running = False
        return

    try:
        buy_resp = await trade_exec.place_limit_buy(symbol, signal.entry_price, quote_amount)
    except Exception as exc:
        await send_alert(f"❌ *Limit buy failed:* `{exc}`", application)
        _state.running = False
        return

    order_id = buy_resp.get("orderId")
    _state.trade.update({
        "status": "pending_fill",
        "entry_order_id": order_id,
        "entry_price": signal.entry_price,
    })
    _state.save()

    await send_alert(
        f"📋 *Limit Buy Placed*\n\n"
        f"Symbol: `{symbol}`\n"
        f"Price: `{signal.entry_price}`\n"
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


# ── State recovery (called on every startup via post_init) ─────────────────────

async def recover_state(application: Application) -> None:
    """
    Re-attach live monitoring after a restart without requiring a new /setup.

    Three cases based on trade.status in state.json:

    idle          → nothing to recover; send a clean-start notification.

    pending_fill  → the limit buy was placed but not yet filled before the
                    restart. Re-attach the fill poller so we still catch the
                    fill and arm the SL watcher when it happens.

    open          → the position is live. Re-attach the WebSocket and re-arm
                    the Virtual Stop-Loss watcher immediately using the entry
                    price and qty already stored in state.
    """
    trade_status = _state.trade.get("status", "idle")
    symbol = _state.config.get("symbol")

    logger.info("recover_state: trade_status=%s symbol=%s", trade_status, symbol)

    # ── Case 1: nothing active ─────────────────────────────────────────────────
    if trade_status == "idle" or not symbol:
        await send_alert(
            "🚀 *Bot started.*\n\nNo active trade found. Use /setup to begin.",
            application,
        )
        return

    # Precision lives only in memory — must be re-fetched on every cold start.
    try:
        await trade_exec.fetch_precision(symbol)
    except Exception as exc:
        logger.error("Could not fetch precision on recovery: %s", exc)
        await send_alert(
            f"⚠️ *Bot restarted* but failed to fetch precision for `{symbol}`.\n"
            f"Error: `{exc}`\n\nUse /emergency_stop then /setup to restart safely.",
            application,
        )
        return

    # ── Case 2: waiting for limit buy to fill ──────────────────────────────────
    if trade_status == "pending_fill":
        order_id = _state.trade.get("entry_order_id")
        if not order_id:
            logger.warning("pending_fill state but no entry_order_id — resetting")
            _state.reset_trade()
            await send_alert(
                "⚠️ *Bot restarted.* Inconsistent pending_fill state — trade reset.\n"
                "Use /setup to begin a new trade.",
                application,
            )
            return

        _state.running = True
        _start_ws(symbol)

        async def _filled_cb(order: dict) -> None:
            await _on_order_filled(order, application)

        _state.fill_poll_task = asyncio.create_task(
            trade_exec.poll_order_fill(symbol, order_id, _filled_cb)
        )

        await send_alert(
            "🚀 *Bot restarted and synchronised with current state.*\n\n"
            f"Symbol: `{symbol}`\n"
            f"Status: Waiting for fill on order `{order_id}`\n"
            f"Entry price: `{_state.trade.get('entry_price')}`\n\n"
            "WebSocket and fill poller re-attached.",
            application,
        )
        logger.info("Recovery: fill poller re-attached for order %s", order_id)
        return

    # ── Case 3: position is open — re-arm SL watcher ──────────────────────────
    if trade_status == "open":
        entry_price = _state.trade.get("entry_price")
        entry_qty = _state.trade.get("entry_qty")
        exit_order_id = _state.trade.get("exit_order_id")
        sl_pct: float = _state.config.get("stop_loss_pct", 0.985)

        if not entry_price or not entry_qty:
            logger.warning("open state but missing entry_price/qty — resetting")
            _state.reset_trade()
            await send_alert(
                "⚠️ *Bot restarted.* Open trade state is incomplete — trade reset.\n"
                "Check your MEXC account manually, then use /setup.",
                application,
            )
            return

        _state.running = True
        _start_ws(symbol)
        _arm_sl_watcher(
            symbol=symbol,
            entry_price=float(entry_price),
            entry_qty=float(entry_qty),
            exit_order_id=exit_order_id,
            sl_pct=sl_pct,
            application=application,
        )

        sl_price = round(float(entry_price) * sl_pct, 8)
        tp_price = _state.trade.get("take_profit_price", "—")

        await send_alert(
            "🚀 *Bot restarted and synchronised with current state.*\n\n"
            f"Symbol: `{symbol}`\n"
            f"Entry: `{entry_price}`\n"
            f"Qty: `{entry_qty}`\n"
            f"Stop-loss: `{sl_price}`\n"
            f"Take-profit: `{tp_price}`\n\n"
            "WebSocket and Virtual Stop-Loss watcher re-attached.",
            application,
        )
        logger.info(
            "Recovery: SL watcher re-armed for %s entry=%.8f sl=%.8f",
            symbol, float(entry_price), sl_price,
        )
        return

    # ── Unexpected status ──────────────────────────────────────────────────────
    logger.warning("Unrecognised trade status '%s' on recovery — resetting", trade_status)
    _state.reset_trade()
    await send_alert(
        f"⚠️ *Bot restarted.* Unknown trade status `{trade_status}` — state reset.\n"
        "Use /setup to begin.",
        application,
    )


# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setup <symbol> <quote_amount> [sl_pct] [tp_pct]

    Configures the bot and launches the strategy.
    sl_pct and tp_pct are percentages (e.g. 1.5 means 1.5% stop-loss).
    """
    if not _is_allowed(update):
        await _deny(update)
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/setup <symbol> <quote_amount> [sl_pct] [tp_pct]`\n"
            "Example: `/setup BTCUSDT 100 1.5 2.0`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    symbol = args[0].upper()
    try:
        quote_amount = float(args[1])
        sl_pct = 1 - float(args[2]) / 100 if len(args) > 2 else 0.985
        tp_pct = 1 + float(args[3]) / 100 if len(args) > 3 else 1.02
    except ValueError:
        await update.message.reply_text("❌ Invalid numeric argument.")
        return

    if _state.running:
        await update.message.reply_text("⚠️ Bot is already running. Use /emergency_stop first.")
        return

    await update.message.reply_text(
        f"🔍 Fetching precision for `{symbol}`…", parse_mode=ParseMode.MARKDOWN
    )
    try:
        await trade_exec.fetch_precision(symbol)
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Failed to fetch symbol info: `{exc}`", parse_mode=ParseMode.MARKDOWN
        )
        return

    prec = get_precision(symbol)
    _state.config = {
        "symbol": symbol,
        "quote_amount": quote_amount,
        "wall_multiplier": 3.0,
        "sma_period": 20,
        "stop_loss_pct": sl_pct,
        "take_profit_pct": tp_pct,
        "price_precision": prec["price_precision"],
        "qty_precision": prec["qty_precision"],
        "tick_size": prec["tick_size"],
    }
    _state.session["started_at"] = int(time.time())
    _state.save()
    _state.running = True

    await update.message.reply_text(
        f"✅ *Setup complete*\n\n"
        f"Symbol: `{symbol}`\n"
        f"Quote: `{quote_amount}`\n"
        f"SL: `{(1 - sl_pct) * 100:.2f}%`\n"
        f"TP: `{(tp_pct - 1) * 100:.2f}%`\n"
        f"Tick: `{prec['tick_size']}`\n\n"
        f"Starting strategy…",
        parse_mode=ParseMode.MARKDOWN,
    )

    asyncio.create_task(_start_strategy(context.application))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — show current trade and session summary."""
    if not _is_allowed(update):
        await _deny(update)
        return

    trade = _state.trade
    cfg = _state.config
    sess = _state.session
    symbol = cfg.get("symbol", "—")

    sma = _state.analyzer.sma()
    sma_str = f"`{sma:.8f}`" if sma else "_not ready_"

    current_price = _state.analyzer._current_price
    price_str = f"`{current_price:.8f}`" if current_price else "_no data_"

    lines = [
        "📊 *Bot Status*",
        "",
        f"*Symbol:* `{symbol}`",
        f"*Running:* `{_state.running}`",
        f"*Trade status:* `{trade.get('status', 'idle')}`",
        "",
        f"*Live price:* {price_str}",
        f"*SMA-20:* {sma_str}",
        "",
        f"*Entry price:* `{trade.get('entry_price', '—')}`",
        f"*Entry qty:* `{trade.get('entry_qty', '—')}`",
        f"*Stop-loss:* `{trade.get('stop_loss_price', '—')}`",
        f"*Take-profit:* `{trade.get('take_profit_price', '—')}`",
        "",
        f"*Session trades:* `{sess.get('total_trades', 0)}`",
        f"*Session P&L:* `{sess.get('total_pnl', 0.0):.4f}`",
        f"*Wins / Losses:* `{sess.get('wins', 0)} / {sess.get('losses', 0)}`",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_emergency_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/emergency_stop — cancel all orders and market-sell the open position."""
    if not _is_allowed(update):
        await _deny(update)
        return

    await update.message.reply_text(
        "🛑 *Emergency stop initiated…*", parse_mode=ParseMode.MARKDOWN
    )

    symbol = _state.config.get("symbol")
    if not symbol:
        await update.message.reply_text("No active symbol configured.")
        _state.reset_trade()
        return

    # 1. Disarm SL watcher first to prevent a race with the market sell below
    if _state.sl_watcher:
        _state.sl_watcher.cancel()
        _state.sl_watcher = None

    # 2. Cancel fill poller
    if _state.fill_poll_task and not _state.fill_poll_task.done():
        _state.fill_poll_task.cancel()

    # 3. Cancel pending entry order
    entry_order_id = _state.trade.get("entry_order_id")
    if entry_order_id and _state.trade.get("status") == "pending_fill":
        try:
            await trade_exec.cancel_order(symbol, entry_order_id)
            await update.message.reply_text(
                f"✅ Entry order `{entry_order_id}` cancelled.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            await update.message.reply_text(
                f"⚠️ Could not cancel entry order: `{exc}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    # 4. Cancel open take-profit order
    exit_order_id = _state.trade.get("exit_order_id")
    if exit_order_id and _state.trade.get("status") == "open":
        try:
            await trade_exec.cancel_order(symbol, exit_order_id)
            await update.message.reply_text(
                f"✅ Exit order `{exit_order_id}` cancelled.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            await update.message.reply_text(
                f"⚠️ Could not cancel exit order: `{exc}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    # 5. Market sell open position
    entry_qty = _state.trade.get("entry_qty")
    if entry_qty and _state.trade.get("status") == "open":
        try:
            resp = await trade_exec.market_sell(symbol, float(entry_qty))
            await update.message.reply_text(
                f"✅ Market sell executed. Order ID: `{resp.get('orderId')}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            await update.message.reply_text(
                f"❌ Market sell failed: `{exc}`",
                parse_mode=ParseMode.MARKDOWN,
            )

    # 6. Stop WebSocket
    _state.ws.stop()
    if _state.ws_task and not _state.ws_task.done():
        _state.ws_task.cancel()

    _state.reset_trade()

    await update.message.reply_text(
        "🔴 *Bot stopped. All positions closed.*", parse_mode=ParseMode.MARKDOWN
    )


# ── Application factory ────────────────────────────────────────────────────────

def build_application() -> Application:
    """Build and return the configured Telegram Application."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("emergency_stop", cmd_emergency_stop))
    return app
