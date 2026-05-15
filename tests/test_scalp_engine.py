"""
Unit tests for core/scalp_engine.py — indicators and signal logic.
No live MEXC connection or real database required.
"""
import pytest
from core.scalp_engine import _ema, _rsi, _compute_signals


# ── EMA ────────────────────────────────────────────────────────────────────────

def test_ema_length_matches_input():
    values = [float(i) for i in range(1, 31)]
    result = _ema(values, period=8)
    assert len(result) == len(values)


def test_ema_seed_equals_sma():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = _ema(values, period=5)
    # seed at index 4 should equal simple average of first 5
    assert abs(result[4] - 3.0) < 1e-9


def test_ema_too_short_returns_zeros():
    result = _ema([1.0, 2.0], period=8)
    assert all(v == 0.0 for v in result)


def test_ema_trending_up():
    values = [float(i) for i in range(1, 31)]
    result = _ema(values, period=8)
    # EMA should be increasing for a linearly rising series
    non_zero = [v for v in result if v > 0]
    assert non_zero == sorted(non_zero)


# ── RSI ────────────────────────────────────────────────────────────────────────

def test_rsi_length_matches_input():
    closes = [100.0 + i * 0.5 for i in range(30)]
    result = _rsi(closes, period=7)
    assert len(result) == len(closes)


def test_rsi_all_gains_gives_100():
    closes = [float(i) for i in range(1, 31)]
    result = _rsi(closes, period=7)
    # All gains → RSI should approach 100
    assert result[-1] > 90


def test_rsi_all_losses_gives_near_zero():
    closes = [float(30 - i) for i in range(30)]
    result = _rsi(closes, period=7)
    assert result[-1] < 10


def test_rsi_flat_series():
    closes = [100.0] * 30
    result = _rsi(closes, period=7)
    # No movement → avg_loss = 0 → RSI = 100
    assert result[-1] == 100.0


# ── _compute_signals ───────────────────────────────────────────────────────────

def _make_candles(n: int, base_price: float = 100.0, trend: float = 0.5) -> list:
    """Return fake OHLCV candles: [ts, open, high, low, close, volume]."""
    candles = []
    for i in range(n):
        close  = base_price + i * trend
        volume = 1000.0 + (500.0 if i % 5 == 0 else 0.0)   # spike every 5
        candles.append([i * 60000, close - 0.1, close + 0.2, close - 0.2, close, volume])
    return candles


def test_compute_signals_returns_error_on_short_data():
    candles = _make_candles(10)
    result = _compute_signals(candles)
    assert result.get("error") is not None


def test_compute_signals_returns_dict_on_sufficient_data():
    candles = _make_candles(50)
    result = _compute_signals(candles)
    assert result.get("error") is None
    for key in ("close", "ema8", "ema13", "ema21", "rsi", "vol_spike",
                "bullish_ribbon", "bearish_ribbon", "buy_signal", "sell_signal"):
        assert key in result, f"Missing key: {key}"


def test_bullish_ribbon_on_uptrend():
    candles = _make_candles(60, trend=1.0)
    result = _compute_signals(candles)
    assert result["error"] is None
    # Strong uptrend → EMA8 > EMA13 > EMA21
    assert result["bullish_ribbon"] is True


def test_bearish_ribbon_on_downtrend():
    candles = _make_candles(60, trend=-1.0)
    result = _compute_signals(candles)
    assert result["error"] is None
    assert result["bearish_ribbon"] is True


def test_buy_signal_requires_volume_spike():
    """buy_signal must be False when volume is flat (no spike)."""
    candles = _make_candles(60, trend=1.0)
    # Flatten all volumes so no spike
    for c in candles:
        c[5] = 1000.0
    result = _compute_signals(candles)
    assert result["error"] is None
    # No volume spike → buy_signal must be False
    assert result["buy_signal"] is False


def test_signals_use_second_to_last_candle():
    """Mutating the last (forming) candle must not change the signal."""
    candles = _make_candles(50, trend=1.0)
    result_before = _compute_signals(candles)

    # Corrupt the last (forming) candle
    candles[-1][4] = 999999.0
    result_after = _compute_signals(candles)

    assert result_before["close"] == result_after["close"]
    assert result_before["buy_signal"] == result_after["buy_signal"]
