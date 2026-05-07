"""
Entry point. Validates env, initialises DB + MEXC client, wires the
GridEngine to the Telegram bot, then starts long-polling.
"""
import logging
from decimal import Decimal

from config.settings import (
    LOG_LEVEL, validate_env,
    BSC_WS_RPC_URL, BSC_HTTP_RPC_URL, BSC_PRIVATE_RPC_URL,
    BSC_WS_USERNAME, BSC_WS_PASSWORD,
    COPY_TARGET_WALLET, MY_BSC_PRIVATE_KEY,
    COPY_TRADE_USDT, COPY_SELLS, COPY_TRADE_ENABLED,
)
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

    # Start copy-trade engine if configured
    copy_engine = application.bot_data.get("copy_engine")
    if copy_engine is not None:
        await copy_engine.start()
        logger.info("CopyTradeEngine started")

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

    copy_engine = application.bot_data.get("copy_engine")
    if copy_engine is not None:
        await copy_engine.stop()

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

    # Wire copy-trade engine if all required vars are present
    # BSC_WS_RPC_URL is optional — engine falls back to block polling
    logger.info("=== COPY TRADE ENV CHECK ===")
    logger.info("MY_BSC_PRIVATE_KEY : %s", "SET" if MY_BSC_PRIVATE_KEY else "MISSING ❌")
    logger.info("COPY_TARGET_WALLET : %s", COPY_TARGET_WALLET[:10] + "..." if COPY_TARGET_WALLET else "MISSING ❌")
    logger.info("BSC_HTTP_RPC_URL   : %s", BSC_HTTP_RPC_URL[:40] + "..." if BSC_HTTP_RPC_URL else "MISSING ❌")
    logger.info("BSC_WS_RPC_URL     : %s", (BSC_WS_RPC_URL[:40] + "...") if BSC_WS_RPC_URL else "NOT SET (block polling mode)")
    logger.info("COPY_TRADE_ENABLED : %s", COPY_TRADE_ENABLED)
    logger.info("============================")

    if MY_BSC_PRIVATE_KEY:
        from core.copy_trade_engine import CopyTradeEngine, set_copy_notifiers
        from bot.copy_bot import (
            register_copy_handlers, set_copy_engine,
            notify_copy_buy, notify_copy_sell, notify_copy_err,
        )

        copy_engine = CopyTradeEngine(
            ws_rpc_url      = BSC_WS_RPC_URL,
            http_rpc_url    = BSC_HTTP_RPC_URL,
            target_wallet   = COPY_TARGET_WALLET,
            my_private_key  = MY_BSC_PRIVATE_KEY,
            trade_usdt      = Decimal(str(COPY_TRADE_USDT)),
            copy_sells      = COPY_SELLS,
            enabled         = COPY_TRADE_ENABLED,
            private_rpc_url = BSC_PRIVATE_RPC_URL,
            ws_username     = BSC_WS_USERNAME,
            ws_password     = BSC_WS_PASSWORD,
        )

        set_copy_notifiers(
            buy  = notify_copy_buy,
            sell = notify_copy_sell,
            err  = notify_copy_err,
        )
        set_copy_engine(copy_engine)
        register_copy_handlers(app)

        app.bot_data["copy_engine"] = copy_engine
        logger.info("CopyTradeEngine configured — target: %s", COPY_TARGET_WALLET[:10])
    else:
        logger.warning("CopyTradeEngine not started — MY_BSC_PRIVATE_KEY is missing")

    app.post_init     = _on_startup
    app.post_shutdown = _on_shutdown

    logger.info("Starting Telegram long-polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
