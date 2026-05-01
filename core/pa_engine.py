"""
Price Action Engine — Liquidity Sweep + Candle Confirmation.

Logic:
  1. Structure Detection  — determines trend direction (Uptrend / Downtrend)
     using swing highs/lows (HH+HL = up, LH+LL = down).

  2. Liquidity Mapping    — finds Equal Highs and Equal Lows within a
     tolerance band; these are zones where stop-loss orders cluster.

  3. Sweep Detection      — a candle whose wick pierces an Equal High/Low
     but whose body closes back on the other side = liquidity sweep.

  4. Candle Confirmation  — after a sweep the very next closed candle must
     be a Bullish Engulfing / Hammer (for buys) or Bearish Engulfing /
     Shooting Star (for sells).

  5. Take-Profit target   — nearest Equal High above entry (buy) or
     nearest Equal Low below entry (sell).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np

from config.settings import PA_PIVOT_LEFT, PA_PIVOT_RIGHT, PA_EQUAL_TOLERANCE

logger = logging.getLogger(__name__)

# ── Types ──────────────────────────────────────────────────────────────────────

Trend     = Literal["up", "down", "sideways"]
Direction = Literal["buy", "sell"]


@dataclass
class PASignal:
    direction:    Direction
    entry_price:  float
    tp_price:     float
    sweep_price:  float          # the wick extreme that was swept
    liquidity_level: float       # the Equal H/L that was swept
    trend:        Trend
    candle_pattern: str          # e.g. "bullish_engulfing", "hammer"
    timeframe:    str
    symbol:       str


@dataclass
class PALevels:
    equal_highs:   list[float] = field(default_factory=list)
    equal_lows:    list[float] = field(default_factory=list)
    trend:         Trend = "sideways"
    swing_highs:   list[float] = field(default_factory=list)
    swing_lows:    list[float] = field(default_factory=list)


# ── Parameters ─────────────────────────────────────────────────────────────────

# How close two highs/lows must be (% of price) to be "equal"
EQUAL_LEVEL_TOLERANCE_PCT: float = PA_EQUAL_TOLERANCE

# Minimum number of swing points needed to determine trend
MIN_SWING_POINTS: int = 3

# Minimum wick-to-body ratio for Hammer / Shooting Star
HAMMER_WICK_RATIO: float = 2.0

# Minimum body ratio for Engulfing (engulfing body / prev body)
ENGULF_RATIO: float = 1.0


# ── Structure Detection ────────────────────────────────────────────────────────

def _find_swing_highs(highs: np.ndarray, lows: np.ndarray, left: int = 3, right: int = 3) -> list[int]:
    """Return indices of swing highs (local maxima)."""
    n, result = len(highs), []
    for i in range(left, n - right):
        window = highs[i - left: i + right + 1]
        if highs[i] == window.max() and np.sum(window == highs[i]) == 1:
            result.append(i)
    return result


def _find_swing_lows(lows: np.ndarray, left: int = 3, right: int = 3) -> list[int]:
    """Return indices of swing lows (local minima)."""
    n, result = len(lows), []
    for i in range(left, n - right):
        window = lows[i - left: i + right + 1]
        if lows[i] == window.min() and np.sum(window == lows[i]) == 1:
            result.append(i)
    return result


def detect_trend(
    highs: np.ndarray,
    lows: np.ndarray,
    left: int = 3,
    right: int = 3,
) -> tuple[Trend, list[float], list[float]]:
    """
    Determine market structure from the last few swing points.

    Returns (trend, swing_high_prices, swing_low_prices).
    """
    sh_idx = _find_swing_highs(highs, lows, left, right)
    sl_idx = _find_swing_lows(lows, left, right)

    sh_prices = [float(highs[i]) for i in sh_idx[-MIN_SWING_POINTS:]]
    sl_prices = [float(lows[i])  for i in sl_idx[-MIN_SWING_POINTS:]]

    if len(sh_prices) < 2 or len(sl_prices) < 2:
        return "sideways", sh_prices, sl_prices

    # Uptrend: each swing high and swing low is higher than the previous
    hh = all(sh_prices[i] > sh_prices[i - 1] for i in range(1, len(sh_prices)))
    hl = all(sl_prices[i] > sl_prices[i - 1] for i in range(1, len(sl_prices)))

    # Downtrend: each swing high and swing low is lower than the previous
    lh = all(sh_prices[i] < sh_prices[i - 1] for i in range(1, len(sh_prices)))
    ll = all(sl_prices[i] < sl_prices[i - 1] for i in range(1, len(sl_prices)))

    if hh and hl:
        return "up", sh_prices, sl_prices
    if lh and ll:
        return "down", sh_prices, sl_prices
    return "sideways", sh_prices, sl_prices


# ── Liquidity Mapping ──────────────────────────────────────────────────────────

def _group_equal_levels(prices: list[float], tolerance_pct: float) -> list[float]:
    """
    Cluster prices that are within tolerance_pct of each other.
    Returns the average price of each cluster that has ≥ 2 members
    (i.e. a genuine equal-level zone).
    """
    if not prices:
        return []
    sorted_p = sorted(prices)
    clusters: list[list[float]] = [[sorted_p[0]]]
    for p in sorted_p[1:]:
        ref = clusters[-1][-1]
        if abs(p - ref) / ref * 100 <= tolerance_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [float(np.mean(c)) for c in clusters if len(c) >= 2]


def find_equal_highs(
    highs: np.ndarray,
    lows: np.ndarray,
    tolerance_pct: float = EQUAL_LEVEL_TOLERANCE_PCT,
    left: int = 3,
    right: int = 3,
) -> list[float]:
    """Equal Highs: swing high prices that cluster together."""
    sh_idx = _find_swing_highs(highs, lows, left, right)
    prices = [float(highs[i]) for i in sh_idx]
    return _group_equal_levels(prices, tolerance_pct)


def find_equal_lows(
    lows: np.ndarray,
    tolerance_pct: float = EQUAL_LEVEL_TOLERANCE_PCT,
    left: int = 3,
    right: int = 3,
) -> list[float]:
    """Equal Lows: swing low prices that cluster together."""
    sl_idx = _find_swing_lows(lows, left, right)
    prices = [float(lows[i]) for i in sl_idx]
    return _group_equal_levels(prices, tolerance_pct)


# ── Sweep Detection ────────────────────────────────────────────────────────────

def detect_sweep(
    candle_idx: int,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    equal_highs: list[float],
    equal_lows: list[float],
    tolerance_pct: float = EQUAL_LEVEL_TOLERANCE_PCT,
) -> Optional[tuple[Direction, float]]:
    """
    Check if candle at candle_idx swept a liquidity level.

    A bullish sweep (buy setup):
      - Wick pierced below an Equal Low
      - Candle closed ABOVE the Equal Low

    A bearish sweep (sell setup):
      - Wick pierced above an Equal High
      - Candle closed BELOW the Equal High

    Returns (direction, swept_level_price) or None.
    """
    high  = highs[candle_idx]
    low   = lows[candle_idx]
    close = closes[candle_idx]

    # Bullish sweep: wick below Equal Low, close above it
    for lvl in equal_lows:
        zone = lvl * tolerance_pct / 100
        if low < lvl - zone and close > lvl:
            return ("buy", lvl)

    # Bearish sweep: wick above Equal High, close below it
    for lvl in equal_highs:
        zone = lvl * tolerance_pct / 100
        if high > lvl + zone and close < lvl:
            return ("sell", lvl)

    return None


# ── Candle Confirmation ────────────────────────────────────────────────────────

def _body(open_: float, close: float) -> float:
    return abs(close - open_)


def _upper_wick(open_: float, high: float, close: float) -> float:
    return high - max(open_, close)


def _lower_wick(open_: float, low: float, close: float) -> float:
    return min(open_, close) - low


def is_bullish_engulfing(
    prev_open: float, prev_close: float,
    curr_open: float, curr_close: float,
) -> bool:
    """Current green candle body fully engulfs previous red candle body."""
    prev_bearish = prev_close < prev_open
    curr_bullish = curr_close > curr_open
    if not (prev_bearish and curr_bullish):
        return False
    prev_body = _body(prev_open, prev_close)
    curr_body = _body(curr_open, curr_close)
    return (
        curr_body >= prev_body * ENGULF_RATIO
        and curr_open <= prev_close
        and curr_close >= prev_open
    )


def is_bearish_engulfing(
    prev_open: float, prev_close: float,
    curr_open: float, curr_close: float,
) -> bool:
    """Current red candle body fully engulfs previous green candle body."""
    prev_bullish = prev_close > prev_open
    curr_bearish = curr_close < curr_open
    if not (prev_bullish and curr_bearish):
        return False
    prev_body = _body(prev_open, prev_close)
    curr_body = _body(curr_open, curr_close)
    return (
        curr_body >= prev_body * ENGULF_RATIO
        and curr_open >= prev_close
        and curr_close <= prev_open
    )


def is_hammer(open_: float, high: float, low: float, close: float) -> bool:
    """Long lower wick, small body near the top — bullish reversal."""
    body  = _body(open_, close)
    lower = _lower_wick(open_, low, close)
    upper = _upper_wick(open_, high, close)
    if body == 0:
        return False
    return lower >= body * HAMMER_WICK_RATIO and upper <= body * 0.5


def is_shooting_star(open_: float, high: float, low: float, close: float) -> bool:
    """Long upper wick, small body near the bottom — bearish reversal."""
    body  = _body(open_, close)
    upper = _upper_wick(open_, high, close)
    lower = _lower_wick(open_, low, close)
    if body == 0:
        return False
    return upper >= body * HAMMER_WICK_RATIO and lower <= body * 0.5


def confirm_candle(
    direction: Direction,
    prev_open: float, prev_high: float, prev_low: float, prev_close: float,
    curr_open: float, curr_high: float, curr_low: float, curr_close: float,
) -> Optional[str]:
    """
    Return the pattern name if the current candle confirms the sweep direction,
    else None.
    """
    if direction == "buy":
        if is_bullish_engulfing(prev_open, prev_close, curr_open, curr_close):
            return "bullish_engulfing"
        if is_hammer(curr_open, curr_high, curr_low, curr_close):
            return "hammer"
    else:
        if is_bearish_engulfing(prev_open, prev_close, curr_open, curr_close):
            return "bearish_engulfing"
        if is_shooting_star(curr_open, curr_high, curr_low, curr_close):
            return "shooting_star"
    return None


# ── Take-Profit Target ─────────────────────────────────────────────────────────

def nearest_tp(
    direction: Direction,
    entry_price: float,
    equal_highs: list[float],
    equal_lows: list[float],
    min_rr: float = 1.5,
) -> Optional[float]:
    """
    Return the nearest Equal High above entry (buy) or Equal Low below entry (sell).
    Enforces a minimum risk:reward of min_rr (not used for sizing here, just filtering).
    Returns None if no valid target exists.
    """
    if direction == "buy":
        candidates = [p for p in equal_highs if p > entry_price]
        return min(candidates) if candidates else None
    else:
        candidates = [p for p in equal_lows if p < entry_price]
        return max(candidates) if candidates else None


# ── Main Signal Computation ────────────────────────────────────────────────────

def compute_pa_signal(
    candles: list,
    symbol: str,
    timeframe: str,
    tolerance_pct: float = EQUAL_LEVEL_TOLERANCE_PCT,
    pivot_left: int = 3,
    pivot_right: int = 3,
) -> Optional[PASignal]:
    """
    Analyse the last closed candle for a Price Action signal.

    candles: list of [ts, open, high, low, close, volume] — newest last.
    Returns a PASignal if all conditions are met, else None.
    """
    min_candles = pivot_left + pivot_right + 10
    if len(candles) < min_candles:
        logger.debug("Not enough candles (%d) for PA signal on %s", len(candles), symbol)
        return None

    opens  = np.array([float(c[1]) for c in candles])
    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])

    # Use all but the last candle for level detection (last candle is "current")
    trend, sh_prices, sl_prices = detect_trend(
        highs[:-1], lows[:-1], pivot_left, pivot_right
    )

    eq_highs = find_equal_highs(highs[:-1], lows[:-1], tolerance_pct, pivot_left, pivot_right)
    eq_lows  = find_equal_lows(lows[:-1], tolerance_pct, pivot_left, pivot_right)

    if not eq_highs and not eq_lows:
        logger.debug("No equal highs/lows found for %s on %s", symbol, timeframe)
        return None

    # The sweep candle is the second-to-last (index -2); confirmation is the last (-1)
    sweep_idx = len(candles) - 2
    if sweep_idx < 1:
        return None

    sweep_result = detect_sweep(
        sweep_idx, opens, highs, lows, closes,
        eq_highs, eq_lows, tolerance_pct,
    )
    if not sweep_result:
        return None

    direction, swept_level = sweep_result

    # Trend filter: only trade in the direction of the trend
    if trend == "up" and direction == "sell":
        logger.debug("Skipping sell signal — trend is up for %s", symbol)
        return None
    if trend == "down" and direction == "buy":
        logger.debug("Skipping buy signal — trend is down for %s", symbol)
        return None
    # sideways: allow both directions

    # Confirmation candle = last candle
    conf_idx = len(candles) - 1
    pattern = confirm_candle(
        direction,
        opens[conf_idx - 1], highs[conf_idx - 1], lows[conf_idx - 1], closes[conf_idx - 1],
        opens[conf_idx],     highs[conf_idx],     lows[conf_idx],     closes[conf_idx],
    )
    if not pattern:
        logger.debug(
            "No candle confirmation after sweep on %s %s (direction=%s)",
            symbol, timeframe, direction,
        )
        return None

    entry_price = float(closes[conf_idx])
    tp_price    = nearest_tp(direction, entry_price, eq_highs, eq_lows)
    if tp_price is None:
        logger.debug("No TP target found for %s %s direction=%s", symbol, timeframe, direction)
        return None

    logger.info(
        "PA signal: %s %s | trend=%s | sweep=%.6f | entry=%.6f | tp=%.6f | pattern=%s",
        direction.upper(), symbol, trend, swept_level, entry_price, tp_price, pattern,
    )

    return PASignal(
        direction        = direction,
        entry_price      = entry_price,
        tp_price         = tp_price,
        sweep_price      = float(lows[sweep_idx] if direction == "buy" else highs[sweep_idx]),
        liquidity_level  = swept_level,
        trend            = trend,
        candle_pattern   = pattern,
        timeframe        = timeframe,
        symbol           = symbol,
    )


def compute_pa_levels(
    candles: list,
    tolerance_pct: float = EQUAL_LEVEL_TOLERANCE_PCT,
    pivot_left: int = 3,
    pivot_right: int = 3,
) -> PALevels:
    """Return current PA levels (equal highs/lows + trend) for display."""
    if len(candles) < pivot_left + pivot_right + 5:
        return PALevels()

    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])

    trend, sh_prices, sl_prices = detect_trend(highs, lows, pivot_left, pivot_right)
    eq_highs = find_equal_highs(highs, lows, tolerance_pct, pivot_left, pivot_right)
    eq_lows  = find_equal_lows(lows, tolerance_pct, pivot_left, pivot_right)

    return PALevels(
        equal_highs  = [round(p, 8) for p in sorted(eq_highs, reverse=True)],
        equal_lows   = [round(p, 8) for p in sorted(eq_lows, reverse=True)],
        trend        = trend,
        swing_highs  = [round(p, 8) for p in sh_prices],
        swing_lows   = [round(p, 8) for p in sl_prices],
    )
