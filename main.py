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
from core.grid_engine import GridEngine
from bot.telegram_bot import build_application, send_notification
from utils.db_manager import init_db, close_db, get_all_active_grids

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


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
                logger.info("Recovered grid: %s", symbol)
            except Exception as exc:
                logger.error("Failed to recover grid %s: %s", symbol, exc)

    await send_notification(
        "🤖 *AI Grid Bot* — تم التشغيل بنجاح!\n"
        f"📊 شبكات نشطة: `{len(active)}`\n"
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

    # Inject shared objects so startup/shutdown hooks can access them
    app.bot_data["client"] = client
    app.bot_data["engine"] = engine

    app.post_init = _on_startup
    app.post_shutdown = _on_shutdown

    logger.info("Starting Telegram long-polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
