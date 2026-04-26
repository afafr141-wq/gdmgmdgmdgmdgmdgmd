"""
Entry point. Validates env, initialises DB + MEXC client, wires the
GridEngine and ConsensusEngine to the Telegram bot, then starts long-polling.
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
from super_consensus.core.consensus_engine import ConsensusEngine
from super_consensus.core.auto_trade_engine import AutoTradeEngine
from super_consensus.utils.super_db import (
    init_super_db,
    close_super_db,
    get_all_active_bots,
    get_auto_settings,
    get_auto_positions,
    make_db_fns,
    upsert_bot,
)

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

    # ── Databases ──────────────────────────────────────────────────────────────
    await init_db()
    await init_super_db()

    # ── MEXC ───────────────────────────────────────────────────────────────────
    client = application.bot_data["client"]
    await client.load_markets()

    # ── Recover grid bots ──────────────────────────────────────────────────────
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

        await _upgrade_existing_grids(engine, application)
        upgraded = recovered

    # ── Recover SuperConsensus bots ────────────────────────────────────────────
    super_engine: ConsensusEngine = application.bot_data["super_engine"]
    super_active = await get_all_active_bots()
    super_recovered = 0

    if super_active:
        logger.info("Recovering %d SuperConsensus bot(s)…", len(super_active))
        for row in super_active:
            symbol = row["symbol"]
            try:
                db_fns = make_db_fns(symbol)
                from super_consensus.core.consensus_engine import BotState
                state = BotState(symbol=symbol, total_investment=float(row["total_investment"]))
                super_engine._bots[symbol] = state

                task = asyncio.create_task(
                    super_engine._loop(symbol, db_fns), name=f"super_{symbol}"
                )
                state._task = task

                # Restore open position if any
                if float(row["current_position_qty"]) > 0:
                    from super_consensus.core.consensus_engine import Position
                    from datetime import timezone
                    import datetime as _dt
                    state.position = Position(
                        qty=float(row["current_position_qty"]),
                        entry_price=float(row["current_position_entry_price"]),
                        entry_time=(
                            row["current_position_entry_time"].replace(tzinfo=timezone.utc)
                            if row["current_position_entry_time"]
                            else _dt.datetime.now(timezone.utc)
                        ),
                        cost_usdt=float(row["current_position_qty"])
                        * float(row["current_position_entry_price"]),
                    )

                state.realized_pnl = float(row["realized_pnl"])
                state.total_trades = int(row["total_trades"])
                state.is_paused    = bool(row["is_paused"])

                super_recovered += 1
                logger.info("Recovered SuperConsensus bot: %s", symbol)
            except Exception as exc:
                logger.error("Failed to recover SuperConsensus bot %s: %s", symbol, exc)

    # ── Recover AutoTradeEngine ────────────────────────────────────────────────
    auto_engine: AutoTradeEngine = application.bot_data["auto_engine"]
    auto_recovered = False
    auto_positions_count = 0
    try:
        auto_settings = await get_auto_settings()
        auto_positions = await get_auto_positions()
        auto_positions_count = len(auto_positions)
        if auto_settings:
            await auto_engine.restore_from_db(auto_settings, auto_positions)
            auto_recovered = bool(auto_settings.get("is_active", False))
            logger.info(
                "AutoTradeEngine restored: active=%s positions=%d",
                auto_recovered, auto_positions_count,
            )
    except Exception as exc:
        logger.error("Failed to restore AutoTradeEngine: %s", exc)

    # ── Startup notification ───────────────────────────────────────────────────
    auto_line = (
        f"🤖 الوضع الآلي: مُستعاد ✅ ({auto_positions_count} صفقة مفتوحة)"
        if auto_recovered
        else "🤖 الوضع الآلي: موقوف — فعّله بـ /menu"
    )
    await send_notification(
        "🤖 *AI Grid Bot* — تم التشغيل بنجاح!\n"
        f"📊 شبكات مستردة: `{recovered}` | مُرقَّاة: `{upgraded}` | فشلت: `{failed}`\n"
        f"🧠 *SuperConsensus* — بوتات مستردة: `{super_recovered}`\n"
        f"{auto_line}\n"
        "اكتب /menu للقائمة التفاعلية | /super\\_menu للإجماع.",
        application=application,
    )
    logger.info("Startup complete. Grids=%d SuperBots=%d", recovered, super_recovered)


async def _on_shutdown(application) -> None:
    """Called when the bot is shutting down."""
    logger.info("Shutting down…")
    engine: GridEngine = application.bot_data["engine"]
    client: MexcClient = application.bot_data["client"]
    super_engine: ConsensusEngine = application.bot_data.get("super_engine")

    # Stop all running grids gracefully (cancel orders, save snapshots)
    for symbol in list(engine.active_symbols()):
        try:
            await engine.stop(symbol, market_sell=False)
        except Exception as exc:
            logger.error("Error stopping grid %s: %s", symbol, exc)

    # Stop all SuperConsensus bots (keep positions open across restarts)
    if super_engine:
        for symbol in list(super_engine.active_symbols()):
            try:
                await super_engine.stop(symbol, market_sell=False)
            except Exception as exc:
                logger.error("Error stopping SuperConsensus bot %s: %s", symbol, exc)

    # Stop AutoTradeEngine background tasks (positions persist in DB)
    auto_engine: AutoTradeEngine = application.bot_data.get("auto_engine")
    if auto_engine:
        await auto_engine.disable()

    await client.close()
    await close_db()
    await close_super_db()
    logger.info("Shutdown complete.")


def main() -> None:
    validate_env()

    client = MexcClient()

    # Shared notify ref — filled after app is built
    _notify_ref: dict = {}

    async def notify(text: str) -> None:
        app = _notify_ref.get("app")
        await send_notification(text, application=app)

    # Grid engine
    engine = GridEngine(client=client, notify=notify)

    # SuperConsensus engine — same notify channel
    super_engine = ConsensusEngine(client=client, notify=notify)

    # AutoTrade engine — same notify channel
    auto_engine = AutoTradeEngine(client=client, notify=notify)

    # Build single Application with all handlers
    # menu_bot handlers are registered inside build_application
    app = build_application(engine, client, super_engine=super_engine)
    _notify_ref["app"] = app

    # Wire grid notification callbacks
    set_notifiers(
        buy_filled     = notify_buy_filled,
        sell_filled    = notify_sell_filled,
        grid_rebuild   = notify_grid_rebuild,
        grid_expansion = notify_grid_expansion,
        error          = notify_error,
    )

    # Inject shared objects for startup/shutdown hooks
    app.bot_data["client"]       = client
    app.bot_data["engine"]       = engine
    app.bot_data["super_engine"] = super_engine
    app.bot_data["auto_engine"]  = auto_engine

    app.post_init     = _on_startup
    app.post_shutdown = _on_shutdown

    logger.info("Starting Telegram long-polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
