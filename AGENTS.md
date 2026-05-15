# AGENTS.md — Grid Bot (MEXC Spot)

Agent guidance for working in this repository.

---

## Project Overview

Telegram-controlled Grid Bot for MEXC spot market.

- **Grid Bot** — places a ladder of limit buy/sell orders within a user-defined price range; rebuilds when price breaks out.

State is persisted to PostgreSQL (Supabase) and recovered automatically on restart.

---

## Repository Layout

```
main.py                  Entry point: wires engine → Telegram bot → long-polling
config/settings.py       All env-var loading and constants
core/
  mexc_client.py         ccxt async wrapper (precision, rate-limit pauses)
  grid_engine.py         Grid strategy: order placement, fill handling, rebuild logic
bot/
  telegram_bot.py        Command handlers, auth guard, notification senders
  menu_bot.py            ConversationHandler for interactive inline-keyboard menus
  portfolio_bridge.py    Template for adding new strategies to the Application
utils/
  db_manager.py          asyncpg pool, schema creation, all DB queries
portfolio/
  supertrend_bot.py      Reusable pure-Python indicators: SuperTrend, UT Bot, combined signal
  engine.py              Portfolio rebalancing loop
  database.py            Portfolio-specific DB layer (SQLite, separate from asyncpg pool)
tests/
  test_grid_engine.py    Unit tests for grid parameter derivation and fill guards
.ona/skills/             Ona agent skill files (multi-ai-market-scanner)
```

---

## Architecture Rules

### Separation of concerns
- `grid_engine.py` owns all exchange interaction and DB writes for the grid strategy.
- `db_manager.py` is the only file that imports `asyncpg`. All DB access goes through it.

### Notifier injection
`grid_engine.py` does not import from `bot/`. Notification callbacks are injected at startup via `set_notifiers()`. Never add a direct import of `telegram_bot` or `menu_bot` inside `core/`.

Any new strategy engine in `core/` must follow the same pattern: define a `set_notifiers(**callbacks)` function that accepts callables, and call those callables instead of importing from `bot/`.

### Bridge pattern for new strategies
`bot/portfolio_bridge.py` is the canonical template for integrating a new strategy into the single `Application` instance. When adding a strategy (e.g. scalping):

1. Create `bot/scalp_bridge.py` mirroring `portfolio_bridge.py`.
2. Use a unique callback prefix (e.g. `scalp:`) to avoid collisions with grid (`grid:`) and portfolio callbacks.
3. Register handlers at a distinct group number (grid=0, portfolio=5/10, scalp=6/11).
4. Inject all engine functions into the strategy's Telegram module via module-level variables (see `_ptb._start_fn = ...` pattern in `portfolio_bridge.py`). Do not import the engine directly inside handler functions.
5. Call `register_scalp_handlers(app)` from `main.py` after `register_portfolio_handlers(app)`.

### Single process
The engine runs in the asyncio event loop started by `python-telegram-bot`'s `Application.run_polling()`. Do not introduce threads or subprocess calls.

---

## Environment Variables

Required (validated at startup by `config/settings.py:validate_env()`):

| Variable | Purpose |
|---|---|
| `MEXC_API_KEY` | MEXC REST API key |
| `MEXC_API_SECRET` | MEXC REST API secret |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `DATABASE_URL` | PostgreSQL connection string (asyncpg format) |

Optional:

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_CHAT_ID` | — | Restrict bot to one chat |
| `ALLOWED_USER_IDS` | — | Comma-separated Telegram user IDs |
| `LOG_LEVEL` | `INFO` | Python logging level |

See `.env.example` for the full list. Copy it to `.env` before running locally.

---

## Running Locally

```bash
cp .env.example .env
# Fill in required vars

pip install -r requirements.txt
python main.py
```

Tests:

```bash
pytest tests/
```

---

## Key Patterns

### Rate limiting
`ORDER_SLEEP_SECONDS = 0.25` — always `await asyncio.sleep(ORDER_SLEEP_SECONDS)` between consecutive REST calls inside loops. Do not remove these pauses.

### DB connection pool
`statement_cache_size=0` is required for PgBouncer transaction mode (Supabase). Do not change this.

### Symbol normalisation
Always pass symbols in `BASE/QUOTE` format (e.g. `BTC/USDT`) to ccxt. Use `_normalize_symbol()` in `telegram_bot.py` to convert user input.

### Grid rebuild guard
`_pending_rebuild` is set when price breaks out of range. The actual rebuild waits for the 1-minute candle to close (`_wait_and_rebuild`). Always cancel `_rebuild_task` in `engine.stop()` to avoid orphaned tasks.

### Candle-close confirmation
Signal strategies must evaluate `candles[-2]` (the last *closed* candle), never `candles[-1]` (the still-forming candle). This applies to all indicator reads: SuperTrend direction, UT Bot signal, and any derived combined signal. `get_combined_signal()` in `portfolio/supertrend_bot.py` enforces this with `idx = len(st_data) - 2`.

### asyncpg explicit type casts
asyncpg cannot infer the PostgreSQL type of `$N` parameters when the column type is `NUMERIC` or `INTEGER`. Every query parameter for a numeric column must include an explicit cast:

```sql
-- correct
INSERT INTO my_table (price, qty) VALUES ($1::numeric, $2::numeric)

-- wrong — raises "could not determine data type of parameter $1"
INSERT INTO my_table (price, qty) VALUES ($1, $2)
```

See `db_manager.upsert_grid()` for the established pattern. Apply the same casts in any new table queries added for scalping or other strategies.

---

## Testing

- Tests live in `tests/`. Run with `pytest tests/`.
- Use `unittest.mock.AsyncMock` for async methods on the fake client.
- Do not write tests that require a live MEXC connection or a real database.

---

## Dependency Constraints

| Package | Pinned version | Reason |
|---|---|---|
| `ccxt` | 4.3.89 | MEXC API compatibility |
| `python-telegram-bot` | 21.5 | PTB v21 async API |
| `asyncpg` | 0.29.0 | PgBouncer transaction mode support |
| `numpy` | 1.26.4 | scipy compatibility |

Do not upgrade these without verifying MEXC and PTB breaking-change logs.

---

## What Not to Do

- Do not add synchronous blocking calls (`requests`, `time.sleep`) inside async functions.
- Do not import `core/` modules from `config/settings.py`.
- Do not store secrets in code or commit `.env`.
- Do not add a `stop_loss` order — MEXC Spot does not support stop orders.
