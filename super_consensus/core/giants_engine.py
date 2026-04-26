"""
Giants Engine — five independent signal sources.

Each giant receives OHLCV candle data and returns one of:
  Signal.BUY / Signal.SELL / Signal.NEUTRAL

Adding a new giant: subclass BaseGiant and implement `compute()`.
The consensus engine discovers giants via the GIANTS registry list.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import List

logger = logging.getLogger(__name__)


# ── Signal type ────────────────────────────────────────────────────────────────

class Signal(str, Enum):
    BUY     = "BUY"
    SELL    = "SELL"
    NEUTRAL = "NEUTRAL"


# ── OHLCV helpers ──────────────────────────────────────────────────────────────

def _closes(candles: list) -> list[float]:
    return [float(c[4]) for c in candles]

def _highs(candles: list) -> list[float]:
    return [float(c[2]) for c in candles]

def _lows(candles: list) -> list[float]:
    return [float(c[3]) for c in candles]

def _volumes(candles: list) -> list[float]:
    return [float(c[5]) for c in candles]


# ── Base class ─────────────────────────────────────────────────────────────────

class BaseGiant(ABC):
    """Abstract base for every signal source (giant)."""

    name: str = "BaseGiant"

    @abstractmethod
    def compute(self, candles: list) -> Signal:
        """
        Receive the last N OHLCV candles and return a Signal.
        candles: list of [timestamp, open, high, low, close, volume]
        """

    def __repr__(self) -> str:
        return f"<Giant:{self.name}>"


# ── Giant 1 — RSI ──────────────────────────────────────────────────────────────

class RSIGiant(BaseGiant):
    """Relative Strength Index (period=14). BUY < 30, SELL > 70."""

    name = "RSI"

    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70):
        self.period     = period
        self.oversold   = oversold
        self.overbought = overbought

    def _rsi(self, closes: list[float]) -> float:
        if len(closes) < self.period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, self.period + 1):
            diff = closes[-self.period + i] - closes[-self.period + i - 1]
            (gains if diff > 0 else losses).append(abs(diff))
        avg_gain = sum(gains) / self.period if gains else 0.0
        avg_loss = sum(losses) / self.period if losses else 0.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def compute(self, candles: list) -> Signal:
        closes = _closes(candles)
        rsi = self._rsi(closes)
        logger.debug("RSI=%.2f", rsi)
        if rsi < self.oversold:
            return Signal.BUY
        if rsi > self.overbought:
            return Signal.SELL
        return Signal.NEUTRAL

    def get_value(self, candles: list) -> float:
        return self._rsi(_closes(candles))


# ── Giant 2 — MACD ─────────────────────────────────────────────────────────────

class MACDGiant(BaseGiant):
    """MACD crossover. BUY when MACD crosses above signal, SELL when below."""

    name = "MACD"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast   = fast
        self.slow   = slow
        self.signal = signal

    def _ema(self, values: list[float], period: int) -> list[float]:
        if not values:
            return []
        k = 2 / (period + 1)
        emas = [values[0]]
        for v in values[1:]:
            emas.append(v * k + emas[-1] * (1 - k))
        return emas

    def _macd_lines(self, closes: list[float]):
        ema_fast   = self._ema(closes, self.fast)
        ema_slow   = self._ema(closes, self.slow)
        min_len    = min(len(ema_fast), len(ema_slow))
        macd_line  = [ema_fast[i] - ema_slow[i] for i in range(min_len)]
        signal_line = self._ema(macd_line, self.signal)
        return macd_line, signal_line

    def compute(self, candles: list) -> Signal:
        closes = _closes(candles)
        if len(closes) < self.slow + self.signal:
            return Signal.NEUTRAL
        macd_line, signal_line = self._macd_lines(closes)
        if len(macd_line) < 2 or len(signal_line) < 2:
            return Signal.NEUTRAL
        # Crossover detection: compare last two bars
        prev_diff = macd_line[-2] - signal_line[-2]
        curr_diff = macd_line[-1] - signal_line[-1]
        logger.debug("MACD diff prev=%.6f curr=%.6f", prev_diff, curr_diff)
        if prev_diff < 0 and curr_diff > 0:
            return Signal.BUY
        if prev_diff > 0 and curr_diff < 0:
            return Signal.SELL
        return Signal.NEUTRAL

    def get_values(self, candles: list) -> dict:
        closes = _closes(candles)
        macd_line, signal_line = self._macd_lines(closes)
        return {
            "macd":   macd_line[-1] if macd_line else 0,
            "signal": signal_line[-1] if signal_line else 0,
        }


# ── Giant 3 — EMA Crossover ────────────────────────────────────────────────────

class EMAGiant(BaseGiant):
    """EMA 20 / EMA 50 crossover. BUY when EMA20 crosses above EMA50."""

    name = "EMA"

    def __init__(self, fast: int = 20, slow: int = 50):
        self.fast = fast
        self.slow = slow

    def _ema(self, values: list[float], period: int) -> list[float]:
        if not values:
            return []
        k = 2 / (period + 1)
        emas = [values[0]]
        for v in values[1:]:
            emas.append(v * k + emas[-1] * (1 - k))
        return emas

    def compute(self, candles: list) -> Signal:
        closes = _closes(candles)
        if len(closes) < self.slow + 2:
            return Signal.NEUTRAL
        ema_fast = self._ema(closes, self.fast)
        ema_slow = self._ema(closes, self.slow)
        min_len  = min(len(ema_fast), len(ema_slow))
        if min_len < 2:
            return Signal.NEUTRAL
        prev_diff = ema_fast[-2] - ema_slow[-2]
        curr_diff = ema_fast[-1] - ema_slow[-1]
        logger.debug("EMA diff prev=%.6f curr=%.6f", prev_diff, curr_diff)
        if prev_diff < 0 and curr_diff > 0:
            return Signal.BUY
        if prev_diff > 0 and curr_diff < 0:
            return Signal.SELL
        return Signal.NEUTRAL

    def get_values(self, candles: list) -> dict:
        closes = _closes(candles)
        ema_fast = self._ema(closes, self.fast)
        ema_slow = self._ema(closes, self.slow)
        return {
            f"ema{self.fast}": ema_fast[-1] if ema_fast else 0,
            f"ema{self.slow}": ema_slow[-1] if ema_slow else 0,
        }


# ── Giant 4 — Bollinger Bands ──────────────────────────────────────────────────

class BollingerGiant(BaseGiant):
    """Bollinger Bands (20, 2σ). BUY when price ≤ lower band, SELL when ≥ upper."""

    name = "Bollinger"

    def __init__(self, period: int = 20, std_dev: float = 2.0):
        self.period  = period
        self.std_dev = std_dev

    def _bands(self, closes: list[float]):
        if len(closes) < self.period:
            return None, None, None
        window = closes[-self.period:]
        mid    = sum(window) / self.period
        var    = sum((x - mid) ** 2 for x in window) / self.period
        std    = var ** 0.5
        return mid - self.std_dev * std, mid, mid + self.std_dev * std

    def compute(self, candles: list) -> Signal:
        closes = _closes(candles)
        lower, mid, upper = self._bands(closes)
        if lower is None:
            return Signal.NEUTRAL
        price = closes[-1]
        logger.debug("BB lower=%.4f price=%.4f upper=%.4f", lower, price, upper)
        if price <= lower:
            return Signal.BUY
        if price >= upper:
            return Signal.SELL
        return Signal.NEUTRAL

    def get_values(self, candles: list) -> dict:
        closes = _closes(candles)
        lower, mid, upper = self._bands(closes)
        return {"lower": lower, "mid": mid, "upper": upper, "price": closes[-1]}


# ── Giant 5 — Volume + Momentum ────────────────────────────────────────────────

class VolumeMomentumGiant(BaseGiant):
    """
    High volume + rapid price move.
    BUY:  volume > avg_volume * threshold AND price change > +momentum_pct
    SELL: volume > avg_volume * threshold AND price change < -momentum_pct
    """

    name = "Volume"

    def __init__(
        self,
        vol_period: int   = 20,
        vol_threshold: float = 1.5,
        momentum_pct: float  = 0.5,   # percent
    ):
        self.vol_period    = vol_period
        self.vol_threshold = vol_threshold
        self.momentum_pct  = momentum_pct

    def compute(self, candles: list) -> Signal:
        closes  = _closes(candles)
        volumes = _volumes(candles)
        if len(candles) < self.vol_period + 1:
            return Signal.NEUTRAL

        avg_vol    = sum(volumes[-self.vol_period - 1:-1]) / self.vol_period
        curr_vol   = volumes[-1]
        price_chg  = (closes[-1] - closes[-2]) / closes[-2] * 100 if closes[-2] else 0

        logger.debug(
            "Volume curr=%.2f avg=%.2f ratio=%.2f price_chg=%.3f%%",
            curr_vol, avg_vol, curr_vol / avg_vol if avg_vol else 0, price_chg,
        )

        if avg_vol == 0:
            return Signal.NEUTRAL

        high_volume = curr_vol > avg_vol * self.vol_threshold
        if high_volume and price_chg > self.momentum_pct:
            return Signal.BUY
        if high_volume and price_chg < -self.momentum_pct:
            return Signal.SELL
        return Signal.NEUTRAL

    def get_values(self, candles: list) -> dict:
        closes  = _closes(candles)
        volumes = _volumes(candles)
        avg_vol = sum(volumes[-self.vol_period - 1:-1]) / self.vol_period if len(volumes) > self.vol_period else 0
        price_chg = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 and closes[-2] else 0
        return {
            "current_volume": volumes[-1] if volumes else 0,
            "avg_volume":     avg_vol,
            "price_change_pct": price_chg,
        }


# ── Registry ───────────────────────────────────────────────────────────────────
# Add or swap giants here without touching the consensus engine.

GIANTS: List[BaseGiant] = [
    RSIGiant(),
    MACDGiant(),
    EMAGiant(),
    BollingerGiant(),
    VolumeMomentumGiant(),
]
