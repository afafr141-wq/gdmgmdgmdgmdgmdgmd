"""
MEXC REST client — direct aiohttp for OHLCV + batch cancel (بدل ccxt).

التحسينات:
  - fetch_ohlcv  → مباشر لـ MEXC REST (أسرع 3-5×، بدون ccxt overhead)
  - get_current_price → /api/v3/ticker/price مباشرة
  - market_buy   → price اختياري لتجنب fetch_ticker الإضافي
  - cancel_all_orders → DELETE /api/v3/openOrders مرة واحدة بدل واحد-واحد
  - ORDER_SLEEP_SECONDS → 0.05 بدل 0.25
"""
import asyncio
import hashlib
import hmac
import logging
import math
import time
import urllib.parse
from typing import Optional

import aiohttp
import ccxt.async_support as ccxt

from config.settings import MEXC_API_KEY, MEXC_API_SECRET, ORDER_SLEEP_SECONDS

logger = logging.getLogger(__name__)

MEXC_REST = "https://api.mexc.com"

_TF_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d",
}


def _sign(params: dict, secret: str) -> str:
    qs = urllib.parse.urlencode(params)
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()


class MexcClient:
    def __init__(self) -> None:
        self._exchange = ccxt.mexc(
            {
                "apiKey": MEXC_API_KEY,
                "secret": MEXC_API_SECRET,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "spot",
                    "adjustForTimeDifference": True,
                    "recvWindow": 10000,
                },
            }
        )
        self._markets: dict = {}
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def load_markets(self) -> None:
        self._markets = await self._exchange.load_markets()
        logger.info("MEXC markets loaded (%d symbols)", len(self._markets))

    async def close(self) -> None:
        await self._exchange.close()
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Market data — direct REST ──────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> dict:
        return await self._exchange.fetch_ticker(symbol)

    async def get_current_price(self, symbol: str) -> float:
        """جلب السعر مباشرة بدون fetch_ticker الثقيل."""
        mexc_symbol = symbol.replace("/", "")
        session = await self._http()
        async with session.get(
            f"{MEXC_REST}/api/v3/ticker/price",
            params={"symbol": mexc_symbol},
        ) as resp:
            resp.raise_for_status()
            return float((await resp.json())["price"])

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "3m", limit: int = 100) -> list:
        """
        جلب الشموع مباشرة من MEXC REST بدل ccxt — أسرع بـ 3-5×.
        يُرجع: [[ts, open, high, low, close, volume], ...]
        """
        mexc_symbol = symbol.replace("/", "")
        interval = _TF_MAP.get(timeframe, timeframe)
        session = await self._http()
        async with session.get(
            f"{MEXC_REST}/api/v3/klines",
            params={"symbol": mexc_symbol, "interval": interval, "limit": limit},
        ) as resp:
            resp.raise_for_status()
            raw = await resp.json()
        return [
            [int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
            for c in raw
        ]

    async def get_balance(self, currency: str) -> float:
        balance = await self._exchange.fetch_balance()
        return float(balance.get("free", {}).get(currency, 0.0))

    # ── Precision helpers ──────────────────────────────────────────────────────

    def _market(self, symbol: str) -> dict:
        if symbol not in self._markets:
            raise ValueError(f"Unknown symbol: {symbol}")
        return self._markets[symbol]

    def price_precision(self, symbol: str) -> int:
        """Number of decimal places for price."""
        m = self._market(symbol)
        precision = m.get("precision", {}).get("price", 8)
        if isinstance(precision, float):
            return max(0, -int(math.floor(math.log10(precision))))
        return int(precision)

    def amount_precision(self, symbol: str) -> int:
        """Number of decimal places for quantity."""
        m = self._market(symbol)
        precision = m.get("precision", {}).get("amount", 8)
        if isinstance(precision, float):
            return max(0, -int(math.floor(math.log10(precision))))
        return int(precision)

    def min_amount(self, symbol: str) -> float:
        m = self._market(symbol)
        return float(m.get("limits", {}).get("amount", {}).get("min", 0) or 0)

    def min_cost(self, symbol: str) -> float:
        m = self._market(symbol)
        return float(m.get("limits", {}).get("cost", {}).get("min", 1) or 1)

    def round_price(self, symbol: str, price: float) -> float:
        dp = self.price_precision(symbol)
        return round(price, dp)

    def round_amount(self, symbol: str, amount: float) -> float:
        dp = self.amount_precision(symbol)
        return round(amount, dp)

    # ── Order placement ────────────────────────────────────────────────────────

    async def place_limit_buy(self, symbol: str, price: float, qty: float) -> Optional[dict]:
        price = self.round_price(symbol, price)
        qty = self.round_amount(symbol, qty)
        if qty < self.min_amount(symbol):
            logger.warning("BUY qty %.8f below min for %s – skipped", qty, symbol)
            return None
        if price * qty < self.min_cost(symbol):
            logger.warning("BUY cost %.4f below min for %s – skipped", price * qty, symbol)
            return None
        try:
            order = await self._exchange.create_limit_buy_order(symbol, qty, price)
            logger.info("LIMIT BUY %s qty=%.6f @ %.6f id=%s", symbol, qty, price, order["id"])
            await asyncio.sleep(ORDER_SLEEP_SECONDS)
            return order
        except ccxt.BaseError as exc:
            logger.error("place_limit_buy failed: %s", exc)
            return None

    async def place_limit_sell(self, symbol: str, price: float, qty: float) -> Optional[dict]:
        price = self.round_price(symbol, price)
        qty = self.round_amount(symbol, qty)
        if qty < self.min_amount(symbol):
            logger.warning("SELL qty %.8f below min for %s – skipped", qty, symbol)
            return None
        try:
            order = await self._exchange.create_limit_sell_order(symbol, qty, price)
            logger.info("LIMIT SELL %s qty=%.6f @ %.6f id=%s", symbol, qty, price, order["id"])
            await asyncio.sleep(ORDER_SLEEP_SECONDS)
            return order
        except ccxt.BaseError as exc:
            logger.error("place_limit_sell failed: %s", exc)
            return None

    async def market_buy(self, symbol: str, qty: float,
                         price: Optional[float] = None) -> Optional[dict]:
        """
        شراء بالسعر الحالي.
        price: مرره من بيانات الشمعة لتجنب fetch_ticker إضافي (توفير رحلة API).
        """
        qty = self.round_amount(symbol, qty)
        if qty <= 0 or qty < self.min_amount(symbol):
            logger.warning("market_buy: qty %.8f below min for %s – skipped", qty, symbol)
            return None
        try:
            if price is None:
                price = await self.get_current_price(symbol)
            cost = round(qty * price, 4)
            order = await self._exchange.create_market_buy_order(
                symbol, cost,
                params={"createMarketBuyOrderRequiresPrice": False},
            )
            logger.info("MARKET BUY %s cost=%.4f USDT id=%s", symbol, cost, order["id"])
            await asyncio.sleep(ORDER_SLEEP_SECONDS)
            return order
        except ccxt.BaseError as exc:
            logger.error("market_buy failed: %s", exc)
            return None

    async def market_sell_qty(self, symbol: str, qty: float) -> Optional[dict]:
        """Sell a specific quantity of base currency at market price."""
        qty = self.round_amount(symbol, qty)
        if qty <= 0 or qty < self.min_amount(symbol):
            logger.warning("market_sell_qty: qty %.8f below min for %s – skipped", qty, symbol)
            return None
        try:
            order = await self._exchange.create_market_sell_order(symbol, qty)
            logger.info("MARKET SELL %s qty=%.6f", symbol, qty)
            await asyncio.sleep(ORDER_SLEEP_SECONDS)
            return order
        except ccxt.BaseError as exc:
            logger.error("market_sell_qty failed: %s", exc)
            return None

    async def market_sell_all(self, symbol: str) -> Optional[dict]:
        """Sell entire free balance of the base currency at market price."""
        base = symbol.split("/")[0]
        qty = await self.get_balance(base)
        qty = self.round_amount(symbol, qty)
        if qty <= 0 or qty < self.min_amount(symbol):
            logger.info("market_sell_all: nothing to sell for %s (qty=%.8f)", symbol, qty)
            return None
        try:
            order = await self._exchange.create_market_sell_order(symbol, qty)
            logger.info("MARKET SELL %s qty=%.6f", symbol, qty)
            return order
        except ccxt.BaseError as exc:
            logger.error("market_sell_all failed: %s", exc)
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._exchange.cancel_order(order_id, symbol)
            await asyncio.sleep(ORDER_SLEEP_SECONDS)
            return True
        except ccxt.OrderNotFound:
            return True   # already gone
        except ccxt.BaseError as exc:
            logger.error("cancel_order %s failed: %s", order_id, exc)
            return False

    async def cancel_all_orders(self, symbol: str) -> int:
        """
        إلغاء جميع أوامر الرمز بطلب REST واحد بدل N طلبات متسلسلة.
        يوفر N-1 رحلات شبكة مقارنة بالنسخة السابقة.
        """
        mexc_symbol = symbol.replace("/", "")
        timestamp = int(time.time() * 1000)
        params: dict = {"symbol": mexc_symbol, "timestamp": timestamp, "recvWindow": 5000}
        params["signature"] = _sign(params, MEXC_API_SECRET)
        session = await self._http()
        try:
            async with session.delete(
                f"{MEXC_REST}/api/v3/openOrders",
                params=params,
                headers={"X-MEXC-APIKEY": MEXC_API_KEY},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    count = len(data) if isinstance(data, list) else 1
                    logger.info("Batch-cancelled %d orders for %s", count, symbol)
                    return count
                text = await resp.text()
                logger.warning("cancel_all_orders %s → %d: %s", symbol, resp.status, text[:200])
        except Exception as exc:
            logger.warning("cancel_all_orders REST failed for %s: %s — fallback", symbol, exc)
        # ── الحالة الاحتياطية: واحد-واحد ────────────────────────────────────
        try:
            open_orders = await self._exchange.fetch_open_orders(symbol)
        except ccxt.BaseError as exc:
            logger.error("fetch_open_orders failed: %s", exc)
            return 0
        count = 0
        for order in open_orders:
            if await self.cancel_order(symbol, order["id"]):
                count += 1
        logger.info("Fallback-cancelled %d orders for %s", count, symbol)
        return count

    async def fetch_order(self, symbol: str, order_id: str) -> Optional[dict]:
        try:
            return await self._exchange.fetch_order(order_id, symbol)
        except ccxt.BaseError as exc:
            logger.error("fetch_order %s failed: %s", order_id, exc)
            return None

    async def fetch_open_orders(self, symbol: str) -> list[dict]:
        try:
            return await self._exchange.fetch_open_orders(symbol)
        except ccxt.BaseError as exc:
            logger.error("fetch_open_orders failed: %s", exc)
            return []
