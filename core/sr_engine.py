"""
Support & Resistance level detection — LuxAlgo pivot method.

Algorithm:
  1. Fetch last SR_LOOKBACK_CANDLES candles on the requested timeframe.
  2. Detect pivot highs and pivot lows using a left/right confirmation
     window (SR_PIVOT_LEFT bars left, SR_PIVOT_RIGHT bars right).
  3. Rank levels: unbroken levels (price never closed through them) first.
  4. Merge levels within SR_MERGE_THRESHOLD % of each other.
  5. Split into supports (below price) and resistances (above price),
     return the 2 closest of each.

Returns SRLevels(supports=[s1, s2], resistances=[r1, r2]) where
  s1 < s2 < current_price < r1 < r2
  (s1 = closest support, r1 = closest resistance)
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
)
from core.mexc_client import MexcClient

logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]


@dataclass
class SRLevels:
    supports:      list[float]   # closest first, all below current price
    resistances:   list[float]   # closest first, all above current price
    current_price: float
    timeframe:     str
    symbol:        str


# ── Pivot detection ────────────────────────────────────────────────────────────

def _pivot_highs(highs: np.ndarray, left: int, right: int) -> list[float]:
    """
    Return price values of confirmed pivot highs.
    A pivot high at index i requires highs[i] to be the strict maximum
    over [i-left .. i+right].
    """
    n = len(highs)
    pivots = []
    for i in range(left, n - right):
        window = highs[i - left: i + right + 1]
        if highs[i] == window.max() and np.sum(window == highs[i]) == 1:
            pivots.append(float(highs[i]))
    return pivots


def _pivot_lows(lows: np.ndarray, left: int, right: int) -> list[float]:
    """
    Return price values of confirmed pivot lows.
    A pivot low at index i requires lows[i] to be the strict minimum
    over [i-left .. i+right].
    """
    n = len(lows)
    pivots = []
    for i in range(left, n - right):
        window = lows[i - left: i + right + 1]
        if lows[i] == window.min() and np.sum(window == lows[i]) == 1:
            pivots.append(float(lows[i]))
    return pivots


# ── Level merging ──────────────────────────────────────────────────────────────

def _merge_levels(levels: list[float], threshold_pct: float) -> list[float]:
    """
    Merge levels within threshold_pct % of each other into their average.
    """
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


# ── Break filter ───────────────────────────────────────────────────────────────

def _filter_broken(
    levels: list[float],
    closes: np.ndarray,
    side: str,          # "support" or "resistance"
) -> list[float]:
    """
    Rank levels by strength: unbroken levels first, broken levels after.
    A support is broken when any close is below it.
    A resistance is broken when any close is above it.
    """
    surviving = []
    broken    = []
    for lvl in levels:
        if side == "support":
            (broken if np.any(closes < lvl) else surviving).append(lvl)
        else:
            (broken if np.any(closes > lvl) else surviving).append(lvl)
    return surviving + broken


# ── Main computation ───────────────────────────────────────────────────────────

def compute_sr_levels(
    candles: list,
    current_price: float,
    symbol: str,
    timeframe: str,
    pivot_left:      int   = SR_PIVOT_LEFT,
    pivot_right:     int   = SR_PIVOT_RIGHT,
    merge_threshold: float = SR_MERGE_THRESHOLD,
    num_levels:      int   = 2,
) -> Optional[SRLevels]:
    """
    Compute num_levels support and num_levels resistance levels using
    LuxAlgo pivot detection.  Returns None if not enough levels found.
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

    # 3. Rank by break status (unbroken = stronger)
    raw_resistances = _filter_broken(raw_resistances, closes, "resistance")
    raw_supports    = _filter_broken(raw_supports,    closes, "support")

    # 4. Split relative to current price, closest first
    supports    = sorted([p for p in raw_supports    if p < current_price], reverse=True)
    resistances = sorted([p for p in raw_resistances if p > current_price])

    # 5. Fallback if pivot detection yields too few levels
    if len(supports) < num_levels or len(resistances) < num_levels:
        fb_sup, fb_res = _fallback_levels(highs, lows, current_price)
        if len(supports) < num_levels:
            supports = fb_sup
        if len(resistances) < num_levels:
            resistances = fb_res

    if len(supports) < num_levels or len(resistances) < num_levels:
        logger.warning(
            "Could not find %d supports and %d resistances for %s on %s "
            "(supports=%d, resistances=%d)",
            num_levels, num_levels, symbol, timeframe,
            len(supports), len(resistances),
        )
        return None

    return SRLevels(
        supports      = [round(p, 8) for p in supports[:num_levels]],
        resistances   = [round(p, 8) for p in resistances[:num_levels]],
        current_price = current_price,
        timeframe     = timeframe,
        symbol        = symbol,
    )


def _fallback_levels(
    highs: np.ndarray,
    lows:  np.ndarray,
    current_price: float,
) -> tuple[list[float], list[float]]:
    """
    Fallback: swing lows as supports, swing highs as resistances.
    A swing low/high requires the bar to be lower/higher than both neighbours.
    """
    swing_lows  = []
    swing_highs = []
    n = len(lows)
    for i in range(1, n - 1):
        if lows[i]  < lows[i - 1]  and lows[i]  < lows[i + 1]:
            swing_lows.append(lows[i])
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append(highs[i])

    supports    = sorted([p for p in swing_lows  if p < current_price], reverse=True)
    resistances = sorted([p for p in swing_highs if p > current_price])
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
