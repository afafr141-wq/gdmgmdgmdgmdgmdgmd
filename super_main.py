"""
SuperConsensus Bot — entry point.

Run independently from the grid bot:
  python super_main.py

Shares the same MEXC credentials and DATABASE_URL from .env,
but uses separate DB tables and a separate Telegram polling loop.
"""
import asyncio
import logging
import sys

from config.settings import LOG_LEVEL, validate_env
from core.mexc_client import MexcClient
from super_consensus.core.consensus_engine import ConsensusEngine
from super_consensus.core.auto_trade_engine import AutoTradeEngine
from super_consensus.bot.super_bot import (
    build_super_application,
    send_super_notification,
)
from super_consensus.utils.super_db import (
    init_super_db,
    close_super_db,
    get_all_active_bots,
    make_db_fns,
    upsert_bot,
    get_auto_settings,
    get_auto_positions,
)
from super_consensus.core.giants_engine import GIANTS

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def _on_startup(application) -> None:
    logger.info("SuperConsensus Bot starting up…")

    # DB
    await init_super_db()

    # MEXC
    client: MexcClient = application.bot_data["client"]
    await client.load_markets()

    # ── Recover manual consensus bots ──────────────────────────────────────────
    engine: ConsensusEngine = application.bot_data["super_engine"]
    active = await get_all_active_bots()
    recovered = 0

    if active:
        logger.info("Recovering %d active SuperConsensus bot(s)…", len(active))
        for row in active:
            symbol = row["symbol"]
            try:
                db_fns = make_db_fns(symbol)
                await engine.start(
                    symbol=symbol,
                    total_investment=float(row["total_investment"]),
                    db_save_fn=upsert_bot,
                )
                # Re-wire the loop with db_fns
                state = engine.get_state(symbol)
                if state and state._task:
                    state._task.cancel()
                task = asyncio.create_task(
                    engine._loop(symbol, db_fns), name=f"super_{symbol}"
                )
                state._task = task

                # Restore open position from DB if any
                if float(row["current_position_qty"]) > 0:
                    from super_consensus.core.consensus_engine import Position
                    from datetime import timezone
                    state.position = Position(
                        qty=float(row["current_position_qty"]),
                        entry_price=float(row["current_position_entry_price"]),
                        entry_time=row["current_position_entry_time"].replace(tzinfo=timezone.utc)
                        if row["current_position_entry_time"]
                        else __import__("datetime").datetime.now(timezone.utc),
                        cost_usdt=float(row["current_position_qty"])
                        * float(row["current_position_entry_price"]),
                    )

                state.realized_pnl = float(row["realized_pnl"])
                state.total_trades = int(row["total_trades"])
                state.is_paused    = bool(row["is_paused"])

                recovered += 1
                logger.info("Recovered SuperConsensus bot: %s", symbol)
            except Exception as exc:
                logger.error("Failed to recover bot %s: %s", symbol, exc)

    # ── Recover Auto-Trade Engine ───────────────────────────────────────────────
    auto_engine: AutoTradeEngine = application.bot_data["auto_engine"]
    auto_recovered = False
    try:
        auto_settings = await get_auto_settings()
        auto_positions = await get_auto_positions()
        if auto_settings:
            await auto_engine.restore_from_db(auto_settings, auto_positions)
            auto_recovered = auto_settings.get("is_active", False)
            logger.info(
                "AutoTradeEngine restored: active=%s, positions=%d",
                auto_recovered, len(auto_positions),
            )
    except Exception as exc:
        logger.error("Failed to restore AutoTradeEngine: %s", exc)

    # ── Startup notification ───────────────────────────────────────────────────
    auto_line = (
        f"🤖 *الوضع الآلي:* مُستعاد ✅ ({len(auto_positions)} صفقة مفتوحة)"
        if auto_recovered
        else "🤖 *الوضع الآلي:* موقوف — فعّله بـ /auto\\_mode"
    )

    await send_super_notification(
        f"🤖 *SuperConsensus Bot* — تم التشغيل!\n"
        f"📊 بوتات يدوية مستردة: `{recovered}`\n"
        f"{auto_line}\n"
        f"👑 العمالقة: `{', '.join(g.name for g in GIANTS)}`\n"
        f"🔍 الماسح الذكي: جاهز — اكتب /scan لمسح السوق\n"
        "اكتب /super\\_menu للقائمة الرئيسية.",
        application=application,
    )
    logger.info("SuperConsensus startup complete. Recovered %d bot(s).", recovered)


async def _on_shutdown(application) -> None:
    logger.info("SuperConsensus Bot shutting down…")
    engine: ConsensusEngine  = application.bot_data["super_engine"]
    auto_engine: AutoTradeEngine = application.bot_data["auto_engine"]
    client: MexcClient       = application.bot_data["client"]

    # Stop manual consensus bots (keep positions open across restarts)
    for symbol in list(engine.active_symbols()):
        try:
            await engine.stop(symbol, market_sell=False)
        except Exception as exc:
            logger.error("Error stopping bot %s: %s", symbol, exc)

    # Stop auto-trade background tasks (positions persist in DB)
    await auto_engine.disable()

    await client.close()
    await close_super_db()
    logger.info("SuperConsensus shutdown complete.")


def main() -> None:
    validate_env()

    client = MexcClient()

    _notify_ref: dict = {}

    async def notify(text: str) -> None:
        app = _notify_ref.get("app")
        await send_super_notification(text, application=app)

    engine      = ConsensusEngine(client=client, notify=notify)
    auto_engine = AutoTradeEngine(client=client, notify=notify)

    app = build_super_application(engine, client, auto_engine=auto_engine)
    _notify_ref["app"] = app

    app.bot_data["client"]       = client
    app.bot_data["super_engine"] = engine
    app.bot_data["auto_engine"]  = auto_engine

    app.post_init     = _on_startup
    app.post_shutdown = _on_shutdown

    logger.info("Starting SuperConsensus Bot (long-polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
