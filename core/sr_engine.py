"""
Support & Resistance level detection — LuxAlgo Liquidity Grabs method.

Matches the "Support & Resistance Channels" indicator (LuxAlgo PAC):

  Bull Wick  → Support level
    A candle whose lower wick is significantly longer than its body.
    The wick shows price swept liquidity below then reversed upward.
    Level = candle low.

  Bear Wick  → Resistance level
    A candle whose upper wick is significantly longer than its body.
    The wick shows price swept liquidity above then reversed downward.
    Level = candle high.

Detection parameters:
  SR_WICK_BODY_RATIO  — minimum ratio of wick-length / body-length to
                        qualify as a liquidity grab (default 2.0 = wick
                        must be at least 2× the body size).
  SR_MIN_BODY_PCT     — minimum body size as % of candle range to filter
                        out doji candles (default 0.1%).
  SR_MERGE_THRESHOLD  — merge levels within this % of each other.
  SR_MIN_DISTANCE_PCT — discard levels closer than this % to current price.
  SR_LOOKBACK_CANDLES — number of candles to analyse.

Scoring:
  Each level is scored by how many subsequent candles returned to test it
  (low/high within SR_TOUCH_ZONE_PCT of the level).
  Unbroken levels (price never closed through them) rank above broken ones.

Output:
  supports    — sorted descending  (S1 = nearest below price)
  resistances — sorted ascending   (R1 = nearest above price)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from config.settings import (
    SR_LOOKBACK_CANDLES,
    SR_MERGE_THRESHOLD,
    SR_MIN_DISTANCE_PCT,
    SR_TOUCH_ZONE_PCT,
)
from core.mexc_client import MexcClient

logger = logging.getLogger(__name__)

VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]

# Wick-to-body ratio threshold — wick must be this many times the body
SR_WICK_BODY_RATIO: float = 2.0
# Minimum body size as % of total candle range (filters doji candles)
SR_MIN_BODY_PCT: float = 0.05


@dataclass
class SRLevels:
    supports:      list[float]   # nearest first (descending), all below current price
    resistances:   list[float]   # nearest first (ascending),  all above current price
    current_price: float
    timeframe:     str
    symbol:        str


# ── Liquidity grab detection ───────────────────────────────────────────────────

def _detect_bull_wicks(
    opens: np.ndarray,
    highs: np.ndarray,
    lows:  np.ndarray,
    closes: np.ndarray,
    wick_body_ratio: float,
    min_body_pct: float,
) -> list[float]:
    """
    Detect Bull Wick candles and return their low prices as support levels.

    Bull Wick: lower_wick >= wick_body_ratio * body  AND  body >= min_body_pct * range
    lower_wick = min(open, close) - low
    body       = abs(close - open)
    range      = high - low
    """
    levels = []
    for i in range(len(opens)):
        body       = abs(closes[i] - opens[i])
        candle_rng = highs[i] - lows[i]
        if candle_rng == 0:
            continue
        lower_wick = min(opens[i], closes[i]) - lows[i]
        # Body must be meaningful (not a doji)
        if body < min_body_pct / 100 * candle_rng:
            continue
        if lower_wick >= wick_body_ratio * body:
            levels.append(float(lows[i]))
    return levels


def _detect_bear_wicks(
    opens: np.ndarray,
    highs: np.ndarray,
    lows:  np.ndarray,
    closes: np.ndarray,
    wick_body_ratio: float,
    min_body_pct: float,
) -> list[float]:
    """
    Detect Bear Wick candles and return their high prices as resistance levels.

    Bear Wick: upper_wick >= wick_body_ratio * body  AND  body >= min_body_pct * range
    upper_wick = high - max(open, close)
    body       = abs(close - open)
    range      = high - low
    """
    levels = []
    for i in range(len(opens)):
        body       = abs(closes[i] - opens[i])
        candle_rng = highs[i] - lows[i]
        if candle_rng == 0:
            continue
        upper_wick = highs[i] - max(opens[i], closes[i])
        if body < min_body_pct / 100 * candle_rng:
            continue
        if upper_wick >= wick_body_ratio * body:
            levels.append(float(highs[i]))
    return levels


# ── Touch scoring ──────────────────────────────────────────────────────────────

def _count_touches(
    level:    float,
    highs:    np.ndarray,
    lows:     np.ndarray,
    zone_pct: float,
    side:     str,
) -> int:
    zone = level * zone_pct / 100
    if side == "support":
        return int(np.sum(np.abs(lows - level) <= zone))
    else:
        return int(np.sum(np.abs(highs - level) <= zone))


def _is_broken(level: float, closes: np.ndarray, side: str) -> bool:
    if side == "support":
        return bool(np.any(closes < level))
    else:
        return bool(np.any(closes > level))


# ── Level merging ──────────────────────────────────────────────────────────────

def _merge_levels(levels: list[float], threshold_pct: float) -> list[float]:
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


# ── Fallback: swing highs/lows ─────────────────────────────────────────────────

def _fallback_levels(
    highs:         np.ndarray,
    lows:          np.ndarray,
    current_price: float,
    min_dist:      float,
) -> tuple[list[float], list[float]]:
    """Simple swing high/low fallback when wick detection finds too few levels."""
    swing_lows, swing_highs = [], []
    n = len(lows)
    for i in range(1, n - 1):
        if lows[i]  < lows[i - 1]  and lows[i]  < lows[i + 1]:
            swing_lows.append(float(lows[i]))
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append(float(highs[i]))
    supports    = sorted([p for p in swing_lows  if p < current_price - min_dist], reverse=True)
    resistances = sorted([p for p in swing_highs if p > current_price + min_dist])
    return supports, resistances


# ── Main computation ───────────────────────────────────────────────────────────

def compute_sr_levels(
    candles: list,
    current_price: float,
    symbol: str,
    timeframe: str,
    merge_threshold:  float = SR_MERGE_THRESHOLD,
    min_distance_pct: float = SR_MIN_DISTANCE_PCT,
    touch_zone_pct:   float = SR_TOUCH_ZONE_PCT,
    wick_body_ratio:  float = SR_WICK_BODY_RATIO,
    min_body_pct:     float = SR_MIN_BODY_PCT,
    num_levels:       int   = 2,
) -> Optional[SRLevels]:
    """
    Detect support/resistance levels from Bull/Bear Wick candles.
    Returns None if not enough levels found.
    """
    if len(candles) < 10:
        logger.warning("Not enough candles (%d) for %s", len(candles), symbol)
        return None

    opens  = np.array([float(c[1]) for c in candles])
    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])

    # 1. Detect wick-based levels
    raw_supports    = _detect_bull_wicks(opens, highs, lows, closes, wick_body_ratio, min_body_pct)
    raw_resistances = _detect_bear_wicks(opens, highs, lows, closes, wick_body_ratio, min_body_pct)

    # 2. Merge nearby levels
    raw_supports    = _merge_levels(raw_supports,    merge_threshold)
    raw_resistances = _merge_levels(raw_resistances, merge_threshold)

    # 3. Distance filter
    min_dist        = current_price * min_distance_pct / 100
    raw_supports    = [p for p in raw_supports    if p < current_price - min_dist]
    raw_resistances = [p for p in raw_resistances if p > current_price + min_dist]

    # 4. Score and rank — unbroken levels first
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
            "Could not find %d S/%d R for %s on %s (sup=%d res=%d)",
            num_levels, num_levels, symbol, timeframe,
            len(supports), len(resistances),
        )
        return None

    # 6. Sort by proximity — S1 nearest below, R1 nearest above
    supports    = sorted(supports[:num_levels],    reverse=True)
    resistances = sorted(resistances[:num_levels], reverse=False)

    return SRLevels(
        supports      = [round(p, 8) for p in supports],
        resistances   = [round(p, 8) for p in resistances],
        current_price = current_price,
        timeframe     = timeframe,
        symbol        = symbol,
    )


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
