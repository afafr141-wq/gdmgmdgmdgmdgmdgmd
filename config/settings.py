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

# ── Risk profiles ──────────────────────────────────────────────────────────────
# Each profile controls how the AI grid engine sizes the range and grid count.
RISK_PROFILES: dict[str, dict] = {
    "low": {
        "atr_multiplier": 1.5,   # range = ATR × multiplier (each side)
        "min_grids": 5,
        "max_grids": 10,
        "atr_period": 14,
        "rebalance_trigger": 1.0,  # rebuild when price moves ≥ 1× ATR outside range
    },
    "medium": {
        "atr_multiplier": 2.0,
        "min_grids": 8,
        "max_grids": 20,
        "atr_period": 14,
        "rebalance_trigger": 1.0,
    },
    "high": {
        "atr_multiplier": 3.0,
        "min_grids": 15,
        "max_grids": 30,
        "atr_period": 14,
        "rebalance_trigger": 1.0,
    },
}

# ── ATR recalculation interval ─────────────────────────────────────────────────
ATR_REFRESH_SECONDS: int = 300   # every 5 minutes
CANDLE_TIMEFRAME: str = "15m"    # 15-minute candles for ATR

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
