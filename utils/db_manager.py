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
                upper_pct       NUMERIC NOT NULL DEFAULT 3.0,
                lower_pct       NUMERIC NOT NULL DEFAULT 3.0,
                started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                extra           JSONB DEFAULT '{}'
            );
            ALTER TABLE active_grids ADD COLUMN IF NOT EXISTS upper_pct NUMERIC NOT NULL DEFAULT 3.0;
            ALTER TABLE active_grids ADD COLUMN IF NOT EXISTS lower_pct NUMERIC NOT NULL DEFAULT 3.0;

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
        "upper_pct":        float(data.get("upper_pct", 3.0)),
        "lower_pct":        float(data.get("lower_pct", 3.0)),
    }

    async with pool.acquire() as conn:
        grid_id = await conn.fetchval(
            """
            INSERT INTO active_grids
                (symbol, risk_level, total_investment, lower_price, upper_price,
                 grid_count, grid_spacing, current_atr, is_active, upper_pct, lower_pct)
            VALUES
                ($1, $2, $3::numeric, $4::numeric, $5::numeric,
                 $6::integer, $7::numeric, $8::numeric, $9::boolean,
                 $10::numeric, $11::numeric)
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
                upper_pct        = CASE WHEN $10::numeric > 0
                                        THEN $10::numeric
                                        ELSE active_grids.upper_pct END,
                lower_pct        = CASE WHEN $11::numeric > 0
                                        THEN $11::numeric
                                        ELSE active_grids.lower_pct END,
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
            row["upper_pct"],
            row["lower_pct"],
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


# ── active_pa_strategies (Price Action) ───────────────────────────────────────

async def _create_pa_tables(conn) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS active_pa_strategies (
            id               SERIAL PRIMARY KEY,
            symbol           TEXT NOT NULL UNIQUE,
            timeframe        TEXT NOT NULL DEFAULT '1h',
            capital_pct      NUMERIC NOT NULL DEFAULT 10,
            held_qty         NUMERIC DEFAULT 0,
            avg_entry_price  NUMERIC DEFAULT 0,
            direction        TEXT DEFAULT '',
            tp_price         NUMERIC DEFAULT 0,
            realized_pnl     NUMERIC DEFAULT 0,
            trade_count      INTEGER DEFAULT 0,
            win_count        INTEGER DEFAULT 0,
            started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            is_active        BOOLEAN NOT NULL DEFAULT TRUE
        );

        CREATE TABLE IF NOT EXISTS pa_trade_history (
            id          SERIAL PRIMARY KEY,
            symbol      TEXT NOT NULL,
            side        TEXT NOT NULL,
            price       NUMERIC NOT NULL,
            qty         NUMERIC NOT NULL,
            order_id    TEXT DEFAULT '',
            strategy_id INTEGER REFERENCES active_pa_strategies(id),
            pnl         NUMERIC DEFAULT 0,
            pattern     TEXT DEFAULT '',
            trend       TEXT DEFAULT '',
            executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)


async def upsert_pa_strategy(data: dict) -> int:
    pool = get_db()
    async with pool.acquire() as conn:
        await _create_pa_tables(conn)
        sid = await conn.fetchval(
            """
            INSERT INTO active_pa_strategies
                (symbol, timeframe, capital_pct, is_active)
            VALUES ($1, $2, $3::numeric, $4)
            ON CONFLICT (symbol) DO UPDATE SET
                timeframe   = COALESCE(EXCLUDED.timeframe,   active_pa_strategies.timeframe),
                capital_pct = CASE WHEN $3::numeric > 0
                                   THEN $3::numeric
                                   ELSE active_pa_strategies.capital_pct END,
                is_active   = EXCLUDED.is_active,
                updated_at  = NOW()
            RETURNING id
            """,
            data.get("symbol", ""),
            data.get("timeframe", "1h"),
            float(data.get("capital_pct", 10)),
            bool(data.get("is_active", True)),
        )
    return sid


async def get_all_active_pa_strategies() -> list[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        await _create_pa_tables(conn)
        rows = await conn.fetch(
            "SELECT * FROM active_pa_strategies WHERE is_active = TRUE ORDER BY started_at"
        )
    return [dict(r) for r in rows]


async def deactivate_pa_strategy(symbol: str) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE active_pa_strategies SET is_active = FALSE, updated_at = NOW() WHERE symbol = $1",
            symbol,
        )


async def update_pa_strategy_state(
    symbol:          str,
    held_qty:        float,
    avg_entry_price: float,
    direction:       str,
    tp_price:        float,
    realized_pnl:    float,
    trade_count:     int,
    win_count:       int,
) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE active_pa_strategies
               SET held_qty = $2, avg_entry_price = $3, direction = $4,
                   tp_price = $5, realized_pnl = $6,
                   trade_count = $7, win_count = $8, updated_at = NOW()
               WHERE symbol = $1""",
            symbol, held_qty, avg_entry_price, direction,
            tp_price, realized_pnl, trade_count, win_count,
        )


async def record_pa_trade(
    symbol:      str,
    side:        str,
    price:       float,
    qty:         float,
    order_id:    str = "",
    strategy_id: Optional[int] = None,
    pnl:         float = 0.0,
    pattern:     str = "",
    trend:       str = "",
) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO pa_trade_history
               (symbol, side, price, qty, order_id, strategy_id, pnl, pattern, trend)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            symbol, side, price, qty, order_id, strategy_id, pnl, pattern, trend,
        )


async def get_pa_trade_history(symbol: str, days: int = 30) -> list[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM pa_trade_history
               WHERE symbol = $1
                 AND executed_at >= NOW() - ($2 * INTERVAL '1 day')
               ORDER BY executed_at DESC""",
            symbol, days,
        )
    return [dict(r) for r in rows]


# ── active_ls_strategies (Liquidity Swings) ────────────────────────────────────

async def _create_ls_tables(conn) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS active_ls_strategies (
            id               SERIAL PRIMARY KEY,
            symbol           TEXT NOT NULL UNIQUE,
            timeframe        TEXT NOT NULL DEFAULT '1h',
            capital_pct      NUMERIC NOT NULL DEFAULT 10,
            pivot_left       INTEGER NOT NULL DEFAULT 14,
            pivot_right      INTEGER NOT NULL DEFAULT 14,
            swing_area       TEXT NOT NULL DEFAULT 'Wick Extremity',
            min_touches      INTEGER NOT NULL DEFAULT 2,
            sl_buffer_pct    NUMERIC NOT NULL DEFAULT 0.3,
            held_qty         NUMERIC DEFAULT 0,
            avg_entry_price  NUMERIC DEFAULT 0,
            tp_price         NUMERIC DEFAULT 0,
            sl_price         NUMERIC DEFAULT 0,
            realized_pnl     NUMERIC DEFAULT 0,
            trade_count      INTEGER DEFAULT 0,
            win_count        INTEGER DEFAULT 0,
            started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            is_active        BOOLEAN NOT NULL DEFAULT TRUE
        );

        CREATE TABLE IF NOT EXISTS ls_trade_history (
            id          SERIAL PRIMARY KEY,
            symbol      TEXT NOT NULL,
            side        TEXT NOT NULL,
            price       NUMERIC NOT NULL,
            qty         NUMERIC NOT NULL,
            order_id    TEXT DEFAULT '',
            strategy_id INTEGER REFERENCES active_ls_strategies(id),
            pnl         NUMERIC DEFAULT 0,
            reason      TEXT DEFAULT '',
            executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)


async def upsert_ls_strategy(data: dict) -> int:
    pool = get_db()
    async with pool.acquire() as conn:
        await _create_ls_tables(conn)
        sid = await conn.fetchval(
            """
            INSERT INTO active_ls_strategies
                (symbol, timeframe, capital_pct, pivot_left, pivot_right,
                 swing_area, min_touches, sl_buffer_pct, is_active)
            VALUES ($1, $2, $3::numeric, $4::integer, $5::integer,
                    $6, $7::integer, $8::numeric, $9::boolean)
            ON CONFLICT (symbol) DO UPDATE SET
                timeframe     = COALESCE(EXCLUDED.timeframe,    active_ls_strategies.timeframe),
                capital_pct   = CASE WHEN $3::numeric > 0
                                     THEN $3::numeric
                                     ELSE active_ls_strategies.capital_pct END,
                pivot_left    = CASE WHEN $4::integer > 0
                                     THEN $4::integer
                                     ELSE active_ls_strategies.pivot_left END,
                pivot_right   = CASE WHEN $5::integer > 0
                                     THEN $5::integer
                                     ELSE active_ls_strategies.pivot_right END,
                swing_area    = COALESCE(EXCLUDED.swing_area,   active_ls_strategies.swing_area),
                min_touches   = CASE WHEN $7::integer > 0
                                     THEN $7::integer
                                     ELSE active_ls_strategies.min_touches END,
                sl_buffer_pct = CASE WHEN $8::numeric > 0
                                     THEN $8::numeric
                                     ELSE active_ls_strategies.sl_buffer_pct END,
                is_active     = EXCLUDED.is_active,
                updated_at    = NOW()
            RETURNING id
            """,
            data.get("symbol", ""),
            data.get("timeframe", "1h"),
            float(data.get("capital_pct", 10)),
            int(data.get("pivot_left", 14)),
            int(data.get("pivot_right", 14)),
            data.get("swing_area", "Wick Extremity"),
            int(data.get("min_touches", 2)),
            float(data.get("sl_buffer_pct", 0.3)),
            bool(data.get("is_active", True)),
        )
    return sid


async def get_all_active_ls_strategies() -> list[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        await _create_ls_tables(conn)
        rows = await conn.fetch(
            "SELECT * FROM active_ls_strategies WHERE is_active = TRUE ORDER BY started_at"
        )
    return [dict(r) for r in rows]


async def deactivate_ls_strategy(symbol: str) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE active_ls_strategies SET is_active = FALSE, updated_at = NOW() WHERE symbol = $1",
            symbol,
        )


async def update_ls_strategy_state(
    symbol: str, held_qty: float, avg_entry_price: float,
    tp_price: float, sl_price: float, realized_pnl: float,
    trade_count: int, win_count: int,
) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE active_ls_strategies
               SET held_qty = $2, avg_entry_price = $3,
                   tp_price = $4, sl_price = $5,
                   realized_pnl = $6, trade_count = $7,
                   win_count = $8, updated_at = NOW()
               WHERE symbol = $1""",
            symbol, held_qty, avg_entry_price,
            tp_price, sl_price, realized_pnl, trade_count, win_count,
        )


async def record_ls_trade(
    symbol: str, side: str, price: float, qty: float,
    order_id: str = "", strategy_id: Optional[int] = None,
    pnl: float = 0.0, reason: str = "",
) -> None:
    pool = get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ls_trade_history
               (symbol, side, price, qty, order_id, strategy_id, pnl, reason)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            symbol, side, price, qty, order_id, strategy_id, pnl, reason,
        )


async def get_ls_trade_history(symbol: str, days: int = 30) -> list[dict]:
    pool = get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM ls_trade_history
               WHERE symbol = $1
                 AND executed_at >= NOW() - ($2 * INTERVAL '1 day')
               ORDER BY executed_at DESC""",
            symbol, days,
        )
    return [dict(r) for r in rows]
