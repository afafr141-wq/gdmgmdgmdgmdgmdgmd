"""
Async PostgreSQL manager (asyncpg + PgBouncer transaction mode).

Tables managed here:
  active_grids    – one row per running grid bot
  trade_history   – every filled buy/sell
  grid_snapshots  – full grid state saved before shutdown / rebuild
  bot_config      – global key-value settings
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from config.settings import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


# ── Pool lifecycle ─────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create the connection pool and ensure all tables exist."""
    global _pool
    # Normalise postgres:// → postgresql:// for asyncpg
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    _pool = await asyncpg.create_pool(
        url,
        min_size=1,
        max_size=5,
        statement_cache_size=0,   # required for PgBouncer transaction mode
    )
    await _create_tables()
    logger.info("Database pool initialised")


def get_db() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised – call init_db() first")
    return _pool


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


# ── Schema ─────────────────────────────────────────────────────────────────────

async def _create_tables() -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS active_grids (
                id              SERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL UNIQUE,
                risk_level      TEXT NOT NULL DEFAULT 'medium',
                total_investment NUMERIC NOT NULL,
                lower_price     NUMERIC NOT NULL,
                upper_price     NUMERIC NOT NULL,
                grid_count      INTEGER NOT NULL,
                grid_spacing    NUMERIC NOT NULL,
                current_atr     NUMERIC,
                avg_buy_price   NUMERIC DEFAULT 0,
                held_qty        NUMERIC DEFAULT 0,
                realized_pnl    NUMERIC DEFAULT 0,
                sell_count      INTEGER DEFAULT 0,
                started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                extra           JSONB DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS trade_history (
                id          SERIAL PRIMARY KEY,
                symbol      TEXT NOT NULL,
                side        TEXT NOT NULL,          -- 'buy' | 'sell'
                price       NUMERIC NOT NULL,
                qty         NUMERIC NOT NULL,
                order_id    TEXT,
                grid_id     INTEGER REFERENCES active_grids(id),
                pnl         NUMERIC DEFAULT 0,
                executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS grid_snapshots (
                id          SERIAL PRIMARY KEY,
                symbol      TEXT NOT NULL,
                snapshot    JSONB NOT NULL,
                saved_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS bot_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS active_strategies (
                id               SERIAL PRIMARY KEY,
                symbol           TEXT NOT NULL UNIQUE,
                timeframe        TEXT NOT NULL DEFAULT '1h',
                total_investment NUMERIC NOT NULL,
                support1         NUMERIC NOT NULL,
                support2         NUMERIC NOT NULL,
                resistance1      NUMERIC NOT NULL,
                resistance2      NUMERIC NOT NULL,
                level_mode       TEXT NOT NULL DEFAULT 'both',
                held_qty         NUMERIC DEFAULT 0,
                avg_buy_price    NUMERIC DEFAULT 0,
                realized_pnl     NUMERIC DEFAULT 0,
                buy_count        INTEGER DEFAULT 0,
                sell_count       INTEGER DEFAULT 0,
                started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                is_active        BOOLEAN NOT NULL DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS snr_trade_history (
                id          SERIAL PRIMARY KEY,
                symbol      TEXT NOT NULL,
                side        TEXT NOT NULL,
                price       NUMERIC NOT NULL,
                qty         NUMERIC NOT NULL,
                order_id    TEXT,
                strategy_id INTEGER REFERENCES active_strategies(id),
                pnl         NUMERIC DEFAULT 0,
                level       TEXT DEFAULT '',
                executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
    # Migrate existing databases: add level_mode column if absent
    async with pool.acquire() as conn:
        await conn.execute("""
            ALTER TABLE active_strategies
                ADD COLUMN IF NOT EXISTS level_mode TEXT NOT NULL DEFAULT 'both';
        """)
    logger.debug("Tables verified / created")


# ── active_grids CRUD ──────────────────────────────────────────────────────────

async def upsert_grid(data: dict) -> int:
    """
    Insert or update a grid row using a fixed-column UPSERT.
    All numeric columns are cast explicitly to avoid asyncpg type-inference
    errors ('could not determine data type of parameter $N').
    Only the keys present in `data` are written; missing keys keep their
    current DB value via DO UPDATE SET ... = COALESCE(EXCLUDED.col, col).
    """
    pool = get_db()

    # Provide defaults for every column so the INSERT path always works
    row = {
        "symbol":           data.get("symbol", ""),
        "risk_level":       data.get("risk_level", "medium"),
        "total_investment": float(data.get("total_investment", 0)),
        "lower_price":      float(data.get("lower_price", 0)),
        "upper_price":      float(data.get("upper_price", 0)),
        "grid_count":       int(data.get("grid_count", 0)),
        "grid_spacing":     float(data.get("grid_spacing", 0)),
        "current_atr":      float(data.get("current_atr", 0)) if data.get("current_atr") is not None else None,
        "is_active":        bool(data.get("is_active", True)),
    }

    async with pool.acquire() as conn:
        grid_id = await conn.fetchval(
            """
            INSERT INTO active_grids
                (symbol, risk_level, total_investment, lower_price, upper_price,
                 grid_count, grid_spacing, current_atr, is_active)
            VALUES
                ($1, $2, $3::numeric, $4::numeric, $5::numeric,
                 $6::integer, $7::numeric, $8::numeric, $9::boolean)
            ON CONFLICT (symbol) DO UPDATE SET
                risk_level       = COALESCE(EXCLUDED.risk_level,       active_grids.risk_level),
                total_investment = CASE WHEN $3::numeric > 0
                                        THEN $3::numeric
                                        ELSE active_grids.total_investment END,
                lower_price      = CASE WHEN $4::numeric > 0
                                        THEN $4::numeric
                                        ELSE active_grids.lower_price END,
                upper_price      = CASE WHEN $5::numeric > 0
                                        THEN $5::numeric
                                        ELSE active_grids.upper_price END,
                grid_count       = CASE WHEN $6::integer > 0
                                        THEN $6::integer
                                        ELSE active_grids.grid_count END,
                grid_spacing     = CASE WHEN $7::numeric > 0
                                        THEN $7::numeric
                                        ELSE active_grids.grid_spacing END,
                current_atr      = COALESCE($8::numeric, active_grids.current_atr),
                is_active        = EXCLUDED.is_active,
                updated_at       = NOW()
            RETURNING id
            """,
            row["symbol"],
            row["risk_level"],
            row["total_investment"],
            row["lower_price"],
            row["upper_price"],
            row["grid_count"],
            row["grid_spacing"],
            row["current_atr"],
            row["is_active"],
        )
    return grid_id


async def get_grid(symbol: str) -> Optional[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM active_grids WHERE symbol = $1 AND is_active = TRUE", symbol
        )
    return dict(row) if row else None


async def get_all_active_grids() -> list[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM active_grids WHERE is_active = TRUE ORDER BY started_at"
        )
    return [dict(r) for r in rows]


async def deactivate_grid(symbol: str) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE active_grids SET is_active = FALSE, updated_at = NOW() WHERE symbol = $1",
            symbol,
        )


async def update_grid_pnl(
    symbol: str,
    realized_pnl: float,
    avg_buy_price: float,
    held_qty: float,
    sell_count: int,
) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE active_grids
               SET realized_pnl = $2, avg_buy_price = $3,
                   held_qty = $4, sell_count = $5, updated_at = NOW()
               WHERE symbol = $1""",
            symbol, realized_pnl, avg_buy_price, held_qty, sell_count,
        )


# ── trade_history ──────────────────────────────────────────────────────────────

async def record_trade(
    symbol: str,
    side: str,
    price: float,
    qty: float,
    order_id: str = "",
    grid_id: Optional[int] = None,
    pnl: float = 0.0,
) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO trade_history (symbol, side, price, qty, order_id, grid_id, pnl)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            symbol, side, price, qty, order_id, grid_id, pnl,
        )


async def get_trade_history(symbol: str, days: int = 30) -> list[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM trade_history
               WHERE symbol = $1
                 AND executed_at >= NOW() - ($2 * INTERVAL '1 day')
               ORDER BY executed_at DESC""",
            symbol, days,
        )
    return [dict(r) for r in rows]


# ── grid_snapshots ─────────────────────────────────────────────────────────────

async def save_snapshot(symbol: str, snapshot: dict) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO grid_snapshots (symbol, snapshot) VALUES ($1, $2)",
            symbol, json.dumps(snapshot),
        )


async def get_latest_snapshot(symbol: str) -> Optional[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT snapshot FROM grid_snapshots WHERE symbol = $1 ORDER BY saved_at DESC LIMIT 1",
            symbol,
        )
    return json.loads(row["snapshot"]) if row else None


# ── bot_config ─────────────────────────────────────────────────────────────────

async def set_config(key: str, value: str) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO bot_config (key, value, updated_at) VALUES ($1, $2, NOW())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
            key, value,
        )


async def get_config(key: str, default: str = "") -> str:
    pool = get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM bot_config WHERE key = $1", key)
    return row["value"] if row else default


# ── active_strategies (S&R) ────────────────────────────────────────────────────

async def upsert_strategy(data: dict) -> int:
    pool = get_db()
    row = {
        "symbol":           data.get("symbol", ""),
        "timeframe":        data.get("timeframe", "1h"),
        "total_investment": float(data.get("total_investment", 0)),
        "support1":         float(data.get("support1", 0)),
        "support2":         float(data.get("support2", 0)),
        "resistance1":      float(data.get("resistance1", 0)),
        "resistance2":      float(data.get("resistance2", 0)),
        "level_mode":       str(data.get("level_mode", "both")),
        "is_active":        bool(data.get("is_active", True)),
    }
    async with pool.acquire() as conn:
        sid = await conn.fetchval(
            """
            INSERT INTO active_strategies
                (symbol, timeframe, total_investment,
                 support1, support2, resistance1, resistance2, level_mode, is_active)
            VALUES ($1, $2, $3::numeric, $4::numeric, $5::numeric,
                    $6::numeric, $7::numeric, $8, $9)
            ON CONFLICT (symbol) DO UPDATE SET
                timeframe        = COALESCE(EXCLUDED.timeframe,        active_strategies.timeframe),
                total_investment = CASE WHEN $3::numeric > 0
                                        THEN $3::numeric
                                        ELSE active_strategies.total_investment END,
                support1         = CASE WHEN $4::numeric > 0
                                        THEN $4::numeric
                                        ELSE active_strategies.support1 END,
                support2         = CASE WHEN $5::numeric > 0
                                        THEN $5::numeric
                                        ELSE active_strategies.support2 END,
                resistance1      = CASE WHEN $6::numeric > 0
                                        THEN $6::numeric
                                        ELSE active_strategies.resistance1 END,
                resistance2      = CASE WHEN $7::numeric > 0
                                        THEN $7::numeric
                                        ELSE active_strategies.resistance2 END,
                level_mode       = COALESCE(EXCLUDED.level_mode, active_strategies.level_mode),
                is_active        = EXCLUDED.is_active,
                updated_at       = NOW()
            RETURNING id
            """,
            row["symbol"], row["timeframe"], row["total_investment"],
            row["support1"], row["support2"], row["resistance1"], row["resistance2"],
            row["level_mode"], row["is_active"],
        )
    return sid


async def get_all_active_strategies() -> list[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM active_strategies WHERE is_active = TRUE ORDER BY started_at"
        )
    return [dict(r) for r in rows]


async def deactivate_strategy(symbol: str) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE active_strategies SET is_active = FALSE, updated_at = NOW() WHERE symbol = $1",
            symbol,
        )


async def update_strategy_state(
    symbol: str,
    held_qty: float,
    avg_buy_price: float,
    realized_pnl: float,
    buy_count: int,
    sell_count: int,
) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE active_strategies
               SET held_qty = $2, avg_buy_price = $3, realized_pnl = $4,
                   buy_count = $5, sell_count = $6, updated_at = NOW()
               WHERE symbol = $1""",
            symbol, held_qty, avg_buy_price, realized_pnl, buy_count, sell_count,
        )


async def record_snr_trade(
    symbol: str,
    side: str,
    price: float,
    qty: float,
    order_id: str = "",
    strategy_id: Optional[int] = None,
    pnl: float = 0.0,
    level: str = "",
) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO snr_trade_history
               (symbol, side, price, qty, order_id, strategy_id, pnl, level)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            symbol, side, price, qty, order_id, strategy_id, pnl, level,
        )


async def get_snr_trade_history(symbol: str, days: int = 30) -> list[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM snr_trade_history
               WHERE symbol = $1
                 AND executed_at >= NOW() - ($2 * INTERVAL '1 day')
               ORDER BY executed_at DESC""",
            symbol, days,
        )
    return [dict(r) for r in rows]
