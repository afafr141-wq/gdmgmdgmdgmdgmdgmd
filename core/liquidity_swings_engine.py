"""
Liquidity Swings Engine — pure signal computation (no I/O, no asyncio, no ccxt).

Ported from LuxAlgo "Liquidity Swings" Pine Script indicator.

Logic:
  1. Pivot Detection
     Swing High: bar whose high is the strict maximum over [i-left .. i+right].
     Swing Low:  bar whose low  is the strict minimum over the same window.

  2. Zone Construction
     Each pivot defines a rectangular zone:

       Swing High zone:
         top    = pivot high (the extreme)
         bottom = max(open, close) at pivot bar  [Wick Extremity]
                  OR low at pivot bar             [Full Range]

       Swing Low zone:
         top    = min(open, close) at pivot bar  [Wick Extremity]
                  OR high at pivot bar            [Full Range]
         bottom = pivot low (the extreme)

  3. Touch Counting
     After a pivot forms, every subsequent bar whose range overlaps the zone
     (bar_low < zone_top AND bar_high > zone_bottom) increments touch_count.

  4. Zone Validity
     A zone stays active until price closes beyond its pivot extreme:
       Swing High: invalidated when close > pivot_high  (zone "crossed")
       Swing Low:  invalidated when close < pivot_low

  5. Signal Generation (spot buy only — MEXC Spot has no short orders)
     A BUY signal fires when:
       - The current bar's low touches an active Swing Low zone
       - touch_count >= min_touches
       - An active Swing High zone exists above current price (used as TP)
     SL = pivot_low * (1 - sl_buffer_pct / 100)
     TP = nearest active Swing High pivot_price above entry
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np

from config.settings import (
    LS_PIVOT_LEFT,
    LS_PIVOT_RIGHT,
    LS_MIN_TOUCHES,
    LS_SL_BUFFER_PCT,
    LS_SWING_AREA,
    LS_LOOKBACK_CANDLES,
)

logger = logging.getLogger(__name__)

SwingArea = Literal["Wick Extremity", "Full Range"]


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SwingZone:
    kind:        Literal["high", "low"]
    pivot_price: float   # high of swing high / low of swing low
    zone_top:    float
    zone_bottom: float
    pivot_bar:   int
    touch_count: int  = 0
    crossed:     bool = False


@dataclass
class LSSignal:
    direction:   Literal["buy"]   # spot only — buy signals only
    entry_price: float
    tp_price:    float
    sl_price:    float
    zone:        SwingZone        # the Swing Low zone that triggered entry
    tp_zone:     SwingZone        # the Swing High zone used as TP
    touch_count: int
    symbol:      str
    timeframe:   str


@dataclass
class LSLevels:
    swing_highs:   list[SwingZone] = field(default_factory=list)
    swing_lows:    list[SwingZone] = field(default_factory=list)
    current_price: float = 0.0


# ── Pivot detection ────────────────────────────────────────────────────────────

def find_pivot_highs(
    highs: np.ndarray,
    lows:  np.ndarray,
    left:  int,
    right: int,
) -> list[int]:
    """Return bar indices of strict pivot highs."""
    n, result = len(highs), []
    for i in range(left, n - right):
        window = highs[i - left: i + right + 1]
        if highs[i] == window.max() and int(np.sum(window == highs[i])) == 1:
            result.append(i)
    return result


def find_pivot_lows(
    lows:  np.ndarray,
    left:  int,
    right: int,
) -> list[int]:
    """Return bar indices of strict pivot lows."""
    n, result = len(lows), []
    for i in range(left, n - right):
        window = lows[i - left: i + right + 1]
        if lows[i] == window.min() and int(np.sum(window == lows[i])) == 1:
            result.append(i)
    return result


# ── Zone construction ──────────────────────────────────────────────────────────

def _build_high_zone(
    idx: int,
    highs: np.ndarray, lows: np.ndarray,
    opens: np.ndarray, closes: np.ndarray,
    swing_area: SwingArea,
) -> SwingZone:
    pivot = float(highs[idx])
    if swing_area == "Wick Extremity":
        bottom = max(float(opens[idx]), float(closes[idx]))
    else:
        bottom = float(lows[idx])
    return SwingZone(kind="high", pivot_price=pivot,
                     zone_top=pivot, zone_bottom=bottom, pivot_bar=idx)


def _build_low_zone(
    idx: int,
    highs: np.ndarray, lows: np.ndarray,
    opens: np.ndarray, closes: np.ndarray,
    swing_area: SwingArea,
) -> SwingZone:
    pivot = float(lows[idx])
    if swing_area == "Wick Extremity":
        top = min(float(opens[idx]), float(closes[idx]))
    else:
        top = float(highs[idx])
    return SwingZone(kind="low", pivot_price=pivot,
                     zone_top=top, zone_bottom=pivot, pivot_bar=idx)


# ── Touch counting + crossed flag ─────────────────────────────────────────────

def _apply_touches(
    zone:   SwingZone,
    highs:  np.ndarray,
    lows:   np.ndarray,
    closes: np.ndarray,
    start:  int,
) -> SwingZone:
    """
    Walk bars from `start` forward, counting overlaps and detecting crosses.
    Returns a new SwingZone with touch_count and crossed populated.
    """
    count   = 0
    crossed = False
    for i in range(start, len(closes)):
        if lows[i] < zone.zone_top and highs[i] > zone.zone_bottom:
            count += 1
        if zone.kind == "high" and closes[i] > zone.pivot_price:
            crossed = True
            break
        if zone.kind == "low" and closes[i] < zone.pivot_price:
            crossed = True
            break
    return SwingZone(
        kind        = zone.kind,
        pivot_price = zone.pivot_price,
        zone_top    = zone.zone_top,
        zone_bottom = zone.zone_bottom,
        pivot_bar   = zone.pivot_bar,
        touch_count = count,
        crossed     = crossed,
    )


# ── Full zone build ────────────────────────────────────────────────────────────

def build_all_zones(
    highs: np.ndarray, lows: np.ndarray,
    opens: np.ndarray, closes: np.ndarray,
    left: int, right: int,
    swing_area: SwingArea,
) -> tuple[list[SwingZone], list[SwingZone]]:
    """
    Detect all pivots and build their zones with touch counts.
    Returns (high_zones, low_zones) sorted newest-first.
    """
    high_zones: list[SwingZone] = []
    for idx in find_pivot_highs(highs, lows, left, right):
        z = _build_high_zone(idx, highs, lows, opens, closes, swing_area)
        z = _apply_touches(z, highs, lows, closes, idx + 1)
        high_zones.append(z)

    low_zones: list[SwingZone] = []
    for idx in find_pivot_lows(lows, left, right):
        z = _build_low_zone(idx, highs, lows, opens, closes, swing_area)
        z = _apply_touches(z, highs, lows, closes, idx + 1)
        low_zones.append(z)

    high_zones.sort(key=lambda z: z.pivot_bar, reverse=True)
    low_zones.sort(key=lambda z: z.pivot_bar, reverse=True)
    return high_zones, low_zones


# ── Signal detection ───────────────────────────────────────────────────────────

def _nearest_high_above(
    high_zones: list[SwingZone], price: float
) -> Optional[SwingZone]:
    candidates = [z for z in high_zones
                  if not z.crossed and z.pivot_price > price]
    return min(candidates, key=lambda z: z.pivot_price) if candidates else None


def detect_ls_signal(
    high_zones:    list[SwingZone],
    low_zones:     list[SwingZone],
    current_price: float,
    current_high:  float,
    current_low:   float,
    min_touches:   int,
    sl_buffer_pct: float,
    symbol:        str,
    timeframe:     str,
) -> Optional[LSSignal]:
    """
    Check the current bar for a buy signal.

    Fires when the bar's low touches an active Swing Low zone that has
    accumulated at least min_touches, and a Swing High zone exists above
    to serve as TP.
    """
    for zone in low_zones:
        if zone.crossed:
            continue
        if zone.touch_count < min_touches:
            continue
        # Bar overlaps the zone
        if current_low <= zone.zone_top and current_high >= zone.zone_bottom:
            tp_zone = _nearest_high_above(high_zones, current_price)
            if tp_zone is None:
                continue

            sl_price = zone.pivot_price * (1 - sl_buffer_pct / 100)

            logger.info(
                "LS buy signal: %s %s | zone=%.6f–%.6f touches=%d "
                "entry=%.6f tp=%.6f sl=%.6f",
                symbol, timeframe,
                zone.zone_bottom, zone.zone_top, zone.touch_count,
                current_price, tp_zone.pivot_price, sl_price,
            )
            return LSSignal(
                direction   = "buy",
                entry_price = current_price,
                tp_price    = tp_zone.pivot_price,
                sl_price    = sl_price,
                zone        = zone,
                tp_zone     = tp_zone,
                touch_count = zone.touch_count,
                symbol      = symbol,
                timeframe   = timeframe,
            )
    return None


# ── Public entry points ────────────────────────────────────────────────────────

def compute_ls_signal(
    candles:       list,
    symbol:        str,
    timeframe:     str,
    *,
    pivot_left:    int       = LS_PIVOT_LEFT,
    pivot_right:   int       = LS_PIVOT_RIGHT,
    swing_area:    SwingArea = LS_SWING_AREA,   # type: ignore[assignment]
    min_touches:   int       = LS_MIN_TOUCHES,
    sl_buffer_pct: float     = LS_SL_BUFFER_PCT,
) -> Optional[LSSignal]:
    """
    Analyse the last closed candle for a Liquidity Swings buy signal.

    candles: list of [ts, open, high, low, close, volume] — newest last.
    Returns LSSignal if all conditions are met, else None.
    """
    min_needed = pivot_left + pivot_right + 5
    if len(candles) < min_needed:
        logger.debug("Not enough candles (%d) for LS signal on %s", len(candles), symbol)
        return None

    opens  = np.array([float(c[1]) for c in candles])
    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])

    # Build zones from all history except the last (current) bar
    high_zones, low_zones = build_all_zones(
        highs[:-1], lows[:-1], opens[:-1], closes[:-1],
        pivot_left, pivot_right, swing_area,
    )

    if not low_zones:
        return None

    return detect_ls_signal(
        high_zones    = high_zones,
        low_zones     = low_zones,
        current_price = float(closes[-1]),
        current_high  = float(highs[-1]),
        current_low   = float(lows[-1]),
        min_touches   = min_touches,
        sl_buffer_pct = sl_buffer_pct,
        symbol        = symbol,
        timeframe     = timeframe,
    )


def compute_ls_levels(
    candles:     list,
    *,
    pivot_left:  int       = LS_PIVOT_LEFT,
    pivot_right: int       = LS_PIVOT_RIGHT,
    swing_area:  SwingArea = LS_SWING_AREA,   # type: ignore[assignment]
    min_touches: int       = LS_MIN_TOUCHES,
) -> LSLevels:
    """Return current active zones for display / status."""
    if len(candles) < pivot_left + pivot_right + 5:
        return LSLevels()

    opens  = np.array([float(c[1]) for c in candles])
    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])

    high_zones, low_zones = build_all_zones(
        highs, lows, opens, closes,
        pivot_left, pivot_right, swing_area,
    )

    active_highs = [z for z in high_zones if not z.crossed and z.touch_count >= min_touches]
    active_lows  = [z for z in low_zones  if not z.crossed and z.touch_count >= min_touches]

    return LSLevels(
        swing_highs   = active_highs[:10],
        swing_lows    = active_lows[:10],
        current_price = float(closes[-1]),
    )
