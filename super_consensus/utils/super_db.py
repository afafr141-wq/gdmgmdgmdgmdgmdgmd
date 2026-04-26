"""
Database manager for SuperConsensus Bot.

Uses the same asyncpg pool as the grid bot (shared DATABASE_URL).
Tables:
  super_consensus_bots      — per-symbol manual bots
  super_consensus_trades    — trade log for manual bots
  super_consensus_logs      — cycle logs for manual bots
  auto_mode_settings        — single-row global auto-trade config
  auto_mode_trades          — trade log for auto-trade mode
  auto_mode_positions       — currently open auto-trade positions
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

            -- Auto-Trade Mode: global settings (one active row at a time)
            CREATE TABLE IF NOT EXISTS auto_mode_settings (
                id                  SERIAL PRIMARY KEY,
                is_active           BOOLEAN NOT NULL DEFAULT FALSE,
                trade_amount_usdt   NUMERIC,          -- fixed USDT per trade (NULL = use risk_pct)
                risk_pct            NUMERIC,          -- % of free capital per trade (NULL = use fixed)
                take_profit_pct     NUMERIC NOT NULL DEFAULT 3.0,
                stop_loss_pct       NUMERIC NOT NULL DEFAULT 2.0,
                max_open_trades     INTEGER NOT NULL DEFAULT 2,
                max_capital_pct     NUMERIC NOT NULL DEFAULT 70.0,
                max_hold_hours      INTEGER NOT NULL DEFAULT 24,
                min_coins_scanned   INTEGER NOT NULL DEFAULT 30,
                min_analyst_conf    INTEGER NOT NULL DEFAULT 75,
                cooldown_minutes    INTEGER NOT NULL DEFAULT 120,
                scan_interval_min   INTEGER NOT NULL DEFAULT 60,
                report_interval_hrs INTEGER NOT NULL DEFAULT 4,
                total_capital_usdt  NUMERIC NOT NULL DEFAULT 0,
                realized_pnl        NUMERIC NOT NULL DEFAULT 0,
                total_auto_trades   INTEGER NOT NULL DEFAULT 0,
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            -- Auto-Trade Mode: currently open positions
            CREATE TABLE IF NOT EXISTS auto_mode_positions (
                id              SERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL UNIQUE,
                qty             NUMERIC NOT NULL,
                entry_price     NUMERIC NOT NULL,
                entry_time      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                cost_usdt       NUMERIC NOT NULL,
                take_profit_pct NUMERIC NOT NULL,
                stop_loss_pct   NUMERIC NOT NULL
            );

            -- Auto-Trade Mode: completed trade history
            CREATE TABLE IF NOT EXISTS auto_mode_trades (
                id              SERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL,
                side            TEXT NOT NULL,
                price           NUMERIC NOT NULL,
                qty             NUMERIC NOT NULL,
                cost_usdt       NUMERIC NOT NULL DEFAULT 0,
                pnl             NUMERIC NOT NULL DEFAULT 0,
                exit_reason     TEXT NOT NULL DEFAULT 'unknown',
                analysts_conf   INTEGER NOT NULL DEFAULT 0,
                executed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
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


# ══════════════════════════════════════════════════════════════════════════════
# Auto-Trade Mode — DB helpers
# ══════════════════════════════════════════════════════════════════════════════

async def get_auto_settings() -> Optional[dict]:
    """Return the single auto_mode_settings row, or None if not yet created."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM auto_mode_settings ORDER BY id LIMIT 1"
        )
    return dict(row) if row else None


async def upsert_auto_settings(data: dict) -> None:
    """Insert or update the global auto-trade settings row."""
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM auto_mode_settings ORDER BY id LIMIT 1"
        )
        if existing:
            await conn.execute(
                """
                UPDATE auto_mode_settings SET
                    is_active           = $1,
                    trade_amount_usdt   = $2,
                    risk_pct            = $3,
                    take_profit_pct     = $4,
                    stop_loss_pct       = $5,
                    max_open_trades     = $6,
                    max_capital_pct     = $7,
                    max_hold_hours      = $8,
                    min_coins_scanned   = $9,
                    min_analyst_conf    = $10,
                    cooldown_minutes    = $11,
                    scan_interval_min   = $12,
                    report_interval_hrs = $13,
                    total_capital_usdt  = $14,
                    realized_pnl        = $15,
                    total_auto_trades   = $16,
                    updated_at          = NOW()
                WHERE id = $17
                """,
                bool(data.get("is_active", False)),
                data.get("trade_amount_usdt"),
                data.get("risk_pct"),
                float(data.get("take_profit_pct", 3.0)),
                float(data.get("stop_loss_pct", 2.0)),
                int(data.get("max_open_trades", 2)),
                float(data.get("max_capital_pct", 70.0)),
                int(data.get("max_hold_hours", 24)),
                int(data.get("min_coins_scanned", 30)),
                int(data.get("min_analyst_conf", 75)),
                int(data.get("cooldown_minutes", 120)),
                int(data.get("scan_interval_min", 60)),
                int(data.get("report_interval_hrs", 4)),
                float(data.get("total_capital_usdt", 0)),
                float(data.get("realized_pnl", 0)),
                int(data.get("total_auto_trades", 0)),
                existing["id"],
            )
        else:
            await conn.execute(
                """
                INSERT INTO auto_mode_settings (
                    is_active, trade_amount_usdt, risk_pct,
                    take_profit_pct, stop_loss_pct, max_open_trades,
                    max_capital_pct, max_hold_hours, min_coins_scanned,
                    min_analyst_conf, cooldown_minutes, scan_interval_min,
                    report_interval_hrs, total_capital_usdt, realized_pnl,
                    total_auto_trades
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                """,
                bool(data.get("is_active", False)),
                data.get("trade_amount_usdt"),
                data.get("risk_pct"),
                float(data.get("take_profit_pct", 3.0)),
                float(data.get("stop_loss_pct", 2.0)),
                int(data.get("max_open_trades", 2)),
                float(data.get("max_capital_pct", 70.0)),
                int(data.get("max_hold_hours", 24)),
                int(data.get("min_coins_scanned", 30)),
                int(data.get("min_analyst_conf", 75)),
                int(data.get("cooldown_minutes", 120)),
                int(data.get("scan_interval_min", 60)),
                int(data.get("report_interval_hrs", 4)),
                float(data.get("total_capital_usdt", 0)),
                float(data.get("realized_pnl", 0)),
                int(data.get("total_auto_trades", 0)),
            )


# ── auto_mode_positions ────────────────────────────────────────────────────────

async def get_auto_positions() -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM auto_mode_positions ORDER BY entry_time"
        )
    return [dict(r) for r in rows]


async def insert_auto_position(data: dict) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO auto_mode_positions
                (symbol, qty, entry_price, entry_time, cost_usdt,
                 take_profit_pct, stop_loss_pct)
            VALUES ($1, $2::numeric, $3::numeric, $4, $5::numeric,
                    $6::numeric, $7::numeric)
            ON CONFLICT (symbol) DO UPDATE SET
                qty             = EXCLUDED.qty,
                entry_price     = EXCLUDED.entry_price,
                entry_time      = EXCLUDED.entry_time,
                cost_usdt       = EXCLUDED.cost_usdt,
                take_profit_pct = EXCLUDED.take_profit_pct,
                stop_loss_pct   = EXCLUDED.stop_loss_pct
            """,
            data["symbol"],
            float(data["qty"]),
            float(data["entry_price"]),
            data.get("entry_time", datetime.now(timezone.utc)),
            float(data["cost_usdt"]),
            float(data["take_profit_pct"]),
            float(data["stop_loss_pct"]),
        )


async def delete_auto_position(symbol: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM auto_mode_positions WHERE symbol = $1",
            symbol.upper(),
        )


# ── auto_mode_trades ───────────────────────────────────────────────────────────

async def record_auto_trade(data: dict) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO auto_mode_trades
                (symbol, side, price, qty, cost_usdt, pnl,
                 exit_reason, analysts_conf)
            VALUES ($1, $2, $3::numeric, $4::numeric, $5::numeric,
                    $6::numeric, $7, $8)
            """,
            data["symbol"],
            data["side"],
            float(data["price"]),
            float(data["qty"]),
            float(data.get("cost_usdt", 0.0)),
            float(data.get("pnl", 0.0)),
            data.get("exit_reason", "unknown"),
            int(data.get("analysts_conf", 0)),
        )


async def get_auto_trades(limit: int = 50) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM auto_mode_trades
            ORDER BY executed_at DESC LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]
