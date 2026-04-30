"""
Support & Resistance level detection.

Matches LuxAlgo "Support and Resistance Levels with Breaks":

  Bear Wick (red)  → resistance: a candle whose HIGH pierced a prior swing
                     high but CLOSED below it (wick rejection above).
  Bull Wick (blue) → support:    a candle whose LOW  pierced a prior swing
                     low  but CLOSED above it (wick rejection below).

Level classification:
  current  — the level has NOT been closed through since it formed.
  previous — the level HAS been closed through (a "break") and is now a
             potential retest zone.

The user chooses at strategy creation time which set to trade:
  "current"  → only unbroken levels
  "previous" → only broken levels (retest trades)
  "both"     → all levels, current ones ranked first
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

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

LevelMode = Literal["current", "previous", "both"]


@dataclass
class SRLevel:
    price:      float
    is_current: bool   # True = unbroken, False = broken (previous)
    touches:    int = 0


@dataclass
class SRLevels:
    supports:          list[float]
    resistances:       list[float]
    support_levels:    list[SRLevel] = field(default_factory=list)
    resistance_levels: list[SRLevel] = field(default_factory=list)
    current_price:     float    = 0.0
    timeframe:         str      = ""
    symbol:            str      = ""
    mode:              LevelMode = "both"


# ── Wick detection (LuxAlgo logic) ────────────────────────────────────────────

def _find_bull_wicks(
    opens: np.ndarray, highs: np.ndarray,
    lows: np.ndarray, closes: np.ndarray,
    left: int, right: int,
) -> list[float]:
    """Bull Wick: lowest low in window where close > low (wick rejection up)."""
    n, levels = len(lows), []
    for i in range(left, n - right):
        window = lows[i - left: i + right + 1]
        if lows[i] == window.min() and np.sum(window == lows[i]) == 1:
            if closes[i] > lows[i]:
                levels.append(float(lows[i]))
    return levels


def _find_bear_wicks(
    opens: np.ndarray, highs: np.ndarray,
    lows: np.ndarray, closes: np.ndarray,
    left: int, right: int,
) -> list[float]:
    """Bear Wick: highest high in window where close < high (wick rejection down)."""
    n, levels = len(highs), []
    for i in range(left, n - right):
        window = highs[i - left: i + right + 1]
        if highs[i] == window.max() and np.sum(window == highs[i]) == 1:
            if closes[i] < highs[i]:
                levels.append(float(highs[i]))
    return levels


# ── Helpers ────────────────────────────────────────────────────────────────────

def _count_touches(level: float, highs: np.ndarray, lows: np.ndarray,
                   zone_pct: float, side: str) -> int:
    zone = level * zone_pct / 100
    if side == "support":
        return int(np.sum(np.abs(lows - level) <= zone))
    return int(np.sum(np.abs(highs - level) <= zone))


def _is_broken(level: float, closes: np.ndarray, side: str) -> bool:
    if side == "support":
        return bool(np.any(closes < level))
    return bool(np.any(closes > level))


def _merge_levels(levels: list[float], threshold_pct: float) -> list[float]:
    if not levels:
        return []
    merged = [sorted(levels)[0]]
    for price in sorted(levels)[1:]:
        if abs(price - merged[-1]) / merged[-1] <= threshold_pct / 100:
            merged[-1] = (merged[-1] + price) / 2
        else:
            merged.append(price)
    return merged


def _rank_and_annotate(
    raw: list[float],
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
    touch_zone_pct: float, side: str,
) -> list[SRLevel]:
    """Return SRLevel list: unbroken (current) first, then broken, each by touch count."""
    unbroken, broken = [], []
    for lvl in raw:
        touches = _count_touches(lvl, highs, lows, touch_zone_pct, side)
        obj = SRLevel(price=lvl, is_current=not _is_broken(lvl, closes, side), touches=touches)
        (unbroken if obj.is_current else broken).append(obj)
    unbroken.sort(key=lambda x: x.touches, reverse=True)
    broken.sort(key=lambda x: x.touches, reverse=True)
    return unbroken + broken


def _filter_by_mode(levels: list[SRLevel], mode: LevelMode) -> list[SRLevel]:
    if mode == "current":
        return [l for l in levels if l.is_current]
    if mode == "previous":
        return [l for l in levels if not l.is_current]
    return levels  # "both" — current already ranked first


# ── Main computation ───────────────────────────────────────────────────────────

def compute_sr_levels(
    candles: list,
    current_price: float,
    symbol: str,
    timeframe: str,
    pivot_left:       int       = SR_PIVOT_LEFT,
    pivot_right:      int       = SR_PIVOT_RIGHT,
    merge_threshold:  float     = SR_MERGE_THRESHOLD,
    min_distance_pct: float     = SR_MIN_DISTANCE_PCT,
    touch_zone_pct:   float     = SR_TOUCH_ZONE_PCT,
    num_levels:       int       = 2,
    mode:             LevelMode = "both",
) -> Optional[SRLevels]:
    """
    Compute num_levels support/resistance levels using LuxAlgo Bull/Bear Wick
    logic.  Returns None if not enough levels found after all fallbacks.
    """
    min_candles = pivot_left + pivot_right + 5
    if len(candles) < min_candles:
        logger.warning("Not enough candles (%d) for %s (need %d)", len(candles), symbol, min_candles)
        return None

    opens  = np.array([float(c[1]) for c in candles])
    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])

    half_dist = current_price * min_distance_pct / 200  # half of min_distance

    def _compute(pl: int, pr: int) -> tuple[list[SRLevel], list[SRLevel]]:
        raw_sup = _merge_levels(
            [p for p in _find_bull_wicks(opens, highs, lows, closes, pl, pr)
             if p < current_price - half_dist],
            merge_threshold,
        )
        raw_res = _merge_levels(
            [p for p in _find_bear_wicks(opens, highs, lows, closes, pl, pr)
             if p > current_price + half_dist],
            merge_threshold,
        )
        return (
            _rank_and_annotate(raw_sup, highs, lows, closes, touch_zone_pct, "support"),
            _rank_and_annotate(raw_res, highs, lows, closes, touch_zone_pct, "resistance"),
        )

    sup_ann, res_ann = _compute(pivot_left, pivot_right)

    # Progressive pivot reduction: 5→3→2
    for reduced in (3, 2):
        if (len(_filter_by_mode(sup_ann, mode)) >= num_levels
                and len(_filter_by_mode(res_ann, mode)) >= num_levels):
            break
        s2, r2 = _compute(reduced, reduced)
        existing_sup = {l.price for l in sup_ann}
        existing_res = {l.price for l in res_ann}
        for l in s2:
            if not any(abs(l.price - p) / max(p, 1e-12) < merge_threshold / 100
                       for p in existing_sup):
                sup_ann.append(l)
                existing_sup.add(l.price)
        for l in r2:
            if not any(abs(l.price - p) / max(p, 1e-12) < merge_threshold / 100
                       for p in existing_res):
                res_ann.append(l)
                existing_res.add(l.price)

    # Last-resort: raw high/low extremes
    filtered_sup = _filter_by_mode(sup_ann, mode)
    filtered_res = _filter_by_mode(res_ann, mode)

    if len(filtered_sup) < num_levels or len(filtered_res) < num_levels:
        logger.info("Using percentile fallback for %s on %s", symbol, timeframe)
        existing_sup_prices = {l.price for l in sup_ann}
        existing_res_prices = {l.price for l in res_ann}

        for p in sorted(set(lows.tolist())):
            if p < current_price * 0.999 and p not in existing_sup_prices:
                sup_ann.append(SRLevel(
                    price=p,
                    is_current=not _is_broken(p, closes, "support"),
                    touches=_count_touches(p, highs, lows, touch_zone_pct, "support"),
                ))
                existing_sup_prices.add(p)

        for p in sorted(set(highs.tolist()), reverse=True):
            if p > current_price * 1.001 and p not in existing_res_prices:
                res_ann.append(SRLevel(
                    price=p,
                    is_current=not _is_broken(p, closes, "resistance"),
                    touches=_count_touches(p, highs, lows, touch_zone_pct, "resistance"),
                ))
                existing_res_prices.add(p)

        filtered_sup = _filter_by_mode(sup_ann, mode)
        filtered_res = _filter_by_mode(res_ann, mode)

    if len(filtered_sup) < num_levels or len(filtered_res) < num_levels:
        logger.warning(
            "Could not find %d S/%d R for %s on %s mode=%s (sup=%d res=%d)",
            num_levels, num_levels, symbol, timeframe, mode,
            len(filtered_sup), len(filtered_res),
        )
        return None

    chosen_sup = sorted(filtered_sup[:num_levels], key=lambda l: l.price, reverse=True)
    chosen_res = sorted(filtered_res[:num_levels], key=lambda l: l.price)

    return SRLevels(
        supports           = [round(l.price, 8) for l in chosen_sup],
        resistances        = [round(l.price, 8) for l in chosen_res],
        support_levels     = chosen_sup,
        resistance_levels  = chosen_res,
        current_price      = current_price,
        timeframe          = timeframe,
        symbol             = symbol,
        mode               = mode,
    )


# ── Async fetch wrapper ────────────────────────────────────────────────────────

async def fetch_sr_levels(
    client:     MexcClient,
    symbol:     str,
    timeframe:  str,
    num_levels: int       = 2,
    mode:       LevelMode = "both",
) -> Optional[SRLevels]:
    """Fetch candles and compute S/R levels. Returns None on failure."""
    try:
        candles       = await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=SR_LOOKBACK_CANDLES)
        current_price = await client.get_current_price(symbol)
    except Exception as exc:
        logger.error("fetch_sr_levels failed for %s/%s: %s", symbol, timeframe, exc)
        return None

    return compute_sr_levels(
        candles, current_price, symbol, timeframe,
        num_levels=num_levels, mode=mode,
    )
