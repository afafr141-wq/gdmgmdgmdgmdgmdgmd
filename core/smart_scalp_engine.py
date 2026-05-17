"""
Smart Scalp Engine — EMA9/21 + RSI + Green Candle.

استراتيجية بسيطة وفعّالة للسكالبينج السريع.

Entry BUY (3 شروط بس):
  1. EMA9 > EMA21                  (اتجاه صاعد)
  2. 40 <= RSI(14) <= 70           (زخم جيد، مش مبالغ فيه)
  3. الشمعة الأخيرة خضرا           (تأكيد الصعود)

Exit SELL (أي شرط):
  - TP: سعر الدخول + ATR × TP_ATR_MULT
  - SL: سعر الدخول - ATR × SL_ATR_MULT
  - Trailing Stop: يتحرك مع السعر (ATR × TRAIL_ATR_MULT)
  - EMA9 يقطع تحت EMA21

Paper mode (افتراضي): بدون أوامر حقيقية.
Live mode: بينفذ market buy/sell عبر MexcClient.

Public API:
  start(client, symbol, capital_usdt, timeframe, paper) → SmartScalpState
  stop(symbol)
  status(symbol) → dict | None
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

CANDLE_LIMIT    = 60
POLL_INTERVAL_S = 10

TP_ATR_MULT     = 2.0
SL_ATR_MULT     = 1.0
TRAIL_ATR_MULT  = 1.2

RSI_PERIOD      = 14
RSI_LOW         = 40
RSI_HIGH        = 70
RSI_EXIT        = 74

ATR_PERIOD      = 14
MIN_ATR_PCT     = 0.001   # 0.1% حد أدنى للتذبذب

MIN_BARS_COOLDOWN = 2

# ── Notifiers ──────────────────────────────────────────────────────────────────

_notify_entry: Optional[Callable[..., Awaitable]] = None
_notify_exit:  Optional[Callable[..., Awaitable]] = None
_notify_error: Optional[Callable[..., Awaitable]] = None


def set_notifiers(
    entry: Callable[..., Awaitable] | None = None,
    exit_: Callable[..., Awaitable] | None = None,
    error: Callable[..., Awaitable] | None = None,
) -> None:
    global _notify_entry, _notify_exit, _notify_error
    _notify_entry = entry
    _notify_exit  = exit_
    _notify_error = error


# ── State ──────────────────────────────────────────────────────────────────────

@dataclass
class SmartScalpState:
    symbol:         str
    capital_usdt:   float
    timeframe:      str
    paper:          bool

    in_position:    bool  = False
    entry_price:    float = 0.0
    position_qty:   float = 0.0
    tp_price:       float = 0.0
    sl_price:       float = 0.0
    trail_high:     float = 0.0
    trailing_stop:  float = 0.0

    realized_pnl:   float = 0.0
    trade_count:    int   = 0
    win_count:      int   = 0
    last_signal:    str   = "—"
    bars_since_exit: int  = MIN_BARS_COOLDOWN
    last_check_ts:  float = field(default_factory=time.monotonic)


_states: dict[str, SmartScalpState] = {}
_tasks:  dict[str, asyncio.Task]    = {}


# ── Indicators ─────────────────────────────────────────────────────────────────

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
    result = [50.0] * len(closes)
    if len(closes) <= period:
        return result
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        idx = i - period
        ag = (ag * (period - 1) + gains[idx]) / period
        al = (al * (period - 1) + losses[idx]) / period
        result[i] = 100.0 if al == 0 else 100 - (100 / (1 + ag / al))
    return result


def _atr(highs: list[float], lows: list[float], closes: list[float],
         period: int = ATR_PERIOD) -> list[float]:
    result = [0.0] * len(closes)
    if len(closes) < period + 1:
        return result
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    if len(trs) < period:
        return result
    val = sum(trs[:period]) / period
    result[period] = val
    for i in range(period + 1, len(closes)):
        val = (val * (period - 1) + trs[i - 1]) / period
        result[i] = val
    return result


# ── Signal ─────────────────────────────────────────────────────────────────────

def _compute(candles: list) -> dict:
    need = max(22, ATR_PERIOD + 2)
    if len(candles) < need:
        return {"error": f"محتاج {need} شمعة على الأقل"}

    opens   = [float(c[1]) for c in candles]
    highs   = [float(c[2]) for c in candles]
    lows    = [float(c[3]) for c in candles]
    closes  = [float(c[4]) for c in candles]

    ema9_v  = _ema(closes, 9)
    ema21_v = _ema(closes, 21)
    rsi_v   = _rsi(closes)
    atr_v   = _atr(highs, lows, closes)

    i = len(candles) - 2   # آخر شمعة مكتملة

    e9      = ema9_v[i]
    e21     = ema21_v[i]
    rsi     = rsi_v[i]
    atr     = atr_v[i]
    price   = closes[i]
    green   = closes[i] > opens[i]
    atr_pct = atr / price if price > 0 else 0.0

    trend_up   = e9 > e21 and e21 > 0
    trend_down = e9 < e21

    buy_signal = (
        trend_up
        and RSI_LOW <= rsi <= RSI_HIGH
        and green
        and atr_pct >= MIN_ATR_PCT
    )
    sell_signal = trend_down or rsi > RSI_EXIT

    return {
        "close": price, "ema9": e9, "ema21": e21,
        "rsi": rsi, "atr": atr, "atr_pct": atr_pct * 100,
        "green": green, "trend_up": trend_up,
        "buy_signal": buy_signal, "sell_signal": sell_signal,
        "error": None,
    }


# ── Trailing ───────────────────────────────────────────────────────────────────

def _update_trailing(state: SmartScalpState, price: float, atr: float) -> None:
    if price > state.trail_high:
        state.trail_high    = price
        state.trailing_stop = price - atr * TRAIL_ATR_MULT


# ── Core loop ──────────────────────────────────────────────────────────────────

async def _loop(client, state: SmartScalpState) -> None:
    sym = state.symbol
    log.info("SmartScalp started: %s tf=%s paper=%s", sym, state.timeframe, state.paper)

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_S)
            state.last_check_ts = time.monotonic()

            raw = await client.fetch_ohlcv(sym, state.timeframe, CANDLE_LIMIT)
            await asyncio.sleep(ORDER_SLEEP_SECONDS)

            sig = _compute(raw)
            if sig.get("error"):
                log.warning("SmartScalp %s: %s", sym, sig["error"])
                continue

            price = sig["close"]
            atr   = sig["atr"]

            # ── خروج ──────────────────────────────────────────────────────────
            if state.in_position:
                _update_trailing(state, price, atr)
                pnl_pct     = (price - state.entry_price) / state.entry_price * 100
                exit_reason = None

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

                    log.info("EXIT %s @ %.6f %s pnl=%.4f paper=%s",
                             sym, price, exit_reason, pnl, state.paper)

                    if not state.paper:
                        await client.market_sell_qty(sym, state.position_qty)

                    wr = state.win_count / state.trade_count * 100 if state.trade_count else 0
                    if _notify_exit:
                        await _notify_exit(
                            symbol=sym, price=price, pnl=pnl,
                            reason=exit_reason, paper=state.paper,
                            total_pnl=state.realized_pnl,
                            trade_count=state.trade_count,
                            win_rate=wr,
                        )
                    continue

            # ── Cooldown ───────────────────────────────────────────────────────
            if not state.in_position:
                state.bars_since_exit += 1

            # ── دخول ──────────────────────────────────────────────────────────
            if (not state.in_position
                    and state.bars_since_exit >= MIN_BARS_COOLDOWN
                    and sig["buy_signal"]):

                qty = state.capital_usdt / price
                tp  = price + atr * TP_ATR_MULT
                sl  = price - atr * SL_ATR_MULT

                state.in_position    = True
                state.entry_price    = price
                state.position_qty   = qty
                state.tp_price       = tp
                state.sl_price       = sl
                state.trail_high     = price
                state.trailing_stop  = price - atr * TRAIL_ATR_MULT
                state.last_signal    = "BUY"

                log.info("ENTRY %s @ %.6f qty=%.6f rsi=%.1f tp=%.6f sl=%.6f paper=%s",
                         sym, price, qty, sig["rsi"], tp, sl, state.paper)

                if not state.paper:
                    await client.market_buy(sym, qty, price=price)

                if _notify_entry:
                    tp_pct = (tp - price) / price * 100
                    sl_pct = (price - sl) / price * 100
                    await _notify_entry(
                        symbol=sym, price=price, qty=qty,
                        capital=state.capital_usdt,
                        rsi=sig["rsi"], atr=atr,
                        tp_price=tp, sl_price=sl,
                        tp_pct=tp_pct, sl_pct=sl_pct,
                        paper=state.paper,
                        timeframe=state.timeframe,
                    )

        except asyncio.CancelledError:
            log.info("SmartScalp cancelled: %s", sym)
            return
        except Exception as exc:
            log.error("SmartScalp error %s: %s", sym, exc)
            if _notify_error:
                await _notify_error(symbol=sym, error=str(exc))
            await asyncio.sleep(30)


# ── Public API ─────────────────────────────────────────────────────────────────

async def start(
    client,
    symbol: str,
    capital_usdt: float,
    timeframe: str = "5m",
    paper: bool = True,
) -> SmartScalpState:
    if symbol in _states:
        raise ValueError(f"الصفقة على {symbol} شغّالة بالفعل")
    state = SmartScalpState(
        symbol=symbol,
        capital_usdt=capital_usdt,
        timeframe=timeframe,
        paper=paper,
    )
    _states[symbol] = state
    _tasks[symbol]  = asyncio.create_task(_loop(client, state))
    return state


def stop(symbol: str) -> None:
    task = _tasks.pop(symbol, None)
    if task and not task.done():
        task.cancel()
    _states.pop(symbol, None)


def status(symbol: str) -> dict | None:
    s = _states.get(symbol)
    if not s:
        return None
    wr = s.win_count / s.trade_count * 100 if s.trade_count else 0.0
    return {
        "symbol":        s.symbol,
        "timeframe":     s.timeframe,
        "paper":         s.paper,
        "capital":       s.capital_usdt,
        "in_position":   s.in_position,
        "entry_price":   s.entry_price,
        "tp_price":      s.tp_price,
        "sl_price":      s.sl_price,
        "trailing_stop": s.trailing_stop,
        "realized_pnl":  s.realized_pnl,
        "trade_count":   s.trade_count,
        "win_rate_pct":  wr,
        "last_signal":   s.last_signal,
        "bars_since_exit": s.bars_since_exit,
    }


def active_symbols() -> list[str]:
    return list(_states.keys())
