"""
Central configuration: loads env vars and provides symbol precision helpers.

On Railway, all variables are injected directly into the process environment —
load_dotenv() is a no-op there but still works for local development.

Precision data is fetched once from MEXC /api/v3/exchangeInfo and cached
so every module can call get_precision(symbol) without extra HTTP calls.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()  # no-op on Railway; used for local .env files

logger = logging.getLogger(__name__)

# ── Credentials ────────────────────────────────────────────────────────────────
MEXC_API_KEY: str = os.getenv("MEXC_API_KEY", "")
MEXC_SECRET_KEY: str = os.getenv("MEXC_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Comma-separated Telegram user IDs allowed to control the bot
_raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = {
    int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip().isdigit()
}

# ── Runtime paths ──────────────────────────────────────────────────────────────
# On Railway, mount a persistent volume at /data and set STATE_PATH=/data/state.json
# so state survives deploys. Falls back to local data/ for development.
STATE_PATH: str = os.getenv("STATE_PATH", "data/state.json")

# ── Logging ────────────────────────────────────────────────────────────────────
# Set LOG_LEVEL=DEBUG in Railway dashboard for verbose output during debugging.
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── MEXC REST endpoints ────────────────────────────────────────────────────────
MEXC_BASE_URL: str = "https://api.mexc.com"
MEXC_WS_URL: str = "wss://wbs.mexc.com/ws"

# ── WebSocket settings ─────────────────────────────────────────────────────────
WS_HEARTBEAT_INTERVAL: int = 20   # seconds between PING frames
WS_RECONNECT_DELAY: int = 5       # seconds before first reconnect attempt

# ── Strategy defaults (overridden per /setup call) ────────────────────────────
DEFAULT_WALL_MULTIPLIER: float = 3.0   # volume > avg * multiplier → wall
DEFAULT_SMA_PERIOD: int = 20
DEFAULT_STOP_LOSS_PCT: float = 0.985   # 1.5% below entry
DEFAULT_TAKE_PROFIT_PCT: float = 1.02  # 2% above entry

# ── Precision cache ────────────────────────────────────────────────────────────
# Populated at startup by fetch_precision(); shape:
#   { "BTCUSDT": {"price_precision": 2, "qty_precision": 5, "tick_size": "0.01"} }
_precision_cache: dict[str, dict] = {}


def set_precision(symbol: str, price_precision: int, qty_precision: int, tick_size: str) -> None:
    """Store precision data for a symbol (called after exchangeInfo fetch)."""
    _precision_cache[symbol.upper()] = {
        "price_precision": price_precision,
        "qty_precision": qty_precision,
        "tick_size": tick_size,
    }
    logger.debug("Precision cached for %s: price=%d qty=%d tick=%s",
                 symbol, price_precision, qty_precision, tick_size)


def get_precision(symbol: str) -> dict:
    """
    Return cached precision dict for symbol.
    Falls back to safe defaults if symbol was never fetched.
    """
    return _precision_cache.get(symbol.upper(), {
        "price_precision": 8,
        "qty_precision": 8,
        "tick_size": "0.00000001",
    })


def validate_env() -> list[str]:
    """Return a list of missing required env-var names."""
    missing = []
    if not MEXC_API_KEY:
        missing.append("MEXC_API_KEY")
    if not MEXC_SECRET_KEY:
        missing.append("MEXC_SECRET_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not ALLOWED_USER_IDS:
        missing.append("ALLOWED_USER_IDS")
    return missing
