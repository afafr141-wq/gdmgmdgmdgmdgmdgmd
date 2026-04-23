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
        """)
    logger.debug("Tables verified / created")


# ── active_grids CRUD ──────────────────────────────────────────────────────────

async def upsert_grid(data: dict) -> int:
    """Insert or update a grid row. Returns the row id."""
    pool = get_db()
    symbol = data["symbol"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM active_grids WHERE symbol = $1", symbol
        )
        if row:
            grid_id = row["id"]
            sets = ", ".join(
                f"{k} = ${i+2}" for i, k in enumerate(data.keys()) if k != "symbol"
            )
            vals = [v for k, v in data.items() if k != "symbol"]
            await conn.execute(
                f"UPDATE active_grids SET {sets}, updated_at = NOW() WHERE id = $1",
                grid_id, *vals,
            )
        else:
            cols = ", ".join(data.keys())
            placeholders = ", ".join(f"${i+1}" for i in range(len(data)))
            grid_id = await conn.fetchval(
                f"INSERT INTO active_grids ({cols}) VALUES ({placeholders}) RETURNING id",
                *data.values(),
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
                 AND executed_at >= NOW() - ($2 || ' days')::INTERVAL
               ORDER BY executed_at DESC""",
            symbol, str(days),
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
