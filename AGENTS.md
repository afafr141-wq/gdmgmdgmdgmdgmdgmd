# AGENTS.md — Grid Bot (MEXC Spot)

Agent guidance for working in this repository.

---

## Project Overview

Telegram-controlled trading bot for MEXC spot market. Two independent strategies share one process:

- **Grid Bot** — places a ladder of limit buy/sell orders within a user-defined price range; rebuilds when price breaks out.
- **Price Action (PA) Bot** — detects Equal Highs/Lows liquidity sweeps with candle confirmation, enters with a market order, exits at the nearest opposing liquidity level.

Both strategies persist state to PostgreSQL (Supabase) and recover automatically on restart.

---

## Repository Layout

```
main.py                  Entry point: wires engines → Telegram bot → long-polling
config/settings.py       All env-var loading and risk-profile constants
core/
  mexc_client.py         ccxt async wrapper (precision, rate-limit pauses)
  grid_engine.py         Grid strategy: order placement, fill handling, rebuild logic
  pa_engine.py           Pure PA signal computation (no orders, no API calls)
  pa_strategy_engine.py  PA lifecycle: polling loop, order execution, DB persistence
bot/
  telegram_bot.py        Command handlers, auth guard, notification senders
  menu_bot.py            ConversationHandler for interactive inline-keyboard menus
utils/
  db_manager.py          asyncpg pool, schema creation, all DB queries
tests/
  test_grid_engine.py    Unit tests for grid parameter derivation and fill guards
.ona/skills/             Ona agent skill files (multi-ai-market-scanner)
```

---

## Architecture Rules

### Separation of concerns
- `pa_engine.py` must stay pure: no I/O, no asyncio, no ccxt imports. It receives candle data and returns signals.
- `grid_engine.py` and `pa_strategy_engine.py` own all exchange interaction and DB writes.
- `db_manager.py` is the only file that imports `asyncpg`. All DB access goes through it.

### Notifier injection
Engines do not import from `bot/`. Notification callbacks are injected at startup via `set_notifiers()` in each engine module. Never add a direct import of `telegram_bot` or `menu_bot` inside `core/`.

### Single process
Both engines run in the same asyncio event loop started by `python-telegram-bot`'s `Application.run_polling()`. Do not introduce threads or subprocess calls.

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
| `OPENROUTER_API_KEY` | — | Required for `/scan` AI market scanner |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3-super-120b-a12b:free` | Default analyst model |
| `AI_JUDGE_INTERVAL_MINUTES` | `10` | Rate-limit guard between AI judge calls |

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

### PA signal deduplication
`PAStrategyState.last_signal_candle_ts` prevents re-entering on the same candle. Do not remove this guard.

---

## Testing

- Tests live in `tests/`. Run with `pytest tests/`.
- Use `unittest.mock.AsyncMock` for async methods on the fake client.
- `pa_engine.py` is pure Python — test it without any mocking.
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
- Do not parallelise OpenRouter analyst calls — sequential with a 3 s gap is intentional (rate-limit avoidance).
