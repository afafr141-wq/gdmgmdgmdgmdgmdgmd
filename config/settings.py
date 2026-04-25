"""
Central configuration: env-var loading and risk-profile presets.
"""
import os
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Credentials ────────────────────────────────────────────────────────────────
MEXC_API_KEY: str = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET: str = os.getenv("MEXC_API_SECRET", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Allowed Telegram users ─────────────────────────────────────────────────────
_raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = (
    {int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip()}
    if _raw_ids
    else set()
)

# ── Order management ───────────────────────────────────────────────────────────
ORDER_SLEEP_SECONDS: float = 0.25   # pause between REST calls to respect rate limits
FILL_POLL_INTERVAL: int = 10        # seconds between fill-check cycles


def validate_env() -> None:
    """Raise if any required variable is missing."""
    missing = [
        name
        for name, val in [
            ("MEXC_API_KEY", MEXC_API_KEY),
            ("MEXC_API_SECRET", MEXC_API_SECRET),
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("DATABASE_URL", DATABASE_URL),
        ]
        if not val
    ]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")
    logger.info("Environment validated OK")
