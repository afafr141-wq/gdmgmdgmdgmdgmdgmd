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

# ── SuperConsensus AI ───────────────────────────────────────────────────────────
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
# Primary model (free tier). Override via env var to use GPT-4 etc.
OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
# Minimum minutes between AIJudge calls per symbol (rate-limit guard)
AI_JUDGE_INTERVAL_MINUTES: int = int(os.getenv("AI_JUDGE_INTERVAL_MINUTES", "10"))

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

# ── Auto-Trade Mode defaults ───────────────────────────────────────────────────
# All values are overridable at runtime via Telegram commands.
AUTO_SCAN_INTERVAL_MINUTES: int = int(os.getenv("AUTO_SCAN_INTERVAL_MINUTES", "60"))
AUTO_TAKE_PROFIT_PCT: float  = float(os.getenv("AUTO_TAKE_PROFIT_PCT", "3.0"))
AUTO_STOP_LOSS_PCT: float    = float(os.getenv("AUTO_STOP_LOSS_PCT", "2.0"))
AUTO_MAX_OPEN_TRADES: int    = int(os.getenv("AUTO_MAX_OPEN_TRADES", "2"))
AUTO_MAX_CAPITAL_PCT: float  = float(os.getenv("AUTO_MAX_CAPITAL_PCT", "70.0"))
AUTO_MAX_HOLD_HOURS: int     = int(os.getenv("AUTO_MAX_HOLD_HOURS", "24"))
AUTO_MIN_COINS_SCANNED: int  = int(os.getenv("AUTO_MIN_COINS_SCANNED", "30"))
AUTO_MIN_ANALYST_CONF: int   = int(os.getenv("AUTO_MIN_ANALYST_CONF", "75"))
AUTO_COOLDOWN_MINUTES: int   = int(os.getenv("AUTO_COOLDOWN_MINUTES", "120"))
AUTO_REPORT_INTERVAL_HOURS: int = int(os.getenv("AUTO_REPORT_INTERVAL_HOURS", "4"))


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
