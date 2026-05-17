"""
Entry point. Validates env, initialises DB + MEXC client, wires the
GridEngine to the Telegram bot, then starts long-polling.

Portfolio management is embedded in the same bot via portfolio_bridge.
"""
import logging

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
from bot.smart_scalp_bridge import (
    register_smart_scalp_handlers,
    init_smart_scalp,
    set_app as smart_scalp_set_app,
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

    status = "🟢 جاهز" if not failed else f"⚠️ جاهز مع `{failed}` خطأ"
    await send_notification(
        f"🤖 *AI Grid Bot — تم التشغيل*\n"
        f"─────────────────────────\n"
        f"✅ مستردة:  `{recovered}`\n"
        f"🔄 مُرقاة:  `{upgraded}`\n"
        f"❌ فشلت:   `{failed}`\n"
        f"─────────────────────────\n"
        f"📡 الحالة: {status}\n"
        f"اكتب /menu للقائمة الرئيسية.\n"
        f"اكتب /quick_menu لاستراتيجية SmartScalp.",
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

    init_smart_scalp(client)
    smart_scalp_set_app(app)
    register_smart_scalp_handlers(app)

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

    logger.info("Starting Telegram long-polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
