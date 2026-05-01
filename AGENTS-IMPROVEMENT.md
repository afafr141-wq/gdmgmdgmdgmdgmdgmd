# AGENTS.md Improvement Spec

Audit date: 2026-05-01

---

## What's Good

- **README is accurate.** Quick-start, command table, and deployment section match the actual code.
- **PA_STRATEGY_EXPLAINED.md is thorough.** The Arabic-language technical walkthrough covers every phase of the signal pipeline with concrete examples. Useful for any agent working on `pa_engine.py`.
- **`config/settings.py` is the single source of truth** for all env vars and tuning constants. No magic numbers scattered across modules.
- **Notifier injection pattern** (`set_notifiers()`) correctly decouples engines from the Telegram layer.
- **`pa_engine.py` is pure.** No I/O, no asyncio — easy to unit-test without mocks.
- **`db_manager.py` is the sole asyncpg consumer.** Clean boundary.
- **`.gitignore` is correct** — `.env`, `__pycache__`, `venv`, IDE dirs all excluded.
- **`multi-ai-market-scanner` skill** is well-structured with concrete code snippets, constants table, and reference files. Reusable.

---

## What's Missing

### 1. AGENTS.md did not exist
No agent guidance file was present. Created at `AGENTS.md` as part of this session.

### 2. No test coverage for `pa_engine.py`
`tests/` contains only `test_grid_engine.py`. The pure signal-computation module (`pa_engine.py`) has zero tests despite being the most logic-dense file in the project (trend detection, equal-level detection, sweep detection, candle confirmation).

### 3. No test coverage for `pa_strategy_engine.py` lifecycle
The PA engine's polling loop, TP-fill detection, and `restore()` path are untested.

### 4. No test coverage for `db_manager.py`
Schema creation, upsert logic, and recovery queries are untested. A broken migration silently fails at startup.

### 5. `.env.example` contains a real database URL
`DATABASE_URL` in `.env.example` is a live Supabase connection string with credentials embedded. This is a secret leak risk if the repo is ever made public or forked.

### 6. `devcontainer.json` uses the 10 GB universal image
The project is Python-only. The universal image adds ~9 GB of unused tooling and slows environment startup significantly.

### 7. No automations defined
`.devcontainer/devcontainer.json` has no `postCreateCommand` and there is no `automations.yaml`. Developers must manually run `pip install -r requirements.txt` after environment creation.

### 8. `AGENTS.md` had no guidance on the multi-ai-market-scanner skill
The skill exists in `.ona/skills/` but nothing in the repo documentation told an agent when or how to use it.

### 9. No type annotations on public engine APIs
`GridEngine.start()`, `GridEngine.stop()`, `PAStrategyEngine.restore()` lack return-type annotations. Agents and IDEs cannot infer call signatures without reading the full implementation.

### 10. `PA_STRATEGY_EXPLAINED.md` is Arabic-only
Useful content, but inaccessible to non-Arabic-speaking agents or contributors without translation.

---

## What's Wrong

### 1. `.env.example` leaks real credentials
**File:** `.env.example`, line `DATABASE_URL=postgresql://postgres.qeipuafxoqdeauemgsxb:Kabokingkaboking@...`

This is a live Supabase URL with a password. It must be replaced with a placeholder immediately. If this repo is public or will be, rotate the Supabase password.

**Fix:**
```
DATABASE_URL=postgresql://user:password@host:port/dbname
```

### 2. `_notify_ref` closure pattern in `main.py` is fragile
`main()` creates a `_notify_ref` dict and captures it in a closure to pass the `Application` instance to `GridEngine` before the app is built. If `app` is not assigned before the first notification fires, `_notify_ref.get("app")` returns `None` and the notification is silently dropped.

**Fix:** Pass the notify callable after `app` is constructed, or use `application.bot_data` directly inside the notifier.

### 3. `ALLOWED_USER_IDS` auth check is split across two files
`telegram_bot.py:_is_allowed()` checks both `ALLOWED_USER_IDS` and `TELEGRAM_CHAT_ID`. `menu_bot.py:_authorized()` checks only `ALLOWED_USER_IDS`. A user blocked by `TELEGRAM_CHAT_ID` can still reach menu callbacks.

**Fix:** Consolidate into a single `_is_allowed(update)` function in `telegram_bot.py` and import it in `menu_bot.py`.

### 4. `psycopg2-binary` is an unused dependency
`requirements.txt` lists `psycopg2-binary==2.9.9` but the codebase uses `asyncpg` exclusively. `psycopg2` is never imported.

**Fix:** Remove `psycopg2-binary` from `requirements.txt`.

### 5. `OPENROUTER_MODEL` default references a model not in the skill's verified list
`settings.py` defaults to `nvidia/nemotron-3-super-120b-a12b:free`. The skill's `references/openrouter-free-models.md` should be checked — if this model is no longer available on the free tier, the AI judge will fail silently on every call.

**Fix:** Align the default with a model confirmed in `references/openrouter-free-models.md`, or add a startup check that validates the model exists via `GET /api/v1/models`.

---

## Concrete Improvement Tasks

Priority order (highest first):

| # | Task | File(s) | Effort |
|---|---|---|---|
| 1 | Replace real DB URL in `.env.example` with placeholder | `.env.example` | 5 min |
| 2 | Remove `psycopg2-binary` from requirements | `requirements.txt` | 2 min |
| 3 | Consolidate auth guard into one function | `telegram_bot.py`, `menu_bot.py` | 30 min |
| 4 | Fix `_notify_ref` closure — pass notify after app construction | `main.py` | 20 min |
| 5 | Add `tests/test_pa_engine.py` — unit tests for signal logic | `tests/` | 2–3 h |
| 6 | Add `tests/test_pa_strategy_engine.py` — lifecycle tests | `tests/` | 2–3 h |
| 7 | Switch devcontainer to `mcr.microsoft.com/devcontainers/python:3.13` | `.devcontainer/devcontainer.json` | 10 min |
| 8 | Add `postCreateCommand` to devcontainer | `.devcontainer/devcontainer.json` | 10 min |
| 9 | Add type annotations to public engine methods | `core/grid_engine.py`, `core/pa_strategy_engine.py` | 1 h |
| 10 | Add English summary section to `PA_STRATEGY_EXPLAINED.md` | `PA_STRATEGY_EXPLAINED.md` | 30 min |
| 11 | Validate `OPENROUTER_MODEL` at startup or align default | `config/settings.py` | 30 min |

---

## AGENTS.md Gaps Filled by This Session

The newly created `AGENTS.md` now documents:

- Repository layout with one-line descriptions of every module
- Architecture rules (separation of concerns, notifier injection, single-process constraint)
- All environment variables with defaults and purpose
- Key patterns: rate limiting, DB pool config, symbol normalisation, rebuild guard, PA deduplication
- Testing conventions (no live connections, AsyncMock usage)
- Pinned dependency rationale
- Explicit "what not to do" list
- Reference to the `multi-ai-market-scanner` skill and when to use it
