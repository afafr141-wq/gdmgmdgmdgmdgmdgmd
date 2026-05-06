"""
Copy-trade engine — BSC mempool watcher + PancakeSwap V2 executor.

Strategy:
  1. Subscribe to BSC pending transactions via WebSocket (Ankr).
  2. Filter transactions sent to PancakeSwap V2 Router from the target wallet.
  3. Decode calldata to extract token path and amounts.
  4. Execute the same swap immediately with a higher gas price (front-run).

Supported PancakeSwap V2 functions:
  - swapExactETHForTokens
  - swapExactTokensForETH
  - swapExactTokensForTokens
  - swapETHForExactTokens
  - swapTokensForExactETH
  - swapTokensForExactTokens
  - swapExactETHForTokensSupportingFeeOnTransferTokens
  - swapExactTokensForETHSupportingFeeOnTransferTokens
  - swapExactTokensForTokensSupportingFeeOnTransferTokens
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal
from typing import Callable, Optional

from web3 import AsyncWeb3
from web3.middleware import async_geth_poa_middleware
from eth_abi import decode as abi_decode
from eth_account import Account

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

PANCAKE_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
GMGN_ROUTER       = "0x1de460f363AF910f51726DEf188F9004276Bf4bc"
WBNB             = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT_BSC         = "0x55d398326f99059fF775485246999027B3197955"
BUSD_BSC         = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"
USDC_BSC         = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"

# Tokens treated as "base" (payment side) in a buy
BASE_TOKENS: set[str] = {
    WBNB.lower(),
    USDT_BSC.lower(),
    BUSD_BSC.lower(),
    USDC_BSC.lower(),
}

# All routers whose swap txs should be mirrored
WATCHED_ROUTERS: set[str] = {
    PANCAKE_V2_ROUTER.lower(),
    GMGN_ROUTER.lower(),
}

# ERC-20 Transfer topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# WBNB Deposit (BNB → WBNB wrap)
WBNB_DEPOSIT_TOPIC = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
# WBNB Withdrawal (WBNB → BNB unwrap)
WBNB_WITHDRAW_TOPIC = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"

# 3% slippage tolerance
SLIPPAGE_BPS = 300   # basis points

# Gas multiplier over target wallet's gas price (front-run)
GAS_MULTIPLIER = 1.15   # 15% higher

# Trade size in USDT equivalent
TRADE_USDT = Decimal("3")

# Reconnect delay on WebSocket drop (base — doubles on each consecutive failure)
WS_RECONNECT_DELAY = 5
WS_RECONNECT_MAX   = 60   # cap backoff at 60 s

# Max concurrent HTTP RPC calls (guards against 429 on remaining calls)
RPC_SEMAPHORE_LIMIT = 5

# BSCScan polling interval (seconds) — fallback when WSS unavailable
BSCSCAN_POLL_INTERVAL = 3
BSCSCAN_API_URL = "https://api.bscscan.com/api"

# PancakeSwap V2 Router ABI — only swap functions needed for decoding
PANCAKE_ROUTER_ABI = json.loads("""[
  {"name":"swapExactETHForTokens","type":"function","inputs":[
    {"name":"amountOutMin","type":"uint256"},
    {"name":"path","type":"address[]"},
    {"name":"to","type":"address"},
    {"name":"deadline","type":"uint256"}]},
  {"name":"swapExactTokensForETH","type":"function","inputs":[
    {"name":"amountIn","type":"uint256"},
    {"name":"amountOutMin","type":"uint256"},
    {"name":"path","type":"address[]"},
    {"name":"to","type":"address"},
    {"name":"deadline","type":"uint256"}]},
  {"name":"swapExactTokensForTokens","type":"function","inputs":[
    {"name":"amountIn","type":"uint256"},
    {"name":"amountOutMin","type":"uint256"},
    {"name":"path","type":"address[]"},
    {"name":"to","type":"address"},
    {"name":"deadline","type":"uint256"}]},
  {"name":"swapETHForExactTokens","type":"function","inputs":[
    {"name":"amountOut","type":"uint256"},
    {"name":"path","type":"address[]"},
    {"name":"to","type":"address"},
    {"name":"deadline","type":"uint256"}]},
  {"name":"swapTokensForExactETH","type":"function","inputs":[
    {"name":"amountOut","type":"uint256"},
    {"name":"amountInMax","type":"uint256"},
    {"name":"path","type":"address[]"},
    {"name":"to","type":"address"},
    {"name":"deadline","type":"uint256"}]},
  {"name":"swapTokensForExactTokens","type":"function","inputs":[
    {"name":"amountOut","type":"uint256"},
    {"name":"amountInMax","type":"uint256"},
    {"name":"path","type":"address[]"},
    {"name":"to","type":"address"},
    {"name":"deadline","type":"uint256"}]},
  {"name":"swapExactETHForTokensSupportingFeeOnTransferTokens","type":"function","inputs":[
    {"name":"amountOutMin","type":"uint256"},
    {"name":"path","type":"address[]"},
    {"name":"to","type":"address"},
    {"name":"deadline","type":"uint256"}]},
  {"name":"swapExactTokensForETHSupportingFeeOnTransferTokens","type":"function","inputs":[
    {"name":"amountIn","type":"uint256"},
    {"name":"amountOutMin","type":"uint256"},
    {"name":"path","type":"address[]"},
    {"name":"to","type":"address"},
    {"name":"deadline","type":"uint256"}]},
  {"name":"swapExactTokensForTokensSupportingFeeOnTransferTokens","type":"function","inputs":[
    {"name":"amountIn","type":"uint256"},
    {"name":"amountOutMin","type":"uint256"},
    {"name":"path","type":"address[]"},
    {"name":"to","type":"address"},
    {"name":"deadline","type":"uint256"}]}
]""")

# ERC-20 minimal ABI for approve + allowance
ERC20_ABI = json.loads("""[
  {"name":"approve","type":"function","inputs":[
    {"name":"spender","type":"address"},
    {"name":"amount","type":"uint256"}],"outputs":[{"type":"bool"}]},
  {"name":"allowance","type":"function","inputs":[
    {"name":"owner","type":"address"},
    {"name":"spender","type":"address"}],"outputs":[{"type":"uint256"}]},
  {"name":"decimals","type":"function","inputs":[],"outputs":[{"type":"uint8"}]},
  {"name":"balanceOf","type":"function","inputs":[
    {"name":"account","type":"address"}],"outputs":[{"type":"uint256"}]}
]""")

# PancakeSwap V2 Pair ABI — getReserves for price estimation
PAIR_ABI = json.loads("""[
  {"name":"getReserves","type":"function","inputs":[],"outputs":[
    {"name":"reserve0","type":"uint112"},
    {"name":"reserve1","type":"uint112"},
    {"name":"blockTimestampLast","type":"uint32"}]},
  {"name":"token0","type":"function","inputs":[],"outputs":[{"type":"address"}]},
  {"name":"token1","type":"function","inputs":[],"outputs":[{"type":"address"}]}
]""")

FACTORY_ABI = json.loads("""[
  {"name":"getPair","type":"function","inputs":[
    {"name":"tokenA","type":"address"},
    {"name":"tokenB","type":"address"}],"outputs":[{"type":"address"}]}
]""")

PANCAKE_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"


# ── Notifier globals (injected from main.py) ───────────────────────────────────

_notify_copy_buy:  Optional[Callable]  = None
_notify_copy_sell: Optional[Callable]  = None
_notify_copy_err:  Optional[Callable]  = None


def set_copy_notifiers(
    buy:  Callable,
    sell: Callable,
    err:  Callable,
) -> None:
    global _notify_copy_buy, _notify_copy_sell, _notify_copy_err
    _notify_copy_buy  = buy
    _notify_copy_sell = sell
    _notify_copy_err  = err


def _parse_transfer_amount(log: dict) -> int:
    """Extract the uint256 value from an ERC-20 Transfer log's data field."""
    data = log.get("data", b"")
    if isinstance(data, bytes):
        return int.from_bytes(data[:32], "big") if len(data) >= 32 else 0
    if isinstance(data, str):
        raw = data[2:] if data.startswith("0x") else data
        return int(raw[:64], 16) if len(raw) >= 64 else 0
    return 0


async def _fire(fn: Optional[Callable], *args) -> None:
    if fn is None:
        return
    try:
        await fn(*args)
    except Exception as exc:
        logger.error("Notifier error: %s", exc)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_buy(path: list[str]) -> bool:
    """Buy = base token → non-base token (path starts with WBNB or a stable)."""
    if not path:
        return False
    return path[0].lower() in BASE_TOKENS


def _apply_slippage(amount: int, bps: int = SLIPPAGE_BPS) -> int:
    """Return amount reduced by slippage basis points."""
    return int(amount * (10_000 - bps) // 10_000)


def _deadline(seconds: int = 120) -> int:
    return int(time.time()) + seconds


# ── Main engine ────────────────────────────────────────────────────────────────

class CopyTradeEngine:
    """
    Watches BSC mempool for swaps from `target_wallet` on PancakeSwap V2
    and mirrors them immediately with a higher gas price.
    """

    def __init__(
        self,
        ws_rpc_url: str,
        http_rpc_url: str,
        target_wallet: str,
        my_private_key: str,
        trade_usdt: Decimal = TRADE_USDT,
        copy_sells: bool = True,
        enabled: bool = True,
    ) -> None:
        self.ws_rpc_url    = ws_rpc_url
        self.http_rpc_url  = http_rpc_url
        self.target_wallet = AsyncWeb3.to_checksum_address(target_wallet)
        self.account       = Account.from_key(my_private_key)
        self.my_address    = self.account.address
        self.trade_usdt    = trade_usdt
        self.copy_sells    = copy_sells
        self.enabled       = enabled

        self._running      = False
        self._task: Optional[asyncio.Task] = None

        # HTTP w3 for sending transactions
        self._w3h: Optional[AsyncWeb3] = None
        # WebSocket w3 for mempool subscription
        self._w3ws: Optional[AsyncWeb3] = None

        # Router contract (HTTP)
        self._router = None
        self._factory = None

        # Seen tx hashes to avoid double-processing
        self._seen: set[str] = set()

        # Last processed block number
        self._last_block: int = 0

        # Token decimals cache
        self._decimals_cache: dict[str, int] = {}

        # Semaphore: limits concurrent HTTP RPC calls to avoid 429
        self._rpc_sem = asyncio.Semaphore(RPC_SEMAPHORE_LIMIT)

        # Consecutive WS failure counter for exponential backoff
        self._ws_fail_count: int = 0

        # BSCScan API key (optional — increases rate limit from 5 to 10 req/s)
        self.bscscan_api_key: str = os.getenv("BSCSCAN_API_KEY", "YourApiKeyToken")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="copy_trade_loop")
        logger.info("CopyTradeEngine started — watching %s", self.target_wallet)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._w3ws and hasattr(self._w3ws, "provider"):
            try:
                await self._w3ws.provider.disconnect()
            except Exception:
                pass
        logger.info("CopyTradeEngine stopped")

    def is_running(self) -> bool:
        return self._running

    # ── Internal loop ──────────────────────────────────────────────────────────

    async def _init_http(self) -> None:
        """Initialise HTTP Web3 connection."""
        rpc = self.http_rpc_url or "https://bsc-dataseed1.binance.org/"
        self._w3h = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc))
        self._w3h.middleware_onion.inject(async_geth_poa_middleware, layer=0)
        self._router  = self._w3h.eth.contract(
            address=AsyncWeb3.to_checksum_address(PANCAKE_V2_ROUTER),
            abi=PANCAKE_ROUTER_ABI,
        )
        self._factory = self._w3h.eth.contract(
            address=AsyncWeb3.to_checksum_address(PANCAKE_FACTORY),
            abi=FACTORY_ABI,
        )

    async def _run_loop(self) -> None:
        await self._init_http()
        self._last_block = await self._w3h.eth.block_number

        if self.ws_rpc_url:
            logger.info("WebSocket RPC detected — using mempool mode")
            await _fire(_notify_copy_err,
                        "🔍 *نسخ التجارة يعمل*\n⚡ وضع الـ Mempool (WebSocket) — أسرع من الـ blocks")
            while self._running:
                try:
                    await self._subscribe_mempool()
                    # Clean exit (idle timeout / normal close) — reset backoff
                    self._ws_fail_count = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._ws_fail_count += 1
                    delay = min(
                        WS_RECONNECT_DELAY * (2 ** (self._ws_fail_count - 1)),
                        WS_RECONNECT_MAX,
                    )
                    err = str(exc)
                    is_normal = "1001" in err or "going away" in err.lower()
                    if is_normal:
                        logger.info("WS closed normally — reconnecting in %ds", delay)
                        self._ws_fail_count = 0
                    else:
                        logger.error("WS error: %s — reconnecting in %ds", exc, delay)
                        await _fire(_notify_copy_err,
                                    f"⚠️ انقطع الـ WebSocket: `{exc}`\nإعادة الاتصال خلال {delay}s...")
                    await asyncio.sleep(delay)
        else:
            logger.info("No WSS URL — using block polling every %ds", BSCSCAN_POLL_INTERVAL)
            await _fire(_notify_copy_err,
                        "🔍 *نسخ التجارة يعمل*\n🕐 وضع الـ Block Polling كل 3 ثوانٍ")
            while self._running:
                try:
                    await self._poll_new_blocks()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Block poll error: %s", exc)
                await asyncio.sleep(BSCSCAN_POLL_INTERVAL)

    async def _subscribe_mempool(self) -> None:
        """Subscribe to pending transactions via WebSocket and mirror target wallet swaps.

        Uses ``newPendingTransactions`` with ``true`` (full-tx mode) so the node
        delivers complete transaction objects over the WebSocket.  This eliminates
        the per-tx ``eth_getTransactionByHash`` HTTP calls that caused HTTP 429
        rate-limit errors when the mempool is busy.

        Fallback: if the node does not support full-tx mode (result is a plain
        hash string), we fetch the transaction via HTTP but throttle concurrent
        calls with ``_rpc_sem``.
        """
        import websockets as _ws

        logger.info("Connecting to mempool WebSocket: %s", self.ws_rpc_url[:60])

        async with _ws.connect(
            self.ws_rpc_url,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            # Request full transaction objects in the subscription result.
            # Ankr and most BSC nodes support this; plain-hash nodes fall back
            # gracefully (see handling below).
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": ["newPendingTransactions", True],
            }))

            resp = json.loads(await ws.recv())
            sub_id = resp.get("result")
            if not sub_id:
                raise RuntimeError(f"eth_subscribe failed: {resp}")
            logger.info("Mempool subscription active: %s", sub_id)

            target_lower = self.target_wallet.lower()

            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    logger.info("WS idle 60s — reconnecting")
                    return

                try:
                    msg = json.loads(raw)
                    result = msg.get("params", {}).get("result")
                    if not result:
                        continue

                    # ── Full-tx mode: node returned a dict ──────────────────
                    if isinstance(result, dict):
                        tx_hash = (result.get("hash") or "").lower()
                        if not tx_hash or tx_hash in self._seen:
                            continue

                        tx_from = (result.get("from") or "").lower()
                        tx_to   = (result.get("to")   or "").lower()

                        if tx_from != target_lower or not tx_to:
                            continue

                        self._seen.add(tx_hash)

                        input_data = result.get("input", "0x") or "0x"
                        if isinstance(input_data, bytes):
                            input_data = "0x" + input_data.hex()

                        tx_web3 = {
                            "hash":     result.get("hash", tx_hash),
                            "from":     result.get("from", ""),
                            "to":       result.get("to", ""),
                            "input":    input_data,
                            "value":    int(result.get("value", "0x0"), 16)
                                        if isinstance(result.get("value"), str)
                                        else result.get("value", 0),
                            "gasPrice": int(result.get("gasPrice", "0x12a05f200"), 16)
                                        if isinstance(result.get("gasPrice"), str)
                                        else result.get("gasPrice", 5_000_000_000),
                        }
                        logger.info("Mempool (full-tx): target tx %s → %s",
                                    tx_hash[:20], tx_to[:10])
                        if self.enabled:
                            asyncio.create_task(self._mirror_swap(tx_web3))

                    # ── Hash-only mode: node returned a plain string ─────────
                    elif isinstance(result, str):
                        tx_hash = result.lower()
                        if tx_hash in self._seen:
                            continue

                        # Throttle HTTP fetches to avoid 429
                        async def _fetch_and_mirror(h: str) -> None:
                            async with self._rpc_sem:
                                try:
                                    tx = await self._w3h.eth.get_transaction(h)
                                except Exception:
                                    return
                            if tx is None:
                                return

                            tx_from = (tx.get("from") or "").lower()
                            tx_to   = (tx.get("to")   or "").lower()
                            if tx_from != target_lower or not tx_to:
                                return

                            self._seen.add(h)
                            input_data = tx.get("input", b"")
                            if isinstance(input_data, bytes):
                                input_data = "0x" + input_data.hex()

                            tx_web3 = {
                                "hash":     h,
                                "from":     tx.get("from", ""),
                                "to":       tx.get("to", ""),
                                "input":    input_data,
                                "value":    tx.get("value", 0),
                                "gasPrice": tx.get("gasPrice", 5_000_000_000),
                            }
                            logger.info("Mempool (hash-only): target tx %s → %s",
                                        h[:20], tx_to[:10])
                            if self.enabled:
                                asyncio.create_task(self._mirror_swap(tx_web3))

                        asyncio.create_task(_fetch_and_mirror(tx_hash))

                    # Trim seen set
                    if len(self._seen) > 10_000:
                        self._seen = set(list(self._seen)[-5_000:])

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Mempool msg error: %s", exc)

    async def _poll_new_blocks(self) -> None:
        """Scan new blocks for any transaction sent FROM the target wallet."""
        latest = await self._w3h.eth.block_number
        if latest <= self._last_block:
            return

        # Process at most 5 blocks per cycle to avoid overload
        from_block = self._last_block + 1
        to_block   = min(latest, from_block + 4)

        for block_num in range(from_block, to_block + 1):
            try:
                block = await self._w3h.eth.get_block(block_num, full_transactions=True)
            except Exception as exc:
                logger.warning("Failed to fetch block %d: %s", block_num, exc)
                continue

            for tx in block.get("transactions", []):
                tx_from = tx.get("from", "") or ""
                tx_to   = (tx.get("to",   "") or "")
                tx_hash = (tx.get("hash",  b"") or b"")

                # Only care about txs FROM the target wallet
                if tx_from.lower() != self.target_wallet.lower():
                    continue

                # Skip contract deployments (tx_to is None/empty)
                if not tx_to:
                    continue

                hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
                if hash_hex in self._seen:
                    continue
                self._seen.add(hash_hex)

                input_data = tx.get("input", b"")
                if isinstance(input_data, bytes):
                    input_data = "0x" + input_data.hex()

                tx_web3 = {
                    "hash":     hash_hex,
                    "from":     tx_from,
                    "to":       tx_to,
                    "input":    input_data,
                    "value":    tx.get("value", 0),
                    "gasPrice": tx.get("gasPrice", 5_000_000_000),
                }
                logger.info("Found target tx in block %d: %s → %s",
                            block_num, hash_hex[:20], tx_to[:10])
                if self.enabled:
                    asyncio.create_task(self._mirror_swap(tx_web3))
                else:
                    logger.debug("Copy trading paused — skipping tx %s", hash_hex[:20])

        self._last_block = to_block

        # Trim seen set
        if len(self._seen) > 10_000:
            self._seen = set(list(self._seen)[-5_000:])



    async def _extract_swap_from_logs(
        self, tx_hash: str, tx_value: int
    ) -> tuple[list[str], bool] | None:
        """
        Derive swap path and direction from transaction receipt logs.

        Works for any DEX router (GMGN, aggregators, PancakeSwap, etc.) by
        reading ERC-20 Transfer and WBNB Deposit/Withdrawal events to find:
          - token received by the target wallet  → token_out (buy)
          - token sent from the target wallet    → token_in  (sell)

        Returns (path, is_buy) or None if no swap can be determined.
        """
        try:
            receipt = await self._w3h.eth.get_transaction_receipt(tx_hash)
        except Exception as exc:
            logger.warning("Could not fetch receipt for %s: %s", tx_hash[:20], exc)
            return None

        # Skip failed transactions
        if receipt.get("status") == 0:
            logger.debug("Tx %s reverted — skipping", tx_hash[:20])
            return None

        wallet = self.target_wallet.lower()

        # Collect Transfer events: tokens leaving and arriving at the wallet
        transfers_to_wallet:   list[str] = []
        transfers_from_wallet: list[str] = []

        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if not topics:
                continue

            topic0 = topics[0].hex() if isinstance(topics[0], bytes) else topics[0]
            topic0 = topic0.lower()

            token_addr = log["address"].lower()

            # ERC-20 Transfer(from, to, value)
            if topic0 == TRANSFER_TOPIC.lower() and len(topics) >= 3:
                frm = ("0x" + (topics[1].hex() if isinstance(topics[1], bytes) else topics[1])[-40:]).lower()
                to  = ("0x" + (topics[2].hex() if isinstance(topics[2], bytes) else topics[2])[-40:]).lower()
                if to == wallet:
                    transfers_to_wallet.append(token_addr)
                elif frm == wallet:
                    transfers_from_wallet.append(token_addr)

            # WBNB Deposit(dst, wad) — BNB → WBNB wrap triggered by the wallet
            elif topic0 == WBNB_DEPOSIT_TOPIC.lower() and len(topics) >= 2:
                dst = ("0x" + (topics[1].hex() if isinstance(topics[1], bytes) else topics[1])[-40:]).lower()
                if dst == wallet:
                    transfers_to_wallet.append(WBNB.lower())

            # WBNB Withdrawal(src, wad) — WBNB → BNB unwrap
            elif topic0 == WBNB_WITHDRAW_TOPIC.lower() and len(topics) >= 2:
                src = ("0x" + (topics[1].hex() if isinstance(topics[1], bytes) else topics[1])[-40:]).lower()
                if src == wallet:
                    transfers_from_wallet.append(WBNB.lower())

        # Separate base tokens (payment side) from non-base tokens (traded asset)
        base_received    = [t for t in transfers_to_wallet   if t in BASE_TOKENS]
        nonbase_received = [t for t in transfers_to_wallet   if t not in BASE_TOKENS]
        base_sent        = [t for t in transfers_from_wallet if t in BASE_TOKENS]
        nonbase_sent     = [t for t in transfers_from_wallet if t not in BASE_TOKENS]

        logger.debug(
            "Logs for %s — nonbase_rcv=%s base_sent=%s nonbase_sent=%s base_rcv=%s",
            tx_hash[:20], nonbase_received, base_sent, nonbase_sent, base_received,
        )

        if nonbase_received:
            # BUY: wallet received a non-base token
            token_out = AsyncWeb3.to_checksum_address(nonbase_received[-1])
            if base_sent:
                token_in = AsyncWeb3.to_checksum_address(base_sent[0])
            elif tx_value > 0:
                # Native BNB sent with tx → route via WBNB
                token_in = AsyncWeb3.to_checksum_address(WBNB)
            else:
                token_in = AsyncWeb3.to_checksum_address(WBNB)
            path = [token_in, token_out]
            logger.info("BUY detected via logs: token_in=%s token_out=%s", token_in, token_out)
            return path, True

        if nonbase_sent:
            # SELL: wallet sent a non-base token
            if not self.copy_sells:
                logger.info("Sell detected but copy_sells=False — skipping")
                return None
            token_in  = AsyncWeb3.to_checksum_address(nonbase_sent[0])
            token_out = (
                AsyncWeb3.to_checksum_address(base_received[0])
                if base_received
                else AsyncWeb3.to_checksum_address(WBNB)
            )
            path = [token_in, token_out]
            logger.info("SELL detected via logs: token_in=%s token_out=%s", token_in, token_out)
            return path, False

        logger.debug("No swap detected in logs for %s", tx_hash[:20])
        return None

    async def _mirror_swap(self, tx: dict) -> None:
        """Decode the swap and execute a mirrored trade.

        Strategy:
          1. If tx goes to PancakeSwap V2 router, try ABI calldata decode first.
          2. For any other router (GMGN, aggregators, etc.) or if calldata decode
             fails, fall back to receipt-log analysis to determine the swap path.
        """
        if not self.enabled:
            logger.debug("Copy trading paused — skipping tx %s", tx.get("hash", "")[:20])
            return

        tx_to = (tx.get("to") or "").lower()
        result = None

        # Try PancakeSwap V2 calldata decode first (fast path, no extra RPC call)
        if tx_to == PANCAKE_V2_ROUTER.lower():
            input_data = tx.get("input") or tx.get("data", "")
            if input_data and len(input_data) >= 10:
                selector = input_data[:10].lower()
                func_map = self._build_selector_map()
                func_name = func_map.get(selector)
                if func_name:
                    try:
                        decoded = self._decode_calldata(func_name, input_data)
                        path: list[str] = [
                            AsyncWeb3.to_checksum_address(a) for a in decoded["path"]
                        ]
                        is_buy = _is_buy(path)
                        result = (path, is_buy)
                        logger.info("PancakeSwap calldata decoded: %s path=%s",
                                    "BUY" if is_buy else "SELL", path)
                    except Exception as exc:
                        logger.warning("Calldata decode failed for %s: %s — falling back to logs",
                                       func_name, exc)

        # Fall back to receipt-log analysis for all other routers or decode failures
        if result is None:
            result = await self._extract_swap_from_logs(
                tx.get("hash", ""), tx.get("value", 0)
            )
            if result is None:
                logger.debug("No swap detected in tx %s — skipping", tx.get("hash", "")[:20])
                return

        path, is_buy = result

        if not is_buy and not self.copy_sells:
            logger.info("Sell detected but copy_sells=False — skipping")
            return

        router_label = "PancakeSwap" if tx_to == PANCAKE_V2_ROUTER.lower() else tx_to[:10]
        logger.info("Mirroring %s %s: path=%s",
                    router_label, "BUY" if is_buy else "SELL", path)

        try:
            if is_buy:
                await self._execute_buy(path, tx)
            else:
                await self._execute_sell(path, tx)
        except Exception as exc:
            logger.error("Mirror swap failed: %s", exc)
            await _fire(_notify_copy_err, f"❌ فشل تنفيذ الصفقة المنسوخة:\n`{exc}`")

    def _build_selector_map(self) -> dict[str, str]:
        """Build 4-byte selector → function name map from ABI."""
        from eth_hash.auto import keccak
        result = {}
        for item in PANCAKE_ROUTER_ABI:
            if item["type"] != "function":
                continue
            name = item["name"]
            types = ",".join(i["type"] for i in item["inputs"])
            sig = f"{name}({types})"
            selector = "0x" + keccak(sig.encode()).hex()[:8]
            result[selector] = name
        return result

    def _decode_calldata(self, func_name: str, input_data: str) -> dict:
        """Decode calldata for a known PancakeSwap V2 function."""
        # Find ABI entry
        abi_entry = next(e for e in PANCAKE_ROUTER_ABI if e["name"] == func_name)
        types = [i["type"] for i in abi_entry["inputs"]]
        names = [i["name"] for i in abi_entry["inputs"]]

        raw = bytes.fromhex(input_data[10:])  # strip 4-byte selector
        values = abi_decode(types, raw)
        return dict(zip(names, values))

    # ── Trade execution ────────────────────────────────────────────────────────

    async def _get_entry_price_from_receipt(
        self,
        tx_hash: str,
        token_out: str,
        amount_in_usdt: float,
    ) -> Optional[float]:
        """
        Calculate actual entry price (USDT per token) from a confirmed receipt.

        Reads the Transfer event for token_out going to our wallet and divides
        amount_in_usdt by the token amount received.
        Returns None if the receipt cannot be parsed.
        """
        try:
            receipt = await self._w3h.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return None

        wallet = self.my_address.lower()
        token_out_lower = token_out.lower()

        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            topic0 = topics[0].hex() if isinstance(topics[0], bytes) else topics[0]
            if topic0.lower() != TRANSFER_TOPIC.lower():
                continue
            if log["address"].lower() != token_out_lower:
                continue
            to = ("0x" + (topics[2].hex() if isinstance(topics[2], bytes) else topics[2])[-40:]).lower()
            if to != wallet:
                continue

            raw_amount = _parse_transfer_amount(log)
            if raw_amount == 0:
                return None
            try:
                decimals = await self._get_decimals(token_out)
                token_amount = raw_amount / (10 ** decimals)
                return amount_in_usdt / token_amount
            except Exception:
                return None

        return None

    async def _get_target_entry_price(
        self,
        original_tx_hash: str,
        token_out: str,
        target_wallet: str,
    ) -> Optional[float]:
        """
        Calculate the target wallet's entry price from the original tx receipt.

        Reads the Transfer event for token_out going to target_wallet and the
        base-token Transfer leaving target_wallet to derive USDT paid / tokens received.
        Returns None if it cannot be determined.
        """
        try:
            receipt = await self._w3h.eth.get_transaction_receipt(original_tx_hash)
        except Exception:
            return None

        wallet = target_wallet.lower()
        token_out_lower = token_out.lower()

        tokens_received: int = 0
        base_sent_wei:   int = 0
        base_sent_token: str = ""

        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            topic0 = topics[0].hex() if isinstance(topics[0], bytes) else topics[0]
            if topic0.lower() != TRANSFER_TOPIC.lower():
                continue

            addr = log["address"].lower()
            frm  = ("0x" + (topics[1].hex() if isinstance(topics[1], bytes) else topics[1])[-40:]).lower()
            to   = ("0x" + (topics[2].hex() if isinstance(topics[2], bytes) else topics[2])[-40:]).lower()
            amt  = _parse_transfer_amount(log)

            if addr == token_out_lower and to == wallet:
                tokens_received = amt
            elif addr in BASE_TOKENS and frm == wallet and amt > 0:
                base_sent_wei   = amt
                base_sent_token = addr

        if tokens_received == 0:
            return None

        try:
            token_decimals = await self._get_decimals(token_out)
            token_amount   = tokens_received / (10 ** token_decimals)

            if base_sent_token in (USDT_BSC.lower(), BUSD_BSC.lower(), USDC_BSC.lower()):
                base_decimals = await self._get_decimals(base_sent_token)
                usdt_paid = base_sent_wei / (10 ** base_decimals)
            elif base_sent_wei > 0:
                # BNB paid — convert via current price
                bnb_price = await self._bnb_price_in_usdt()
                usdt_paid = float(bnb_price) * base_sent_wei / 10**18
            else:
                # Fallback: use tx value (native BNB)
                try:
                    tx = await self._w3h.eth.get_transaction(original_tx_hash)
                    bnb_price = await self._bnb_price_in_usdt()
                    usdt_paid = float(bnb_price) * tx.get("value", 0) / 10**18
                except Exception:
                    return None

            if usdt_paid == 0 or token_amount == 0:
                return None
            return usdt_paid / token_amount
        except Exception:
            return None

    async def _get_decimals(self, token_address: str) -> int:
        addr = token_address.lower()
        if addr not in self._decimals_cache:
            contract = self._w3h.eth.contract(
                address=AsyncWeb3.to_checksum_address(token_address),
                abi=ERC20_ABI,
            )
            self._decimals_cache[addr] = await contract.functions.decimals().call()
        return self._decimals_cache[addr]

    async def _bnb_price_in_usdt(self) -> Decimal:
        """Get BNB price via WBNB/USDT pair reserves."""
        try:
            pair_addr = await self._factory.functions.getPair(
                AsyncWeb3.to_checksum_address(WBNB),
                AsyncWeb3.to_checksum_address(USDT_BSC),
            ).call()
            pair = self._w3h.eth.contract(
                address=AsyncWeb3.to_checksum_address(pair_addr),
                abi=PAIR_ABI,
            )
            t0 = await pair.functions.token0().call()
            r0, r1, _ = await pair.functions.getReserves().call()
            if t0.lower() == WBNB.lower():
                # r0 = WBNB (18 dec), r1 = USDT (18 dec on BSC)
                return Decimal(r1) / Decimal(r0)
            else:
                return Decimal(r0) / Decimal(r1)
        except Exception as exc:
            logger.warning("BNB price fetch failed: %s — using fallback 600", exc)
            return Decimal("600")

    async def _check_bnb_balance(
        self,
        required_wei: int,
        token_out: str = "",
        original_tx_hash: str = "",
    ) -> bool:
        """Return True if wallet has enough BNB.

        When balance is insufficient, fires a watch-mode notification that
        includes the token contract and GMGN link so the trade can be tracked
        manually even without execution.
        """
        balance = await self._w3h.eth.get_balance(self.my_address)
        # Keep 0.005 BNB reserve for gas
        gas_reserve = int(0.005 * 10**18)
        if balance < required_wei + gas_reserve:
            bnb_balance  = balance / 10**18
            required_bnb = required_wei / 10**18

            await self._notify_insufficient_balance(
                token_out=token_out,
                original_tx_hash=original_tx_hash,
                balance_line=(
                    f"الرصيد الحالي: `{bnb_balance:.4f} BNB`\n"
                    f"المطلوب: `{required_bnb:.4f} BNB` (~${float(self.trade_usdt):.2f} USDT)\n"
                    f"احتياطي الـ gas: `0.005 BNB`"
                ),
            )
            logger.warning(
                "Insufficient BNB: have %.4f, need %.4f",
                bnb_balance, required_bnb + 0.005,
            )
            return False
        return True

    async def _notify_insufficient_balance(
        self,
        token_out: str,
        original_tx_hash: str,
        balance_line: str,
    ) -> None:
        """Send insufficient-balance alert with watch-mode token info."""
        watch_block = ""
        if token_out:
            tgt: Optional[float] = None
            if original_tx_hash:
                try:
                    tgt = await self._get_target_entry_price(
                        original_tx_hash, token_out, self.target_wallet
                    )
                except Exception:
                    pass
            price_line = f"\n🎯 سعر دخوله: `${tgt:.8f}`" if tgt else ""
            watch_block = (
                f"\n━━━━━━━━━━━━━━━━━━━━\n"
                f"👁 *مراقبة فقط — لم يُنفَّذ*\n"
                f"🪙 العقد: `{token_out}`{price_line}\n"
                f"[📈 GMGN](https://gmgn.ai/bsc/token/{token_out})"
            )
        await _fire(
            _notify_copy_err,
            f"⚠️ *رصيد غير كافٍ — صفقة فاتت*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{balance_line}"
            f"{watch_block}",
        )

    async def _execute_buy(self, path: list[str], original_tx: dict) -> None:
        """
        Buy `trade_usdt` worth of the target token.

        Supports two payment modes based on path[0]:
          - WBNB: sends BNB as tx value via swapExactETHForTokens
          - Stable (USDT/USDC/BUSD): approves and sends token via swapExactTokensForTokens
        """
        token_in = path[0].lower()
        is_bnb_buy = token_in == WBNB.lower()

        gas_price = int(int(original_tx.get("gasPrice", 5_000_000_000)) * GAS_MULTIPLIER)
        nonce = await self._w3h.eth.get_transaction_count(self.my_address, "pending")

        if is_bnb_buy:
            bnb_price = await self._bnb_price_in_usdt()
            amount_in_wei = int(self.trade_usdt / bnb_price * Decimal(10**18))

            if not await self._check_bnb_balance(
                amount_in_wei,
                token_out=path[-1],
                original_tx_hash=original_tx.get("hash", ""),
            ):
                return

            try:
                amounts = await self._router.functions.getAmountsOut(
                    amount_in_wei, path
                ).call()
                amount_out_min = _apply_slippage(amounts[-1])
            except Exception as exc:
                logger.warning("getAmountsOut failed: %s — using 0 min", exc)
                amount_out_min = 0

            tx = await self._router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
                amount_out_min,
                path,
                self.my_address,
                _deadline(),
            ).build_transaction({
                "from":     self.my_address,
                "value":    amount_in_wei,
                "gas":      300_000,
                "gasPrice": gas_price,
                "nonce":    nonce,
                "chainId":  56,
            })
        else:
            # Stable token buy (USDT / USDC / BUSD)
            decimals = await self._get_decimals(path[0])
            amount_in_wei = int(self.trade_usdt * Decimal(10 ** decimals))

            # Check stable balance
            stable_contract = self._w3h.eth.contract(
                address=AsyncWeb3.to_checksum_address(path[0]),
                abi=ERC20_ABI,
            )
            balance = await stable_contract.functions.balanceOf(self.my_address).call()
            if balance < amount_in_wei:
                await self._notify_insufficient_balance(
                    token_out=path[-1],
                    original_tx_hash=original_tx.get("hash", ""),
                    balance_line=f"الرصيد الحالي: `{balance / 10**decimals:.2f}` | المطلوب: `{float(self.trade_usdt):.2f}` USDT",
                )
                logger.warning("Insufficient stable balance: have %s, need %s", balance, amount_in_wei)
                return

            # Approve router if needed
            allowance = await stable_contract.functions.allowance(
                self.my_address,
                AsyncWeb3.to_checksum_address(PANCAKE_V2_ROUTER),
            ).call()
            if allowance < amount_in_wei:
                await self._approve_token(path[0], amount_in_wei)
                nonce += 1

            try:
                amounts = await self._router.functions.getAmountsOut(
                    amount_in_wei, path
                ).call()
                amount_out_min = _apply_slippage(amounts[-1])
            except Exception as exc:
                logger.warning("getAmountsOut failed: %s — using 0 min", exc)
                amount_out_min = 0

            tx = await self._router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amount_in_wei,
                amount_out_min,
                path,
                self.my_address,
                _deadline(),
            ).build_transaction({
                "from":     self.my_address,
                "value":    0,
                "gas":      350_000,
                "gasPrice": gas_price,
                "nonce":    nonce,
                "chainId":  56,
            })

        signed = self.account.sign_transaction(tx)
        tx_hash = await self._w3h.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        logger.info("✅ BUY sent: %s | amount_in=%s | token_out=%s",
                    tx_hash_hex, amount_in_wei, path[-1])

        # Persist immediately — don't wait for confirmation
        await self._record_copy_trade(
            side="buy",
            token_in=path[0],
            token_out=path[-1],
            amount_in_usdt=float(self.trade_usdt),
            tx_hash=tx_hash_hex,
            original_tx=original_tx.get("hash", ""),
        )

        # Confirm + calculate prices + notify in background (non-blocking)
        asyncio.create_task(self._confirm_and_notify_buy(
            tx_hash=tx_hash,
            tx_hash_hex=tx_hash_hex,
            token_out=path[-1],
            trade_usdt=float(self.trade_usdt),
            original_tx_hash=original_tx.get("hash", ""),
        ))

    async def _confirm_and_notify_buy(
        self,
        tx_hash: bytes,
        tx_hash_hex: str,
        token_out: str,
        trade_usdt: float,
        original_tx_hash: str,
    ) -> None:
        """Wait for BUY confirmation then send enriched notification. Non-blocking."""
        my_entry:     Optional[float] = None
        target_entry: Optional[float] = None
        try:
            await self._w3h.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            my_entry = await self._get_entry_price_from_receipt(
                tx_hash_hex, token_out, trade_usdt
            )
            if original_tx_hash:
                target_entry = await self._get_target_entry_price(
                    original_tx_hash, token_out, self.target_wallet
                )
        except Exception as exc:
            logger.warning("Could not calculate entry prices: %s", exc)

        await _fire(
            _notify_copy_buy,
            token_out,
            trade_usdt,
            tx_hash_hex,
            my_entry,
            target_entry,
        )

    async def _execute_sell(self, path: list[str], original_tx: dict) -> None:
        """
        Sell our entire balance of the token.
        Path: token → ... → WBNB
        """
        token_in = path[0]
        token_contract = self._w3h.eth.contract(
            address=AsyncWeb3.to_checksum_address(token_in),
            abi=ERC20_ABI,
        )

        balance = await token_contract.functions.balanceOf(self.my_address).call()
        if balance == 0:
            logger.info("No balance for %s — skipping sell", token_in)
            return

        # Approve router if needed
        allowance = await token_contract.functions.allowance(
            self.my_address,
            AsyncWeb3.to_checksum_address(PANCAKE_V2_ROUTER),
        ).call()
        if allowance < balance:
            await self._approve_token(token_in, balance)

        try:
            amounts = await self._router.functions.getAmountsOut(
                balance, path
            ).call()
            amount_out_min = _apply_slippage(amounts[-1])
        except Exception:
            amount_out_min = 0

        gas_price = int(int(original_tx.get("gasPrice", 5_000_000_000)) * GAS_MULTIPLIER)
        nonce = await self._w3h.eth.get_transaction_count(self.my_address, "pending")

        tx = await self._router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            balance,
            amount_out_min,
            path,
            self.my_address,
            _deadline(),
        ).build_transaction({
            "from":     self.my_address,
            "gas":      300_000,
            "gasPrice": gas_price,
            "nonce":    nonce,
            "chainId":  56,
        })

        signed = self.account.sign_transaction(tx)
        tx_hash = await self._w3h.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        decimals = await self._get_decimals(token_in)
        token_amount = balance / (10 ** decimals)
        logger.info("✅ SELL sent: %s | token=%s amount=%.4f",
                    tx_hash_hex, token_in[:8], token_amount)

        await _fire(_notify_copy_sell, token_in, token_amount, tx_hash_hex)

        await self._record_copy_trade(
            side="sell",
            token_in=token_in,
            token_out=path[-1],
            amount_in_usdt=0.0,
            tx_hash=tx_hash_hex,
            original_tx=original_tx.get("hash", ""),
        )

    async def _approve_token(self, token_address: str, amount: int) -> None:
        """Approve PancakeSwap router to spend token."""
        contract = self._w3h.eth.contract(
            address=AsyncWeb3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        nonce = await self._w3h.eth.get_transaction_count(self.my_address, "pending")
        gas_price = await self._w3h.eth.gas_price

        tx = await contract.functions.approve(
            AsyncWeb3.to_checksum_address(PANCAKE_V2_ROUTER),
            2**256 - 1,  # max approval
        ).build_transaction({
            "from":     self.my_address,
            "gas":      100_000,
            "gasPrice": gas_price,
            "nonce":    nonce,
            "chainId":  56,
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = await self._w3h.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("Approve sent: %s for token %s", tx_hash.hex(), token_address[:10])
        # Must wait — swap will fail if approval isn't confirmed first
        await self._w3h.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    async def _record_copy_trade(
        self,
        side: str,
        token_in: str,
        token_out: str,
        amount_in_usdt: float,
        tx_hash: str,
        original_tx: str,
    ) -> None:
        """Persist copy trade to DB (non-blocking)."""
        try:
            from utils.db_manager import record_copy_trade
            await record_copy_trade(
                side=side,
                token_in=token_in,
                token_out=token_out,
                amount_in_usdt=amount_in_usdt,
                tx_hash=tx_hash,
                original_tx_hash=original_tx,
            )
        except Exception as exc:
            logger.warning("Failed to record copy trade: %s", exc)
