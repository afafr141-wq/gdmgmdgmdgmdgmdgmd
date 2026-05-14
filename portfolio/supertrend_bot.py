"""
SuperTrend + UT Bot signal strategy.

Computes two independent indicators on OHLCV candles:
  - SuperTrend  : trend-following indicator using ATR bands
  - UT Bot      : trailing stop-based signal (UT Bot Alert by QuantNomad)

A BUY signal fires when BOTH indicators flip bullish simultaneously.
A SELL signal fires when BOTH indicators flip bearish simultaneously.

Signals are independent — a BUY does not require a prior SELL and vice versa.
"""
from __future__ import annotations

import logging
import math
from typing import Literal

log = logging.getLogger(__name__)

Signal = Literal["BUY", "SELL", None]


# ── ATR helper ─────────────────────────────────────────────────────────────────

def _compute_atr(candles: list[dict], period: int) -> list[float]:
    """Return ATR values aligned with candles (first `period-1` entries are 0)."""
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["high"] - c["low"])
        else:
            prev_close = candles[i - 1]["close"]
            tr = max(
                c["high"] - c["low"],
                abs(c["high"] - prev_close),
                abs(c["low"] - prev_close),
            )
            trs.append(tr)

    atrs: list[float] = [0.0] * len(trs)
    if len(trs) < period:
        return atrs

    # Seed with simple average for the first window
    atrs[period - 1] = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atrs[i] = (atrs[i - 1] * (period - 1) + trs[i]) / period
    return atrs


# ── SuperTrend ─────────────────────────────────────────────────────────────────

def compute_supertrend(
    candles: list[dict],
    period: int = 10,
    multiplier: float = 3.0,
) -> list[dict]:
    """
    Compute SuperTrend for each candle.

    Returns a list of dicts:
        {
            "direction": 1 (bullish) | -1 (bearish),
            "supertrend": float,   # the band value
            "upper": float,
            "lower": float,
        }
    Entries before the ATR warm-up window are filled with direction=0.
    """
    atrs = _compute_atr(candles, period)
    n = len(candles)
    result: list[dict] = [{"direction": 0, "supertrend": 0.0, "upper": 0.0, "lower": 0.0}] * n

    upper_band = [0.0] * n
    lower_band = [0.0] * n
    direction  = [0]   * n
    st_val     = [0.0] * n

    for i in range(period - 1, n):
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2
        atr = atrs[i]
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr

        if i == period - 1:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
            direction[i]  = 1
            st_val[i]     = lower_band[i]
        else:
            prev_upper = upper_band[i - 1]
            prev_lower = lower_band[i - 1]
            prev_close = candles[i - 1]["close"]
            close      = candles[i]["close"]

            # Adjust bands: only tighten, never widen
            upper_band[i] = (
                basic_upper
                if basic_upper < prev_upper or prev_close > prev_upper
                else prev_upper
            )
            lower_band[i] = (
                basic_lower
                if basic_lower > prev_lower or prev_close < prev_lower
                else prev_lower
            )

            # Direction
            if direction[i - 1] == -1:
                direction[i] = 1 if close > upper_band[i] else -1
            else:
                direction[i] = -1 if close < lower_band[i] else 1

            st_val[i] = lower_band[i] if direction[i] == 1 else upper_band[i]

        result[i] = {
            "direction": direction[i],
            "supertrend": st_val[i],
            "upper": upper_band[i],
            "lower": lower_band[i],
        }

    return result


# ── UT Bot ─────────────────────────────────────────────────────────────────────

def compute_ut_bot(
    candles: list[dict],
    key_value: float = 1.0,
    atr_period: int = 1,
) -> list[dict]:
    """
    UT Bot Alert (QuantNomad) — trailing stop with ATR-based sensitivity.

    key_value  : ATR multiplier (sensitivity); higher = fewer signals
    atr_period : ATR period (1 = pure candle range, no smoothing)

    Returns list of dicts per candle:
        {
            "direction": 1 (bullish) | -1 (bearish) | 0 (warm-up),
            "trailing_stop": float,
            "signal": "BUY" | "SELL" | None,
        }
    """
    atrs = _compute_atr(candles, max(atr_period, 1))
    n = len(candles)
    result: list[dict] = [{"direction": 0, "trailing_stop": 0.0, "signal": None}] * n

    trailing_stop = [0.0] * n
    direction     = [0]   * n

    for i in range(1, n):
        close = candles[i]["close"]
        atr   = atrs[i] if atrs[i] > 0 else (candles[i]["high"] - candles[i]["low"])
        loss  = key_value * atr

        prev_ts = trailing_stop[i - 1]
        prev_close = candles[i - 1]["close"]

        if close > prev_ts:
            trailing_stop[i] = max(prev_ts, close - loss)
        elif close < prev_ts:
            trailing_stop[i] = min(prev_ts, close + loss)
        else:
            trailing_stop[i] = prev_ts

        # Direction: price crosses trailing stop
        if prev_close <= prev_ts and close > trailing_stop[i]:
            direction[i] = 1
        elif prev_close >= prev_ts and close < trailing_stop[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

        # Signal only on direction change
        signal: Signal = None
        if direction[i] == 1 and direction[i - 1] != 1:
            signal = "BUY"
        elif direction[i] == -1 and direction[i - 1] != -1:
            signal = "SELL"

        result[i] = {
            "direction": direction[i],
            "trailing_stop": trailing_stop[i],
            "signal": signal,
        }

    return result


# ── Combined signal ────────────────────────────────────────────────────────────

def get_combined_signal(
    st_data: list[dict],
    ut_data: list[dict],
) -> Signal:
    """
    Return the combined signal for the LAST completed candle.

    BUY  : SuperTrend bullish (direction=1) AND UT Bot just fired BUY
    SELL : SuperTrend bearish (direction=-1) AND UT Bot just fired SELL
    None : no confluence
    """
    if len(st_data) < 2 or len(ut_data) < 2:
        return None

    # Use second-to-last candle (last completed, not the forming one)
    idx = len(st_data) - 2
    st  = st_data[idx]
    ut  = ut_data[idx]

    if st["direction"] == 1 and ut["signal"] == "BUY":
        return "BUY"
    if st["direction"] == -1 and ut["signal"] == "SELL":
        return "SELL"
    return None


# ── Trade execution ────────────────────────────────────────────────────────────

def execute_signal(
    client,
    cfg: dict,
    signal: Signal,
    bot_id: int,
) -> dict:
    """
    Execute a BUY or SELL market order based on the signal.

    cfg keys used:
        symbol          : e.g. "BTCUSDT"
        capital_usdt    : USDT to deploy on BUY
        paper_trading   : bool — skip real orders if True
        position_size   : float | None — override base qty for SELL

    Returns a result dict with keys: action, symbol, price, qty, usdt, paper, error.
    """
    import os

    symbol       = cfg.get("symbol", "BTCUSDT").upper()
    capital_usdt = float(cfg.get("capital_usdt", 50.0))
    paper        = cfg.get("paper_trading", False) or os.environ.get("PAPER_TRADING", "").lower() == "true"

    result: dict = {
        "action": signal,
        "symbol": symbol,
        "price": 0.0,
        "qty": 0.0,
        "usdt": 0.0,
        "paper": paper,
        "error": None,
    }

    try:
        price = client.get_price(symbol)
        result["price"] = price

        if signal == "BUY":
            usdt_balance = client.get_asset_balance("USDT")
            spend = min(capital_usdt, usdt_balance)
            if spend < 1.0:
                result["error"] = f"رصيد USDT غير كافٍ ({usdt_balance:.2f})"
                return result
            result["usdt"] = spend
            result["qty"]  = spend / price if price > 0 else 0
            if not paper:
                client.place_market_buy(symbol, spend)
            log.info("ST+UT BUY %s %.2f USDT @ %.6f (paper=%s)", symbol, spend, price, paper)

        elif signal == "SELL":
            base = symbol.replace("USDT", "")
            base_balance = client.get_asset_balance(base)
            if base_balance <= 0:
                result["error"] = f"لا يوجد رصيد {base} للبيع"
                return result
            result["qty"]  = base_balance
            result["usdt"] = base_balance * price
            if not paper:
                client.place_market_sell(symbol, base_balance)
            log.info("ST+UT SELL %s %.6f @ %.6f (paper=%s)", symbol, base_balance, price, paper)

    except Exception as e:
        result["error"] = str(e)
        log.error("execute_signal bot_id=%d %s %s: %s", bot_id, signal, symbol, e)

    return result


# ── Signal analysis helper (for status display) ────────────────────────────────

def analyze_current_state(
    candles: list[dict],
    st_period: int = 10,
    st_multiplier: float = 3.0,
    ut_key_value: float = 1.0,
    ut_atr_period: int = 1,
) -> dict:
    """
    Run both indicators and return a summary dict for the current market state.
    Used by the Telegram status command.
    """
    if len(candles) < max(st_period, ut_atr_period) + 5:
        return {"error": "بيانات غير كافية للحساب"}

    st_data = compute_supertrend(candles, st_period, st_multiplier)
    ut_data = compute_ut_bot(candles, ut_key_value, ut_atr_period)
    signal  = get_combined_signal(st_data, ut_data)

    last_idx = len(candles) - 2  # last completed candle
    last_c   = candles[last_idx]
    st_last  = st_data[last_idx]
    ut_last  = ut_data[last_idx]

    st_dir_str = "🟢 صاعد" if st_last["direction"] == 1 else ("🔴 هابط" if st_last["direction"] == -1 else "—")
    ut_dir_str = "🟢 صاعد" if ut_last["direction"] == 1 else ("🔴 هابط" if ut_last["direction"] == -1 else "—")

    return {
        "close":          last_c["close"],
        "st_direction":   st_last["direction"],
        "st_value":       st_last["supertrend"],
        "st_direction_str": st_dir_str,
        "ut_direction":   ut_last["direction"],
        "ut_trailing":    ut_last["trailing_stop"],
        "ut_direction_str": ut_dir_str,
        "signal":         signal,
        "error":          None,
    }
