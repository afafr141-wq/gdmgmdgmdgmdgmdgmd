"""
MEXC REST client built on top of ccxt.
Handles precision, rate-limit pauses, and order lifecycle.
"""
import asyncio
import logging
import math
from typing import Optional

import ccxt.async_support as ccxt

from config.settings import MEXC_API_KEY, MEXC_API_SECRET, ORDER_SLEEP_SECONDS

logger = logging.getLogger(__name__)


class MexcClient:
    def __init__(self) -> None:
        self._exchange = ccxt.mexc(
            {
                "apiKey": MEXC_API_KEY,
                "secret": MEXC_API_SECRET,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        self._markets: dict = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def load_markets(self) -> None:
        self._markets = await self._exchange.load_markets()
        logger.info("MEXC markets loaded (%d symbols)", len(self._markets))

    async def close(self) -> None:
        await self._exchange.close()

    # ── Market data ────────────────────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> dict:
        return await self._exchange.fetch_ticker(symbol)

    async def get_current_price(self, symbol: str) -> float:
        ticker = await self.get_ticker(symbol)
        return float(ticker["last"])

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 50) -> list:
        """Return OHLCV candles as list of [ts, o, h, l, c, v]."""
        return await self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

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
        """Cancel every open order for symbol. Returns count cancelled."""
        try:
            open_orders = await self._exchange.fetch_open_orders(symbol)
        except ccxt.BaseError as exc:
            logger.error("fetch_open_orders failed: %s", exc)
            return 0
        count = 0
        for order in open_orders:
            if await self.cancel_order(symbol, order["id"]):
                count += 1
        logger.info("Cancelled %d orders for %s", count, symbol)
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
