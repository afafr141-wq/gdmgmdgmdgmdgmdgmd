"""
Database layer — PostgreSQL (Railway) when DATABASE_URL is set, SQLite otherwise.

PostgreSQL uses a persistent connection pool (psycopg2.pool.ThreadedConnectionPool)
so every query reuses an existing connection instead of opening a new one.
Transient errors (dropped connections, Railway restarts) are retried automatically.
"""

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Generator

log = logging.getLogger(__name__)

_DATABASE_URL: str | None = os.environ.get("DATABASE_URL")
_SQLITE_PATH = os.path.join(os.path.dirname(__file__), "portfolio.db")

# ---------------------------------------------------------------------------
# PostgreSQL connection pool
# ---------------------------------------------------------------------------

_pg_pool = None  # ThreadedConnectionPool, initialised lazily


def _get_pg_pool():
    """Return (or create) the shared PostgreSQL connection pool."""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    from psycopg2 import pool as pg_pool
    # options: force read-committed so every query sees the latest committed data.
    # This prevents stale reads when connections are reused from the pool
    # (critical for Supabase/PgBouncer in transaction-pooling mode on port 6543).
    dsn = _DATABASE_URL
    if "options=" not in (dsn or ""):
        sep = "&" if "?" in (dsn or "") else "?"
        dsn = f"{dsn}{sep}options=-c%20default_transaction_isolation%3Dread%5C%20committed"
    _pg_pool = pg_pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        dsn=dsn,
        connect_timeout=10,
    )
    log.info("PostgreSQL connection pool created (min=1, max=5)")
    return _pg_pool


def _try_postgres() -> bool:
    if not _DATABASE_URL:
        log.info("DATABASE_URL not set — using SQLite")
        return False
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        log.warning("psycopg2 not installed — falling back to SQLite")
        return False
    try:
        _get_pg_pool()
        return True
    except Exception as e:
        log.warning("PostgreSQL unavailable (%s) — falling back to SQLite", e)
        return False


_USE_POSTGRES = _try_postgres()
_BACKEND = "postgresql" if _USE_POSTGRES else "sqlite"

# ---------------------------------------------------------------------------
# Connection context managers
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_DELAY = 0.5  # seconds


@contextmanager
def _conn() -> Generator:
    if _USE_POSTGRES:
        with _pg_conn() as conn:
            yield conn
    else:
        with _sqlite_conn() as conn:
            yield conn


@contextmanager
def _pg_conn() -> Generator:
    """
    PostgreSQL connection context manager with retry logic on connection errors.

    The retry loop only covers *connection acquisition* — the yield happens
    exactly once, outside the loop.  This prevents the RuntimeError that occurs
    when a @contextmanager generator tries to yield a second time after catching
    an exception thrown by the caller's with-block.
    """
    global _pg_pool
    last_err = None
    conn = None
    pool = None

    # --- Acquire a connection with retries ---
    for attempt in range(_MAX_RETRIES):
        try:
            pool = _get_pg_pool()
            conn = pool.getconn()
            # Always rollback any leftover transaction from a previous use of
            # this connection.  Critical for PgBouncer transaction-pooling mode
            # (Supabase port 6543) so the connection doesn't carry a stale
            # snapshot and return outdated rows.
            try:
                conn.rollback()
            except Exception:
                pass
            conn.autocommit = False
            break  # connection acquired successfully
        except Exception as e:
            last_err = e
            if conn is not None:
                try:
                    if pool:
                        pool.putconn(conn, close=True)
                except Exception:
                    pass
                conn = None
            # Reset pool so the next attempt creates fresh connections
            _pg_pool = None
            pool = None
            log.warning("PostgreSQL connection error (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, e)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAY * (attempt + 1))
    else:
        # All retries exhausted — raise the last connection error
        raise last_err

    # --- Single yield outside the retry loop ---
    try:
        yield conn
        conn.commit()
        if pool:
            pool.putconn(conn)
        else:
            conn.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            if pool:
                pool.putconn(conn, close=True)
            else:
                conn.close()
        except Exception:
            pass
        _pg_pool = None
        raise


@contextmanager
def _sqlite_conn() -> Generator:
    conn = sqlite3.connect(_SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _q(sql: str) -> str:
    if _USE_POSTGRES:
        return sql.replace("?", "%s")
    return sql


def _rows_to_dicts(rows, cursor=None) -> list[dict]:
    if _USE_POSTGRES:
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    if _USE_POSTGRES:
        stmts = [
            """
            CREATE TABLE IF NOT EXISTS rebalance_history (
                id           SERIAL PRIMARY KEY,
                ts           TEXT    NOT NULL,
                mode         TEXT    NOT NULL,
                total_usdt   REAL    NOT NULL,
                details      TEXT    NOT NULL,
                paper        INTEGER NOT NULL DEFAULT 0,
                portfolio_id INTEGER NOT NULL DEFAULT 1
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id           SERIAL PRIMARY KEY,
                ts           TEXT    NOT NULL,
                total_usdt   REAL    NOT NULL,
                assets_json  TEXT    NOT NULL,
                portfolio_id INTEGER NOT NULL DEFAULT 1
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS portfolios (
                id          SERIAL PRIMARY KEY,
                ts_created  TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                config_json TEXT    NOT NULL,
                active      INTEGER NOT NULL DEFAULT 0,
                running     INTEGER NOT NULL DEFAULT 0
            )
            """,
        ]
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='portfolios' AND column_name='user_id'
                    ) THEN
                        DROP TABLE IF EXISTS portfolios CASCADE;
                    END IF;
                END$$;
            """)
            for stmt in stmts:
                cur.execute(stmt)
            migrations = [
                "ALTER TABLE rebalance_history ADD COLUMN IF NOT EXISTS ts TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE rebalance_history ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE rebalance_history ADD COLUMN IF NOT EXISTS total_usdt REAL NOT NULL DEFAULT 0",
                "ALTER TABLE rebalance_history ADD COLUMN IF NOT EXISTS details TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE rebalance_history ADD COLUMN IF NOT EXISTS paper INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE rebalance_history ADD COLUMN IF NOT EXISTS portfolio_id INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS ts TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS total_usdt REAL NOT NULL DEFAULT 0",
                "ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS assets_json TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS portfolio_id INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE portfolios ADD COLUMN IF NOT EXISTS bot_running INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE portfolios ADD COLUMN IF NOT EXISTS running INTEGER NOT NULL DEFAULT 0",
            ]
            for m in migrations:
                try:
                    cur.execute(m)
                except Exception as e:
                    log.debug("Migration skipped: %s", e)
        log.info("PostgreSQL tables ready (Railway)")
    else:
        with _conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rebalance_history (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           TEXT    NOT NULL,
                    mode         TEXT    NOT NULL,
                    total_usdt   REAL    NOT NULL,
                    details      TEXT    NOT NULL,
                    paper        INTEGER NOT NULL DEFAULT 0,
                    portfolio_id INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           TEXT    NOT NULL,
                    total_usdt   REAL    NOT NULL,
                    assets_json  TEXT    NOT NULL,
                    portfolio_id INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS portfolios (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_created  TEXT    NOT NULL,
                    name        TEXT    NOT NULL,
                    config_json TEXT    NOT NULL,
                    active      INTEGER NOT NULL DEFAULT 0,
                    running     INTEGER NOT NULL DEFAULT 0
                );
            """)
        # SQLite doesn't support IF NOT EXISTS in ALTER TABLE, so we try/except
        for _col_sql in [
            "ALTER TABLE portfolios ADD COLUMN bot_running INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE portfolios ADD COLUMN running INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                with _conn() as conn:
                    conn.execute(_col_sql)
            except Exception:
                pass  # column already exists
        log.info("SQLite tables ready: %s", _SQLITE_PATH)


# ---------------------------------------------------------------------------
# Rebalance history
# ---------------------------------------------------------------------------

def record_rebalance(mode: str, total_usdt: float, details: list,
                     paper: bool = False, portfolio_id: int = 1) -> None:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                _q("INSERT INTO rebalance_history (ts, mode, total_usdt, details, paper, portfolio_id) VALUES (?,?,?,?,?,?)"),
                (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), mode, total_usdt,
                 json.dumps(details), int(paper), portfolio_id),
            )
    except Exception as e:
        log.error("record_rebalance failed: %s", e)


def get_rebalance_history(limit: int = 10, portfolio_id: int = 1) -> list:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                _q("SELECT * FROM rebalance_history WHERE portfolio_id=? ORDER BY id DESC LIMIT ?"),
                (portfolio_id, limit),
            )
            rows = _rows_to_dicts(cur.fetchall(), cur)
        for d in rows:
            raw = d.get("details") or "[]"
            try:
                d["details"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d["details"] = []
        return rows
    except Exception as e:
        log.error("get_rebalance_history failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Portfolio snapshots
# ---------------------------------------------------------------------------

def record_snapshot(total_usdt: float, assets: list, portfolio_id: int = 1) -> None:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                _q("INSERT INTO portfolio_snapshots (ts, total_usdt, assets_json, portfolio_id) VALUES (?,?,?,?)"),
                (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), total_usdt, json.dumps(assets), portfolio_id),
            )
    except Exception as e:
        log.error("record_snapshot failed: %s", e)


def get_snapshots(limit: int = 90, portfolio_id: int = 1) -> list:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                _q("SELECT ts, total_usdt FROM portfolio_snapshots WHERE portfolio_id=? ORDER BY id DESC LIMIT ?"),
                (portfolio_id, limit),
            )
            rows = _rows_to_dicts(cur.fetchall(), cur)
        return list(reversed(rows))
    except Exception as e:
        log.error("get_snapshots failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Multi-portfolio management
# ---------------------------------------------------------------------------

def save_portfolio(name: str, config: dict) -> int:
    with _conn() as conn:
        cur = conn.cursor()
        if _USE_POSTGRES:
            cur.execute(
                "INSERT INTO portfolios (ts_created, name, config_json, active) VALUES (%s,%s,%s,0) RETURNING id",
                (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), name, json.dumps(config)),
            )
            return cur.fetchone()[0]
        else:
            cur.execute(
                "INSERT INTO portfolios (ts_created, name, config_json, active) VALUES (?,?,?,0)",
                (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), name, json.dumps(config)),
            )
            return cur.lastrowid


def list_portfolios() -> list:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, ts_created, active, config_json FROM portfolios ORDER BY id DESC")
            rows = _rows_to_dicts(cur.fetchall(), cur)
        result = []
        for r in rows:
            cfg = json.loads(r["config_json"])
            assets = cfg.get("portfolio", {}).get("assets", [])
            result.append({
                "id": r["id"],
                "name": r["name"],
                "ts_created": r["ts_created"],
                "active": bool(r["active"]),
                "mode": cfg.get("rebalance", {}).get("mode", "—"),
                "total_usdt": cfg.get("portfolio", {}).get("total_usdt", 0),
                "assets": [{"symbol": a["symbol"], "allocation_pct": a["allocation_pct"]} for a in assets],
                "paper_trading": cfg.get("paper_trading", False),
            })
        return result
    except Exception as e:
        log.error("list_portfolios failed: %s", e)
        return []


def get_portfolio(portfolio_id: int) -> dict | None:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(_q("SELECT config_json FROM portfolios WHERE id=?"), (portfolio_id,))
            row = cur.fetchone()
        if row:
            val = row[0] if _USE_POSTGRES else row["config_json"]
            return json.loads(val)
        return None
    except Exception as e:
        log.error("get_portfolio failed: %s", e)
        return None


def set_active_portfolio(portfolio_id: int) -> None:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE portfolios SET active=0")
            cur.execute(_q("UPDATE portfolios SET active=1 WHERE id=?"), (portfolio_id,))
    except Exception as e:
        log.error("set_active_portfolio failed: %s", e)


def set_bot_running(portfolio_id: int, running: bool) -> None:
    """Persist the running state of a portfolio's bot loop so it can be
    resumed automatically after a Railway restart."""
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                _q("UPDATE portfolios SET running=? WHERE id=?"),
                (1 if running else 0, portfolio_id),
            )
    except Exception as e:
        log.error("set_bot_running failed: %s", e)


def get_running_portfolios() -> list:
    """Return list of portfolio IDs whose bot loop was running before the
    last shutdown (used by the lifespan handler for auto-resume)."""
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM portfolios WHERE running=1")
            rows = cur.fetchall()
        if _USE_POSTGRES:
            return [r[0] for r in rows]
        return [r["id"] for r in rows]
    except Exception as e:
        log.error("get_running_portfolios failed: %s", e)
        return []


def delete_portfolio(portfolio_id: int) -> None:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(_q("DELETE FROM portfolios WHERE id=?"), (portfolio_id,))
            cur.execute(_q("DELETE FROM rebalance_history WHERE portfolio_id=?"), (portfolio_id,))
            cur.execute(_q("DELETE FROM portfolio_snapshots WHERE portfolio_id=?"), (portfolio_id,))
    except Exception as e:
        log.error("delete_portfolio failed: %s", e)


def update_portfolio_config(portfolio_id: int, config: dict) -> None:
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                _q("UPDATE portfolios SET config_json=? WHERE id=?"),
                (json.dumps(config), portfolio_id),
            )
    except Exception as e:
        log.error("update_portfolio_config failed: %s", e)


# ---------------------------------------------------------------------------
# SuperTrend + UT Bot
# ---------------------------------------------------------------------------

def _init_supertrend_tables(conn) -> None:
    """Create supertrend_bots and supertrend_signals tables if they don't exist."""
    if _USE_POSTGRES:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supertrend_bots (
                id          SERIAL PRIMARY KEY,
                ts_created  TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                config_json TEXT    NOT NULL,
                running     INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supertrend_signals (
                id        SERIAL PRIMARY KEY,
                bot_id    INTEGER NOT NULL,
                ts        TEXT    NOT NULL,
                signal    TEXT    NOT NULL,
                price     REAL    NOT NULL DEFAULT 0,
                qty       REAL    NOT NULL DEFAULT 0,
                usdt      REAL    NOT NULL DEFAULT 0,
                paper     INTEGER NOT NULL DEFAULT 0,
                error     TEXT
            )
        """)
        # Migrations for existing deployments
        for m in [
            "ALTER TABLE supertrend_bots ADD COLUMN IF NOT EXISTS running INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                cur.execute(m)
            except Exception as e:
                log.debug("ST migration skipped: %s", e)
    else:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS supertrend_bots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_created  TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                config_json TEXT    NOT NULL,
                running     INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS supertrend_signals (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id    INTEGER NOT NULL,
                ts        TEXT    NOT NULL,
                signal    TEXT    NOT NULL,
                price     REAL    NOT NULL DEFAULT 0,
                qty       REAL    NOT NULL DEFAULT 0,
                usdt      REAL    NOT NULL DEFAULT 0,
                paper     INTEGER NOT NULL DEFAULT 0,
                error     TEXT
            );
        """)


def create_supertrend_bot(name: str, config: dict) -> int:
    """Persist a new SuperTrend bot config and return its ID."""
    with _conn() as conn:
        _init_supertrend_tables(conn)
        cur = conn.cursor()
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if _USE_POSTGRES:
            cur.execute(
                "INSERT INTO supertrend_bots (ts_created, name, config_json) VALUES (%s,%s,%s) RETURNING id",
                (ts, name, json.dumps(config)),
            )
            return cur.fetchone()[0]
        else:
            cur.execute(
                "INSERT INTO supertrend_bots (ts_created, name, config_json) VALUES (?,?,?)",
                (ts, name, json.dumps(config)),
            )
            return cur.lastrowid


def get_supertrend_bot(bot_id: int) -> dict | None:
    try:
        with _conn() as conn:
            _init_supertrend_tables(conn)
            cur = conn.cursor()
            cur.execute(_q("SELECT * FROM supertrend_bots WHERE id=?"), (bot_id,))
            row = cur.fetchone()
        if not row:
            return None
        d = dict(zip([c[0] for c in cur.description], row)) if _USE_POSTGRES else dict(row)
        d["config"] = json.loads(d["config_json"])
        return d
    except Exception as e:
        log.error("get_supertrend_bot failed: %s", e)
        return None


def list_supertrend_bots() -> list:
    try:
        with _conn() as conn:
            _init_supertrend_tables(conn)
            cur = conn.cursor()
            cur.execute("SELECT id, name, ts_created, running, config_json FROM supertrend_bots ORDER BY id DESC")
            rows = _rows_to_dicts(cur.fetchall(), cur)
        result = []
        for r in rows:
            cfg = json.loads(r["config_json"])
            result.append({
                "id":         r["id"],
                "name":       r["name"],
                "ts_created": r["ts_created"],
                "running":    bool(r["running"]),
                "symbol":     cfg.get("symbol", "—"),
                "interval":   cfg.get("interval", "—"),
                "capital":    cfg.get("capital_usdt", 0),
                "paper":      cfg.get("paper_trading", False),
                "config":     cfg,
            })
        return result
    except Exception as e:
        log.error("list_supertrend_bots failed: %s", e)
        return []


def update_supertrend_bot_status(bot_id: int, running: bool) -> None:
    try:
        with _conn() as conn:
            _init_supertrend_tables(conn)
            cur = conn.cursor()
            cur.execute(
                _q("UPDATE supertrend_bots SET running=? WHERE id=?"),
                (1 if running else 0, bot_id),
            )
    except Exception as e:
        log.error("update_supertrend_bot_status failed: %s", e)


def get_running_supertrend_bots() -> list[int]:
    try:
        with _conn() as conn:
            _init_supertrend_tables(conn)
            cur = conn.cursor()
            cur.execute("SELECT id FROM supertrend_bots WHERE running=1")
            rows = cur.fetchall()
        if _USE_POSTGRES:
            return [r[0] for r in rows]
        return [r["id"] for r in rows]
    except Exception as e:
        log.error("get_running_supertrend_bots failed: %s", e)
        return []


def update_supertrend_bot_config(bot_id: int, config: dict) -> None:
    try:
        with _conn() as conn:
            _init_supertrend_tables(conn)
            cur = conn.cursor()
            cur.execute(
                _q("UPDATE supertrend_bots SET config_json=? WHERE id=?"),
                (json.dumps(config), bot_id),
            )
    except Exception as e:
        log.error("update_supertrend_bot_config failed: %s", e)


def delete_supertrend_bot(bot_id: int) -> None:
    try:
        with _conn() as conn:
            _init_supertrend_tables(conn)
            cur = conn.cursor()
            cur.execute(_q("DELETE FROM supertrend_bots WHERE id=?"), (bot_id,))
            cur.execute(_q("DELETE FROM supertrend_signals WHERE bot_id=?"), (bot_id,))
    except Exception as e:
        log.error("delete_supertrend_bot failed: %s", e)


def record_supertrend_signal(
    bot_id: int,
    signal: str,
    price: float,
    qty: float,
    usdt: float,
    paper: bool,
    error: str | None = None,
) -> None:
    try:
        with _conn() as conn:
            _init_supertrend_tables(conn)
            cur = conn.cursor()
            cur.execute(
                _q("INSERT INTO supertrend_signals (bot_id, ts, signal, price, qty, usdt, paper, error) VALUES (?,?,?,?,?,?,?,?)"),
                (
                    bot_id,
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    signal,
                    price,
                    qty,
                    usdt,
                    int(paper),
                    error,
                ),
            )
    except Exception as e:
        log.error("record_supertrend_signal failed: %s", e)


def get_supertrend_signals(bot_id: int, limit: int = 20) -> list:
    try:
        with _conn() as conn:
            _init_supertrend_tables(conn)
            cur = conn.cursor()
            cur.execute(
                _q("SELECT * FROM supertrend_signals WHERE bot_id=? ORDER BY id DESC LIMIT ?"),
                (bot_id, limit),
            )
            rows = _rows_to_dicts(cur.fetchall(), cur)
        return rows
    except Exception as e:
        log.error("get_supertrend_signals failed: %s", e)
        return []


