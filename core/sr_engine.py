"""
Support & Resistance level detection — LuxAlgo pivot method.

Algorithm:
  1. Fetch last SR_LOOKBACK_CANDLES candles on the requested timeframe.
  2. Detect pivot highs (resistance candidates) and pivot lows (support
     candidates) using SR_PIVOT_LEFT/RIGHT confirmation bars.
  3. Score each level by the number of times price has touched it
     (touched = candle high/low came within SR_TOUCH_ZONE_PCT of the level).
     More touches = stronger level.
  4. Keep only unbroken levels (price never closed through them).
     If not enough unbroken levels exist, include broken ones as fallback.
  5. Merge levels within SR_MERGE_THRESHOLD % of each other.
  6. Apply SR_MIN_DISTANCE_PCT: discard levels too close to current price.
  7. Return the num_levels strongest supports and resistances.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from config.settings import (
    SR_LOOKBACK_CANDLES,
    SR_PIVOT_LEFT,
    SR_PIVOT_RIGHT,
    SR_MERGE_THRESHOLD,
    SR_MIN_DISTANCE_PCT,
    SR_TOUCH_ZONE_PCT,
)
from core.mexc_client import MexcClient

logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]


@dataclass
class SRLevels:
    supports:      list[float]   # strongest first, all below current price
    resistances:   list[float]   # strongest first, all above current price
    current_price: float
    timeframe:     str
    symbol:        str


# ── Pivot detection ────────────────────────────────────────────────────────────

def _pivot_highs(highs: np.ndarray, left: int, right: int) -> list[float]:
    n = len(highs)
    pivots = []
    for i in range(left, n - right):
        window = highs[i - left: i + right + 1]
        if highs[i] == window.max() and np.sum(window == highs[i]) == 1:
            pivots.append(float(highs[i]))
    return pivots


def _pivot_lows(lows: np.ndarray, left: int, right: int) -> list[float]:
    n = len(lows)
    pivots = []
    for i in range(left, n - right):
        window = lows[i - left: i + right + 1]
        if lows[i] == window.min() and np.sum(window == lows[i]) == 1:
            pivots.append(float(lows[i]))
    return pivots


# ── Touch scoring ──────────────────────────────────────────────────────────────

def _count_touches(
    level:    float,
    highs:    np.ndarray,
    lows:     np.ndarray,
    zone_pct: float,
    side:     str,
) -> int:
    """
    Count candles that touched the level within zone_pct %.
    Support: candle low within zone. Resistance: candle high within zone.
    """
    zone = level * zone_pct / 100
    if side == "support":
        return int(np.sum(np.abs(lows - level) <= zone))
    else:
        return int(np.sum(np.abs(highs - level) <= zone))


def _is_broken(level: float, closes: np.ndarray, side: str) -> bool:
    """Return True if price has closed through the level."""
    if side == "support":
        return bool(np.any(closes < level))
    else:
        return bool(np.any(closes > level))


# ── Level merging ──────────────────────────────────────────────────────────────

def _merge_levels(levels: list[float], threshold_pct: float) -> list[float]:
    """Merge levels within threshold_pct % of each other into their average."""
    if not levels:
        return []
    sorted_levels = sorted(levels)
    merged = [sorted_levels[0]]
    for price in sorted_levels[1:]:
        if abs(price - merged[-1]) / merged[-1] <= threshold_pct / 100:
            merged[-1] = (merged[-1] + price) / 2
        else:
            merged.append(price)
    return merged


# ── Main computation ───────────────────────────────────────────────────────────

def compute_sr_levels(
    candles: list,
    current_price: float,
    symbol: str,
    timeframe: str,
    pivot_left:       int   = SR_PIVOT_LEFT,
    pivot_right:      int   = SR_PIVOT_RIGHT,
    merge_threshold:  float = SR_MERGE_THRESHOLD,
    min_distance_pct: float = SR_MIN_DISTANCE_PCT,
    touch_zone_pct:   float = SR_TOUCH_ZONE_PCT,
    num_levels:       int   = 2,
) -> Optional[SRLevels]:
    """
    Compute num_levels support and resistance levels ranked by touch count.
    Unbroken levels with most touches come first.
    Returns None if not enough levels found.
    """
    min_candles = pivot_left + pivot_right + 5
    if len(candles) < min_candles:
        logger.warning(
            "Not enough candles (%d) to compute S/R for %s (need %d)",
            len(candles), symbol, min_candles,
        )
        return None

    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])

    # 1. Detect pivot highs/lows
    raw_resistances = _pivot_highs(highs, pivot_left, pivot_right)
    raw_supports    = _pivot_lows(lows,   pivot_left, pivot_right)

    # 2. Merge nearby levels
    raw_resistances = _merge_levels(raw_resistances, merge_threshold)
    raw_supports    = _merge_levels(raw_supports,    merge_threshold)

    # 3. Apply minimum distance filter
    min_dist = current_price * min_distance_pct / 100
    raw_supports    = [p for p in raw_supports    if p < current_price - min_dist]
    raw_resistances = [p for p in raw_resistances if p > current_price + min_dist]

    # 4. Rank by touch count — unbroken levels first, then broken
    def _rank(levels: list[float], side: str) -> list[float]:
        unbroken, broken = [], []
        for lvl in levels:
            touches = _count_touches(lvl, highs, lows, touch_zone_pct, side)
            bucket  = broken if _is_broken(lvl, closes, side) else unbroken
            bucket.append((touches, lvl))
        unbroken.sort(key=lambda x: x[0], reverse=True)
        broken.sort(key=lambda x: x[0], reverse=True)
        return [lvl for _, lvl in unbroken + broken]

    supports    = _rank(raw_supports,    "support")
    resistances = _rank(raw_resistances, "resistance")

    # 5. Fallback if not enough levels
    if len(supports) < num_levels or len(resistances) < num_levels:
        fb_sup, fb_res = _fallback_levels(highs, lows, current_price, min_dist)
        if len(supports) < num_levels:
            supports = fb_sup
        if len(resistances) < num_levels:
            resistances = fb_res

    if len(supports) < num_levels or len(resistances) < num_levels:
        logger.warning(
            "Could not find %d supports/%d resistances for %s on %s "
            "(found: sup=%d res=%d)",
            num_levels, num_levels, symbol, timeframe,
            len(supports), len(resistances),
        )
        return None

    # 6. Sort by price proximity to current price:
    #    supports    → descending (nearest first, i.e. highest price below current)
    #    resistances → ascending  (nearest first, i.e. lowest price above current)
    #    This ensures S1 is the nearest support and R1 is the nearest resistance,
    #    so each Si is paired with a distinct Ri in order.
    supports    = sorted(supports[:num_levels],    reverse=True)
    resistances = sorted(resistances[:num_levels], reverse=False)

    return SRLevels(
        supports      = [round(p, 8) for p in supports],
        resistances   = [round(p, 8) for p in resistances],
        current_price = current_price,
        timeframe     = timeframe,
        symbol        = symbol,
    )


def _fallback_levels(
    highs:         np.ndarray,
    lows:          np.ndarray,
    current_price: float,
    min_dist:      float,
) -> tuple[list[float], list[float]]:
    """
    Fallback: swing lows/highs sorted by distance from price
    (furthest first = more significant level).
    """
    swing_lows, swing_highs = [], []
    n = len(lows)
    for i in range(1, n - 1):
        if lows[i]  < lows[i - 1]  and lows[i]  < lows[i + 1]:
            swing_lows.append(lows[i])
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append(highs[i])

    supports    = sorted(
        [p for p in swing_lows  if p < current_price - min_dist],
        key=lambda p: abs(p - current_price), reverse=True,
    )
    resistances = sorted(
        [p for p in swing_highs if p > current_price + min_dist],
        key=lambda p: abs(p - current_price), reverse=True,
    )
    return supports, resistances


# ── Async fetch wrapper ────────────────────────────────────────────────────────

async def fetch_sr_levels(
    client: MexcClient,
    symbol: str,
    timeframe: str,
    num_levels: int = 2,
) -> Optional[SRLevels]:
    """Fetch candles and compute S/R levels. Returns None on failure."""
    try:
        candles       = await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=SR_LOOKBACK_CANDLES)
        current_price = await client.get_current_price(symbol)
    except Exception as exc:
        logger.error("fetch_sr_levels failed for %s/%s: %s", symbol, timeframe, exc)
        return None

    return compute_sr_levels(candles, current_price, symbol, timeframe, num_levels=num_levels)
