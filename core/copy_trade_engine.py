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
WBNB             = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT_BSC         = "0x55d398326f99059fF775485246999027B3197955"
BUSD_BSC         = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"

# 3% slippage tolerance
SLIPPAGE_BPS = 300   # basis points

# Gas multiplier over target wallet's gas price (front-run)
GAS_MULTIPLIER = 1.15   # 15% higher

# Trade size in USDT equivalent
TRADE_USDT = Decimal("3")

# Reconnect delay on WebSocket drop
WS_RECONNECT_DELAY = 5

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


async def _fire(fn: Optional[Callable], *args) -> None:
    if fn is None:
        return
    try:
        await fn(*args)
    except Exception as exc:
        logger.error("Notifier error: %s", exc)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_buy(path: list[str]) -> bool:
    """Buy = BNB/stable → token (path starts with WBNB or stable)."""
    if not path:
        return False
    src = path[0].lower()
    return src in (WBNB.lower(), USDT_BSC.lower(), BUSD_BSC.lower())


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

        # Token decimals cache
        self._decimals_cache: dict[str, int] = {}

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
        self._w3h = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self.http_rpc_url))
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
        while self._running:
            try:
                await self._subscribe_mempool()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Mempool subscription error: %s — reconnecting in %ds",
                             exc, WS_RECONNECT_DELAY)
                await _fire(_notify_copy_err, f"⚠️ انقطع الاتصال بـ mempool: {exc}\nإعادة الاتصال...")
                await asyncio.sleep(WS_RECONNECT_DELAY)

    async def _subscribe_mempool(self) -> None:
        """Connect via WebSocket and subscribe to pending transactions."""
        import websockets as _ws

        ws_url = self.ws_rpc_url
        logger.info("Connecting to mempool WebSocket: %s", ws_url[:60])

        async with _ws.connect(ws_url, ping_interval=20, ping_timeout=30) as ws:
            # Subscribe to pending transactions
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": ["newPendingTransactions"]
            }))
            sub_response = json.loads(await ws.recv())
            sub_id = sub_response.get("result")
            logger.info("Subscribed to pending txs — subscription id: %s", sub_id)

            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)
                    tx_hash = msg.get("params", {}).get("result")
                    if tx_hash:
                        asyncio.create_task(self._process_tx(tx_hash))
                except asyncio.TimeoutError:
                    # keepalive ping
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 2,
                        "method": "eth_blockNumber", "params": []
                    }))
                except Exception as exc:
                    logger.debug("WS recv error: %s", exc)
                    raise

    async def _process_tx(self, tx_hash: str) -> None:
        """Fetch and evaluate a pending transaction."""
        if not self.enabled:
            return
        if tx_hash in self._seen:
            return
        self._seen.add(tx_hash)
        # Trim seen set to avoid unbounded growth
        if len(self._seen) > 10_000:
            self._seen = set(list(self._seen)[-5_000:])

        try:
            tx = await self._w3h.eth.get_transaction(tx_hash)
        except Exception:
            return

        if tx is None:
            return

        # Filter: must be from target wallet to PancakeSwap V2 Router
        if tx.get("from", "").lower() != self.target_wallet.lower():
            return
        if (tx.get("to") or "").lower() != PANCAKE_V2_ROUTER.lower():
            return

        logger.info("🎯 Target tx detected: %s", tx_hash)
        await self._mirror_swap(tx)

    async def _mirror_swap(self, tx: dict) -> None:
        """Decode the swap calldata and execute a mirrored swap."""
        input_data = tx.get("input") or tx.get("data", "")
        if not input_data or len(input_data) < 10:
            return

        selector = input_data[:10].lower()

        # Build selector → function name map from ABI
        func_map = self._build_selector_map()
        func_name = func_map.get(selector)
        if not func_name:
            logger.debug("Unknown selector %s — skipping", selector)
            return

        try:
            decoded = self._decode_calldata(func_name, input_data)
        except Exception as exc:
            logger.warning("Failed to decode %s: %s", func_name, exc)
            return

        path: list[str] = [AsyncWeb3.to_checksum_address(a) for a in decoded["path"]]
        is_buy = _is_buy(path)

        if not is_buy and not self.copy_sells:
            logger.info("Sell detected but copy_sells=False — skipping")
            return

        logger.info("Mirroring %s: path=%s", "BUY" if is_buy else "SELL", path)

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

    async def _execute_buy(self, path: list[str], original_tx: dict) -> None:
        """
        Buy `trade_usdt` worth of the target token.
        Path: WBNB → ... → token  (we send BNB as value)
        """
        bnb_price = await self._bnb_price_in_usdt()
        bnb_amount_wei = int(self.trade_usdt / bnb_price * Decimal(10**18))

        # Get expected output via getAmountsOut
        try:
            amounts = await self._router.functions.getAmountsOut(
                bnb_amount_wei, path
            ).call()
            amount_out_min = _apply_slippage(amounts[-1])
        except Exception as exc:
            logger.warning("getAmountsOut failed: %s — using 0 min", exc)
            amount_out_min = 0

        gas_price = int(int(original_tx.get("gasPrice", 5_000_000_000)) * GAS_MULTIPLIER)
        nonce = await self._w3h.eth.get_transaction_count(self.my_address, "pending")

        tx = await self._router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
            amount_out_min,
            path,
            self.my_address,
            _deadline(),
        ).build_transaction({
            "from":     self.my_address,
            "value":    bnb_amount_wei,
            "gas":      300_000,
            "gasPrice": gas_price,
            "nonce":    nonce,
            "chainId":  56,
        })

        signed = self.account.sign_transaction(tx)
        tx_hash = await self._w3h.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        logger.info("✅ BUY sent: %s | BNB=%.6f | token=%s",
                    tx_hash_hex, bnb_amount_wei / 1e18, path[-1])

        token_symbol = path[-1][:8]
        await _fire(
            _notify_copy_buy,
            token_symbol,
            float(self.trade_usdt),
            tx_hash_hex,
        )

        # Persist to DB
        await self._record_copy_trade(
            side="buy",
            token_in=path[0],
            token_out=path[-1],
            amount_in_usdt=float(self.trade_usdt),
            tx_hash=tx_hash_hex,
            original_tx=original_tx.get("hash", ""),
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

        await _fire(_notify_copy_sell, token_in[:8], token_amount, tx_hash_hex)

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
        # Wait for approval to be mined
        await self._w3h.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

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
