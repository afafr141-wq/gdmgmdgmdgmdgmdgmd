"""
Giants Engine — five signal sources for SuperConsensus Bot.

Architecture:
  Giant1 — MarketSentiment  : CoinGecko + Fear & Greed Index (live data)
  Giant2 — AIJudge          : OpenRouter LLM — receives all other giants'
                              reports and issues the FINAL decision
  Giant3/4/5                : Placeholder stubs ready for future APIs

Each giant returns a GiantReport:
  signal     : "BUY" | "SELL" | "HOLD"
  confidence : 0-100
  reason     : one short sentence

Giant2 (AIJudge) is special — it does NOT produce its own market signal.
Instead, consensus_engine feeds it the other giants' reports and it
returns the authoritative final decision.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from config.settings import OPENROUTER_API_KEY, OPENROUTER_MODEL

logger = logging.getLogger(__name__)


# ── Report dataclass ───────────────────────────────────────────────────────────

@dataclass
class GiantReport:
    name:       str
    signal:     str   # "BUY" | "SELL" | "HOLD"
    confidence: int   # 0-100
    reason:     str

    def to_prompt_line(self) -> str:
        return (
            f"- {self.name}: signal={self.signal}, "
            f"confidence={self.confidence}, reason='{self.reason}'"
        )


# ── Base class ─────────────────────────────────────────────────────────────────

class BaseGiant(ABC):
    name: str = "BaseGiant"

    @abstractmethod
    async def analyze(self, symbol: str, price: float) -> GiantReport:
        """Fetch data and return a GiantReport for the given symbol."""

    def __repr__(self) -> str:
        return f"<Giant:{self.name}>"


# ── HTTP helper ────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SuperConsensusBot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── Giant 1 — Market Sentiment ─────────────────────────────────────────────────

class MarketSentimentGiant(BaseGiant):
    """
    Combines three free data sources:
      1. Fear & Greed Index (alternative.me)
      2. CoinGecko 24h price change for the symbol
      3. CoinGecko global market cap change

    Decision logic:
      BUY  — Fear & Greed < 30 (extreme fear) AND price recovering (24h > -2%)
      SELL — Fear & Greed > 70 (extreme greed) OR price falling fast (24h < -5%)
      HOLD — everything else
    """

    name = "MarketSentiment"

    _SYMBOL_MAP = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
        "BNB": "binancecoin", "XRP": "ripple", "ADA": "cardano",
        "DOGE": "dogecoin", "AVAX": "avalanche-2", "DOT": "polkadot",
        "MATIC": "matic-network", "LINK": "chainlink", "UNI": "uniswap",
        "LTC": "litecoin", "ATOM": "cosmos", "NEAR": "near",
    }

    def _coingecko_id(self, symbol: str) -> str:
        base = symbol.replace("/USDT", "").replace("USDT", "").upper()
        return self._SYMBOL_MAP.get(base, base.lower())

    async def analyze(self, symbol: str, price: float) -> GiantReport:
        try:
            return self._fetch_and_decide(symbol, price)
        except Exception as exc:
            logger.warning("MarketSentiment fetch failed: %s", exc)
            return GiantReport(
                name=self.name,
                signal="HOLD",
                confidence=30,
                reason=f"Data fetch failed: {exc}",
            )

    def _fetch_and_decide(self, symbol: str, price: float) -> GiantReport:
        # 1. Fear & Greed
        fg_data  = _get_json("https://api.alternative.me/fng/?limit=1")
        fg_value = int(fg_data["data"][0]["value"])
        fg_label = fg_data["data"][0]["value_classification"]

        # 2. 24h price change from CoinGecko
        cg_id = self._coingecko_id(symbol)
        try:
            price_data = _get_json(
                f"https://api.coingecko.com/api/v3/simple/price"
                f"?ids={cg_id}&vs_currencies=usd&include_24hr_change=true"
            )
            change_24h = float(price_data.get(cg_id, {}).get("usd_24h_change", 0) or 0)
        except Exception:
            change_24h = 0.0

        # 3. Global market cap change
        try:
            global_data  = _get_json("https://api.coingecko.com/api/v3/global")
            global_change = float(
                global_data["data"].get("market_cap_change_percentage_24h_usd", 0) or 0
            )
        except Exception:
            global_change = 0.0

        signal, confidence, reason = self._decide(fg_value, fg_label, change_24h, global_change)

        logger.info(
            "MarketSentiment [%s]: F&G=%d (%s) 24h=%.2f%% global=%.2f%% -> %s (%d%%)",
            symbol, fg_value, fg_label, change_24h, global_change, signal, confidence,
        )
        return GiantReport(name=self.name, signal=signal, confidence=confidence, reason=reason)

    def _decide(self, fg: int, fg_label: str, change_24h: float, global_change: float):
        if fg < 25 and change_24h > -3:
            conf = min(90, 60 + (25 - fg))
            return "BUY", conf, f"Extreme fear ({fg_label}, {fg}) with stable/recovering price"
        if fg > 75:
            conf = min(90, 55 + (fg - 75))
            return "SELL", conf, f"Extreme greed ({fg_label}, {fg}) — market overheated"
        if change_24h < -6:
            return "SELL", 70, f"Sharp 24h drop ({change_24h:.1f}%) signals bearish momentum"
        if fg < 40 and global_change > 1:
            return "BUY", 55, f"Fear index ({fg}) with positive global market (+{global_change:.1f}%)"
        if fg > 60 and global_change < -1:
            return "SELL", 55, f"Greed index ({fg}) with declining global market ({global_change:.1f}%)"
        return "HOLD", 50, f"Neutral conditions — F&G={fg}, 24h={change_24h:.1f}%"


# ── Giant 2 — AI Judge (OpenRouter) ───────────────────────────────────────────

class AIJudgeGiant(BaseGiant):
    """
    The final decision-maker. Does NOT produce its own market signal.
    Receives reports from all other giants and calls OpenRouter LLM
    to synthesize a final BUY/SELL/HOLD with confidence and reason.

    Rate-limiting is enforced by ConsensusEngine (AI_JUDGE_INTERVAL_MINUTES).
    """

    name = "AIJudge"

    _SYSTEM_PROMPT = (
        "You are an expert crypto trading judge. "
        "You receive analysis reports from specialized market agents and must "
        "synthesize them into one final trading decision. "
        "Be concise and decisive. "
        "Always respond with ONLY a valid JSON object — no markdown, no explanation outside JSON."
    )

    def __init__(self, model: str = OPENROUTER_MODEL) -> None:
        self.model = model

    async def analyze(self, symbol: str, price: float) -> GiantReport:
        return GiantReport(
            name=self.name, signal="HOLD", confidence=0,
            reason="AIJudge must be called via judge(reports), not analyze()",
        )

    async def judge(
        self,
        symbol: str,
        price: float,
        reports: List[GiantReport],
    ) -> GiantReport:
        """Call OpenRouter with all giant reports and return final decision."""
        if not OPENROUTER_API_KEY:
            logger.warning("OPENROUTER_API_KEY not set — AIJudge returning HOLD")
            return GiantReport(
                name=self.name, signal="HOLD", confidence=0,
                reason="OpenRouter API key not configured",
            )

        agent_lines = "\n".join(r.to_prompt_line() for r in reports)
        user_prompt = (
            f"Symbol: {symbol} | Current Price: {price:.4f} USDT\n\n"
            f"Agent Reports:\n{agent_lines}\n\n"
            f"Based on these reports, provide your final trading decision.\n"
            f'Respond with ONLY this JSON (no other text):\n'
            f'{{"signal": "BUY|SELL|HOLD", "confidence": 0-100, "reason": "one clear sentence"}}'
        )

        payload = json.dumps({
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 150,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
        }).encode()

        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://github.com/mejodmando-art/orderbook",
                "X-Title":       "SuperConsensus Bot",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())

            content = data["choices"][0]["message"]["content"].strip()
            logger.debug("AIJudge raw response: %s", content)

            match = re.search(r"\{[^{}]+\}", content, re.DOTALL)
            if not match:
                raise ValueError(f"No JSON in response: {content[:200]}")

            result     = json.loads(match.group())
            signal     = str(result.get("signal", "HOLD")).upper()
            confidence = int(result.get("confidence", 50))
            reason     = str(result.get("reason", "No reason provided"))

            if signal not in ("BUY", "SELL", "HOLD"):
                signal = "HOLD"

            logger.info("AIJudge [%s]: %s (%d%%) — %s", symbol, signal, confidence, reason)
            return GiantReport(name=self.name, signal=signal, confidence=confidence, reason=reason)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode()[:200]
            logger.error("AIJudge HTTP error %d: %s", exc.code, body)
            return GiantReport(
                name=self.name, signal="HOLD", confidence=0,
                reason=f"AI API error {exc.code}: {body[:80]}",
            )
        except Exception as exc:
            logger.error("AIJudge failed: %s", exc)
            return GiantReport(
                name=self.name, signal="HOLD", confidence=0,
                reason=f"AI judge unavailable: {exc}",
            )


# ── Giants 3 / 4 / 5 — Placeholder stubs ──────────────────────────────────────

class PlaceholderGiant(BaseGiant):
    """
    Ready-to-replace stub. Returns HOLD with 0 confidence so AIJudge
    knows this source has no data yet.

    To activate: subclass PlaceholderGiant, override analyze(),
    and replace the instance in MARKET_GIANTS below.
    """

    def __init__(self, slot: int) -> None:
        self.name = f"Giant{slot}"
        self.slot = slot

    async def analyze(self, symbol: str, price: float) -> GiantReport:
        return GiantReport(
            name=self.name, signal="HOLD", confidence=0,
            reason=f"Slot {self.slot} — not yet connected to an API",
        )


# ── Registry ───────────────────────────────────────────────────────────────────

MARKET_GIANTS: List[BaseGiant] = [
    MarketSentimentGiant(),   # Giant1 — live data
    PlaceholderGiant(3),      # Giant3 — future API slot
    PlaceholderGiant(4),      # Giant4 — future API slot
    PlaceholderGiant(5),      # Giant5 — future API slot
]

AI_JUDGE = AIJudgeGiant()
