"""
MEXC Spot API client.
Handles authentication (HMAC-SHA256) and core endpoints needed
for the Smart Portfolio bot.

Auth: API Key + Secret Key only (no passphrase).
Signature: HMAC-SHA256 over the query string, appended as &signature=...
Header: X-MEXC-APIKEY
"""

import hashlib
import hmac
import time
import os
import requests
from typing import Optional
from urllib.parse import urlencode


BASE_URL = "https://api.mexc.com"


class MEXCClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("MEXC_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("MEXC_API_SECRET", "")
        self.session = requests.Session()
        self.session.headers.update({
            "X-MEXC-APIKEY": self.api_key,
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, params: dict) -> str:
        """Return HMAC-SHA256 hex signature over the URL-encoded params."""
        query = urlencode(params)
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

    def _signed_params(self, params: Optional[dict] = None) -> dict:
        """Merge params with timestamp and append signature."""
        p = dict(params or {})
        p["timestamp"] = self._timestamp()
        p["signature"] = self._sign(p)
        return p

    # ------------------------------------------------------------------
    # HTTP wrappers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None, signed: bool = False) -> dict:
        p = self._signed_params(params) if signed else (params or {})
        resp = self.session.get(BASE_URL + path, params=p, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, params: Optional[dict] = None) -> dict:
        """MEXC signed POST sends params as query string (not JSON body)."""
        p = self._signed_params(params)
        resp = self.session.post(BASE_URL + path, params=p, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str, params: Optional[dict] = None) -> dict:
        p = self._signed_params(params)
        resp = self.session.delete(BASE_URL + path, params=p, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self) -> dict:
        """Return full account info including balances."""
        return self._get("/api/v3/account", signed=True)

    def get_spot_assets(self) -> list:
        """Return list of balances with non-zero free or locked amount."""
        data = self.get_account()
        return data.get("balances", [])

    def get_all_balances(self) -> dict[str, float]:
        """Return a symbol→free-balance map built from a single get_account() call.

        Use this instead of calling get_asset_balance() per asset to avoid
        making one API request per asset (N+1 pattern).
        """
        return {
            a["asset"].upper(): float(a.get("free", 0))
            for a in self.get_spot_assets()
        }

    def get_asset_balance(self, symbol: str) -> float:
        """Return free (available) balance for a single asset (e.g. 'BTC').

        Makes one full get_account() call. When fetching multiple assets,
        prefer get_all_balances() to avoid N+1 API calls.
        """
        sym = symbol.upper()
        for a in self.get_spot_assets():
            if a.get("asset", "").upper() == sym:
                return float(a.get("free", 0))
        return 0.0

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_ticker(self, symbol: str) -> dict:
        """
        symbol: e.g. 'BTCUSDT'
        Returns 24hr ticker dict with 'lastPrice'.
        """
        data = self._get("/api/v3/ticker/24hr", {"symbol": symbol})
        if isinstance(data, list):
            if data:
                return data[0]
            raise ValueError(f"No ticker found for {symbol}")
        return data

    def get_price(self, symbol: str) -> float:
        """Return last traded price for symbol (e.g. 'BTCUSDT')."""
        data = self._get("/api/v3/ticker/price", {"symbol": symbol})
        return float(data.get("price", 0))

    def get_symbol_info(self, symbol: str) -> dict:
        """Return trading rules for a spot symbol."""
        data = self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = data.get("symbols", [])
        if symbols:
            return symbols[0]
        raise ValueError(f"Symbol info not found for {symbol}")

    def get_lot_size_precision(self, symbol: str) -> int:
        """Return the number of decimal places allowed for base-asset quantity.

        Reads LOT_SIZE.stepSize from exchangeInfo. Falls back to 8 if the
        filter is missing (safe default that MEXC will round server-side).
        """
        try:
            info = self.get_symbol_info(symbol)
            for f in info.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = f.get("stepSize", "0.00000001")
                    # stepSize like "0.01" → 2 decimal places
                    step_str = step.rstrip("0") or "1"
                    if "." in step_str:
                        return len(step_str.split(".")[1])
                    return 0
        except Exception as e:
            log.warning("get_lot_size_precision(%s) failed: %s — defaulting to 8", symbol, e)
        return 8

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_market_buy(self, symbol: str, quote_size: float) -> dict:
        """
        Market buy using quote currency (USDT).
        quote_size: amount in USDT to spend.
        MEXC uses quoteOrderQty for market buy by quote amount.
        """
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": str(round(quote_size, 6)),
        }
        return self._post("/api/v3/order", params)

    def place_market_sell(self, symbol: str, base_size: float,
                          qty_precision: int | None = None) -> dict:
        """
        Market sell using base currency amount.
        base_size: amount of the base asset to sell.
        qty_precision: decimal places for quantity (from LOT_SIZE.stepSize).
                       If None, reads it from exchangeInfo automatically.
        """
        if qty_precision is None:
            qty_precision = self.get_lot_size_precision(symbol)
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": str(round(base_size, qty_precision)),
        }
        return self._post("/api/v3/order", params)

    def place_stop_loss_limit_order(
        self,
        symbol:    str,
        quantity:  float,
        stop_price: float,
        qty_precision: int = 8,
    ) -> dict:
        """Place a STOP_LOSS_LIMIT sell order.

        stop_price: the trigger price at which the limit sell activates.
        The limit price is set 0.1% below stop_price to ensure fill.
        quantity: base asset amount to sell.
        """
        limit_price = round(stop_price * 0.999, qty_precision)
        params = {
            "symbol":       symbol,
            "side":         "SELL",
            "type":         "STOP_LOSS_LIMIT",
            "quantity":     str(round(quantity, qty_precision)),
            "price":        str(limit_price),
            "stopPrice":    str(round(stop_price, qty_precision)),
            "timeInForce":  "GTC",
        }
        return self._post("/api/v3/order", params)

    def get_order(self, symbol: str, order_id: str) -> dict:
        """Fetch order details by orderId."""
        return self._get(
            "/api/v3/order",
            {"symbol": symbol, "orderId": order_id},
            signed=True,
        )

    def get_all_usdt_symbols(self) -> list[str]:
        """Return all active USDT spot pair symbols from MEXC exchangeInfo.

        Filters for status == '1' (trading enabled). Used by market-wide scanners
        to avoid per-symbol exchangeInfo calls.
        """
        data = self._get("/api/v3/exchangeInfo")
        return [
            s["symbol"]
            for s in data.get("symbols", [])
            if s["symbol"].endswith("USDT") and s.get("status") == "1"
        ]

    # MEXC supported kline intervals (verified)
    _INTERVAL_MAP = {
        "1m":  "1m",
        "5m":  "5m",
        "15m": "15m",
        "30m": "30m",
        "1h":  "60m",   # MEXC uses 60m not 1h
        "2h":  "60m",   # fallback to 60m
        "4h":  "4h",
        "8h":  "8h",
        "1d":  "1d",
    }

    def get_klines(self, symbol: str, interval: str = "15m", limit: int = 100) -> list[dict]:
        """Return OHLCV candles for symbol.

        interval: 1m, 5m, 15m, 30m, 1h, 4h, 8h, 1d
        Returns list of dicts with keys: open_time, open, high, low, close, volume.
        Retries up to 3 times with exponential backoff on HTTP 400/429 errors.
        """
        mexc_interval = self._INTERVAL_MAP.get(interval, interval)
        params = {
            "symbol":   str(symbol).upper().replace("/", ""),
            "interval": str(mexc_interval),
            "limit":    int(limit),
        }
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                raw = self._get("/api/v3/klines", params)
                candles = []
                for c in raw:
                    candles.append({
                        "open_time": int(c[0]),
                        "open":      float(c[1]),
                        "high":      float(c[2]),
                        "low":       float(c[3]),
                        "close":     float(c[4]),
                        "volume":    float(c[5]),
                    })
                return candles
            except Exception as exc:
                last_exc = exc
                import time as _time
                wait = 2 ** attempt  # 1s, 2s, 4s
                log.warning(
                    "get_klines %s %s attempt %d/%d failed: %s — retry in %ds",
                    symbol, interval, attempt + 1, 3, exc, wait,
                )
                _time.sleep(wait)
        raise last_exc
