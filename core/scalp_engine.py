"""
Scalping Engine v2 — EMA Ribbon + RSI + MACD + ADX + ATR + Supertrend + Volume.

ملاحظة هامة: MEXC Spot لا يدعم أوامر وقف الخسارة في البورصة.
وقف الخسارة يتم بالكامل في الكود (price-based) — لا يحتاج أوامر تبادل.

Strategy:
  Entry BUY:
    - EMA8 > EMA13 > EMA21            (bullish ribbon)
    - 35 <= RSI14 <= 72               (momentum, not overbought)
    - MACD line > Signal line          (bullish momentum)
    - ADX > 18                         (avoid ranging markets)
    - Volume spike > avg × VOLUME_SPIKE_MULT (معلومة فقط، مش شرط إجباري)
    - Supertrend direction = UP        (macro trend filter)
    - ATR > MIN_ATR_PCT of price       (minimum volatility to scalp)

  Exit SELL (any one of):
    - Trailing stop hit (ATR-based, code-side only)
    - Take-profit hit (ATR × TP_ATR_MULT)
    - Hard stop-loss hit (ATR × SL_ATR_MULT, code-side only)
    - EMA8 < EMA13 AND RSI > RSI_OVERBOUGHT
    - Supertrend flips to DOWN

  Cooldown: MIN_BARS_BETWEEN_TRADES candles after any exit before re-entry.

Paper mode (default=True): logs + notifications, no real orders placed.
Live mode: executes market buy/sell via MexcClient.

Public API:
  start(client, symbol, capital_usdt, timeframe, paper)
  stop(symbol)
  status(symbol)  → dict
  active_symbols() → list[str]
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

CANDLE_LIMIT             = 100
POLL_INTERVAL_S          = 15

TP_ATR_MULT              = 2.0
SL_ATR_MULT              = 1.0
TRAIL_ATR_MULT           = 1.2

VOLUME_SPIKE_MULT        = 1.1   # مش شرط إجباري، بس بيُستخدم كمعلومة في الإشعارات

RSI_PERIOD               = 14
RSI_BUY_LOW              = 35   # كان 40 — تم تخفيفه عشان يجيب صفقات أكتر
RSI_BUY_HIGH             = 72   # كان 68 — تم توسيعه
RSI_OVERBOUGHT           = 75   # كان 72

ADX_PERIOD               = 14
ADX_MIN                  = 18   # كان 20 — تم تخفيفه

ATR_PERIOD               = 14
MIN_ATR_PCT              = 0.002

ST_PERIOD                = 10
ST_MULTIPLIER            = 3.0

MACD_FAST                = 12
MACD_SLOW                = 26
MACD_SIGNAL_PERIOD       = 9

MIN_BARS_BETWEEN_TRADES  = 3

# ── Notifier callbacks ─────────────────────────────────────────────────────────

_notify_entry:  Optional[Callable[..., Awaitable]] = None
_notify_exit:   Optional[Callable[..., Awaitable]] = None
_notify_error:  Optional[Callable[..., Awaitable]] = None


def set_notifiers(
    entry:  Callable[..., Awaitable] | None = None,
    exit_:  Callable[..., Awaitable] | None = None,
    error:  Callable[..., Awaitable] | None = None,
    scan:   Callable[..., Awaitable] | None = None,
) -> None:
    global _notify_entry, _notify_exit, _notify_error
    _notify_entry = entry
    _notify_exit  = exit_
    _notify_error = error


# ── State ──────────────────────────────────────────────────────────────────────

@dataclass
class ScalpState:
    symbol:            str
    capital_usdt:      float
    timeframe:         str
    paper:             bool

    in_position:       bool  = False
    entry_price:       float = 0.0
    position_qty:      float = 0.0
    tp_price:          float = 0.0
    sl_price:          float = 0.0
    trail_high:        float = 0.0
    trailing_stop:     float = 0.0

    realized_pnl:      float = 0.0
    trade_count:       int   = 0
    win_count:         int   = 0
    last_signal:       str   = "—"
    bars_since_exit:   int   = MIN_BARS_BETWEEN_TRADES
    last_check_ts:     float = field(default_factory=time.monotonic)


_states: dict[str, ScalpState] = {}
_tasks:  dict[str, asyncio.Task] = {}


# ── Indicator helpers ──────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    result = [0.0] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def _rsi(closes: list[float], period: int = RSI_PERIOD) -> list[float]:
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
        result[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return result


def _atr(highs: list[float], lows: list[float], closes: list[float],
         period: int = ATR_PERIOD) -> list[float]:
    result = [0.0] * len(closes)
    if len(closes) < period + 1:
        return result
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        for i in range(1, len(closes))
    ]
    if len(trs) < period:
        return result
    atr_val = sum(trs[:period]) / period
    result[period] = atr_val
    for i in range(period + 1, len(closes)):
        atr_val = (atr_val * (period - 1) + trs[i - 1]) / period
        result[i] = atr_val
    return result


def _macd(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    ema_fast  = _ema(closes, MACD_FAST)
    ema_slow  = _ema(closes, MACD_SLOW)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    sig_line  = _ema(macd_line, MACD_SIGNAL_PERIOD)
    histogram = [m - s for m, s in zip(macd_line, sig_line)]
    return macd_line, sig_line, histogram


def _adx(highs: list[float], lows: list[float], closes: list[float],
         period: int = ADX_PERIOD) -> list[float]:
    result = [0.0] * len(closes)
    if len(closes) < period * 2 + 1:
        return result
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        hd = highs[i] - highs[i-1]
        ld = lows[i-1] - lows[i]
        plus_dm.append(max(hd, 0.0) if hd > ld else 0.0)
        minus_dm.append(max(ld, 0.0) if ld > hd else 0.0)
        tr_list.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1]),
        ))

    def _smooth(arr, p):
        s = [sum(arr[:p])]
        for v in arr[p:]:
            s.append(s[-1] - s[-1] / p + v)
        return s

    splus  = _smooth(plus_dm,  period)
    sminus = _smooth(minus_dm, period)
    str_   = _smooth(tr_list,  period)
    dx_list = []
    for sp, sm, st in zip(splus, sminus, str_):
        if st == 0:
            dx_list.append(0.0)
            continue
        pdi = 100 * sp / st
        mdi = 100 * sm / st
        denom = pdi + mdi
        dx_list.append(100 * abs(pdi - mdi) / denom if denom else 0.0)

    if len(dx_list) < period:
        return result
    adx_val = sum(dx_list[:period]) / period
    base = period * 2
    result[base] = adx_val
    for i in range(1, len(dx_list) - period + 1):
        adx_val = (adx_val * (period - 1) + dx_list[period + i - 1]) / period
        if base + i < len(result):
            result[base + i] = adx_val
    return result


def _supertrend(highs: list[float], lows: list[float], closes: list[float],
                period: int = ST_PERIOD, mult: float = ST_MULTIPLIER) -> list[int]:
    direction = [0] * len(closes)
    if len(closes) < period + 1:
        return direction
    atr_vals = _atr(highs, lows, closes, period)
    upper = [0.0] * len(closes)
    lower = [0.0] * len(closes)
    for i in range(period, len(closes)):
        hl2 = (highs[i] + lows[i]) / 2
        upper[i] = hl2 + mult * atr_vals[i]
        lower[i] = hl2 - mult * atr_vals[i]
    fu = list(upper)
    fl = list(lower)
    direction[period] = 1
    for i in range(period + 1, len(closes)):
        fu[i] = upper[i] if upper[i] < fu[i-1] or closes[i-1] > fu[i-1] else fu[i-1]
        fl[i] = lower[i] if lower[i] > fl[i-1] or closes[i-1] < fl[i-1] else fl[i-1]
        if closes[i] > fu[i-1]:
            direction[i] = 1
        elif closes[i] < fl[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]
    return direction


# ── Signal computation ─────────────────────────────────────────────────────────

def _compute_signals(candles_raw: list) -> dict:
    """Evaluate all indicators on candles[-2] (last closed candle)."""
    min_needed = MACD_SLOW + MACD_SIGNAL_PERIOD + 5
    if len(candles_raw) < min_needed:
        return {"error": f"حاجة {min_needed} شمعة على الأقل"}

    highs   = [float(c[2]) for c in candles_raw]
    lows    = [float(c[3]) for c in candles_raw]
    closes  = [float(c[4]) for c in candles_raw]
    volumes = [float(c[5]) for c in candles_raw]

    ema8   = _ema(closes, 8)
    ema13  = _ema(closes, 13)
    ema21  = _ema(closes, 21)
    rsi_v  = _rsi(closes, RSI_PERIOD)
    atr_v  = _atr(highs, lows, closes, ATR_PERIOD)
    adx_v  = _adx(highs, lows, closes, ADX_PERIOD)
    st_dir = _supertrend(highs, lows, closes, ST_PERIOD, ST_MULTIPLIER)
    macd_line, sig_line, histogram = _macd(closes)

    idx = len(candles_raw) - 2  # last CLOSED candle

    e8, e13, e21 = ema8[idx], ema13[idx], ema21[idx]
    rsi_val  = rsi_v[idx]
    atr_val  = atr_v[idx]
    adx_val  = adx_v[idx]
    st_val   = st_dir[idx]
    price    = closes[idx]

    vol_window = volumes[max(0, idx - 20): idx]
    avg_vol    = sum(vol_window) / len(vol_window) if vol_window else 0.0
    vol_spike  = avg_vol > 0 and volumes[idx] > avg_vol * VOLUME_SPIKE_MULT

    bullish_ribbon = e8 > e13 > e21 and e21 > 0
    bearish_ribbon = e8 < e13
    macd_bullish   = macd_line[idx] > sig_line[idx] and histogram[idx] > 0
    trend_up       = st_val == 1
    trend_down     = st_val == -1
    strong_trend   = adx_val >= ADX_MIN
    atr_pct        = atr_val / price if price > 0 else 0.0

    buy_signal = (
        bullish_ribbon
        and RSI_BUY_LOW <= rsi_val <= RSI_BUY_HIGH
        and macd_bullish
        and strong_trend
        and trend_up
        and atr_pct >= MIN_ATR_PCT
        # vol_spike اتشال من الشروط الإجبارية — بيظهر في الإشعارات كمعلومة بس
    )
    sell_signal = bearish_ribbon or rsi_val > RSI_OVERBOUGHT or trend_down

    return {
        "close": price, "high": highs[idx], "low": lows[idx],
        "ema8": e8, "ema13": e13, "ema21": e21,
        "rsi": rsi_val, "atr": atr_val, "atr_pct": atr_pct * 100,
        "adx": adx_val, "supertrend_dir": st_val,
        "macd": macd_line[idx], "macd_signal": sig_line[idx],
        "vol_spike": vol_spike,
        "bullish_ribbon": bullish_ribbon, "bearish_ribbon": bearish_ribbon,
        "buy_signal": buy_signal, "sell_signal": sell_signal,
        "error": None,
    }


# ── Trailing stop ──────────────────────────────────────────────────────────────

def _update_trailing(state: ScalpState, price: float, atr: float) -> None:
    if price > state.trail_high:
        state.trail_high    = price
        state.trailing_stop = price - atr * TRAIL_ATR_MULT


# ── Core loop ──────────────────────────────────────────────────────────────────

async def _scalp_loop(client, state: ScalpState) -> None:
    symbol = state.symbol
    log.info("Scalp loop v2 started: %s paper=%s tf=%s", symbol, state.paper, state.timeframe)

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_S)
            state.last_check_ts = time.monotonic()

            raw = await client.fetch_ohlcv(symbol, state.timeframe, CANDLE_LIMIT)
            await asyncio.sleep(ORDER_SLEEP_SECONDS)

            sig = _compute_signals(raw)
            if sig.get("error"):
                log.warning("scalp %s: %s", symbol, sig["error"])
                continue

            price = sig["close"]
            atr   = sig["atr"]

            # ── Exit check ─────────────────────────────────────────────────────
            if state.in_position:
                _update_trailing(state, price, atr)

                pnl_pct      = (price - state.entry_price) / state.entry_price * 100
                exit_reason  = None

                if price >= state.tp_price:
                    exit_reason = f"TP +{pnl_pct:.2f}%"
                elif price <= state.sl_price:
                    exit_reason = f"SL {pnl_pct:.2f}%"
                elif state.trailing_stop > 0 and price <= state.trailing_stop:
                    exit_reason = f"Trailing {pnl_pct:.2f}%"
                elif sig["sell_signal"]:
                    exit_reason = f"إشارة بيع {pnl_pct:.2f}%"

                if exit_reason:
                    pnl = (price - state.entry_price) * state.position_qty
                    state.realized_pnl   += pnl
                    state.trade_count    += 1
                    if pnl > 0:
                        state.win_count  += 1
                    state.in_position     = False
                    state.bars_since_exit = 0
                    state.last_signal     = "SELL"

                    log.info(
                        "SCALP EXIT %s @ %.6f reason=%s pnl=%.4f paper=%s",
                        symbol, price, exit_reason, pnl, state.paper,
                    )
                    if not state.paper:
                        await client.market_sell_qty(symbol, state.position_qty)

                    win_rate = state.win_count / state.trade_count * 100 if state.trade_count else 0
                    if _notify_exit:
                        await _notify_exit(
                            symbol=symbol, price=price, pnl=pnl,
                            reason=exit_reason, paper=state.paper,
                            total_pnl=state.realized_pnl,
                            trade_count=state.trade_count,
                            win_rate=win_rate,
                        )
                    continue

            # ── Cooldown counter ───────────────────────────────────────────────
            if not state.in_position:
                state.bars_since_exit += 1

            # ── Entry check ────────────────────────────────────────────────────
            cooldown_done = state.bars_since_exit >= MIN_BARS_BETWEEN_TRADES

            if not state.in_position and cooldown_done and sig["buy_signal"]:
                qty = state.capital_usdt / price
                tp  = price + atr * TP_ATR_MULT
                sl  = price - atr * SL_ATR_MULT

                state.in_position   = True
                state.entry_price   = price
                state.position_qty  = qty
                state.tp_price      = tp
                state.sl_price      = sl
                state.trail_high    = price
                state.trailing_stop = price - atr * TRAIL_ATR_MULT
                state.last_signal   = "BUY"

                log.info(
                    "SCALP ENTRY %s @ %.6f qty=%.6f rsi=%.1f adx=%.1f tp=%.6f sl=%.6f paper=%s",
                    symbol, price, qty, sig["rsi"], sig["adx"], tp, sl, state.paper,
                )
                if not state.paper:
                    await client.market_buy(symbol, qty, price=price)

                if _notify_entry:
                    await _notify_entry(
                        symbol=symbol, price=price, qty=qty,
                        capital=state.capital_usdt,
                        rsi=sig["rsi"], adx=sig["adx"], atr=atr,
                        tp_price=tp, sl_price=sl,
                        supertrend=sig["supertrend_dir"],
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
    if symbol in _tasks and not _tasks[symbol].done():
        raise ValueError(f"Scalp already running for {symbol}")
    state = ScalpState(
        symbol=symbol, capital_usdt=capital_usdt,
        timeframe=timeframe, paper=paper,
    )
    _states[symbol] = state
    _tasks[symbol]  = asyncio.create_task(_scalp_loop(client, state))
    log.info("Scalp v2 started: %s capital=%.2f tf=%s paper=%s", symbol, capital_usdt, timeframe, paper)
    return state


async def stop(symbol: str) -> Optional[ScalpState]:
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
    win_rate = round(state.win_count / state.trade_count * 100, 1) if state.trade_count else 0
    return {
        "symbol":          state.symbol,
        "paper":           state.paper,
        "timeframe":       state.timeframe,
        "capital":         state.capital_usdt,
        "in_position":     state.in_position,
        "entry_price":     state.entry_price,
        "tp_price":        state.tp_price,
        "sl_price":        state.sl_price,
        "trailing_stop":   state.trailing_stop,
        "position_qty":    state.position_qty,
        "realized_pnl":    state.realized_pnl,
        "trade_count":     state.trade_count,
        "win_count":       state.win_count,
        "win_rate_pct":    win_rate,
        "last_signal":     state.last_signal,
        "bars_since_exit": state.bars_since_exit,
    }
