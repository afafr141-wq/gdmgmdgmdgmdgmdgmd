"""
Market analysis: Order Block detection with order-book volume confirmation.

Order Block (OB) definition used here:
    Bullish OB — the last bearish (red) candle immediately before a strong
                 upward impulse move.  Price is expected to return to this
                 zone and bounce upward.
    Bearish OB — the last bullish (green) candle immediately before a strong
                 downward impulse move.  Price is expected to return to this
                 zone and reject downward.

Impulse threshold:
    A move is "strong" when the body of the impulse candle is ≥
    `impulse_multiplier` × the average body size of the preceding candles.

Order-book volume confirmation:
    An OB zone is only accepted when the cumulative order-book volume
    within the zone's price range exceeds `ob_volume_threshold` × the
    average per-level volume across the visible book.  This filters out
    OBs that have no real liquidity backing them.

Entry signal:
    Triggered when the current live price re-enters a confirmed bullish OB
    zone (price ≤ OB high) and the SMA-20 trend filter is satisfied.

Dynamic TP / SL:
    - Stop-Loss  → placed just below the low of the entry OB (one tick below).
    - Take-Profit → placed just below the low of the nearest confirmed
                    bearish OB above the current price (the next supply zone).
      If no bearish OB is found above price, TP falls back to a configurable
      fixed multiplier (default 2 %).

Data flow:
    fetch_klines()  → detect_order_blocks()  → find_entry_signal()
    update_depth()  → _confirm_ob_volume()
    update_price()  → SMA-20 + SL watcher feed
"""

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass
class OrderBlock:
    kind: Literal["bullish", "bearish"]
    high: float          # top of the OB zone
    low: float           # bottom of the OB zone
    origin_time: int     # open_time of the OB candle
    volume_confirmed: bool = False   # set by _confirm_ob_volume()

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2


@dataclass
class WallLevel:
    price: float
    volume: float
    side: str  # "bid" | "ask"


@dataclass
class EntrySignal:
    """Returned by find_entry_signal() when all conditions are met."""
    entry_ob: OrderBlock        # the bullish OB we are entering from
    tp_ob: OrderBlock | None    # nearest bearish OB above price (TP target)
    entry_price: float          # suggested limit-buy price (OB high + 1 tick)
    sl_price: float             # one tick below OB low
    tp_price: float             # just below tp_ob.low, or fallback fixed %
    wall_price: float           # strongest bid wall inside the OB zone
    wall_volume: float
    sma: float
    current_price: float


# ── Main analyser ──────────────────────────────────────────────────────────────

class MarketAnalyzer:
    """
    Stateful analyser that consumes live order-book, price ticks, and
    periodically refreshed OHLCV candles.

    Thread-safety: designed for single-threaded asyncio use only.
    """

    def __init__(
        self,
        wall_multiplier: float = 3.0,
        sma_period: int = 20,
        price_precision: int = 8,
        impulse_multiplier: float = 2.0,
        ob_volume_threshold: float = 1.5,
        tp_fallback_pct: float = 1.02,
    ) -> None:
        self.wall_multiplier = wall_multiplier
        self.sma_period = sma_period
        self.price_precision = price_precision
        self.impulse_multiplier = impulse_multiplier
        self.ob_volume_threshold = ob_volume_threshold
        self.tp_fallback_pct = tp_fallback_pct

        self._prices: deque[float] = deque(maxlen=sma_period)
        self._bids: list[list[str]] = []
        self._asks: list[list[str]] = []
        self._current_price: float = 0.0

        # Populated by detect_order_blocks()
        self._bullish_obs: list[OrderBlock] = []
        self._bearish_obs: list[OrderBlock] = []

    # ── Feed methods ───────────────────────────────────────────────────────────

    def update_price(self, price: float) -> None:
        self._current_price = price
        self._prices.append(price)

    def update_depth(self, bids: list[list[str]], asks: list[list[str]]) -> None:
        self._bids = bids
        self._asks = asks
        # Re-confirm all OBs whenever the book updates
        for ob in self._bullish_obs + self._bearish_obs:
            ob.volume_confirmed = self._confirm_ob_volume(ob)

    # ── OB detection ───────────────────────────────────────────────────────────

    def detect_order_blocks(self, candles: list[dict]) -> None:
        """
        Scan a list of OHLCV dicts (oldest→newest) and populate
        self._bullish_obs / self._bearish_obs.

        A bullish OB is the last bearish candle before a strong bullish impulse.
        A bearish OB is the last bullish candle before a strong bearish impulse.
        """
        if len(candles) < 3:
            logger.debug("Not enough candles to detect OBs (%d)", len(candles))
            return

        parsed = [
            Candle(
                open_time=c["open_time"],
                open=c["open"], high=c["high"],
                low=c["low"],  close=c["close"],
                volume=c["volume"],
            )
            for c in candles
        ]

        # Rolling average body size (exclude the last candle — it may be live)
        bodies = [c.body for c in parsed[:-1]]
        if not bodies:
            return
        avg_body = statistics.mean(bodies) or 1e-10
        threshold = avg_body * self.impulse_multiplier

        bullish: list[OrderBlock] = []
        bearish: list[OrderBlock] = []

        for i in range(1, len(parsed) - 1):
            prev = parsed[i - 1]
            curr = parsed[i]

            # Bullish OB: prev candle is bearish, curr candle is a strong bullish impulse
            if prev.is_bearish and curr.is_bullish and curr.body >= threshold:
                ob = OrderBlock(
                    kind="bullish",
                    high=prev.high,
                    low=prev.low,
                    origin_time=prev.open_time,
                )
                ob.volume_confirmed = self._confirm_ob_volume(ob)
                bullish.append(ob)
                logger.debug(
                    "Bullish OB detected: high=%.8f low=%.8f vol_confirmed=%s",
                    ob.high, ob.low, ob.volume_confirmed,
                )

            # Bearish OB: prev candle is bullish, curr candle is a strong bearish impulse
            if prev.is_bullish and curr.is_bearish and curr.body >= threshold:
                ob = OrderBlock(
                    kind="bearish",
                    high=prev.high,
                    low=prev.low,
                    origin_time=prev.open_time,
                )
                ob.volume_confirmed = self._confirm_ob_volume(ob)
                bearish.append(ob)
                logger.debug(
                    "Bearish OB detected: high=%.8f low=%.8f vol_confirmed=%s",
                    ob.high, ob.low, ob.volume_confirmed,
                )

        self._bullish_obs = bullish
        self._bearish_obs = bearish
        logger.info(
            "OB scan complete: %d bullish, %d bearish (from %d candles)",
            len(bullish), len(bearish), len(parsed),
        )

    # ── Volume confirmation ────────────────────────────────────────────────────

    def _confirm_ob_volume(self, ob: OrderBlock) -> bool:
        """
        Return True when cumulative order-book volume inside the OB price
        range exceeds ob_volume_threshold × average per-level volume.

        Uses bids for bullish OBs (support), asks for bearish OBs (resistance).
        Falls back to True when no book data is available yet (avoids blocking
        signal detection on startup before the first depth snapshot arrives).
        """
        levels = self._bids if ob.kind == "bullish" else self._asks
        if not levels:
            return True  # no book data yet — optimistic default

        parsed: list[tuple[float, float]] = []
        for row in levels:
            try:
                parsed.append((float(row[0]), float(row[1])))
            except (ValueError, IndexError):
                continue

        if not parsed:
            return True

        all_vols = [v for _, v in parsed]
        avg_vol = statistics.mean(all_vols) or 1e-10
        zone_vol = sum(v for p, v in parsed if ob.low <= p <= ob.high)

        confirmed = zone_vol >= avg_vol * self.ob_volume_threshold
        logger.debug(
            "OB volume check [%s %.8f–%.8f]: zone_vol=%.4f avg=%.4f → %s",
            ob.kind, ob.low, ob.high, zone_vol, avg_vol,
            "✓" if confirmed else "✗",
        )
        return confirmed

    # ── Wall detection (kept for compatibility / dashboard display) ────────────

    def find_walls(self, side: str = "bid") -> list[WallLevel]:
        levels = self._bids if side == "bid" else self._asks
        if not levels:
            return []

        parsed: list[tuple[float, float]] = []
        for row in levels:
            try:
                parsed.append((float(row[0]), float(row[1])))
            except (ValueError, IndexError):
                continue

        if not parsed:
            return []

        volumes = [v for _, v in parsed]
        avg_vol = statistics.mean(volumes)
        threshold = avg_vol * self.wall_multiplier

        walls = [
            WallLevel(price=p, volume=v, side=side)
            for p, v in parsed
            if v >= threshold
        ]
        walls.sort(key=lambda w: w.volume, reverse=True)
        return walls

    # ── SMA ────────────────────────────────────────────────────────────────────

    def sma(self) -> float | None:
        if len(self._prices) < self.sma_period:
            return None
        return statistics.mean(self._prices)

    # ── Entry signal ───────────────────────────────────────────────────────────

    def find_entry_signal(self, tick_size: float = 0.01) -> EntrySignal | None:
        """
        Return an EntrySignal when all conditions are met, else None.

        Conditions:
        1. SMA-20 is available.
        2. Current price is above SMA-20 (uptrend filter).
        3. At least one volume-confirmed bullish OB exists below current price
           (price has pulled back into the OB zone, i.e. price ≤ OB high).
        4. A bid wall exists within the OB zone (liquidity confirmation).

        TP  → just below the low of the nearest volume-confirmed bearish OB
              above current price.  Falls back to tp_fallback_pct if none found.
        SL  → one tick below the entry OB's low.
        """
        current = self._current_price
        if current <= 0:
            logger.debug("No current price yet")
            return None

        sma = self.sma()
        if sma is None:
            logger.debug("SMA not ready (%d/%d)", len(self._prices), self.sma_period)
            return None

        if current <= sma:
            logger.debug("Price %.8f below SMA %.8f — no entry", current, sma)
            return None

        if not self._bullish_obs:
            logger.debug("No bullish OBs detected yet — run detect_order_blocks()")
            return None

        # Find confirmed bullish OBs where price has pulled back into the zone
        valid_obs = [
            ob for ob in self._bullish_obs
            if ob.volume_confirmed
            and ob.low < current <= ob.high   # price inside or just above OB
        ]
        if not valid_obs:
            logger.debug("No confirmed bullish OB contains current price %.8f", current)
            return None

        # Pick the OB with the highest low (closest support below price)
        entry_ob = max(valid_obs, key=lambda ob: ob.low)

        # Require a bid wall inside the OB zone for extra liquidity confirmation
        bid_walls = self.find_walls("bid")
        ob_walls = [w for w in bid_walls if entry_ob.low <= w.price <= entry_ob.high]
        if not ob_walls:
            logger.debug(
                "No bid wall inside OB zone [%.8f–%.8f]", entry_ob.low, entry_ob.high
            )
            return None

        best_wall = max(ob_walls, key=lambda w: w.volume)

        # Entry price: OB high + 1 tick (sit just above the OB)
        entry_price = round(entry_ob.high + tick_size, self.price_precision)

        # SL: one tick below OB low
        sl_price = round(entry_ob.low - tick_size, self.price_precision)

        # TP: nearest volume-confirmed bearish OB above current price
        tp_ob: OrderBlock | None = None
        bearish_above = [
            ob for ob in self._bearish_obs
            if ob.volume_confirmed and ob.low > current
        ]
        if bearish_above:
            tp_ob = min(bearish_above, key=lambda ob: ob.low)
            # Place TP just below the bearish OB's low (enter the supply zone)
            tp_price = round(tp_ob.low - tick_size, self.price_precision)
        else:
            # Fallback: fixed percentage above entry
            tp_price = round(entry_price * self.tp_fallback_pct, self.price_precision)
            logger.debug("No bearish OB above price — using fallback TP %.8f", tp_price)

        # Sanity: TP must be above entry
        if tp_price <= entry_price:
            logger.debug("TP %.8f ≤ entry %.8f — skipping signal", tp_price, entry_price)
            return None

        logger.info(
            "Entry signal: OB[%.8f–%.8f] entry=%.8f sl=%.8f tp=%.8f wall=%.8f(vol %.4f)",
            entry_ob.low, entry_ob.high, entry_price, sl_price, tp_price,
            best_wall.price, best_wall.volume,
        )
        return EntrySignal(
            entry_ob=entry_ob,
            tp_ob=tp_ob,
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
            wall_price=best_wall.price,
            wall_volume=best_wall.volume,
            sma=sma,
            current_price=current,
        )

    # ── Reset ──────────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all accumulated data (called between trades)."""
        self._prices.clear()
        self._bids = []
        self._asks = []
        self._current_price = 0.0
        self._bullish_obs = []
        self._bearish_obs = []
        logger.debug("Analyzer state reset")
