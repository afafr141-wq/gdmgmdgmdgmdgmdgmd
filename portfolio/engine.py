"""
Portfolio loop engine — manages per-portfolio rebalance threads and
SuperTrend + UT Bot signal threads.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Optional

log = logging.getLogger("engine")

# pid -> {"thread": Thread, "stop": Event, "error": str|None, "started_at": str|None}
_portfolio_loops: dict[int, dict] = {}
_loops_lock = threading.Lock()

# bot_id -> {"thread": Thread, "stop": Event, "error": str|None, "started_at": str|None}
_supertrend_loops: dict[int, dict] = {}
_st_lock = threading.Lock()


# ── Telegram notification (fire-and-forget) ────────────────────────────────────

def notify_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        import requests as _req
        _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        log.warning("Telegram notification failed: %s", e)


# ── Loop worker ────────────────────────────────────────────────────────────────

def _make_loop(portfolio_id: int, stop_event: threading.Event) -> None:
    from portfolio.smart_portfolio import (
        execute_rebalance, needs_rebalance_proportional,
        next_run_time, get_portfolio_value, check_sl_tp,
        TIMED_FREQUENCY_MINUTES,
    )
    from portfolio.database import get_portfolio as db_get_portfolio
    from portfolio.mexc_client import MEXCClient

    with _loops_lock:
        if portfolio_id in _portfolio_loops:
            _portfolio_loops[portfolio_id]["error"] = None
            _portfolio_loops[portfolio_id]["started_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    try:
        cfg = db_get_portfolio(portfolio_id)
        if cfg is None:
            raise ValueError(f"Portfolio {portfolio_id} not found")

        client = MEXCClient()
        mode = cfg["rebalance"]["mode"]
        log.info("Portfolio %d loop started | mode: %s", portfolio_id, mode)
        timed_next_run = None

        while not stop_event.is_set():
            try:
                cfg = db_get_portfolio(portfolio_id)
                if cfg is None:
                    break
                current_mode = cfg["rebalance"]["mode"]

                sl_tp_triggered = check_sl_tp(client, cfg)
                sl_tp_symbols = {t["symbol"] for t in sl_tp_triggered}
                if sl_tp_symbols:
                    for t in sl_tp_triggered:
                        msg = (
                            f"⚠️ *{t['action']}* — `{t['symbol']}`\n"
                            f"دخول: `{t['entry_price']:.4f}` | حالي: `{t['current_price']:.4f}`\n"
                            f"تغيير: `{t['change_pct']:+.2f}%`"
                        )
                        notify_telegram(msg)

                if current_mode == "proportional":
                    interval = cfg["rebalance"]["proportional"]["check_interval_minutes"] * 60
                    buy_enabled = cfg.get("buy_enabled", False)
                    if needs_rebalance_proportional(client, cfg, exclude_symbols=sl_tp_symbols):
                        result = execute_rebalance(
                            client, cfg,
                            exclude_symbols=sl_tp_symbols,
                            portfolio_id=portfolio_id,
                            buy_enabled=buy_enabled,
                        )
                        trades = [r for r in result if r.get("action") in ("BUY", "SELL")]
                        if trades:
                            summary = "\n".join(
                                f"{'🟢' if r['action']=='BUY' else '🔴'} `{r['symbol']}` {r['diff_usdt']:+.2f}$"
                                for r in trades
                            )
                            notify_telegram(f"🔄 *إعادة توازن تلقائية*\n\n{summary}")
                    stop_event.wait(interval)

                elif current_mode == "timed":
                    timed_cfg = cfg["rebalance"]["timed"]
                    frequency = timed_cfg["frequency"]
                    target_hour = timed_cfg.get("hour", 0)
                    buy_enabled = cfg.get("buy_enabled", False)
                    if timed_next_run is None:
                        timed_next_run = next_run_time(frequency, target_hour=target_hour)
                    if datetime.utcnow() >= timed_next_run:
                        result = execute_rebalance(
                            client, cfg,
                            exclude_symbols=sl_tp_symbols,
                            portfolio_id=portfolio_id,
                            buy_enabled=buy_enabled,
                        )
                        trades = [r for r in result if r.get("action") in ("BUY", "SELL")]
                        if trades:
                            summary = "\n".join(
                                f"{'🟢' if r['action']=='BUY' else '🔴'} `{r['symbol']}` {r['diff_usdt']:+.2f}$"
                                for r in trades
                            )
                            notify_telegram(f"🔄 *إعادة توازن ({frequency})*\n\n{summary}")
                        timed_next_run = next_run_time(frequency, target_hour=target_hour)
                    short_freq = (
                        frequency in TIMED_FREQUENCY_MINUTES
                        and frequency not in ("daily", "weekly", "monthly")
                    )
                    stop_event.wait(30 if short_freq else 60)

                else:
                    timed_next_run = None
                    stop_event.wait(60)

            except Exception as e:
                log.error("Portfolio %d loop error: %s", portfolio_id, e)
                stop_event.wait(30)

    except Exception as e:
        with _loops_lock:
            if portfolio_id in _portfolio_loops:
                _portfolio_loops[portfolio_id]["error"] = str(e)
        log.error("Portfolio %d loop crashed: %s", portfolio_id, e)

    log.info("Portfolio %d loop stopped", portfolio_id)


# ── Public API ─────────────────────────────────────────────────────────────────

def is_portfolio_running(portfolio_id: int) -> bool:
    with _loops_lock:
        entry = _portfolio_loops.get(portfolio_id)
    return entry is not None and entry["thread"].is_alive()


def start_portfolio_loop(portfolio_id: int) -> None:
    from portfolio.database import set_bot_running
    with _loops_lock:
        existing = _portfolio_loops.get(portfolio_id)
        if existing is not None and existing["thread"].is_alive():
            return
        stop_ev = threading.Event()
        t = threading.Thread(
            target=_make_loop, args=(portfolio_id, stop_ev),
            daemon=True, name=f"portfolio-{portfolio_id}",
        )
        _portfolio_loops[portfolio_id] = {
            "thread": t, "stop": stop_ev,
            "error": None, "started_at": None,
        }
    t.start()
    set_bot_running(portfolio_id, True)
    log.info("Portfolio %d loop started", portfolio_id)


def stop_portfolio_loop(portfolio_id: int) -> None:
    from portfolio.database import set_bot_running
    with _loops_lock:
        entry = _portfolio_loops.get(portfolio_id)
    if entry:
        entry["stop"].set()
        entry["thread"].join(timeout=5)
        with _loops_lock:
            if portfolio_id in _portfolio_loops and not _portfolio_loops[portfolio_id]["thread"].is_alive():
                del _portfolio_loops[portfolio_id]
    set_bot_running(portfolio_id, False)
    log.info("Portfolio %d loop stopped", portfolio_id)


def get_loop_info(portfolio_id: int) -> Optional[dict]:
    with _loops_lock:
        entry = _portfolio_loops.get(portfolio_id)
    if entry is None:
        return None
    return {
        "running": entry["thread"].is_alive(),
        "error": entry.get("error"),
        "started_at": entry.get("started_at"),
    }


# ── SuperTrend + UT Bot loop ───────────────────────────────────────────────────

# Candle interval → sleep seconds between checks
_INTERVAL_SECONDS: dict[str, int] = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
    "60m": 3600,
    "4h":  14400,
    "8h":  28800,
    "1d":  86400,
}


def _make_supertrend_loop(bot_id: int, stop_event: threading.Event) -> None:
    from portfolio.database import (
        get_supertrend_bot, update_supertrend_bot_status,
        record_supertrend_signal,
    )
    from portfolio.supertrend_bot import (
        compute_supertrend, compute_ut_bot,
        get_combined_signal, execute_signal,
    )
    from portfolio.mexc_client import MEXCClient

    with _st_lock:
        if bot_id in _supertrend_loops:
            _supertrend_loops[bot_id]["error"] = None
            _supertrend_loops[bot_id]["started_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    try:
        row = get_supertrend_bot(bot_id)
        if row is None:
            raise ValueError(f"SuperTrend bot {bot_id} not found")

        client = MEXCClient()
        cfg    = row["config"]
        symbol   = cfg.get("symbol", "BTCUSDT").upper()
        # Normalize interval to MEXC-accepted values
        _iv_fix = {"1h": "60m", "2h": "60m", "3m": "5m", "6h": "4h", "12h": "8h"}
        interval = _iv_fix.get(cfg.get("interval", "60m"), cfg.get("interval", "60m"))
        st_cfg   = cfg.get("supertrend", {})
        ut_cfg   = cfg.get("ut_bot", {})
        st_period     = int(st_cfg.get("period", 10))
        st_multiplier = float(st_cfg.get("multiplier", 3.0))
        ut_key_value  = float(ut_cfg.get("key_value", 1.0))
        ut_atr_period = int(ut_cfg.get("atr_period", 1))
        candles_needed = max(st_period, ut_atr_period) * 3 + 10
        sleep_secs = _INTERVAL_SECONDS.get(interval, 3600)

        log.info("SuperTrend bot %d started | %s %s", bot_id, symbol, interval)

        # Track last signal to avoid duplicate orders on the same candle
        last_signal_ts: int = 0

        while not stop_event.is_set():
            try:
                # Re-read config so live changes take effect
                row = get_supertrend_bot(bot_id)
                if row is None:
                    break
                cfg = row["config"]
                symbol        = cfg.get("symbol", symbol).upper()
                interval      = _iv_fix.get(cfg.get("interval", interval), cfg.get("interval", interval))
                st_cfg        = cfg.get("supertrend", {})
                ut_cfg        = cfg.get("ut_bot", {})
                st_period     = int(st_cfg.get("period", st_period))
                st_multiplier = float(st_cfg.get("multiplier", st_multiplier))
                ut_key_value  = float(ut_cfg.get("key_value", ut_key_value))
                ut_atr_period = int(ut_cfg.get("atr_period", ut_atr_period))
                sleep_secs    = _INTERVAL_SECONDS.get(interval, sleep_secs)
                candles_needed = max(st_period, ut_atr_period) * 3 + 10

                candles = client.get_klines(symbol, interval, limit=candles_needed + 2)
                if len(candles) < candles_needed:
                    log.warning("ST bot %d: not enough candles (%d)", bot_id, len(candles))
                    stop_event.wait(30)
                    continue

                st_data = compute_supertrend(candles, st_period, st_multiplier)
                ut_data = compute_ut_bot(candles, ut_key_value, ut_atr_period)
                signal  = get_combined_signal(st_data, ut_data)

                # Use the open_time of the last completed candle as dedup key
                last_completed_ts = candles[-2]["open_time"]

                if signal and last_completed_ts != last_signal_ts:
                    last_signal_ts = last_completed_ts
                    log.info("ST bot %d signal: %s %s", bot_id, signal, symbol)

                    result = execute_signal(client, cfg, signal, bot_id)
                    record_supertrend_signal(
                        bot_id=bot_id,
                        signal=signal,
                        price=result["price"],
                        qty=result["qty"],
                        usdt=result["usdt"],
                        paper=result["paper"],
                        error=result.get("error"),
                    )

                    # Telegram notification
                    paper_tag = " 📄 ورقي" if result["paper"] else ""
                    icon = "🟢" if signal == "BUY" else "🔴"
                    if result.get("error"):
                        msg = (
                            f"⚠️ *ST+UT إشارة {signal}* — `{symbol}`{paper_tag}\n"
                            f"❌ خطأ: `{result['error']}`"
                        )
                    else:
                        msg = (
                            f"{icon} *ST+UT إشارة {signal}* — `{symbol}`{paper_tag}\n"
                            f"السعر: `{result['price']:.6f}`\n"
                            f"الكمية: `{result['qty']:.6f}`\n"
                            f"القيمة: `{result['usdt']:.2f} USDT`"
                        )
                    notify_telegram(msg)

            except Exception as e:
                log.error("SuperTrend bot %d loop error: %s", bot_id, e)
                with _st_lock:
                    if bot_id in _supertrend_loops:
                        _supertrend_loops[bot_id]["error"] = str(e)
                stop_event.wait(30)
                continue

            stop_event.wait(sleep_secs)

    except Exception as e:
        with _st_lock:
            if bot_id in _supertrend_loops:
                _supertrend_loops[bot_id]["error"] = str(e)
        log.error("SuperTrend bot %d crashed: %s", bot_id, e)

    update_supertrend_bot_status(bot_id, False)
    log.info("SuperTrend bot %d loop stopped", bot_id)


# ── SuperTrend public API ──────────────────────────────────────────────────────

def is_supertrend_running(bot_id: int) -> bool:
    with _st_lock:
        entry = _supertrend_loops.get(bot_id)
    return entry is not None and entry["thread"].is_alive()


def start_supertrend_loop(bot_id: int) -> None:
    from portfolio.database import update_supertrend_bot_status
    with _st_lock:
        existing = _supertrend_loops.get(bot_id)
        if existing is not None and existing["thread"].is_alive():
            return
        stop_ev = threading.Event()
        t = threading.Thread(
            target=_make_supertrend_loop, args=(bot_id, stop_ev),
            daemon=True, name=f"supertrend-{bot_id}",
        )
        _supertrend_loops[bot_id] = {
            "thread": t, "stop": stop_ev,
            "error": None, "started_at": None,
        }
    t.start()
    update_supertrend_bot_status(bot_id, True)
    log.info("SuperTrend bot %d loop started", bot_id)


def stop_supertrend_loop(bot_id: int) -> None:
    from portfolio.database import update_supertrend_bot_status
    with _st_lock:
        entry = _supertrend_loops.get(bot_id)
    if entry:
        entry["stop"].set()
        entry["thread"].join(timeout=5)
        with _st_lock:
            if bot_id in _supertrend_loops and not _supertrend_loops[bot_id]["thread"].is_alive():
                del _supertrend_loops[bot_id]
    update_supertrend_bot_status(bot_id, False)
    log.info("SuperTrend bot %d loop stopped", bot_id)


def get_supertrend_loop_info(bot_id: int) -> Optional[dict]:
    with _st_lock:
        entry = _supertrend_loops.get(bot_id)
    if entry is None:
        return None
    return {
        "running":    entry["thread"].is_alive(),
        "error":      entry.get("error"),
        "started_at": entry.get("started_at"),
    }
