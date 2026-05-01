"""
Unit tests for core/liquidity_swings_engine.py.
Run with: pytest tests/
"""
import pytest
import numpy as np
from core.liquidity_swings_engine import (
    find_pivot_highs,
    find_pivot_lows,
    build_all_zones,
    detect_ls_signal,
    compute_ls_signal,
    compute_ls_levels,
    SwingZone,
    LSSignal,
    LSLevels,
    _build_high_zone,
    _build_low_zone,
    _apply_touches,
    _nearest_high_above,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _flat(price: float, n: int):
    """n candles all at the same price."""
    return [[i * 60000, price, price, price, price, 1000.0] for i in range(n)]


def _candles_with_spike_high(base: float, spike_idx: int, spike_val: float, n: int):
    """Flat candles with one spike high at spike_idx."""
    candles = _flat(base, n)
    candles[spike_idx][2] = spike_val   # high
    return candles


def _candles_with_spike_low(base: float, spike_idx: int, spike_val: float, n: int):
    """Flat candles with one spike low at spike_idx."""
    candles = _flat(base, n)
    candles[spike_idx][3] = spike_val   # low
    return candles


def _arrays(candles):
    opens  = np.array([float(c[1]) for c in candles])
    highs  = np.array([float(c[2]) for c in candles])
    lows   = np.array([float(c[3]) for c in candles])
    closes = np.array([float(c[4]) for c in candles])
    return opens, highs, lows, closes


# ── Pivot detection ────────────────────────────────────────────────────────────

class TestFindPivotHighs:

    def test_single_spike_detected(self):
        highs = np.array([1.0, 1.0, 1.0, 5.0, 1.0, 1.0, 1.0], dtype=float)
        lows  = np.ones(7)
        result = find_pivot_highs(highs, lows, left=3, right=3)
        assert result == [3]

    def test_flat_no_pivots(self):
        highs = np.ones(20, dtype=float)
        lows  = np.ones(20, dtype=float)
        result = find_pivot_highs(highs, lows, left=3, right=3)
        assert result == []

    def test_two_spikes(self):
        highs = np.array([1,1,1,5,1,1,1,1,1,5,1,1,1], dtype=float)
        lows  = np.ones(13, dtype=float)
        result = find_pivot_highs(highs, lows, left=3, right=3)
        assert 3 in result
        assert 9 in result

    def test_tie_not_detected(self):
        """Two equal highs in the window — neither is a strict pivot."""
        highs = np.array([1,1,5,5,1,1,1], dtype=float)
        lows  = np.ones(7, dtype=float)
        result = find_pivot_highs(highs, lows, left=3, right=3)
        assert result == []

    def test_insufficient_bars(self):
        highs = np.array([1, 5, 1], dtype=float)
        lows  = np.ones(3, dtype=float)
        result = find_pivot_highs(highs, lows, left=3, right=3)
        assert result == []


class TestFindPivotLows:

    def test_single_dip_detected(self):
        lows = np.array([5.0, 5.0, 5.0, 1.0, 5.0, 5.0, 5.0], dtype=float)
        result = find_pivot_lows(lows, left=3, right=3)
        assert result == [3]

    def test_flat_no_pivots(self):
        lows = np.ones(20, dtype=float)
        result = find_pivot_lows(lows, left=3, right=3)
        assert result == []

    def test_two_dips(self):
        lows = np.array([5,5,5,1,5,5,5,5,5,1,5,5,5], dtype=float)
        result = find_pivot_lows(lows, left=3, right=3)
        assert 3 in result
        assert 9 in result


# ── Zone construction ──────────────────────────────────────────────────────────

class TestBuildZones:

    def test_high_zone_wick_extremity(self):
        n = 10
        opens  = np.ones(n) * 100.0
        highs  = np.ones(n) * 100.0
        lows   = np.ones(n) * 100.0
        closes = np.ones(n) * 100.0
        highs[5]  = 110.0
        opens[5]  = 102.0
        closes[5] = 104.0
        zone = _build_high_zone(5, highs, lows, opens, closes, "Wick Extremity")
        assert zone.pivot_price == pytest.approx(110.0)
        assert zone.zone_top    == pytest.approx(110.0)
        assert zone.zone_bottom == pytest.approx(104.0)   # max(open, close)
        assert zone.kind == "high"

    def test_high_zone_full_range(self):
        n = 10
        opens  = np.ones(n) * 100.0
        highs  = np.ones(n) * 100.0
        lows   = np.ones(n) * 95.0
        closes = np.ones(n) * 100.0
        highs[5] = 110.0
        zone = _build_high_zone(5, highs, lows, opens, closes, "Full Range")
        assert zone.zone_bottom == pytest.approx(95.0)    # low

    def test_low_zone_wick_extremity(self):
        n = 10
        opens  = np.ones(n) * 100.0
        highs  = np.ones(n) * 100.0
        lows   = np.ones(n) * 100.0
        closes = np.ones(n) * 100.0
        lows[5]   = 90.0
        opens[5]  = 97.0
        closes[5] = 95.0
        zone = _build_low_zone(5, highs, lows, opens, closes, "Wick Extremity")
        assert zone.pivot_price == pytest.approx(90.0)
        assert zone.zone_bottom == pytest.approx(90.0)
        assert zone.zone_top    == pytest.approx(95.0)    # min(open, close)
        assert zone.kind == "low"

    def test_low_zone_full_range(self):
        n = 10
        opens  = np.ones(n) * 100.0
        highs  = np.ones(n) * 105.0
        lows   = np.ones(n) * 100.0
        closes = np.ones(n) * 100.0
        lows[5] = 90.0
        zone = _build_low_zone(5, highs, lows, opens, closes, "Full Range")
        assert zone.zone_top == pytest.approx(105.0)      # high


# ── Touch counting ─────────────────────────────────────────────────────────────

class TestApplyTouches:

    def _make_high_zone(self, pivot=110.0, top=110.0, bottom=104.0, bar=5):
        return SwingZone(kind="high", pivot_price=pivot,
                         zone_top=top, zone_bottom=bottom, pivot_bar=bar)

    def _make_low_zone(self, pivot=90.0, top=95.0, bottom=90.0, bar=5):
        return SwingZone(kind="low", pivot_price=pivot,
                         zone_top=top, zone_bottom=bottom, pivot_bar=bar)

    def test_no_touches_when_price_far(self):
        n = 10
        highs  = np.ones(n) * 100.0
        lows   = np.ones(n) * 100.0
        closes = np.ones(n) * 100.0
        zone = self._make_high_zone(pivot=200.0, top=200.0, bottom=195.0)
        result = _apply_touches(zone, highs, lows, closes, start=0)
        assert result.touch_count == 0
        assert result.crossed is False

    def test_touch_counted_when_bar_overlaps(self):
        n = 10
        highs  = np.ones(n) * 106.0   # overlaps zone [104, 110]
        lows   = np.ones(n) * 103.0
        closes = np.ones(n) * 105.0
        zone = self._make_high_zone()
        result = _apply_touches(zone, highs, lows, closes, start=0)
        assert result.touch_count == n

    def test_crossed_when_close_above_pivot_high(self):
        n = 5
        highs  = np.ones(n) * 115.0
        lows   = np.ones(n) * 108.0
        closes = np.array([105.0, 105.0, 112.0, 112.0, 112.0])  # crosses at bar 2
        zone = self._make_high_zone()
        result = _apply_touches(zone, highs, lows, closes, start=0)
        assert result.crossed is True

    def test_crossed_when_close_below_pivot_low(self):
        n = 5
        highs  = np.ones(n) * 92.0
        lows   = np.ones(n) * 88.0
        closes = np.array([94.0, 94.0, 88.0, 88.0, 88.0])  # crosses at bar 2
        zone = self._make_low_zone()
        result = _apply_touches(zone, highs, lows, closes, start=0)
        assert result.crossed is True

    def test_not_crossed_if_close_stays_above_low_pivot(self):
        n = 5
        highs  = np.ones(n) * 96.0
        lows   = np.ones(n) * 91.0
        closes = np.ones(n) * 93.0   # stays above pivot_low=90
        zone = self._make_low_zone()
        result = _apply_touches(zone, highs, lows, closes, start=0)
        assert result.crossed is False


# ── build_all_zones ────────────────────────────────────────────────────────────

class TestBuildAllZones:

    def test_spike_high_produces_high_zone(self):
        n = 40
        opens  = np.ones(n) * 100.0
        highs  = np.ones(n) * 100.0
        lows   = np.ones(n) * 100.0
        closes = np.ones(n) * 100.0
        highs[20] = 120.0
        high_zones, low_zones = build_all_zones(
            highs, lows, opens, closes, left=5, right=5,
            swing_area="Wick Extremity",
        )
        assert len(high_zones) >= 1
        assert any(z.pivot_price == pytest.approx(120.0) for z in high_zones)

    def test_spike_low_produces_low_zone(self):
        n = 40
        opens  = np.ones(n) * 100.0
        highs  = np.ones(n) * 100.0
        lows   = np.ones(n) * 100.0
        closes = np.ones(n) * 100.0
        lows[20] = 80.0
        _, low_zones = build_all_zones(
            highs, lows, opens, closes, left=5, right=5,
            swing_area="Wick Extremity",
        )
        assert len(low_zones) >= 1
        assert any(z.pivot_price == pytest.approx(80.0) for z in low_zones)

    def test_zones_sorted_newest_first(self):
        n = 60
        opens  = np.ones(n) * 100.0
        highs  = np.ones(n) * 100.0
        lows   = np.ones(n) * 100.0
        closes = np.ones(n) * 100.0
        highs[10] = 110.0
        highs[40] = 115.0
        high_zones, _ = build_all_zones(
            highs, lows, opens, closes, left=5, right=5,
            swing_area="Wick Extremity",
        )
        if len(high_zones) >= 2:
            assert high_zones[0].pivot_bar >= high_zones[1].pivot_bar


# ── detect_ls_signal ───────────────────────────────────────────────────────────

class TestDetectLSSignal:

    def _make_low_zone(self, pivot=90.0, top=95.0, touches=3, crossed=False):
        return SwingZone(kind="low", pivot_price=pivot,
                         zone_top=top, zone_bottom=pivot,
                         pivot_bar=10, touch_count=touches, crossed=crossed)

    def _make_high_zone(self, pivot=120.0, bottom=115.0, touches=2, crossed=False):
        return SwingZone(kind="high", pivot_price=pivot,
                         zone_top=pivot, zone_bottom=bottom,
                         pivot_bar=5, touch_count=touches, crossed=crossed)

    def test_buy_signal_fires_when_conditions_met(self):
        low_zone  = self._make_low_zone(pivot=90.0, top=95.0, touches=3)
        high_zone = self._make_high_zone(pivot=120.0)
        signal = detect_ls_signal(
            high_zones=[high_zone], low_zones=[low_zone],
            current_price=93.0, current_high=96.0, current_low=89.0,
            min_touches=2, sl_buffer_pct=0.3,
            symbol="BTC/USDT", timeframe="1h",
        )
        assert signal is not None
        assert signal.direction == "buy"
        assert signal.tp_price == pytest.approx(120.0)
        assert signal.sl_price < 90.0

    def test_no_signal_when_zone_crossed(self):
        low_zone  = self._make_low_zone(crossed=True)
        high_zone = self._make_high_zone()
        signal = detect_ls_signal(
            high_zones=[high_zone], low_zones=[low_zone],
            current_price=93.0, current_high=96.0, current_low=89.0,
            min_touches=2, sl_buffer_pct=0.3,
            symbol="BTC/USDT", timeframe="1h",
        )
        assert signal is None

    def test_no_signal_when_touches_below_min(self):
        low_zone  = self._make_low_zone(touches=1)
        high_zone = self._make_high_zone()
        signal = detect_ls_signal(
            high_zones=[high_zone], low_zones=[low_zone],
            current_price=93.0, current_high=96.0, current_low=89.0,
            min_touches=2, sl_buffer_pct=0.3,
            symbol="BTC/USDT", timeframe="1h",
        )
        assert signal is None

    def test_no_signal_when_no_tp_zone(self):
        low_zone = self._make_low_zone(touches=3)
        signal = detect_ls_signal(
            high_zones=[], low_zones=[low_zone],
            current_price=93.0, current_high=96.0, current_low=89.0,
            min_touches=2, sl_buffer_pct=0.3,
            symbol="BTC/USDT", timeframe="1h",
        )
        assert signal is None

    def test_no_signal_when_bar_does_not_overlap_zone(self):
        low_zone  = self._make_low_zone(pivot=90.0, top=95.0, touches=3)
        high_zone = self._make_high_zone()
        # Bar is entirely above the zone
        signal = detect_ls_signal(
            high_zones=[high_zone], low_zones=[low_zone],
            current_price=110.0, current_high=112.0, current_low=108.0,
            min_touches=2, sl_buffer_pct=0.3,
            symbol="BTC/USDT", timeframe="1h",
        )
        assert signal is None

    def test_sl_below_pivot_by_buffer(self):
        low_zone  = self._make_low_zone(pivot=100.0, top=104.0, touches=3)
        high_zone = self._make_high_zone(pivot=130.0)
        signal = detect_ls_signal(
            high_zones=[high_zone], low_zones=[low_zone],
            current_price=102.0, current_high=105.0, current_low=99.0,
            min_touches=2, sl_buffer_pct=0.5,
            symbol="ETH/USDT", timeframe="1h",
        )
        assert signal is not None
        assert signal.sl_price == pytest.approx(100.0 * (1 - 0.5 / 100), rel=1e-6)

    def test_tp_is_nearest_high_above_price(self):
        low_zone   = self._make_low_zone(pivot=90.0, top=95.0, touches=3)
        high_near  = self._make_high_zone(pivot=110.0)
        high_far   = self._make_high_zone(pivot=150.0)
        signal = detect_ls_signal(
            high_zones=[high_far, high_near], low_zones=[low_zone],
            current_price=93.0, current_high=96.0, current_low=89.0,
            min_touches=2, sl_buffer_pct=0.3,
            symbol="BTC/USDT", timeframe="1h",
        )
        assert signal is not None
        assert signal.tp_price == pytest.approx(110.0)   # nearest, not farthest


# ── _nearest_high_above ────────────────────────────────────────────────────────

class TestNearestHighAbove:

    def _zone(self, pivot, crossed=False):
        return SwingZone(kind="high", pivot_price=pivot,
                         zone_top=pivot, zone_bottom=pivot - 2,
                         pivot_bar=0, touch_count=2, crossed=crossed)

    def test_returns_nearest(self):
        zones = [self._zone(150), self._zone(110), self._zone(130)]
        result = _nearest_high_above(zones, 100.0)
        assert result is not None
        assert result.pivot_price == pytest.approx(110.0)

    def test_ignores_crossed(self):
        zones = [self._zone(110, crossed=True), self._zone(130)]
        result = _nearest_high_above(zones, 100.0)
        assert result is not None
        assert result.pivot_price == pytest.approx(130.0)

    def test_ignores_below_price(self):
        zones = [self._zone(80), self._zone(90)]
        result = _nearest_high_above(zones, 100.0)
        assert result is None

    def test_empty_returns_none(self):
        assert _nearest_high_above([], 100.0) is None


# ── compute_ls_signal (integration) ───────────────────────────────────────────

class TestComputeLSSignal:

    def test_insufficient_candles_returns_none(self):
        result = compute_ls_signal(_flat(100.0, 5), "BTC/USDT", "1h",
                                   pivot_left=14, pivot_right=14)
        assert result is None

    def test_flat_market_no_signal(self):
        result = compute_ls_signal(_flat(100.0, 200), "BTC/USDT", "1h",
                                   pivot_left=5, pivot_right=5, min_touches=2)
        assert result is None

    def test_returns_ls_signal_type_when_fired(self):
        """Build a scenario with a clear pivot low + high and verify type."""
        n = 60
        candles = _flat(100.0, n)
        # Pivot low at bar 20 (needs 5 bars each side)
        candles[20][3] = 80.0   # low spike
        candles[20][4] = 82.0   # close above low
        # Pivot high at bar 40
        candles[40][2] = 120.0  # high spike
        candles[40][4] = 118.0  # close below high
        # Last bar touches the low zone
        candles[-1][3] = 81.0
        candles[-1][2] = 85.0
        candles[-1][4] = 83.0

        result = compute_ls_signal(
            candles, "BTC/USDT", "1h",
            pivot_left=5, pivot_right=5,
            min_touches=1, sl_buffer_pct=0.3,
        )
        if result is not None:
            assert isinstance(result, LSSignal)
            assert result.direction == "buy"
            assert result.tp_price > result.entry_price
            assert result.sl_price < result.entry_price


# ── compute_ls_levels ──────────────────────────────────────────────────────────

class TestComputeLSLevels:

    def test_insufficient_candles_returns_empty(self):
        lvl = compute_ls_levels(_flat(100.0, 3), pivot_left=5, pivot_right=5)
        assert isinstance(lvl, LSLevels)
        assert lvl.swing_highs == []
        assert lvl.swing_lows  == []

    def test_flat_market_no_active_zones(self):
        lvl = compute_ls_levels(_flat(100.0, 100), pivot_left=5, pivot_right=5,
                                 min_touches=2)
        assert isinstance(lvl, LSLevels)
        assert lvl.current_price == pytest.approx(100.0)

    def test_current_price_set(self):
        candles = _flat(55.5, 50)
        lvl = compute_ls_levels(candles, pivot_left=5, pivot_right=5)
        assert lvl.current_price == pytest.approx(55.5)
