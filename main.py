"""
Entry point. Validates env, initialises DB + MEXC client, wires the
GridEngine to the Telegram bot, then starts long-polling.
"""
import asyncio
import logging
import signal
import sys

from config.settings import LOG_LEVEL, validate_env
from core.mexc_client import MexcClient
from core.grid_engine import GridEngine, set_notifiers
from bot.telegram_bot import (
    build_application,
    send_notification,
    notify_buy_filled,
    notify_sell_filled,
    notify_grid_rebuild,
    notify_grid_expansion,
    notify_error,
)
from utils.db_manager import init_db, close_db, get_all_active_grids

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def _upgrade_existing_grids(engine: GridEngine, application) -> None:
    """Rebuild all active grids at the current price/ATR after a restart."""
    symbols = engine.active_symbols()
    if not symbols:
        return
    logger.info("Upgrading %d grid(s) to latest engine logic…", len(symbols))
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
    """Called once after the bot is initialised, before polling starts."""
    logger.info("Bot starting up…")

    # DB
    await init_db()

    # MEXC
    client = application.bot_data["client"]
    await client.load_markets()

    # Recover any grids that were active before a restart
    engine: GridEngine = application.bot_data["engine"]
    active = await get_all_active_grids()
    recovered, upgraded, failed = 0, 0, 0
    if active:
        logger.info("Recovering %d active grid(s) from DB…", len(active))
        for row in active:
            symbol = row["symbol"]
            try:
                await engine.start(
                    symbol=symbol,
                    total_investment=float(row["total_investment"]),
                    risk=row["risk_level"],
                )
                recovered += 1
                logger.info("Recovered grid: %s", symbol)
            except Exception as exc:
                logger.error("Failed to recover grid %s: %s", symbol, exc)
                failed += 1
                continue

        # Upgrade all recovered grids to the latest engine logic
        await _upgrade_existing_grids(engine, application)
        upgraded = recovered

    await send_notification(
        "🤖 *AI Grid Bot* — تم التشغيل بنجاح!\n"
        f"📊 شبكات مستردة: `{recovered}` | مُرقَّاة: `{upgraded}` | فشلت: `{failed}`\n"
        "اكتب /start للقائمة الرئيسية.",
        application=application,
    )
    logger.info("Startup complete.")


async def _on_shutdown(application) -> None:
    """Called when the bot is shutting down."""
    logger.info("Shutting down…")
    engine: GridEngine = application.bot_data["engine"]
    client: MexcClient = application.bot_data["client"]

    # Stop all running grids gracefully (cancel orders, save snapshots)
    for symbol in list(engine.active_symbols()):
        try:
            await engine.stop(symbol, market_sell=False)
        except Exception as exc:
            logger.error("Error stopping grid %s: %s", symbol, exc)

    await client.close()
    await close_db()
    logger.info("Shutdown complete.")


def main() -> None:
    validate_env()

    client = MexcClient()

    # Notify callback — needs the application reference, injected after build
    _notify_ref: dict = {}

    async def notify(text: str) -> None:
        app = _notify_ref.get("app")
        await send_notification(text, application=app)

    engine = GridEngine(client=client, notify=notify)

    app = build_application(engine, client)
    _notify_ref["app"] = app

    # Wire notification functions into the engine (after app is built
    # so _application is set inside telegram_bot)
    set_notifiers(
        buy_filled    = notify_buy_filled,
        sell_filled   = notify_sell_filled,
        grid_rebuild  = notify_grid_rebuild,
        grid_expansion = notify_grid_expansion,
        error         = notify_error,
    )

    # Inject shared objects so startup/shutdown hooks can access them
    app.bot_data["client"] = client
    app.bot_data["engine"] = engine

    app.post_init = _on_startup
    app.post_shutdown = _on_shutdown

    logger.info("Starting Telegram long-polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
