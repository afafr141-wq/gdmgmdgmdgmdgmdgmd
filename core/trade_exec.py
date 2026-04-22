"""
Trade execution and Virtual Stop-Loss watcher.

Responsibilities:
- Sign and send REST requests to MEXC (HMAC-SHA256).
- Fetch exchange info to populate precision cache.
- Place limit buy / limit sell orders.
- Execute market sell for emergency exits.
- Run the Virtual Stop-Loss asyncio task that monitors live price and
  triggers an emergency market sell when price drops to entry * stop_loss_pct.

State persistence is handled exclusively via utils.db_manager (Supabase).
No local file I/O occurs in this module.
"""

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Callable, Coroutine
from urllib.parse import urlencode

import aiohttp

from config.settings import (
    MEXC_API_KEY,
    MEXC_BASE_URL,
    MEXC_SECRET_KEY,
    get_precision,
    set_precision,
)

logger = logging.getLogger(__name__)

TelegramNotifier = Callable[[str], Coroutine[Any, Any, None]]


# ── HMAC signing ───────────────────────────────────────────────────────────────

def _sign(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(
        MEXC_SECRET_KEY.encode(), query.encode(), hashlib.sha256
    ).hexdigest()


def _headers() -> dict:
    return {"X-MEXC-APIKEY": MEXC_API_KEY, "Content-Type": "application/json"}


def _timestamp() -> int:
    return int(time.time() * 1000)


# ── Generic REST client ────────────────────────────────────────────────────────

async def _request(
    method: str,
    path: str,
    params: dict | None = None,
    signed: bool = False,
) -> dict:
    """Async REST call to MEXC. Raises RuntimeError on non-2xx responses."""
    params = params or {}
    if signed:
        params["timestamp"] = _timestamp()
        params["signature"] = _sign(params)

    url = MEXC_BASE_URL + path
    async with aiohttp.ClientSession() as session:
        if method == "GET":
            async with session.get(url, params=params, headers=_headers()) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    raise RuntimeError(f"MEXC GET {path} → {resp.status}: {data}")
                return data
        elif method == "POST":
            async with session.post(url, params=params, headers=_headers()) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    raise RuntimeError(f"MEXC POST {path} → {resp.status}: {data}")
                return data
        elif method == "DELETE":
            async with session.delete(url, params=params, headers=_headers()) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    raise RuntimeError(f"MEXC DELETE {path} → {resp.status}: {data}")
                return data
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")


# ── Exchange info ──────────────────────────────────────────────────────────────

async def fetch_precision(symbol: str) -> dict:
    """
    Query MEXC exchangeInfo for the given symbol and populate the
    in-memory precision cache. Returns the precision dict.
    """
    data = await _request("GET", "/api/v3/exchangeInfo", {"symbol": symbol.upper()})
    symbols = data.get("symbols", [])
    if not symbols:
        raise RuntimeError(f"Symbol {symbol} not found in exchangeInfo")

    info = symbols[0]
    tick_size: str = "0.01"
    price_precision: int = info.get("quotePrecision", 8)
    qty_precision: int = info.get("baseAssetPrecision", 8)

    for f in info.get("filters", []):
        if f.get("filterType") == "PRICE_FILTER":
            tick_size = f.get("tickSize", tick_size)
            if "." in tick_size:
                price_precision = len(tick_size.rstrip("0").split(".")[1])
            break

    set_precision(symbol.upper(), price_precision, qty_precision, tick_size)
    logger.info("Precision for %s: price=%d qty=%d tick=%s",
                symbol, price_precision, qty_precision, tick_size)
    return get_precision(symbol.upper())


# ── Order placement ────────────────────────────────────────────────────────────

async def place_limit_buy(symbol: str, price: float, quote_amount: float) -> dict:
    """Place a GTC limit buy. qty = quote_amount / price."""
    prec = get_precision(symbol)
    qty = round(quote_amount / price, prec["qty_precision"])
    price_str = f"{price:.{prec['price_precision']}f}"
    qty_str = f"{qty:.{prec['qty_precision']}f}"

    params = {
        "symbol": symbol.upper(),
        "side": "BUY",
        "type": "LIMIT",
        "quantity": qty_str,
        "price": price_str,
        "timeInForce": "GTC",
        "recvWindow": 5000,
    }
    logger.info("Placing LIMIT BUY %s qty=%s @ %s", symbol, qty_str, price_str)
    resp = await _request("POST", "/api/v3/order", params, signed=True)
    logger.info("Limit buy placed: orderId=%s", resp.get("orderId"))
    return resp


async def place_limit_sell(symbol: str, price: float, qty: float) -> dict:
    """Place a GTC limit sell."""
    prec = get_precision(symbol)
    price_str = f"{price:.{prec['price_precision']}f}"
    qty_str = f"{qty:.{prec['qty_precision']}f}"

    params = {
        "symbol": symbol.upper(),
        "side": "SELL",
        "type": "LIMIT",
        "quantity": qty_str,
        "price": price_str,
        "timeInForce": "GTC",
        "recvWindow": 5000,
    }
    logger.info("Placing LIMIT SELL %s qty=%s @ %s", symbol, qty_str, price_str)
    resp = await _request("POST", "/api/v3/order", params, signed=True)
    logger.info("Limit sell placed: orderId=%s", resp.get("orderId"))
    return resp


async def market_sell(symbol: str, qty: float) -> dict:
    """Execute an immediate MARKET SELL — used for emergency stop-loss exits."""
    prec = get_precision(symbol)
    qty_str = f"{qty:.{prec['qty_precision']}f}"

    params = {
        "symbol": symbol.upper(),
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty_str,
        "recvWindow": 5000,
    }
    logger.warning("MARKET SELL %s qty=%s (emergency exit)", symbol, qty_str)
    resp = await _request("POST", "/api/v3/order", params, signed=True)
    logger.warning("Market sell executed: orderId=%s", resp.get("orderId"))
    return resp


async def cancel_order(symbol: str, order_id: str) -> dict:
    """Cancel an open order by ID."""
    params = {
        "symbol": symbol.upper(),
        "orderId": order_id,
        "recvWindow": 5000,
    }
    logger.info("Cancelling order %s on %s", order_id, symbol)
    return await _request("DELETE", "/api/v3/order", params, signed=True)


async def fetch_klines(
    symbol: str,
    interval: str,
    limit: int = 100,
) -> list[dict]:
    """
    Fetch OHLCV candlestick data from MEXC REST API.

    interval examples: "1m", "5m", "15m", "30m", "1h", "4h", "1d"
    Returns a list of dicts with keys: open_time, open, high, low, close, volume.
    Sorted oldest → newest.
    """
    params = {
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": limit,
    }
    raw = await _request("GET", "/api/v3/klines", params)
    # MEXC klines format: [open_time, open, high, low, close, volume, ...]
    candles = []
    for row in raw:
        try:
            candles.append({
                "open_time": int(row[0]),
                "open":      float(row[1]),
                "high":      float(row[2]),
                "low":       float(row[3]),
                "close":     float(row[4]),
                "volume":    float(row[5]),
            })
        except (IndexError, ValueError, TypeError):
            continue
    logger.debug("Fetched %d klines for %s [%s]", len(candles), symbol, interval)
    return candles


async def get_order(symbol: str, order_id: str) -> dict:
    """Fetch the current status of an order."""
    params = {
        "symbol": symbol.upper(),
        "orderId": order_id,
        "recvWindow": 5000,
    }
    return await _request("GET", "/api/v3/order", params, signed=True)


# ── Virtual Stop-Loss watcher ──────────────────────────────────────────────────

class VirtualStopLossWatcher:
    """
    Monitors live price and triggers an emergency market sell when
    price drops to entry_price * stop_loss_pct.

    State is persisted to Supabase (via db_manager) on trigger so the
    closed status survives a restart.

    Usage:
        watcher = VirtualStopLossWatcher(...)
        asyncio.create_task(watcher.run())
        watcher.update_price(price)   # called from WS ticker handler
        watcher.cancel()              # called when trade closes normally
    """

    def __init__(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        exit_order_id: str | None,
        stop_loss_pct: float,
        notifier: TelegramNotifier,
        on_triggered: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.symbol = symbol
        self.entry_price = entry_price
        self.qty = qty
        self.exit_order_id = exit_order_id
        self.stop_loss_price = round(entry_price * stop_loss_pct, 8)
        self.notifier = notifier
        self.on_triggered = on_triggered

        self._current_price: float = 0.0
        self._price_event = asyncio.Event()
        self._cancelled = False
        self._triggered = False

        logger.info(
            "VirtualSL armed: entry=%.8f sl=%.8f (%.1f%%) qty=%.8f",
            entry_price, self.stop_loss_price, (1 - stop_loss_pct) * 100, qty,
        )

    def update_price(self, price: float) -> None:
        """Feed a new price tick from the WS ticker handler."""
        self._current_price = price
        self._price_event.set()

    def cancel(self) -> None:
        """Signal the watcher to stop (trade closed normally)."""
        self._cancelled = True
        self._price_event.set()

    async def run(self) -> None:
        """Main loop: wait for price ticks, check SL level."""
        logger.info("VirtualSL watcher started for %s", self.symbol)
        while not self._cancelled and not self._triggered:
            self._price_event.clear()
            await self._price_event.wait()

            if self._cancelled:
                break

            price = self._current_price
            if price <= 0:
                continue

            if price <= self.stop_loss_price:
                await self._trigger_emergency_exit(price)
                break

        logger.info("VirtualSL watcher stopped for %s", self.symbol)

    async def _trigger_emergency_exit(self, current_price: float) -> None:
        """
        Emergency exit sequence:
        1. Cancel pending TP limit order.
        2. Market sell.
        3. Reset Supabase state to idle.
        4. Notify Telegram.
        5. Run on_triggered callback.
        """
        self._triggered = True
        logger.warning(
            "STOP-LOSS TRIGGERED: price=%.8f ≤ sl=%.8f",
            current_price, self.stop_loss_price,
        )

        # 1. Cancel take-profit order
        if self.exit_order_id:
            try:
                await cancel_order(self.symbol, self.exit_order_id)
                logger.info("Exit order %s cancelled", self.exit_order_id)
            except Exception as exc:
                logger.error("Failed to cancel exit order: %s", exc)

        # 2. Market sell
        sell_order_id = "FAILED"
        try:
            resp = await market_sell(self.symbol, self.qty)
            sell_order_id = resp.get("orderId", "unknown")
        except Exception as exc:
            logger.error("Market sell failed: %s", exc)

        # 3. Reset Supabase state
        try:
            from utils.db_manager import get_db
            await get_db().reset_state()
        except Exception as exc:
            logger.error("Failed to reset Supabase state after SL trigger: %s", exc)

        # 4. Telegram alert
        msg = (
            "🚨 *EMERGENCY EXIT — Virtual Stop-Loss Triggered*\n\n"
            f"Symbol: `{self.symbol}`\n"
            f"Entry: `{self.entry_price:.8f}`\n"
            f"SL Level: `{self.stop_loss_price:.8f}`\n"
            f"Exit Price (approx): `{current_price:.8f}`\n"
            f"Market Sell Order: `{sell_order_id}`\n"
            f"Qty: `{self.qty:.8f}`"
        )
        try:
            await self.notifier(msg)
        except Exception as exc:
            logger.error("Telegram notification failed: %s", exc)

        # 5. Post-trigger callback (resets in-memory bot state)
        if self.on_triggered:
            try:
                await self.on_triggered()
            except Exception as exc:
                logger.error("on_triggered callback error: %s", exc)


# ── Order fill poller ──────────────────────────────────────────────────────────

async def poll_order_fill(
    symbol: str,
    order_id: str,
    on_filled: Callable[[dict], Coroutine[Any, Any, None]],
    poll_interval: float = 2.0,
    timeout: float = 3600.0,
) -> None:
    """
    Poll an order's status every poll_interval seconds until FILLED or timeout.
    Calls on_filled(order_data) when status becomes "FILLED".
    """
    deadline = time.time() + timeout
    logger.info("Polling fill for order %s on %s", order_id, symbol)

    while time.time() < deadline:
        try:
            order = await get_order(symbol, order_id)
            status = order.get("status", "")
            logger.debug("Order %s status: %s", order_id, status)

            if status == "FILLED":
                logger.info("Order %s FILLED", order_id)
                await on_filled(order)
                return
            elif status in ("CANCELED", "REJECTED", "EXPIRED"):
                logger.warning(
                    "Order %s ended with status %s — stopping poll", order_id, status
                )
                return
        except Exception as exc:
            logger.error("Error polling order %s: %s", order_id, exc)

        await asyncio.sleep(poll_interval)

    logger.warning("Order %s poll timed out after %.0fs", order_id, timeout)
