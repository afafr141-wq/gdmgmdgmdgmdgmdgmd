"""
Scalping Engine — EMA Ribbon + RSI + Volume Spike.

Strategy:
  Entry BUY  : EMA8 > EMA13 > EMA21  AND  45 <= RSI7 <= 65  AND  volume spike
  Exit  SELL : EMA8 < EMA13           OR   RSI7 > 75          OR   take-profit hit
               OR  stop-loss hit (price-based, no exchange order)

Signals are evaluated on candles[-2] (last *closed* candle only).

Paper-trading mode (default): logs and notifies without placing real orders.
Live mode: executes market buy/sell via MexcClient.

Lifecycle:
  start(symbol, capital_usdt, timeframe, paper)  → starts asyncio loop task
  stop(symbol)                                    → cancels task, clears state
  status(symbol)                                  → returns state dict
  active_symbols()                                → list of running symbols
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

from config.settings import ORDER_SLEEP_SECONDS

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

CANDLE_LIMIT        = 60      # candles fetched per cycle
POLL_INTERVAL_S     = 15      # seconds between candle checks
TAKE_PROFIT_PCT     = 0.008   # 0.8% take-profit
STOP_LOSS_PCT       = 0.004   # 0.4% stop-loss
VOLUME_SPIKE_MULT   = 1.5     # volume must be > avg_20 × this
RSI_BUY_LOW         = 45
RSI_BUY_HIGH        = 65
RSI_OVERBOUGHT      = 75

# ── Notifier callbacks (injected from bot layer) ───────────────────────────────

_notify_entry:  Optional[Callable[..., Awaitable]] = None
_notify_exit:   Optional[Callable[..., Awaitable]] = None
_notify_error:  Optional[Callable[..., Awaitable]] = None
_notify_scan:   Optional[Callable[..., Awaitable]] = None


def set_notifiers(
    entry:  Callable[..., Awaitable] | None = None,
    exit_:  Callable[..., Awaitable] | None = None,
    error:  Callable[..., Awaitable] | None = None,
    scan:   Callable[..., Awaitable] | None = None,
) -> None:
    global _notify_entry, _notify_exit, _notify_error, _notify_scan
    _notify_entry = entry
    _notify_exit  = exit_
    _notify_error = error
    _notify_scan  = scan


# ── State ──────────────────────────────────────────────────────────────────────

@dataclass
class ScalpState:
    symbol:        str
    capital_usdt:  float
    timeframe:     str
    paper:         bool

    in_position:   bool  = False
    entry_price:   float = 0.0
    position_qty:  float = 0.0
    realized_pnl:  float = 0.0
    trade_count:   int   = 0
    last_signal:   str   = "—"
    last_check_ts: float = field(default_factory=time.monotonic)


_states: dict[str, ScalpState] = {}
_tasks:  dict[str, asyncio.Task] = {}


# ── Indicator helpers ──────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average aligned with input list."""
    result = [0.0] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    # seed with SMA
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def _rsi(closes: list[float], period: int = 7) -> list[float]:
    """RSI aligned with input list (first `period` entries are 0)."""
    result = [0.0] * len(closes)
    if len(closes) <= period:
        return result

    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(closes)):
        idx = i - period
        avg_gain = (avg_gain * (period - 1) + gains[idx]) / period
        avg_loss = (avg_loss * (period - 1) + losses[idx]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result


def _compute_signals(candles_raw: list) -> dict:
    """
    Compute EMA8/13/21, RSI7, and volume spike on closed candles.
    Returns signal dict for candles[-2] (last closed candle).
    """
    if len(candles_raw) < 25:
        return {"error": "not enough candles"}

    closes  = [float(c[4]) for c in candles_raw]
    volumes = [float(c[5]) for c in candles_raw]

    ema8  = _ema(closes, 8)
    ema13 = _ema(closes, 13)
    ema21 = _ema(closes, 21)
    rsi7  = _rsi(closes, 7)

    # Use second-to-last (last closed) candle
    idx = len(candles_raw) - 2

    e8, e13, e21 = ema8[idx], ema13[idx], ema21[idx]
    rsi          = rsi7[idx]
    vol          = volumes[idx]
    avg_vol      = sum(volumes[max(0, idx - 20):idx]) / min(20, idx) if idx > 0 else 0

    bullish_ribbon = e8 > e13 > e21 and e21 > 0
    bearish_ribbon = e8 < e13
    vol_spike      = avg_vol > 0 and vol > avg_vol * VOLUME_SPIKE_MULT

    buy_signal  = bullish_ribbon and RSI_BUY_LOW <= rsi <= RSI_BUY_HIGH and vol_spike
    sell_signal = bearish_ribbon or rsi > RSI_OVERBOUGHT

    return {
        "close":          closes[idx],
        "ema8":           e8,
        "ema13":          e13,
        "ema21":          e21,
        "rsi":            rsi,
        "vol":            vol,
        "avg_vol":        avg_vol,
        "vol_spike":      vol_spike,
        "bullish_ribbon": bullish_ribbon,
        "bearish_ribbon": bearish_ribbon,
        "buy_signal":     buy_signal,
        "sell_signal":    sell_signal,
        "error":          None,
    }


# ── Core loop ──────────────────────────────────────────────────────────────────

async def _scalp_loop(client, state: ScalpState) -> None:
    """Main polling loop for one symbol."""
    symbol = state.symbol
    log.info("Scalp loop started: %s (paper=%s)", symbol, state.paper)

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_S)
            state.last_check_ts = time.monotonic()

            # Fetch candles
            raw = await client.fetch_ohlcv(symbol, state.timeframe, CANDLE_LIMIT)
            await asyncio.sleep(ORDER_SLEEP_SECONDS)

            sig = _compute_signals(raw)
            if sig.get("error"):
                log.warning("scalp %s: %s", symbol, sig["error"])
                continue

            price = sig["close"]

            # ── Exit logic (check first) ───────────────────────────────────────
            if state.in_position:
                tp_price = state.entry_price * (1 + TAKE_PROFIT_PCT)
                sl_price = state.entry_price * (1 - STOP_LOSS_PCT)

                exit_reason = None
                if price >= tp_price:
                    exit_reason = f"TP +{TAKE_PROFIT_PCT*100:.1f}%"
                elif price <= sl_price:
                    exit_reason = f"SL -{STOP_LOSS_PCT*100:.1f}%"
                elif sig["sell_signal"]:
                    exit_reason = "إشارة بيع (EMA/RSI)"

                if exit_reason:
                    pnl = (price - state.entry_price) * state.position_qty
                    state.realized_pnl += pnl
                    state.trade_count  += 1
                    state.in_position   = False
                    state.last_signal   = "SELL"

                    log.info(
                        "SCALP EXIT %s @ %.6f | reason=%s | pnl=%.4f USDT (paper=%s)",
                        symbol, price, exit_reason, pnl, state.paper,
                    )

                    if not state.paper:
                        await client.market_sell_qty(symbol, state.position_qty)

                    if _notify_exit:
                        await _notify_exit(
                            symbol=symbol,
                            price=price,
                            pnl=pnl,
                            reason=exit_reason,
                            paper=state.paper,
                            total_pnl=state.realized_pnl,
                            trade_count=state.trade_count,
                        )
                    continue

            # ── Entry logic ────────────────────────────────────────────────────
            if not state.in_position and sig["buy_signal"]:
                qty = state.capital_usdt / price
                state.in_position  = True
                state.entry_price  = price
                state.position_qty = qty
                state.last_signal  = "BUY"

                log.info(
                    "SCALP ENTRY %s @ %.6f | qty=%.6f | rsi=%.1f | vol_spike=%s (paper=%s)",
                    symbol, price, qty, sig["rsi"], sig["vol_spike"], state.paper,
                )

                if not state.paper:
                    await client.market_buy(symbol, qty)

                if _notify_entry:
                    await _notify_entry(
                        symbol=symbol,
                        price=price,
                        qty=qty,
                        capital=state.capital_usdt,
                        rsi=sig["rsi"],
                        paper=state.paper,
                    )

        except asyncio.CancelledError:
            log.info("Scalp loop cancelled: %s", symbol)
            return
        except Exception as exc:
            log.error("Scalp loop error %s: %s", symbol, exc)
            if _notify_error:
                await _notify_error(symbol=symbol, error=str(exc))
            await asyncio.sleep(30)


# ── Public API ─────────────────────────────────────────────────────────────────

async def start(
    client,
    symbol:       str,
    capital_usdt: float = 20.0,
    timeframe:    str   = "3m",
    paper:        bool  = True,
) -> ScalpState:
    """Start scalping loop for symbol. Raises if already running."""
    if symbol in _tasks and not _tasks[symbol].done():
        raise ValueError(f"Scalp already running for {symbol}")

    state = ScalpState(
        symbol=symbol,
        capital_usdt=capital_usdt,
        timeframe=timeframe,
        paper=paper,
    )
    _states[symbol] = state
    _tasks[symbol]  = asyncio.create_task(_scalp_loop(client, state))
    log.info("Scalp started: %s capital=%.2f timeframe=%s paper=%s", symbol, capital_usdt, timeframe, paper)
    return state


async def stop(symbol: str) -> Optional[ScalpState]:
    """Stop scalping loop for symbol. Returns final state."""
    task = _tasks.pop(symbol, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    state = _states.pop(symbol, None)
    log.info("Scalp stopped: %s", symbol)
    return state


def active_symbols() -> list[str]:
    return [s for s, t in _tasks.items() if not t.done()]


def status(symbol: str) -> Optional[dict]:
    state = _states.get(symbol)
    if not state:
        return None
    return {
        "symbol":       state.symbol,
        "paper":        state.paper,
        "timeframe":    state.timeframe,
        "capital":      state.capital_usdt,
        "in_position":  state.in_position,
        "entry_price":  state.entry_price,
        "position_qty": state.position_qty,
        "realized_pnl": state.realized_pnl,
        "trade_count":  state.trade_count,
        "last_signal":  state.last_signal,
    }
