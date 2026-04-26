"""
Database manager for SuperConsensus Bot.

Uses the same asyncpg pool as the grid bot (shared DATABASE_URL).
Tables: super_consensus_bots, super_consensus_trades, super_consensus_logs
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from config.settings import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


# ── Pool lifecycle ─────────────────────────────────────────────────────────────

async def init_super_db() -> None:
    global _pool
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    _pool = await asyncpg.create_pool(
        url,
        min_size=1,
        max_size=5,
        statement_cache_size=0,
    )
    await _create_tables()
    logger.info("SuperConsensus DB pool initialised")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("SuperConsensus DB pool not initialised")
    return _pool


async def close_super_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("SuperConsensus DB pool closed")


# ── Schema ─────────────────────────────────────────────────────────────────────

async def _create_tables() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS super_consensus_bots (
                id                          SERIAL PRIMARY KEY,
                symbol                      TEXT NOT NULL UNIQUE,
                total_investment            NUMERIC NOT NULL,
                current_position_qty        NUMERIC NOT NULL DEFAULT 0,
                current_position_entry_price NUMERIC NOT NULL DEFAULT 0,
                current_position_entry_time TIMESTAMPTZ,
                realized_pnl                NUMERIC NOT NULL DEFAULT 0,
                total_trades                INTEGER NOT NULL DEFAULT 0,
                is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
                is_paused                   BOOLEAN NOT NULL DEFAULT FALSE,
                started_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS super_consensus_trades (
                id              SERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL,
                side            TEXT NOT NULL,
                entry_price     NUMERIC NOT NULL,
                qty             NUMERIC NOT NULL,
                pnl             NUMERIC NOT NULL DEFAULT 0,
                consensus_votes INTEGER NOT NULL DEFAULT 0,
                executed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS super_consensus_logs (
                id              SERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL,
                timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                price           NUMERIC NOT NULL,
                rsi_signal      TEXT NOT NULL DEFAULT 'NEUTRAL',
                macd_signal     TEXT NOT NULL DEFAULT 'NEUTRAL',
                ema_signal      TEXT NOT NULL DEFAULT 'NEUTRAL',
                bb_signal       TEXT NOT NULL DEFAULT 'NEUTRAL',
                volume_signal   TEXT NOT NULL DEFAULT 'NEUTRAL',
                buy_votes       INTEGER NOT NULL DEFAULT 0,
                sell_votes      INTEGER NOT NULL DEFAULT 0,
                action_taken    TEXT NOT NULL DEFAULT 'none'
            );
        """)
    logger.debug("SuperConsensus tables verified / created")


# ── super_consensus_bots CRUD ──────────────────────────────────────────────────

async def upsert_bot(state) -> None:
    """Insert or update a bot row from a BotState object."""
    pool = get_pool()
    pos  = state.position
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO super_consensus_bots
                (symbol, total_investment, current_position_qty,
                 current_position_entry_price, current_position_entry_time,
                 realized_pnl, total_trades, is_active, is_paused)
            VALUES ($1, $2::numeric, $3::numeric, $4::numeric, $5, $6::numeric, $7, $8, $9)
            ON CONFLICT (symbol) DO UPDATE SET
                total_investment             = EXCLUDED.total_investment,
                current_position_qty         = EXCLUDED.current_position_qty,
                current_position_entry_price = EXCLUDED.current_position_entry_price,
                current_position_entry_time  = EXCLUDED.current_position_entry_time,
                realized_pnl                 = EXCLUDED.realized_pnl,
                total_trades                 = EXCLUDED.total_trades,
                is_active                    = EXCLUDED.is_active,
                is_paused                    = EXCLUDED.is_paused
            """,
            state.symbol,
            float(state.total_investment),
            float(pos.qty) if pos else 0.0,
            float(pos.entry_price) if pos else 0.0,
            pos.entry_time if pos else None,
            float(state.realized_pnl),
            int(state.total_trades),
            bool(state.is_active),
            bool(state.is_paused),
        )


async def get_bot(symbol: str) -> Optional[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM super_consensus_bots WHERE symbol = $1 AND is_active = TRUE",
            symbol.upper(),
        )
    return dict(row) if row else None


async def get_all_active_bots() -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM super_consensus_bots WHERE is_active = TRUE ORDER BY started_at"
        )
    return [dict(r) for r in rows]


async def deactivate_bot(symbol: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE super_consensus_bots SET is_active = FALSE WHERE symbol = $1",
            symbol.upper(),
        )


# ── super_consensus_trades ─────────────────────────────────────────────────────

async def record_trade(data: dict) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO super_consensus_trades
                (symbol, side, entry_price, qty, pnl, consensus_votes)
            VALUES ($1, $2, $3::numeric, $4::numeric, $5::numeric, $6)
            """,
            data["symbol"],
            data["side"],
            float(data["entry_price"]),
            float(data["qty"]),
            float(data.get("pnl", 0.0)),
            int(data.get("consensus_votes", 0)),
        )


async def get_trades(symbol: str, limit: int = 20) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM super_consensus_trades
            WHERE symbol = $1
            ORDER BY executed_at DESC
            LIMIT $2
            """,
            symbol.upper(), limit,
        )
    return [dict(r) for r in rows]


# ── super_consensus_logs ───────────────────────────────────────────────────────

async def log_cycle(data: dict) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO super_consensus_logs
                (symbol, timestamp, price,
                 rsi_signal, macd_signal, ema_signal, bb_signal, volume_signal,
                 buy_votes, sell_votes, action_taken)
            VALUES ($1, $2, $3::numeric, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            data["symbol"],
            data.get("timestamp", datetime.now(timezone.utc)),
            float(data["price"]),
            data.get("rsi_signal", "NEUTRAL"),
            data.get("macd_signal", "NEUTRAL"),
            data.get("ema_signal", "NEUTRAL"),
            data.get("bb_signal", "NEUTRAL"),
            data.get("volume_signal", "NEUTRAL"),
            int(data.get("buy_votes", 0)),
            int(data.get("sell_votes", 0)),
            data.get("action_taken", "none"),
        )


async def get_recent_logs(symbol: str, limit: int = 10) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM super_consensus_logs
            WHERE symbol = $1
            ORDER BY timestamp DESC
            LIMIT $2
            """,
            symbol.upper(), limit,
        )
    return [dict(r) for r in rows]


# ── DB function bundle (passed to engine) ──────────────────────────────────────

def make_db_fns(symbol: str) -> dict:
    """Return a dict of async callables for the consensus engine to use."""
    return {
        "log_cycle":    log_cycle,
        "record_trade": record_trade,
        "update_bot":   upsert_bot,
        "deactivate":   deactivate_bot,
    }
