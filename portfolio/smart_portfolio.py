"""
Smart Portfolio – MEXC Spot Auto-Rebalancing Bot.

Rebalance modes
---------------
proportional : rebalance when any asset drifts beyond threshold_pct.
               Checks every check_interval_minutes (default 5 min).
               Only executes if deviation >= min_deviation_to_execute_pct (default 3%).
               Supported thresholds: 1%, 3%, 5%.
timed        : rebalance on a fixed schedule (daily / weekly / monthly).
unbalanced   : no automatic rebalancing; user triggers manually via CLI.

Setup modes
-----------
recommended  : load a pre-built allocation from config.json.
manual       : user defines assets and percentages interactively.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

from portfolio.mexc_client import MEXCClient
from portfolio.database import init_db, record_rebalance, record_snapshot, get_rebalance_history, get_snapshots

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

# Initialise DB on import
init_db()

VALID_THRESHOLDS = {1, 3, 5}
VALID_TIMED_FREQUENCIES = {"30min", "1h", "4h", "8h", "12h", "daily", "weekly", "monthly"}
# Interval in minutes for each timed frequency
TIMED_FREQUENCY_MINUTES: dict[str, int] = {
    "30min": 30,
    "1h": 60,
    "4h": 240,
    "8h": 480,
    "12h": 720,
    "daily": 1440,
    "weekly": 10080,
    "monthly": 43200,
}
VALID_ALLOCATION_MODES = {"ai_balance", "equal", "market_cap"}
MIN_ASSETS = 1
MAX_ASSETS = 20


def is_paper_trading(cfg: dict) -> bool:
    """Resolve effective paper-trading mode consistently across the app.

    Priority:
    1) PAPER_TRADING env var (true/false)
    2) cfg["paper_trading"] fallback
    """
    env_paper = os.environ.get("PAPER_TRADING", "").lower()
    if env_paper in ("true", "1", "yes"):
        return True
    if env_paper in ("false", "0", "no"):
        return False
    return bool(cfg.get("paper_trading", False))


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config(path: str = CONFIG_PATH) -> dict:
    """Load config from the active portfolio in DB if one exists, else fall back to config.json."""
    try:
        from portfolio.database import list_portfolios, get_portfolio as db_get_portfolio
        portfolios = list_portfolios()
        active = next((p for p in portfolios if p.get("active")), None)
        if active:
            cfg = db_get_portfolio(active["id"])
            if cfg:
                return cfg
    except Exception as e:
        log.warning("Could not load active portfolio from DB (%s) — falling back to config.json", e)
    with open(path, "r") as f:
        return json.load(f)


def save_config(cfg: dict, path: str = CONFIG_PATH) -> None:
    """Persist config to the active portfolio in DB (if one exists) and to config.json."""
    try:
        from portfolio.database import list_portfolios, update_portfolio_config
        portfolios = list_portfolios()
        active = next((p for p in portfolios if p.get("active")), None)
        if active:
            update_portfolio_config(active["id"], cfg)
            log.info("Config saved to DB (portfolio id=%s)", active["id"])
    except Exception as e:
        log.warning("Could not save config to DB (%s) — saving to config.json only", e)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    log.info("Config saved to %s", path)


def validate_allocations(assets: list) -> None:
    if not (MIN_ASSETS <= len(assets) <= MAX_ASSETS):
        raise ValueError(f"Number of assets must be between {MIN_ASSETS} and {MAX_ASSETS}.")
    symbols = [a["symbol"].strip().upper() for a in assets]
    # Check for empty symbols
    if any(not s for s in symbols):
        raise ValueError("All assets must have a symbol.")
    # Check for duplicates
    if len(symbols) != len(set(symbols)):
        dupes = [s for s in symbols if symbols.count(s) > 1]
        raise ValueError(f"Duplicate symbols: {', '.join(set(dupes))}")
    total = sum(a["allocation_pct"] for a in assets)
    if abs(total - 100.0) > 0.01:
        raise ValueError(f"Allocations must sum to 100% (got {total:.2f}%).")


# ---------------------------------------------------------------------------
# Allocation mode helpers
# ---------------------------------------------------------------------------

def apply_equal_allocation(assets: list) -> list:
    """Distribute 100% equally across all assets."""
    n = len(assets)
    base = round(100.0 / n, 4)
    remainder = round(100.0 - base * (n - 1), 4)
    for i, a in enumerate(assets):
        a["allocation_pct"] = remainder if i == n - 1 else base
    return assets


def apply_market_cap_allocation(client: MEXCClient, assets: list) -> list:
    """Weight each asset by its current market price (proxy for market cap rank).

    Uses the live USDT price as a relative weight.  This is a simplified
    proxy — a full market-cap weighting would require CoinGecko data.
    """
    prices: dict[str, float] = {}
    for a in assets:
        sym = a["symbol"]
        try:
            prices[sym] = client.get_price(f"{sym}USDT") if sym != "USDT" else 1.0
        except Exception as e:
            log.warning("Cannot fetch price for %s (market-cap alloc): %s", sym, e)
            prices[sym] = 0.0

    total_price = sum(prices.values())
    if total_price <= 0:
        log.warning("All prices zero — falling back to equal allocation")
        return apply_equal_allocation(assets)

    for a in assets:
        a["allocation_pct"] = round(prices[a["symbol"]] / total_price * 100, 4)

    # Fix rounding so total == 100
    diff = round(100.0 - sum(a["allocation_pct"] for a in assets), 4)
    assets[-1]["allocation_pct"] = round(assets[-1]["allocation_pct"] + diff, 4)
    return assets


# ---------------------------------------------------------------------------
# Interactive setup (manual mode)
# ---------------------------------------------------------------------------

def interactive_setup(cfg: dict) -> dict:
    """Walk the user through manual portfolio configuration."""
    print("\n=== Smart Portfolio Setup (Manual) ===")

    # Bot name (set once, never changed)
    name = input("Bot name (press Enter to keep current): ").strip()
    if name:
        cfg["bot"]["name"] = name

    # Assets
    print(f"\nEnter between {MIN_ASSETS} and {MAX_ASSETS} assets.")
    assets = []
    while True:
        sym = input("  Asset symbol (e.g. BTC) or 'done': ").strip().upper()
        if sym == "DONE":
            if len(assets) < MIN_ASSETS:
                print(f"  Need at least {MIN_ASSETS} assets.")
                continue
            break
        if len(assets) >= MAX_ASSETS:
            print(f"  Maximum {MAX_ASSETS} assets reached.")
            break
        assets.append({"symbol": sym, "allocation_pct": 0.0})

    # Allocation
    equal = input("Allocate equally? (y/n): ").strip().lower()
    if equal == "y":
        pct = round(100.0 / len(assets), 4)
        for a in assets:
            a["allocation_pct"] = pct
        # Fix rounding so total == 100
        diff = 100.0 - sum(a["allocation_pct"] for a in assets)
        assets[-1]["allocation_pct"] = round(assets[-1]["allocation_pct"] + diff, 4)
    else:
        remaining = 100.0
        for i, a in enumerate(assets):
            if i == len(assets) - 1:
                a["allocation_pct"] = round(remaining, 4)
                print(f"  {a['symbol']}: {a['allocation_pct']}% (auto-assigned)")
            else:
                while True:
                    try:
                        pct = float(input(f"  {a['symbol']} allocation %: "))
                        if pct <= 0 or pct >= remaining:
                            print(f"  Must be > 0 and < {remaining:.2f}")
                            continue
                        a["allocation_pct"] = round(pct, 4)
                        remaining = round(remaining - pct, 4)
                        break
                    except ValueError:
                        print("  Enter a number.")

    cfg["portfolio"]["assets"] = assets

    # USDT amount
    while True:
        try:
            amount = float(input("\nTotal USDT to invest: "))
            if amount <= 0:
                print("  Must be positive.")
                continue
            cfg["portfolio"]["total_usdt"] = amount
            break
        except ValueError:
            print("  Enter a number.")

    # Rebalance mode
    print("\nRebalance mode:")
    print("  1. proportional")
    print("  2. timed")
    print("  3. unbalanced")
    while True:
        choice = input("Choose (1/2/3): ").strip()
        if choice == "1":
            cfg["rebalance"]["mode"] = "proportional"
            print("  Threshold options: 1%, 3%, 5%")
            while True:
                try:
                    t = int(input("  Threshold %: "))
                    if t not in VALID_THRESHOLDS:
                        print(f"  Must be one of {VALID_THRESHOLDS}")
                        continue
                    cfg["rebalance"]["proportional"]["threshold_pct"] = t
                    break
                except ValueError:
                    print("  Enter a number.")
            break
        elif choice == "2":
            cfg["rebalance"]["mode"] = "timed"
            print("  Frequency options: daily / weekly / monthly")
            while True:
                freq = input("  Frequency: ").strip().lower()
                if freq not in VALID_TIMED_FREQUENCIES:
                    print(f"  Must be one of {VALID_TIMED_FREQUENCIES}")
                    continue
                cfg["rebalance"]["timed"]["frequency"] = freq
                break
            break
        elif choice == "3":
            cfg["rebalance"]["mode"] = "unbalanced"
            break
        else:
            print("  Enter 1, 2, or 3.")

    # Termination
    sell = input("\nSell all assets to USDT at termination? (y/n): ").strip().lower()
    cfg["termination"]["sell_at_termination"] = sell == "y"

    # Asset transfer
    transfer = input("Enable asset transfer (use wallet balance first)? (y/n): ").strip().lower()
    cfg["asset_transfer"]["enable_asset_transfer"] = transfer == "y"

    validate_allocations(cfg["portfolio"]["assets"])
    save_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Portfolio valuation
# ---------------------------------------------------------------------------

def get_portfolio_value(
    client: MEXCClient,
    assets: list,
    budget_usdt: float | None = None,
) -> dict:
    """
    Returns the current value of the configured portfolio assets only.

    budget_usdt: when provided, this value is used as the authoritative
    portfolio total instead of summing live asset values.  Percentages
    (actual_pct) are calculated against budget_usdt, so the rebalancer
    always works within the user-defined allocation and never touches
    funds outside it.  Pass None for informational/status calls where
    you want the true live value.

    Returns:
        {
          "total_usdt": float,
          "assets": [
            {"symbol": str, "balance": float, "price": float,
             "value_usdt": float, "actual_pct": float},
            ...
          ]
        }
    """
    result = []
    invalid_symbols = []

    # Fetch all balances in one API call to avoid N+1 requests.
    try:
        all_balances = client.get_all_balances()
    except Exception as e:
        log.warning("get_all_balances failed: %s — falling back to per-asset calls", e)
        all_balances = {}

    for a in assets:
        sym = a["symbol"]
        try:
            balance = all_balances.get(sym.upper(), client.get_asset_balance(sym))
            price = 1.0 if sym == "USDT" else client.get_price(f"{sym}USDT")
        except Exception as e:
            log.warning("Cannot fetch price for %s: %s – skipping", sym, e)
            invalid_symbols.append(sym)
            result.append({
                "symbol": sym,
                "balance": 0.0,
                "price": 0.0,
                "value_usdt": 0.0,
                "actual_pct": 0.0,
                "error": str(e),
            })
            continue
        value = balance * price
        result.append({
            "symbol": sym,
            "balance": balance,
            "price": price,
            "value_usdt": value,
            "actual_pct": 0.0,
        })

    if invalid_symbols:
        log.warning("Invalid symbols (not found on MEXC): %s", invalid_symbols)

    # When a budget is set, use it as the fixed total so that actual_pct
    # reflects how each asset sits relative to the intended allocation.
    # This prevents assets held outside the portfolio from inflating the
    # total and causing the bot to over-trade.
    if budget_usdt is not None:
        total = budget_usdt
        log.info("Portfolio total (fixed budget): %.2f USDT", total)
    else:
        # Informational mode: sum live values + free USDT not in portfolio.
        total = sum(r["value_usdt"] for r in result)
        asset_symbols = {a["symbol"].upper() for a in assets}
        if "USDT" not in asset_symbols:
            usdt_free = all_balances.get("USDT", 0.0)
            total += usdt_free
        log.info("Portfolio total (live): %.2f USDT", total)

    for r in result:
        r["actual_pct"] = (r["value_usdt"] / total * 100) if total > 0 else 0.0

    return {"total_usdt": total, "assets": result, "invalid_symbols": invalid_symbols}


# ---------------------------------------------------------------------------
# Rebalance execution
# ---------------------------------------------------------------------------

def execute_rebalance(client: MEXCClient, cfg: dict,
                      exclude_symbols: set[str] | None = None,
                      portfolio_id: int = 1,
                      buy_enabled: bool = True) -> list:
    """
    Rebalance logic:
    1. Compute the effective portfolio total = sum(asset values) + free USDT
       capped at the configured budget_usdt.  This means the bot only ever
       works within the user-defined budget and never touches funds outside it.
    2. Sell assets that are worth more than their target share of that total.
    3. After sells settle, buy assets that are worth less than their target.

    The effective total is recalculated after sells so that the USDT freed by
    selling is available for buys.

    exclude_symbols: assets to skip entirely this cycle (e.g. already sold by
    SL/TP — prevents the rebalancer from buying them back immediately).
    """
    paper = is_paper_trading(cfg)
    exclude_symbols = {s.upper() for s in (exclude_symbols or set())}

    assets_cfg = cfg["portfolio"]["assets"]
    budget_usdt = cfg["portfolio"].get("total_usdt", 0)
    details = []

    log.info("Rebalance start | budget=%.2f USDT%s", budget_usdt, " [PAPER]" if paper else "")
    if exclude_symbols:
        log.info("Skipping SL/TP-triggered symbols this cycle: %s", exclude_symbols)

    # ── Fetch all balances in one API call, then prices per asset ────────
    try:
        all_balances = client.get_all_balances()
    except Exception as e:
        log.warning("get_all_balances failed: %s — falling back to per-asset calls", e)
        all_balances = {}

    actuals = {}
    lot_prec: dict[str, int] = {}   # sym → decimal places for base qty
    for a in assets_cfg:
        sym = a["symbol"]
        try:
            balance = all_balances.get(sym.upper(), client.get_asset_balance(sym))
            price   = client.get_price(f"{sym}USDT") if sym != "USDT" else 1.0
            actuals[sym] = {"balance": balance, "price": price, "value_usdt": balance * price}
            if sym != "USDT":
                lot_prec[sym] = client.get_lot_size_precision(f"{sym}USDT")
        except Exception as e:
            log.warning("Cannot fetch %s: %s", sym, e)
            actuals[sym] = {"balance": 0.0, "price": 0.0, "value_usdt": 0.0}

    # ── Compute effective total ──────────────────────────────────────────
    # Sum the value of all configured assets + free USDT in the account,
    # then cap at the user-defined budget so we never over-spend.
    asset_symbols = {a["symbol"].upper() for a in assets_cfg}
    assets_value = sum(actuals[sym]["value_usdt"] for sym in asset_symbols if sym != "USDT")
    # USDT balance already fetched in all_balances — no extra API call needed.
    free_usdt = all_balances.get("USDT", 0.0)

    # Effective total = what we actually have, but never more than the budget.
    effective_total = min(assets_value + free_usdt, budget_usdt)
    log.info(
        "assets_value=%.2f$ free_usdt=%.2f$ budget=%.2f$ → effective_total=%.2f$",
        assets_value, free_usdt, budget_usdt, effective_total,
    )

    sells = []
    buys  = []

    for a in assets_cfg:
        sym        = a["symbol"]

        # Skip assets already handled by SL/TP this cycle to avoid buying back
        # immediately after a stop-loss or take-profit sell.
        if sym.upper() in exclude_symbols:
            log.info("%s skipped (SL/TP triggered this cycle)", sym)
            details.append({
                "symbol":     sym,
                "target_pct": a["allocation_pct"],
                "actual_pct": round(actuals[sym]["value_usdt"] / effective_total * 100, 2) if effective_total else 0,
                "deviation":  0.0,
                "diff_usdt":  0.0,
                "action":     "SKIP_SL_TP",
            })
            continue

        target_val = round(effective_total * a["allocation_pct"] / 100, 2)
        actual_val = actuals[sym]["value_usdt"]
        diff       = round(actual_val - target_val, 2)   # + = زيادة → بيع، - = ناقص → شراء

        log.info("%s | actual=%.2f$ target=%.2f$ diff=%+.2f$", sym, actual_val, target_val, diff)

        entry = {
            "symbol":     sym,
            "target_pct": a["allocation_pct"],
            "actual_pct": round(actual_val / effective_total * 100, 2) if effective_total else 0,
            "deviation":  round(diff / effective_total * 100, 2) if effective_total else 0,
            "diff_usdt":  diff,
            "action":     "SKIP",
        }

        if diff > 1.0:    # زيادة أكبر من 1$ → بيع
            sells.append({"sym": sym, "diff": diff, "price": actuals[sym]["price"], "entry": entry})
        elif diff < -1.0: # ناقص أكبر من 1$ → شراء (الحد الأدنى 1$)
            buys.append({"sym": sym, "diff": abs(diff), "entry": entry})
        else:
            details.append(entry)

    # ── Sells first ──────────────────────────────────────────────────────
    for s in sells:
        sym, entry = s["sym"], s["entry"]
        if sym == "USDT":
            details.append(entry)
            continue
        prec = lot_prec.get(sym, 8)
        qty = round(s["diff"] / s["price"], prec)
        log.info("%sSELL %.8f %s (~%.2f$)", "[PAPER] " if paper else "", qty, sym, s["diff"])
        entry["action"] = "PAPER_SELL" if paper else "SELL"
        if not paper:
            try:
                resp = client.place_market_sell(f"{sym}USDT", qty,
                                                qty_precision=lot_prec.get(sym, 8))
                log.info("Sell OK: %s", resp)
            except Exception as e:
                log.error("Sell failed %s: %s", sym, e)
                entry["action"] = f"SELL_ERROR: {e}"
        details.append(entry)

    if not paper:
        time.sleep(3)

    # ── Buys — re-fetch USDT after sells settle ──────────────────────────
    if not buy_enabled:
        for b in buys:
            b["entry"]["action"] = "SKIP_BUY_DISABLED"
            details.append(b["entry"])
        return details

    # Re-read the live USDT balance so that proceeds from sells are included.
    if not paper:
        try:
            usdt_remaining = client.get_asset_balance("USDT")
            # Never spend more than the budget allows.
            usdt_remaining = min(usdt_remaining, budget_usdt)
            log.info("USDT available for buys: %.2f", usdt_remaining)
        except Exception as e:
            log.warning("Could not fetch USDT: %s", e)
            usdt_remaining = None
    else:
        usdt_remaining = None

    for b in buys:
        sym, entry = b["sym"], b["entry"]
        if sym == "USDT":
            details.append(entry)
            continue

        spend = b["diff"]

        if not paper and usdt_remaining is not None:
            if usdt_remaining <= 0:
                entry["action"] = "SKIP_NO_FUNDS"
                details.append(entry)
                continue
            if spend > usdt_remaining:
                spend = round(usdt_remaining, 2)

        if spend < 1.0:
            entry["action"] = "SKIP_MIN"
            details.append(entry)
            continue

        log.info("%sBUY %.2f$ of %s", "[PAPER] " if paper else "", spend, sym)
        entry["action"] = "PAPER_BUY" if paper else "BUY"
        if not paper:
            try:
                resp = client.place_market_buy(f"{sym}USDT", spend)
                log.info("Buy OK: %s", resp)
                if usdt_remaining is not None:
                    usdt_remaining -= spend
            except Exception as e:
                log.error("Buy failed %s: %s", sym, e)
                entry["action"] = f"BUY_ERROR: {e}"
        details.append(entry)

    # Persist to DB and config
    mode = cfg["rebalance"]["mode"]
    record_rebalance(mode, effective_total, details, paper=paper, portfolio_id=portfolio_id)
    record_snapshot(effective_total, [
        {"symbol": a["symbol"], "value_usdt": round(effective_total * a["allocation_pct"] / 100, 2), "actual_pct": a["allocation_pct"]}
        for a in assets_cfg
    ], portfolio_id=portfolio_id)

    cfg["last_rebalance"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    save_config(cfg)

    return details


def execute_rebalance_equal(client: MEXCClient, cfg: dict,
                            portfolio_id: int = 1) -> list:
    """
    Rebalance by redistributing equally across all assets regardless of
    their configured allocation_pct.  Temporarily overrides targets to
    equal shares, then delegates to execute_rebalance.
    """
    import copy
    cfg_eq = copy.deepcopy(cfg)
    assets = cfg_eq["portfolio"]["assets"]
    n = len(assets)
    base = round(100.0 / n, 4)
    remainder = round(100.0 - base * (n - 1), 4)
    for i, a in enumerate(assets):
        a["allocation_pct"] = remainder if i == n - 1 else base
    return execute_rebalance(client, cfg_eq, portfolio_id=portfolio_id)


def get_pnl(cfg: dict, current_usdt: float | None = None) -> dict:
    """Return simple P&L vs initial invested value.

    current_usdt: live portfolio value from MEXC. If not provided, falls back
    to the last recorded snapshot (or initial value if no snapshots exist).
    """
    initial = cfg["portfolio"].get("initial_value_usdt", cfg["portfolio"].get("total_usdt", 0))
    if current_usdt is None:
        snapshots = get_snapshots(1)
        current_usdt = snapshots[0]["total_usdt"] if snapshots else initial
    # Don't show P&L if we have no real data yet
    if current_usdt == 0:
        return {"initial_usdt": initial, "current_usdt": 0, "pnl_usdt": 0, "pnl_pct": 0}
    pnl_usdt = current_usdt - initial
    pnl_pct = (pnl_usdt / initial * 100) if initial else 0.0
    return {
        "initial_usdt": initial,
        "current_usdt": current_usdt,
        "pnl_usdt": round(pnl_usdt, 2),
        "pnl_pct": round(pnl_pct, 2),
    }


# ---------------------------------------------------------------------------
# Deviation check (proportional mode)
# ---------------------------------------------------------------------------

def needs_rebalance_proportional(client: MEXCClient, cfg: dict,
                                  exclude_symbols: set[str] | None = None) -> bool:
    """Return True if any asset deviates >= min_deviation_to_execute_pct.

    Uses the same effective_total calculation as execute_rebalance:
    min(assets_value + free_usdt, budget_usdt).  This ensures the deviation
    check and the rebalance execution always agree on what the total is.

    exclude_symbols: assets to ignore in the deviation check (e.g. already
    handled by SL/TP this cycle).
    """
    exclude_symbols = {s.upper() for s in (exclude_symbols or set())}
    assets_cfg = cfg["portfolio"]["assets"]
    min_dev = cfg["rebalance"]["proportional"]["min_deviation_to_execute_pct"]
    budget_usdt = cfg["portfolio"].get("total_usdt", 0)

    # Compute effective total the same way execute_rebalance does.
    # Fetch all balances in one API call to avoid N+1 requests.
    try:
        all_balances = client.get_all_balances()
    except Exception as e:
        log.warning("get_all_balances failed: %s — falling back to per-asset calls", e)
        all_balances = {}

    assets_value = 0.0
    actuals = {}
    for a in assets_cfg:
        sym = a["symbol"]
        try:
            balance = all_balances.get(sym.upper(), client.get_asset_balance(sym))
            price   = client.get_price(f"{sym}USDT") if sym != "USDT" else 1.0
            val = balance * price
        except Exception as e:
            log.warning("Cannot fetch %s for deviation check: %s", sym, e)
            val = 0.0
        actuals[sym] = val
        if sym != "USDT":
            assets_value += val

    free_usdt = all_balances.get("USDT", 0.0)

    effective_total = min(assets_value + free_usdt, budget_usdt)

    if effective_total <= 0:
        log.info("Effective total is 0 — skipping deviation check.")
        return False

    targets = {a["symbol"]: a["allocation_pct"] for a in assets_cfg}

    for a in assets_cfg:
        sym = a["symbol"]
        if sym.upper() in exclude_symbols:
            log.info("%s skipped (SL/TP triggered this cycle)", sym)
            continue
        actual_pct = actuals[sym] / effective_total * 100
        deviation = abs(actual_pct - targets[sym])
        log.info(
            "%s actual=%.2f%% target=%.2f%% deviation=%.2f%%",
            sym, actual_pct, targets[sym], deviation,
        )
        if deviation >= min_dev:
            log.info(
                "%s deviation=%.2f%% >= threshold %.2f%% → rebalance triggered",
                sym, deviation, min_dev,
            )
            return True
    return False


# ---------------------------------------------------------------------------
# Timed schedule helper
# ---------------------------------------------------------------------------

def next_run_time(
    frequency: str,
    from_dt: Optional[datetime] = None,
    target_hour: int = 0,
) -> datetime:
    """Return the next scheduled run datetime.

    Short intervals (30min, 1h, 4h, 8h, 12h) fire relative to now.
    Long intervals (daily, weekly, monthly) fire at target_hour UTC.
    """
    now = from_dt or datetime.utcnow()

    # Short fixed-interval frequencies
    if frequency in TIMED_FREQUENCY_MINUTES and frequency not in ("daily", "weekly", "monthly"):
        minutes = TIMED_FREQUENCY_MINUTES[frequency]
        return now + timedelta(minutes=minutes)

    if frequency == "daily":
        candidate = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    elif frequency == "weekly":
        candidate = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(weeks=1)
        return candidate
    elif frequency == "monthly":
        candidate = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=30)
        return candidate
    raise ValueError(f"Unknown frequency: {frequency}")


# ---------------------------------------------------------------------------
# Stop Loss / Take Profit guard
# ---------------------------------------------------------------------------

def check_sl_tp(client: MEXCClient, cfg: dict) -> list[dict]:
    """Check each asset against its stop-loss and take-profit thresholds.

    Returns a list of triggered actions (each dict has symbol, action, price).
    Assets are sold to USDT when triggered.  Entry prices are read from
    cfg["portfolio"]["assets"][i]["entry_price_usdt"] (optional).

    Stop-loss  : sell when price drops >= stop_loss_pct% below entry.
    Take-profit: sell when price rises >= take_profit_pct% above entry.
    """
    risk_cfg = cfg.get("risk", {})
    sl_pct = risk_cfg.get("stop_loss_pct")    # e.g. 10  → 10%
    tp_pct = risk_cfg.get("take_profit_pct")  # e.g. 50  → 50%

    if not sl_pct and not tp_pct:
        return []

    paper = is_paper_trading(cfg)
    triggered = []

    for a in cfg["portfolio"]["assets"]:
        sym = a["symbol"]
        if sym == "USDT":
            continue

        entry_price = a.get("entry_price_usdt")
        if not entry_price:
            continue  # no entry price set — skip SL/TP for this asset

        try:
            current_price = client.get_price(f"{sym}USDT")
        except Exception as e:
            log.warning("SL/TP: cannot fetch price for %s: %s", sym, e)
            continue

        change_pct = (current_price - entry_price) / entry_price * 100

        action = None
        if sl_pct and change_pct <= -abs(sl_pct):
            action = "STOP_LOSS"
        elif tp_pct and change_pct >= abs(tp_pct):
            action = "TAKE_PROFIT"

        if action:
            log.info(
                "%s %s triggered | entry=%.4f current=%.4f change=%.2f%%",
                sym, action, entry_price, current_price, change_pct,
            )
            if not paper:
                try:
                    balance = client.get_asset_balance(sym)
                    if balance > 0:
                        resp = client.place_market_sell(f"{sym}USDT", balance)
                        log.info("%s sell OK: %s", action, resp)
                except Exception as e:
                    log.error("%s sell failed for %s: %s", action, sym, e)
            triggered.append({
                "symbol": sym,
                "action": action,
                "entry_price": entry_price,
                "current_price": current_price,
                "change_pct": round(change_pct, 2),
            })

    return triggered


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------

def terminate(client: MEXCClient, cfg: dict) -> None:
    """Stop the bot. If sell_at_termination is True, liquidate all assets to USDT.

    In paper mode, liquidation is simulated only (no real sell orders).
    """
    log.info("Terminating bot '%s'", cfg["bot"]["name"])
    paper = is_paper_trading(cfg)
    if cfg["termination"]["sell_at_termination"]:
        log.info("sell_at_termination=True%s", " [PAPER]" if paper else "")
        for a in cfg["portfolio"]["assets"]:
            sym = a["symbol"]
            if sym == "USDT":
                continue
            balance = client.get_asset_balance(sym)
            if balance > 0:
                log.info("%sSelling %.8f %s", "[PAPER] " if paper else "", balance, sym)
                if paper:
                    continue
                try:
                    resp = client.place_market_sell(f"{sym}USDT", balance)
                    log.info("Sell response: %s", resp)
                except Exception as e:
                    log.error("Failed to sell %s: %s", sym, e)
    else:
        log.info("sell_at_termination=False – assets left as-is")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(cfg: dict) -> None:
    client = MEXCClient()
    mode = cfg["rebalance"]["mode"]
    bot_name = cfg["bot"]["name"]

    log.info("Starting Smart Portfolio bot: %s | mode: %s", bot_name, mode)

    if mode == "proportional":
        interval_sec = cfg["rebalance"]["proportional"]["check_interval_minutes"] * 60
        log.info("Check interval: %d seconds", interval_sec)
        try:
            while True:
                log.info("--- Proportional check ---")
                # SL/TP guard runs before rebalance.
                # Symbols sold by SL/TP are excluded from rebalance this cycle
                # to prevent the rebalancer from immediately buying them back.
                triggered = check_sl_tp(client, cfg)
                sl_tp_symbols = {t["symbol"] for t in triggered}
                if triggered:
                    log.info("SL/TP triggered for: %s", list(sl_tp_symbols))
                if needs_rebalance_proportional(client, cfg, exclude_symbols=sl_tp_symbols):
                    execute_rebalance(client, cfg, exclude_symbols=sl_tp_symbols)
                else:
                    log.info("No rebalance needed.")
                time.sleep(interval_sec)
        except KeyboardInterrupt:
            terminate(client, cfg)

    elif mode == "timed":
        timed_cfg = cfg["rebalance"]["timed"]
        frequency = timed_cfg["frequency"]
        target_hour = timed_cfg.get("hour", 0)
        next_run = next_run_time(frequency, target_hour=target_hour)
        log.info("First rebalance scheduled at %s UTC", next_run.isoformat())
        # For short intervals, poll every 30 s; for long ones every 60 s.
        short_freq = frequency in TIMED_FREQUENCY_MINUTES and frequency not in ("daily", "weekly", "monthly")
        poll_sec = 30 if short_freq else 60
        try:
            while True:
                now = datetime.utcnow()
                # SL/TP guard runs every poll cycle regardless of schedule.
                # Triggered symbols are excluded from the timed rebalance this cycle.
                triggered = check_sl_tp(client, cfg)
                sl_tp_symbols = {t["symbol"] for t in triggered}
                if triggered:
                    log.info("SL/TP triggered for: %s", list(sl_tp_symbols))
                if now >= next_run:
                    log.info("--- Timed rebalance (%s) ---", frequency)
                    execute_rebalance(client, cfg, exclude_symbols=sl_tp_symbols)
                    cfg = load_config()
                    frequency = cfg["rebalance"]["timed"]["frequency"]
                    target_hour = cfg["rebalance"]["timed"].get("hour", 0)
                    next_run = next_run_time(frequency, target_hour=target_hour)
                    log.info("Next rebalance at %s UTC", next_run.isoformat())
                time.sleep(poll_sec)
        except KeyboardInterrupt:
            terminate(client, cfg)

    elif mode == "unbalanced":
        log.info("Mode: unbalanced – no automatic rebalancing.")
        log.info("Run with --rebalance-now to trigger a manual rebalance.")
        portfolio = get_portfolio_value(client, cfg["portfolio"]["assets"])
        log.info("Current portfolio value: %.2f USDT", portfolio["total_usdt"])
        for r in portfolio["assets"]:
            log.info(
                "  %s: %.8f @ %.4f = %.2f USDT (%.2f%%)",
                r["symbol"], r["balance"], r["price"], r["value_usdt"], r["actual_pct"],
            )

    else:
        raise ValueError(f"Unknown rebalance mode: {mode}")
