"""
Entry point. Validates env, initialises DB + MEXC client, wires the
GridEngine to the Telegram bot, then starts long-polling.
"""
import logging

from config.settings import LOG_LEVEL, validate_env
from core.mexc_client import MexcClient
from core.grid_engine import GridEngine, set_notifiers as grid_set_notifiers
from core.strategy_engine import StrategyEngine, set_notifiers as snr_set_notifiers
from bot.telegram_bot import (
    build_application,
    send_notification,
    notify_buy_filled,
    notify_sell_filled,
    notify_grid_rebuild,
    notify_grid_expansion,
    notify_error,
    notify_balance_drift,
    notify_snr_buy_filled,
    notify_snr_sell_filled,
    notify_snr_refresh,
)
from utils.db_manager import init_db, close_db, get_all_active_grids, get_all_active_strategies

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
    logger.info("Upgrading %d grid(s)…", len(symbols))
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
    logger.info("Bot starting up…")

    await init_db()

    client = application.bot_data["client"]
    await client.load_markets()

    # ── Recover Grid strategies ────────────────────────────────────────────────
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
            except Exception as exc:
                logger.error("Failed to recover grid %s: %s", symbol, exc)
                failed += 1
                continue
        await _upgrade_existing_grids(engine)
        upgraded = recovered

    # ── Recover S&R strategies ─────────────────────────────────────────────────
    snr_engine: StrategyEngine = application.bot_data["snr_engine"]
    snr_active = await get_all_active_strategies()
    snr_recovered, snr_failed = 0, 0

    for row in snr_active:
        symbol = row["symbol"]
        try:
            await snr_engine.restore(
                symbol=symbol,
                timeframe=row["timeframe"],
                total_investment=float(row["total_investment"]),
                held_qty=float(row.get("held_qty") or 0),
                avg_buy_price=float(row.get("avg_buy_price") or 0),
                realized_pnl=float(row.get("realized_pnl") or 0),
                buy_count=int(row.get("buy_count") or 0),
                sell_count=int(row.get("sell_count") or 0),
                mode=row.get("level_mode") or "both",
            )
            snr_recovered += 1
        except Exception as exc:
            logger.error("Failed to recover S&R strategy %s: %s", symbol, exc)
            snr_failed += 1

    await send_notification(
        "🤖 *AI Grid Bot* — تم التشغيل بنجاح!\n"
        f"📊 شبكات Grid مستردة: `{recovered}` | مُرقَّاة: `{upgraded}` | فشلت: `{failed}`\n"
        f"📈 استراتيجيات S&R مستردة: `{snr_recovered}` | فشلت: `{snr_failed}`\n"
        "اكتب /menu للقائمة التفاعلية.",
        application=application,
    )
    logger.info("Startup complete. Grids=%d S&R=%d", recovered, snr_recovered)


async def _on_shutdown(application) -> None:
    logger.info("Shutting down…")
    engine:     GridEngine     = application.bot_data["engine"]
    snr_engine: StrategyEngine = application.bot_data["snr_engine"]
    client:     MexcClient     = application.bot_data["client"]

    for symbol in list(engine.active_symbols()):
        try:
            await engine.stop(symbol, market_sell=False)
        except Exception as exc:
            logger.error("Error stopping grid %s: %s", symbol, exc)

    for symbol in list(snr_engine.active_symbols()):
        try:
            # persist=False keeps is_active=TRUE in DB so the strategy
            # is restored automatically on the next startup.
            await snr_engine.stop(symbol, market_sell=False, persist=False)
        except Exception as exc:
            logger.error("Error stopping S&R %s: %s", symbol, exc)

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

    engine     = GridEngine(client=client, notify=notify)
    snr_engine = StrategyEngine(client=client)

    app = build_application(engine, client)
    _notify_ref["app"] = app

    # Grid notifiers
    grid_set_notifiers(
        buy_filled     = notify_buy_filled,
        sell_filled    = notify_sell_filled,
        grid_rebuild   = notify_grid_rebuild,
        grid_expansion = notify_grid_expansion,
        error          = notify_error,
        balance_drift  = notify_balance_drift,
    )

    # S&R notifiers
    snr_set_notifiers(
        buy_filled  = notify_snr_buy_filled,
        sell_filled = notify_snr_sell_filled,
        sr_refresh  = notify_snr_refresh,
        error       = notify_error,
    )

    app.bot_data["client"]     = client
    app.bot_data["engine"]     = engine
    app.bot_data["snr_engine"] = snr_engine
    app.post_init     = _on_startup
    app.post_shutdown = _on_shutdown

    logger.info("Starting Telegram long-polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
