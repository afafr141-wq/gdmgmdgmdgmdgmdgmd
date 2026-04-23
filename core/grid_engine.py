"""
AI Dynamic Grid Engine — mirrors KuCoin AI Plus Bot behaviour.

Logic:
  1. On start: fetch current price, compute ATR from 15m candles.
  2. Derive range (lower/upper) and grid count from ATR + risk profile.
  3. Place alternating limit buy/sell orders across the grid.
  4. Every ATR_REFRESH_SECONDS: recompute ATR; if price escaped the range
     by ≥ rebalance_trigger × ATR, cancel all orders and rebuild.
  5. Poll open orders; when a buy fills → place matching sell one grid up.
     When a sell fills → record PnL, update DB.
"""
import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Optional

from config.settings import (
    ATR_REFRESH_SECONDS,
    CANDLE_TIMEFRAME,
    FILL_POLL_INTERVAL,
    RISK_PROFILES,
)
from core.mexc_client import MexcClient
from utils import db_manager as db

logger = logging.getLogger(__name__)


# ── ATR calculation ────────────────────────────────────────────────────────────

def compute_atr(ohlcv: list, period: int = 14) -> float:
    """Wilder ATR from OHLCV list [[ts,o,h,l,c,v], ...]."""
    if len(ohlcv) < period + 1:
        # Fallback: use average candle range
        ranges = [row[2] - row[3] for row in ohlcv]
        return sum(ranges) / len(ranges) if ranges else 0.0

    true_ranges = []
    for i in range(1, len(ohlcv)):
        high = ohlcv[i][2]
        low = ohlcv[i][3]
        prev_close = ohlcv[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    # Initial ATR = simple average of first `period` TRs
    atr = sum(true_ranges[:period]) / period
    # Wilder smoothing for the rest
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ── Grid parameter derivation ──────────────────────────────────────────────────

@dataclass
class GridParams:
    lower: float
    upper: float
    grid_count: int
    grid_spacing: float
    qty_per_grid: float
    atr: float


def derive_grid_params(
    current_price: float,
    atr: float,
    total_investment: float,
    risk: str,
    client: MexcClient,
    symbol: str,
) -> GridParams:
    """
    Compute grid boundaries and order sizing from ATR + risk profile.
    Mirrors KuCoin AI Plus Bot: wider ATR → wider range → more grids.
    """
    profile = RISK_PROFILES[risk]
    mult = profile["atr_multiplier"]
    min_g = profile["min_grids"]
    max_g = profile["max_grids"]

    lower = current_price - atr * mult
    upper = current_price + atr * mult
    lower = max(lower, current_price * 0.01)  # sanity floor

    # Grid count scales with ATR relative to price (volatility ratio)
    vol_ratio = atr / current_price
    raw_grids = int(min_g + (max_g - min_g) * min(vol_ratio / 0.02, 1.0))
    grid_count = max(min_g, min(raw_grids, max_g))

    grid_spacing = (upper - lower) / grid_count
    # Each grid gets an equal share of the investment
    qty_per_grid = client.round_amount(
        symbol, (total_investment / grid_count) / current_price
    )

    return GridParams(
        lower=client.round_price(symbol, lower),
        upper=client.round_price(symbol, upper),
        grid_count=grid_count,
        grid_spacing=client.round_price(symbol, grid_spacing),
        qty_per_grid=qty_per_grid,
        atr=atr,
    )


# ── Grid state ─────────────────────────────────────────────────────────────────

@dataclass
class GridState:
    symbol: str
    risk: str
    total_investment: float
    params: GridParams
    grid_id: int = 0
    # order_id → {"side": "buy"|"sell", "price": float, "qty": float}
    open_orders: dict = field(default_factory=dict)
    avg_buy_price: float = 0.0
    held_qty: float = 0.0
    realized_pnl: float = 0.0
    sell_count: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    running: bool = True


# ── Engine ─────────────────────────────────────────────────────────────────────

class GridEngine:
    """
    Manages one AI grid per symbol.
    Call start() to launch; call stop() to shut down gracefully.
    """

    def __init__(
        self,
        client: MexcClient,
        notify: Callable[[str], Coroutine],
    ) -> None:
        self._client = client
        self._notify = notify          # async callback → sends Telegram message
        self._grids: dict[str, GridState] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(
        self,
        symbol: str,
        total_investment: float,
        risk: str = "medium",
    ) -> GridState:
        if symbol in self._grids and self._grids[symbol].running:
            raise ValueError(f"Grid already running for {symbol}")

        price = await self._client.get_current_price(symbol)
        atr = await self._fetch_atr(symbol, RISK_PROFILES[risk]["atr_period"])
        params = derive_grid_params(price, atr, total_investment, risk, self._client, symbol)

        state = GridState(
            symbol=symbol,
            risk=risk,
            total_investment=total_investment,
            params=params,
        )
        self._grids[symbol] = state

        # Persist to DB
        grid_id = await db.upsert_grid(
            {
                "symbol": symbol,
                "risk_level": risk,
                "total_investment": total_investment,
                "lower_price": params.lower,
                "upper_price": params.upper,
                "grid_count": params.grid_count,
                "grid_spacing": params.grid_spacing,
                "current_atr": params.atr,
                "is_active": True,
            }
        )
        state.grid_id = grid_id

        await self._place_initial_orders(state)
        self._tasks[symbol] = asyncio.create_task(self._run_loop(state))
        logger.info(
            "Grid started: %s | range %.4f–%.4f | %d grids | ATR=%.4f",
            symbol, params.lower, params.upper, params.grid_count, params.atr,
        )
        return state

    async def stop(self, symbol: str, market_sell: bool = True) -> float:
        """Stop the grid, cancel all orders, optionally market-sell holdings."""
        state = self._grids.get(symbol)
        if not state:
            raise ValueError(f"No active grid for {symbol}")

        state.running = False
        task = self._tasks.pop(symbol, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self._client.cancel_all_orders(symbol)

        sell_value = 0.0
        if market_sell:
            order = await self._client.market_sell_all(symbol)
            if order and order.get("cost"):
                sell_value = float(order["cost"])

        await db.deactivate_grid(symbol)
        await db.save_snapshot(symbol, self._state_snapshot(state))
        del self._grids[symbol]
        logger.info("Grid stopped: %s", symbol)
        return sell_value

    def get_state(self, symbol: str) -> Optional[GridState]:
        return self._grids.get(symbol)

    def active_symbols(self) -> list[str]:
        return [s for s, g in self._grids.items() if g.running]

    # ── Internal loop ──────────────────────────────────────────────────────────

    async def _run_loop(self, state: GridState) -> None:
        last_atr_refresh = asyncio.get_event_loop().time()
        while state.running:
            try:
                now = asyncio.get_event_loop().time()

                # ── ATR refresh ────────────────────────────────────────────────
                if now - last_atr_refresh >= ATR_REFRESH_SECONDS:
                    await self._maybe_rebalance(state)
                    last_atr_refresh = now

                # ── Fill polling ───────────────────────────────────────────────
                await self._poll_fills(state)

                await asyncio.sleep(FILL_POLL_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Grid loop error for %s: %s", state.symbol, exc)
                await asyncio.sleep(30)

    async def _maybe_rebalance(self, state: GridState) -> None:
        """Recompute ATR; rebuild grid if price escaped the range."""
        profile = RISK_PROFILES[state.risk]
        atr = await self._fetch_atr(state.symbol, profile["atr_period"])
        price = await self._client.get_current_price(state.symbol)

        trigger = profile["rebalance_trigger"] * atr
        outside = price < state.params.lower - trigger or price > state.params.upper + trigger

        if outside:
            logger.info(
                "Price %.4f outside range [%.4f, %.4f] by >ATR — rebuilding grid for %s",
                price, state.params.lower, state.params.upper, state.symbol,
            )
            await self._notify(
                f"🔄 *{state.symbol}* — Price escaped range. Rebuilding grid…\n"
                f"Price: `{price:.4f}` | ATR: `{atr:.4f}`"
            )
            await self._rebuild(state, price, atr)
        else:
            # Just update ATR in DB
            state.params.atr = atr
            await db.upsert_grid({"symbol": state.symbol, "current_atr": atr})

    async def _rebuild(self, state: GridState, price: float, atr: float) -> None:
        await self._client.cancel_all_orders(state.symbol)
        state.open_orders.clear()

        params = derive_grid_params(
            price, atr, state.total_investment, state.risk, self._client, state.symbol
        )
        state.params = params

        await db.upsert_grid(
            {
                "symbol": state.symbol,
                "lower_price": params.lower,
                "upper_price": params.upper,
                "grid_count": params.grid_count,
                "grid_spacing": params.grid_spacing,
                "current_atr": params.atr,
            }
        )
        await self._place_initial_orders(state)

    async def _place_initial_orders(self, state: GridState) -> None:
        """
        Place buy orders below current price and sell orders above it.
        Levels are evenly spaced between lower and upper.
        """
        p = state.params
        price = await self._client.get_current_price(state.symbol)
        levels = [
            self._client.round_price(state.symbol, p.lower + i * p.grid_spacing)
            for i in range(p.grid_count + 1)
        ]

        for level in levels:
            if level < price:
                order = await self._client.place_limit_buy(state.symbol, level, p.qty_per_grid)
                if order:
                    state.open_orders[order["id"]] = {
                        "side": "buy",
                        "price": level,
                        "qty": p.qty_per_grid,
                    }
            elif level > price:
                order = await self._client.place_limit_sell(state.symbol, level, p.qty_per_grid)
                if order:
                    state.open_orders[order["id"]] = {
                        "side": "sell",
                        "price": level,
                        "qty": p.qty_per_grid,
                    }

        logger.info(
            "Placed %d orders for %s (range %.4f–%.4f)",
            len(state.open_orders), state.symbol, p.lower, p.upper,
        )

    async def _poll_fills(self, state: GridState) -> None:
        """Check each tracked order; handle fills."""
        if not state.open_orders:
            return

        filled_ids = []
        for order_id, meta in list(state.open_orders.items()):
            order = await self._client.fetch_order(state.symbol, order_id)
            if not order:
                continue
            status = order.get("status", "")
            if status == "closed":
                filled_ids.append((order_id, meta, order))
            elif status == "canceled":
                filled_ids.append((order_id, meta, None))

        for order_id, meta, order in filled_ids:
            del state.open_orders[order_id]
            if order is None:
                continue
            await self._handle_fill(state, meta, order)

    async def _handle_fill(self, state: GridState, meta: dict, order: dict) -> None:
        side = meta["side"]
        fill_price = float(order.get("average") or order.get("price") or meta["price"])
        qty = float(order.get("filled") or meta["qty"])
        pnl = 0.0

        if side == "buy":
            # Update average buy price (weighted)
            total_cost = state.avg_buy_price * state.held_qty + fill_price * qty
            state.held_qty += qty
            state.avg_buy_price = total_cost / state.held_qty if state.held_qty else 0.0

            # Place matching sell one grid up
            sell_price = self._client.round_price(
                state.symbol, fill_price + state.params.grid_spacing
            )
            sell_order = await self._client.place_limit_sell(state.symbol, sell_price, qty)
            if sell_order:
                state.open_orders[sell_order["id"]] = {
                    "side": "sell",
                    "price": sell_price,
                    "qty": qty,
                }

        else:  # sell
            pnl = (fill_price - state.avg_buy_price) * qty
            state.realized_pnl += pnl
            state.sell_count += 1
            state.held_qty = max(0.0, state.held_qty - qty)

            # Place matching buy one grid down
            buy_price = self._client.round_price(
                state.symbol, fill_price - state.params.grid_spacing
            )
            buy_order = await self._client.place_limit_buy(state.symbol, buy_price, qty)
            if buy_order:
                state.open_orders[buy_order["id"]] = {
                    "side": "buy",
                    "price": buy_price,
                    "qty": qty,
                }

        await db.record_trade(
            symbol=state.symbol,
            side=side,
            price=fill_price,
            qty=qty,
            order_id=order.get("id", ""),
            grid_id=state.grid_id,
            pnl=pnl,
        )
        await db.update_grid_pnl(
            state.symbol,
            state.realized_pnl,
            state.avg_buy_price,
            state.held_qty,
            state.sell_count,
        )

        logger.info(
            "Fill: %s %s qty=%.6f @ %.4f | PnL=%.4f | realized=%.4f",
            side.upper(), state.symbol, qty, fill_price, pnl, state.realized_pnl,
        )

    # ── Profit calculations (exact formulas from spec) ─────────────────────────

    def calc_profit_report(self, state: GridState, current_price: float) -> dict:
        """
        Returns all profit metrics using the exact formulas specified.
        """
        p = state.params
        days_running = max(
            (datetime.now(timezone.utc) - state.started_at).total_seconds() / 86400,
            1 / 1440,  # at least 1 minute to avoid division by zero
        )

        # Grid profit = spacing × investment_per_grid × sell_count
        investment_per_grid = state.total_investment / p.grid_count
        grid_profit = p.grid_spacing * investment_per_grid * state.sell_count

        # Unrealised PnL
        unrealised_pnl = (current_price - state.avg_buy_price) * state.held_qty

        # Total profit
        total_profit = state.realized_pnl + unrealised_pnl

        # APY
        apy = (total_profit / state.total_investment) / days_running * 365 * 100
        grid_apy = (grid_profit / state.total_investment) / days_running * 365 * 100

        return {
            "symbol": state.symbol,
            "risk": state.risk,
            "total_investment": state.total_investment,
            "lower": p.lower,
            "upper": p.upper,
            "grid_count": p.grid_count,
            "grid_spacing": p.grid_spacing,
            "atr": p.atr,
            "current_price": current_price,
            "avg_buy_price": state.avg_buy_price,
            "held_qty": state.held_qty,
            "sell_count": state.sell_count,
            "realized_pnl": state.realized_pnl,
            "unrealised_pnl": unrealised_pnl,
            "grid_profit": grid_profit,
            "total_profit": total_profit,
            "apy": apy,
            "grid_apy": grid_apy,
            "days_running": days_running,
            "open_orders": len(state.open_orders),
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _fetch_atr(self, symbol: str, period: int) -> float:
        ohlcv = await self._client.fetch_ohlcv(symbol, CANDLE_TIMEFRAME, limit=period + 5)
        atr = compute_atr(ohlcv, period)
        if atr <= 0:
            # Fallback: 1% of current price
            price = await self._client.get_current_price(symbol)
            atr = price * 0.01
        return atr

    def _state_snapshot(self, state: GridState) -> dict:
        return {
            "symbol": state.symbol,
            "risk": state.risk,
            "total_investment": state.total_investment,
            "params": {
                "lower": state.params.lower,
                "upper": state.params.upper,
                "grid_count": state.params.grid_count,
                "grid_spacing": state.params.grid_spacing,
                "qty_per_grid": state.params.qty_per_grid,
                "atr": state.params.atr,
            },
            "avg_buy_price": state.avg_buy_price,
            "held_qty": state.held_qty,
            "realized_pnl": state.realized_pnl,
            "sell_count": state.sell_count,
            "open_orders": state.open_orders,
        }
