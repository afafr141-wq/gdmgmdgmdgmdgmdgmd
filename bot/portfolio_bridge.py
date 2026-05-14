"""
Bridge: registers portfolio bot handlers inside the grid bot's Application.

All portfolio callbacks are prefixed with "portfolio:", "paction:", "psettings:",
"asset:", "confirm:", "wizard:", "staction:", "stbot:", "stwizard:", "action:"
so they don't collide with grid bot callbacks.

The portfolio bot's message handler runs at group=10 (lower priority than grid
ConversationHandler at group=0) and only fires when user_data["state"] is a
portfolio wizard state.
"""
from __future__ import annotations

import logging
import threading

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger(__name__)

# Portfolio wizard states that the message handler should respond to
_PORTFOLIO_STATES = {
    "wizard_name", "wizard_symbols", "wizard_manual_alloc",
    "wizard_deviation_custom", "wizard_balance_amount",
    "await_buy_amount", "await_sell_amount", "await_replace_new",
    "settings_rename", "settings_budget", "settings_deviation",
    "st_wizard_name", "st_wizard_symbol", "st_wizard_capital_custom",
}

# Callback prefixes handled by portfolio bot
_PORTFOLIO_CB_PREFIXES = (
    "portfolio:", "paction:", "psettings:", "asset:", "confirm:",
    "wizard:", "staction:", "stbot:", "stwizard:", "action:",
)

_initialized = False
_init_lock   = threading.Lock()


def _init_portfolio(app: Application) -> None:
    """Initialise portfolio DB and resume running loops (called once at startup)."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        _initialized = True

    try:
        from portfolio.database import (
            init_db as portfolio_init_db,
            get_running_portfolios, get_portfolio, set_bot_running,
            list_portfolios, save_portfolio, update_portfolio_config,
            delete_portfolio,
            create_supertrend_bot, get_supertrend_bot, list_supertrend_bots,
            update_supertrend_bot_config, delete_supertrend_bot,
            get_running_supertrend_bots, get_supertrend_signals,
        )
        from portfolio.engine import (
            start_portfolio_loop, stop_portfolio_loop, is_portfolio_running,
            start_supertrend_loop, stop_supertrend_loop,
            is_supertrend_running, get_supertrend_loop_info,
        )
        from portfolio.smart_portfolio import execute_rebalance
        from portfolio.mexc_client import MEXCClient as PortfolioMEXCClient

        portfolio_init_db()

        for pid in get_running_portfolios():
            cfg = get_portfolio(pid)
            if cfg is None:
                set_bot_running(pid, False)
                continue
            log.info("Resuming portfolio loop %d", pid)
            start_portfolio_loop(pid)

        for st_id in get_running_supertrend_bots():
            if get_supertrend_bot(st_id) is None:
                continue
            log.info("Resuming SuperTrend bot %d", st_id)
            start_supertrend_loop(st_id)

        # Wire injected functions into portfolio telegram_bot module
        import portfolio.telegram_bot as _ptb

        _ptb._start_fn            = start_portfolio_loop
        _ptb._stop_fn             = stop_portfolio_loop
        _ptb._rebalance_fn        = lambda pid: execute_rebalance(
            PortfolioMEXCClient(), get_portfolio(pid) or {}, portfolio_id=pid
        )
        _ptb._list_portfolios     = list_portfolios
        _ptb._is_running_fn       = is_portfolio_running
        _ptb._get_portfolio_fn    = get_portfolio
        _ptb._save_portfolio_fn   = save_portfolio
        _ptb._update_portfolio_fn = update_portfolio_config
        _ptb._delete_portfolio_fn = delete_portfolio
        _ptb._buy_fn              = lambda sym, usdt: PortfolioMEXCClient().place_market_buy(sym, usdt)
        _ptb._sell_fn             = lambda sym, amt:  PortfolioMEXCClient().place_market_sell(sym, amt)
        _ptb._get_balances_fn     = lambda: PortfolioMEXCClient().get_all_balances()
        _ptb._st_start_fn         = start_supertrend_loop
        _ptb._st_stop_fn          = stop_supertrend_loop
        _ptb._st_is_running       = is_supertrend_running
        _ptb._st_create_fn        = create_supertrend_bot
        _ptb._st_get_fn           = get_supertrend_bot
        _ptb._st_list_fn          = list_supertrend_bots
        _ptb._st_update_fn        = update_supertrend_bot_config
        _ptb._st_delete_fn        = delete_supertrend_bot
        _ptb._st_signals_fn       = get_supertrend_signals
        _ptb._st_loop_info_fn     = get_supertrend_loop_info

        log.info("Portfolio bridge initialised")

    except Exception as exc:
        log.error("Portfolio bridge init failed: %s", exc)


async def _portfolio_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all portfolio callback queries to portfolio telegram_bot handler."""
    from portfolio.telegram_bot import handle_callback
    await handle_callback(update, ctx)


async def _portfolio_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route text messages when user is in a portfolio wizard state."""
    state = ctx.user_data.get("state", "")
    if state not in _PORTFOLIO_STATES:
        return
    from portfolio.telegram_bot import handle_message
    await handle_message(update, ctx)


async def _portfolio_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route /done command to portfolio wizard."""
    from portfolio.telegram_bot import cmd_done
    await cmd_done(update, ctx)


def register_portfolio_handlers(app: Application) -> None:
    """Register portfolio handlers into the grid bot's Application."""
    _init_portfolio(app)

    # Callback handler — matches all portfolio prefixes
    pattern = "^(" + "|".join(_PORTFOLIO_CB_PREFIXES) + ")"
    app.add_handler(CallbackQueryHandler(_portfolio_callback, pattern=pattern), group=5)

    # Message handler — only fires during portfolio wizard states (group=10)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _portfolio_message),
        group=10,
    )

    # /done command for portfolio wizard
    app.add_handler(CommandHandler("done", _portfolio_done), group=5)

    log.info("Portfolio handlers registered in grid bot application")
