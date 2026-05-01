"""
Entry point. Validates env, initialises DB + MEXC client, wires the
GridEngine and PAStrategyEngine to the Telegram bot, then starts long-polling.
"""
import logging

from config.settings import LOG_LEVEL, validate_env
from core.mexc_client import MexcClient
from core.grid_engine import GridEngine, set_notifiers as grid_set_notifiers
from core.pa_strategy_engine import PAStrategyEngine, set_notifiers as pa_set_notifiers
from core.liquidity_swings_strategy_engine import (
    LSStrategyEngine, LSParams,
    set_notifiers as ls_set_notifiers,
)
from bot.telegram_bot import (
    build_application,
    send_notification,
    notify_buy_filled,
    notify_sell_filled,
    notify_grid_rebuild,
    notify_grid_expansion,
    notify_error,
    notify_balance_drift,
    notify_pa_entry,
    notify_pa_tp_hit,
    notify_ls_entry,
    notify_ls_tp_hit,
    notify_ls_sl_hit,
)
from utils.db_manager import (
    init_db, close_db,
    get_all_active_grids,
    get_all_active_pa_strategies,
    get_all_active_ls_strategies,
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

    # ── Recover Grid strategies ────────────────────────────────────────────────
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

    # ── Recover PA strategies ──────────────────────────────────────────────────
    pa_engine: PAStrategyEngine = application.bot_data["pa_engine"]
    pa_active = await get_all_active_pa_strategies()
    pa_recovered, pa_failed = 0, 0

    for row in pa_active:
        symbol = row["symbol"]
        try:
            await pa_engine.restore(
                symbol          = symbol,
                timeframe       = row["timeframe"],
                capital_pct     = float(row.get("capital_pct") or 10),
                held_qty        = float(row.get("held_qty") or 0),
                avg_entry_price = float(row.get("avg_entry_price") or 0),
                direction       = row.get("direction") or "",
                tp_price        = float(row.get("tp_price") or 0),
                realized_pnl    = float(row.get("realized_pnl") or 0),
                trade_count     = int(row.get("trade_count") or 0),
                win_count       = int(row.get("win_count") or 0),
            )
            pa_recovered += 1
        except Exception as exc:
            logger.error("Failed to recover PA strategy %s: %s", symbol, exc)
            pa_failed += 1

    # ── Recover Liquidity Swings strategies ───────────────────────────────────
    ls_engine: LSStrategyEngine = application.bot_data["ls_engine"]
    ls_active = await get_all_active_ls_strategies()
    ls_recovered, ls_failed = 0, 0

    for row in ls_active:
        symbol = row["symbol"]
        try:
            params = LSParams(
                pivot_left    = int(row.get("pivot_left", 14)),
                pivot_right   = int(row.get("pivot_right", 14)),
                swing_area    = row.get("swing_area", "Wick Extremity"),
                min_touches   = int(row.get("min_touches", 2)),
                sl_buffer_pct = float(row.get("sl_buffer_pct", 0.3)),
            )
            await ls_engine.restore(
                symbol          = symbol,
                timeframe       = row["timeframe"],
                capital_pct     = float(row.get("capital_pct") or 10),
                params          = params,
                held_qty        = float(row.get("held_qty") or 0),
                avg_entry_price = float(row.get("avg_entry_price") or 0),
                tp_price        = float(row.get("tp_price") or 0),
                sl_price        = float(row.get("sl_price") or 0),
                realized_pnl    = float(row.get("realized_pnl") or 0),
                trade_count     = int(row.get("trade_count") or 0),
                win_count       = int(row.get("win_count") or 0),
            )
            ls_recovered += 1
        except Exception as exc:
            logger.error("Failed to recover LS strategy %s: %s", symbol, exc)
            ls_failed += 1

    await send_notification(
        "*AI Grid Bot* — تم التشغيل بنجاح!\n"
        f"شبكات Grid مستردة: `{recovered}` | مرقاة: `{upgraded}` | فشلت: `{failed}`\n"
        f"استراتيجيات PA مستردة: `{pa_recovered}` | فشلت: `{pa_failed}`\n"
        f"استراتيجيات LS مستردة: `{ls_recovered}` | فشلت: `{ls_failed}`\n"
        "اكتب /menu للقائمة التفاعلية.",
        application=application,
    )
    logger.info("Startup complete. Grids=%d PA=%d LS=%d", recovered, pa_recovered, ls_recovered)


async def _on_shutdown(application) -> None:
    logger.info("Shutting down...")
    engine:    GridEngine       = application.bot_data["engine"]
    pa_engine: PAStrategyEngine = application.bot_data["pa_engine"]
    ls_engine: LSStrategyEngine = application.bot_data["ls_engine"]
    client:    MexcClient       = application.bot_data["client"]

    for symbol in list(engine.active_symbols()):
        try:
            await engine.stop(symbol, market_sell=False)
        except Exception as exc:
            logger.error("Error stopping grid %s: %s", symbol, exc)

    for symbol in list(pa_engine.active_symbols()):
        try:
            await pa_engine.stop(symbol, market_sell=False, persist=False)
        except Exception as exc:
            logger.error("Error stopping PA %s: %s", symbol, exc)

    for symbol in list(ls_engine.active_symbols()):
        try:
            await ls_engine.stop(symbol, market_sell=False, persist=False)
        except Exception as exc:
            logger.error("Error stopping LS %s: %s", symbol, exc)

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

    engine    = GridEngine(client=client, notify=notify)
    pa_engine = PAStrategyEngine(client=client)
    ls_engine = LSStrategyEngine(client=client)

    app = build_application(engine, client, pa_engine=pa_engine, ls_engine=ls_engine)
    _notify_ref["app"] = app

    grid_set_notifiers(
        buy_filled     = notify_buy_filled,
        sell_filled    = notify_sell_filled,
        grid_rebuild   = notify_grid_rebuild,
        grid_expansion = notify_grid_expansion,
        error          = notify_error,
        balance_drift  = notify_balance_drift,
    )

    pa_set_notifiers(
        entry  = notify_pa_entry,
        tp_hit = notify_pa_tp_hit,
        error  = notify_error,
    )

    ls_set_notifiers(
        entry  = notify_ls_entry,
        tp_hit = notify_ls_tp_hit,
        sl_hit = notify_ls_sl_hit,
        error  = notify_error,
    )

    app.bot_data["client"]    = client
    app.bot_data["engine"]    = engine
    app.bot_data["pa_engine"] = pa_engine
    app.bot_data["ls_engine"] = ls_engine
    app.post_init     = _on_startup
    app.post_shutdown = _on_shutdown

    logger.info("Starting Telegram long-polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
