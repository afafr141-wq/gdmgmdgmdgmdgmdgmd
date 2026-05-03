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

# ── Copy-trade (BSC / PancakeSwap V2 + GMGN) ──────────────────────────────────
BSC_WS_RPC_URL: str   = os.getenv("BSC_WS_RPC_URL", "")    # Ankr WebSocket endpoint
BSC_HTTP_RPC_URL: str = os.getenv("BSC_HTTP_RPC_URL", "https://bsc-dataseed1.binance.org/")  # BSC HTTP RPC
COPY_TARGET_WALLET: str = os.getenv(
    "COPY_TARGET_WALLET",
    "0x7e8fb0392542812476d9f2d0d71c01d1fa0776c5",
)
MY_BSC_PRIVATE_KEY: str = os.getenv("MY_BSC_PRIVATE_KEY", "")
COPY_TRADE_USDT: float  = float(os.getenv("COPY_TRADE_USDT", "3"))
COPY_SELLS: bool        = os.getenv("COPY_SELLS", "true").lower() == "true"
COPY_TRADE_ENABLED: bool = os.getenv("COPY_TRADE_ENABLED", "true").lower() == "true"
BSCSCAN_API_KEY: str    = os.getenv("BSCSCAN_API_KEY", "YourApiKeyToken")


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

    # Warn (don't raise) if required copy-trade vars are missing
    # BSC_WS_RPC_URL is optional — engine uses BSCScan HTTP polling when absent
    if not MY_BSC_PRIVATE_KEY:
        logger.warning("Copy-trade disabled — missing env var: MY_BSC_PRIVATE_KEY")
    else:
        logger.info("Copy-trade: using RPC %s", BSC_HTTP_RPC_URL)

    logger.info("Environment validated OK")
