"""
Entry point. Validates env, initialises DB + MEXC client, wires the
GridEngine to the Telegram bot, then starts long-polling.

Portfolio bot (PORTFOLIO_BOT_TOKEN) runs in a background thread with its
own asyncio event loop so both bots operate independently.
"""
import logging
import os
import threading

from config.settings import LOG_LEVEL, validate_env
from core.mexc_client import MexcClient
from core.grid_engine import GridEngine, set_notifiers as grid_set_notifiers
from bot.telegram_bot import (
    build_application,
    send_notification,
    notify_buy_filled,
    notify_sell_filled,
    notify_grid_rebuild,
    notify_grid_expansion,
    notify_error,
    notify_balance_drift,
)
from utils.db_manager import (
    init_db, close_db,
    get_all_active_grids,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def _upgrade_existing_grids(engine: GridEngine) -> None:
    symbols = engine.active_symbols()
    if not symbols:
        return
    logger.info("Upgrading %d grid(s)...", len(symbols))
    ok, failed = [], []
    for sym in symbols:
        try:
            await engine.upgrade_grid(sym)
            ok.append(sym)
        except Exception as exc:
            logger.error("upgrade_grid %s failed: %s", sym, exc)
            failed.append(sym)
    logger.info("Upgrade complete — ok=%s failed=%s", ok, failed)


async def _on_startup(application) -> None:
    logger.info("Bot starting up...")

    await init_db()

    client = application.bot_data["client"]
    await client.load_markets()

    engine: GridEngine = application.bot_data["engine"]
    active = await get_all_active_grids()
    recovered, upgraded, failed = 0, 0, 0

    if active:
        logger.info("Recovering %d active grid(s) from DB...", len(active))
        for row in active:
            symbol = row["symbol"]
            try:
                await engine.start(
                    symbol=symbol,
                    total_investment=float(row["total_investment"]),
                    risk=row["risk_level"],
                    num_grids=int(row["grid_count"]) // 2,
                    upper_pct=float(row.get("upper_pct") or 3.0),
                    lower_pct=float(row.get("lower_pct") or 3.0),
                )
                recovered += 1
            except Exception as exc:
                logger.error("Failed to recover grid %s: %s", symbol, exc)
                failed += 1
                continue
        await _upgrade_existing_grids(engine)
        upgraded = recovered

    await send_notification(
        "*Grid Bot* — تم التشغيل بنجاح!\n"
        f"شبكات مستردة: `{recovered}` | مرقاة: `{upgraded}` | فشلت: `{failed}`\n"
        "اكتب /menu للقائمة التفاعلية.",
        application=application,
    )
    logger.info("Startup complete. Grids=%d", recovered)


async def _on_shutdown(application) -> None:
    logger.info("Shutting down...")
    engine: GridEngine = application.bot_data["engine"]
    client: MexcClient = application.bot_data["client"]

    for symbol in list(engine.active_symbols()):
        try:
            await engine.stop(symbol, market_sell=False)
        except Exception as exc:
            logger.error("Error stopping grid %s: %s", symbol, exc)

    await client.close()
    await close_db()
    logger.info("Shutdown complete.")


def _start_portfolio_bot() -> None:
    """Start the portfolio bot in a background thread if PORTFOLIO_BOT_TOKEN is set."""
    token = os.environ.get("PORTFOLIO_BOT_TOKEN", "").strip()
    if not token:
        logger.info("PORTFOLIO_BOT_TOKEN not set — portfolio bot disabled")
        return

    def _run() -> None:
        import asyncio
        from portfolio.database import init_db as portfolio_init_db, get_running_portfolios, get_portfolio, set_bot_running, list_portfolios, save_portfolio, update_portfolio_config, create_supertrend_bot, get_supertrend_bot, list_supertrend_bots, update_supertrend_bot_config, delete_supertrend_bot, get_running_supertrend_bots, get_supertrend_signals
        from portfolio.engine import start_portfolio_loop, stop_portfolio_loop, is_portfolio_running, start_supertrend_loop, stop_supertrend_loop, is_supertrend_running, get_supertrend_loop_info
        from portfolio.smart_portfolio import execute_rebalance
        from portfolio.mexc_client import MEXCClient as PortfolioMEXCClient
        from portfolio.telegram_bot import run_bot

        portfolio_init_db()

        for pid in get_running_portfolios():
            cfg = get_portfolio(pid)
            if cfg is None:
                set_bot_running(pid, False)
                continue
            logger.info("Resuming portfolio loop %d", pid)
            start_portfolio_loop(pid)

        for st_id in get_running_supertrend_bots():
            if get_supertrend_bot(st_id) is None:
                continue
            logger.info("Resuming SuperTrend bot %d", st_id)
            start_supertrend_loop(st_id)

        def _rebalance_fn(portfolio_id: int) -> list:
            cfg = get_portfolio(portfolio_id)
            if cfg is None:
                return []
            return execute_rebalance(PortfolioMEXCClient(), cfg, portfolio_id=portfolio_id)

        def _buy_fn(symbol: str, usdt_amount: float) -> dict:
            return PortfolioMEXCClient().place_market_buy(symbol, usdt_amount)

        def _sell_fn(symbol: str, base_amount: float) -> dict:
            return PortfolioMEXCClient().place_market_sell(symbol, base_amount)

        def _get_balances_fn() -> dict:
            return PortfolioMEXCClient().get_all_balances()

        # Override token so run_bot picks up PORTFOLIO_BOT_TOKEN
        os.environ["TELEGRAM_BOT_TOKEN"] = token

        run_bot(
            start_fn=start_portfolio_loop,
            stop_fn=stop_portfolio_loop,
            rebalance_fn=_rebalance_fn,
            list_portfolios_fn=list_portfolios,
            is_running_fn=is_portfolio_running,
            get_portfolio_fn=get_portfolio,
            save_portfolio_fn=save_portfolio,
            update_portfolio_fn=update_portfolio_config,
            buy_fn=_buy_fn,
            sell_fn=_sell_fn,
            get_balances_fn=_get_balances_fn,
            st_start_fn=start_supertrend_loop,
            st_stop_fn=stop_supertrend_loop,
            st_is_running_fn=is_supertrend_running,
            st_create_fn=create_supertrend_bot,
            st_get_fn=get_supertrend_bot,
            st_list_fn=list_supertrend_bots,
            st_update_fn=update_supertrend_bot_config,
            st_delete_fn=delete_supertrend_bot,
            st_signals_fn=get_supertrend_signals,
            st_loop_info_fn=get_supertrend_loop_info,
        )

    t = threading.Thread(target=_run, daemon=True, name="portfolio-bot")
    t.start()
    logger.info("Portfolio bot thread started")


def main() -> None:
    validate_env()

    client = MexcClient()
    _notify_ref: dict = {}

    async def notify(text: str) -> None:
        app = _notify_ref.get("app")
        await send_notification(text, application=app)

    engine = GridEngine(client=client, notify=notify)

    app = build_application(engine, client)
    _notify_ref["app"] = app

    grid_set_notifiers(
        buy_filled     = notify_buy_filled,
        sell_filled    = notify_sell_filled,
        grid_rebuild   = notify_grid_rebuild,
        grid_expansion = notify_grid_expansion,
        error          = notify_error,
        balance_drift  = notify_balance_drift,
    )

    app.bot_data["client"] = client
    app.bot_data["engine"] = engine

    app.post_init     = _on_startup
    app.post_shutdown = _on_shutdown

    _start_portfolio_bot()

    logger.info("Starting Telegram long-polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
