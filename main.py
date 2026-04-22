"""
Entry point for the MEXC Passive Market Maker bot.

Startup sequence:
1. Validate required environment variables.
2. Build the Telegram Application.
3. Register a post-init hook that runs inside the event loop:
   a. Reads data/state.json.
   b. If an active trade exists, re-attaches the WebSocket listener and
      Virtual Stop-Loss watcher automatically (no user command needed).
   c. Sends a startup notification to all ALLOWED_USER_IDS.
4. Start long-polling.

Railway deployment notes:
- Runs as a `worker` dyno (no inbound HTTP port needed).
- Set STATE_PATH=/data/state.json and mount a Railway volume at /data so
  state survives deploys and container restarts.
- Set LOG_LEVEL=DEBUG for verbose output during debugging.
"""

import asyncio
import logging
import sys

from telegram.ext import Application

from config.settings import LOG_LEVEL, validate_env
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
    the first update is processed.  Safe to create asyncio Tasks here.
    """
    logger.info("Running startup hook…")
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
        allowed_updates=["message"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
