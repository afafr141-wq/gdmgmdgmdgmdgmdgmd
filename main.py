"""
Entry point for the MEXC Passive Market Maker bot.

Startup sequence:
1. Validate required environment variables (including Supabase credentials).
2. Initialise the Supabase DatabaseManager singleton.
3. Build the Telegram Application.
4. Register a post-init hook that runs inside the event loop:
   a. Ensures the bot_state singleton row exists in Supabase.
   b. If is_active == true, re-attaches the WebSocket listener and
      Virtual Stop-Loss watcher automatically (no user command needed).
   c. Sends a startup notification to all ALLOWED_USER_IDS.
5. Start long-polling.

Railway deployment notes:
- Runs as a `worker` dyno (no inbound HTTP port needed).
- Set DATABASE_URL in the Railway Variables tab (Supabase pooler connection string).
- Set LOG_LEVEL=DEBUG for verbose output during debugging.
"""

import logging
import sys

from telegram.ext import Application

from config.settings import LOG_LEVEL, validate_env
from utils.db_manager import init_db  # async — awaited inside _on_startup
from bot.telegram_bot import build_application, recover_state

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
# Reduce noise from third-party libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── Post-init hook (runs inside the event loop, before polling starts) ─────────

async def _on_startup(app: Application) -> None:
    """
    Called by python-telegram-bot after the event loop is running but before
    the first update is processed.  Safe to await coroutines and create Tasks.

    init_db() is awaited here (not in main()) because asyncpg.create_pool()
    requires a running event loop.
    """
    logger.info("Running startup hook…")
    await init_db()
    logger.info("Database ready")
    await recover_state(app)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    missing = validate_env()
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        logger.error(
            "Set them in Railway's dashboard (Variables tab) or in a local .env file."
        )
        sys.exit(1)

    logger.info("Starting MEXC Market Maker bot…")
    app = build_application()

    # Register the startup hook via post_init.
    # python-telegram-bot v20 calls this coroutine after Application.initialize()
    # but before polling begins, so the event loop is already running.
    app.post_init = _on_startup

    # run_polling() manages its own event loop and handles graceful shutdown
    # on SIGINT / SIGTERM automatically (python-telegram-bot v20+).
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
