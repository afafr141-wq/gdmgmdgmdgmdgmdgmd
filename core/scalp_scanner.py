"""
Market scanner for the scalping engine.

Fetches top coins from CoinGecko (CoinPaprika fallback), pre-filters by
momentum + volume, runs 3 AI analysts via OpenRouter, merges consensus,
and returns the top pick symbol ready for scalp_engine.start().

All HTTP calls are synchronous (urllib) — wrapped in asyncio.to_thread
so they don't block the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

import os

log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
SCAN_COIN_COUNT     = 50
PROMPT_COIN_LIMIT   = 20
SCAN_TOP_PICKS      = 5
SCAN_FINAL_TOP      = 3
ANALYST_DELAY_S     = 3
MARKET_CACHE_TTL    = 300   # seconds

ANALYST_MODELS = [
    ("LLaMA-70B",   "meta-llama/llama-3.3-70b-instruct:free"),
    ("Gemma-27B",   "google/gemma-3-27b-it:free"),
    ("GPT-OSS-20B", "openai/gpt-oss-20b:free"),
]
JUDGE_MODEL = "openai/gpt-oss-120b:free"

STABLECOINS = {
    "usdt","usdc","busd","dai","tusd","usdp","usdd","frax","lusd",
    "susd","gusd","fdusd","pyusd","usd1","usde","usds","crvusd",
    "xaut","paxg","wbtc","steth","weth","cbbtc",
}

# ── Cache ──────────────────────────────────────────────────────────────────────

_market_cache: dict[int, tuple] = {}


def _get_cached(count: int):
    cached = _market_cache.get(count)
    if cached:
        ts, coins = cached
        if time.monotonic() - ts < MARKET_CACHE_TTL:
            return coins
    return None


def _set_cache(count: int, coins: list) -> None:
    _market_cache[count] = (time.monotonic(), coins)


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class CoinData:
    id:         str
    symbol:     str
    name:       str
    price:      float
    market_cap: float
    volume_24h: float
    change_24h: float
    change_7d:  float
    rank:       int

    def summary_line(self) -> str:
        return (
            f"{self.symbol.upper()}: ${self.price:.4f} | "
            f"24h:{self.change_24h:+.1f}% 7d:{self.change_7d:+.1f}% | "
            f"vol:${self.volume_24h/1e6:.1f}M"
        )


@dataclass
class AnalystPick:
    symbol:     str
    name:       str
    signal:     str
    confidence: int
    reason:     str


@dataclass
class AnalystReport:
    analyst_name: str
    picks:        list[AnalystPick] = field(default_factory=list)
    error:        Optional[str]     = None


@dataclass
class FinalPick:
    rank:       int
    symbol:     str
    name:       str
    signal:     str
    confidence: int
    reason:     str
    analysts:   list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    coins_scanned:    int
    scan_duration_s:  float
    final_picks:      list[FinalPick]
    analyst_reports:  list[AnalystReport]
    top_symbol:       Optional[str] = None   # best BUY pick for auto-entry


# ── Market data ────────────────────────────────────────────────────────────────

def _fetch_coingecko_raw(count: int, retries: int = 3) -> list:
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&order=market_cap_desc&per_page={count}&page=1"
        "&sparkline=false&price_change_percentage=24h,7d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "ScalpBot/1.0"})
    last_exc: Exception = RuntimeError("no attempts")
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                wait = 10 * attempt
                log.warning("CoinGecko 429 — sleeping %ds", wait)
                time.sleep(wait)
            else:
                raise
        except Exception as exc:
            last_exc = exc
            time.sleep(3)
    raise last_exc


def _parse_coingecko(raw: list) -> list[CoinData]:
    coins = []
    for i, item in enumerate(raw):
        sym = (item.get("symbol") or "").lower()
        if sym in STABLECOINS:
            continue
        coins.append(CoinData(
            id=item.get("id", ""),
            symbol=sym,
            name=item.get("name", ""),
            price=float(item.get("current_price") or 0),
            market_cap=float(item.get("market_cap") or 0),
            volume_24h=float(item.get("total_volume") or 0),
            change_24h=float(item.get("price_change_percentage_24h") or 0),
            change_7d=float(item.get("price_change_percentage_7d_in_currency") or 0),
            rank=i + 1,
        ))
    return coins


def _fetch_coinpaprika_fallback(count: int) -> list[CoinData]:
    with urllib.request.urlopen(
        urllib.request.Request(
            "https://api.coinpaprika.com/v1/tickers",
            headers={"User-Agent": "ScalpBot/1.0"},
        ),
        timeout=30,
    ) as r:
        raw = json.loads(r.read())
    coins, rank = [], 0
    for item in raw:
        sym = (item.get("symbol") or "").lower()
        if sym in STABLECOINS:
            continue
        rank += 1
        if rank > count:
            break
        q = item.get("quotes", {}).get("USD", {})
        coins.append(CoinData(
            id=item.get("id", ""),
            symbol=sym,
            name=item.get("name", ""),
            price=float(q.get("price") or 0),
            market_cap=float(q.get("market_cap") or 0),
            volume_24h=float(q.get("volume_24h") or 0),
            change_24h=float(q.get("percent_change_24h") or 0),
            change_7d=float(q.get("percent_change_7d") or 0),
            rank=rank,
        ))
    return coins


def _fetch_top_coins(count: int = SCAN_COIN_COUNT) -> list[CoinData]:
    coins = _get_cached(count)
    if coins:
        return coins
    try:
        raw   = _fetch_coingecko_raw(count)
        coins = _parse_coingecko(raw)
        source = "CoinGecko"
    except Exception as cg_exc:
        log.warning("CoinGecko failed (%s) — trying CoinPaprika", cg_exc)
        try:
            coins  = _fetch_coinpaprika_fallback(count)
            source = "CoinPaprika"
        except Exception as cp_exc:
            raise RuntimeError(
                f"Both sources failed. CoinGecko: {cg_exc} | CoinPaprika: {cp_exc}"
            ) from cp_exc
    _set_cache(count, coins)
    log.info("%s: %d coins fetched", source, len(coins))
    return coins


def _prefilter(coins: list[CoinData], limit: int = PROMPT_COIN_LIMIT) -> list[CoinData]:
    def score(c: CoinData) -> float:
        vol_ratio = (c.volume_24h / c.market_cap) if c.market_cap > 0 else 0
        momentum  = c.change_24h + c.change_7d * 0.3
        return vol_ratio * 100 + momentum
    return sorted(coins, key=score, reverse=True)[:limit]


# ── AI analysts ────────────────────────────────────────────────────────────────

_ANALYST_SYSTEM = (
    "You are a crypto trading analyst. "
    "Respond with ONLY a valid JSON array. No markdown, no text outside the JSON."
)


def _build_prompt(coins: list[CoinData], top_n: int) -> str:
    lines = "\n".join(c.summary_line() for c in coins)
    return (
        f"Pick the TOP {top_n} best BUY opportunities from this list.\n\n"
        f"{lines}\n\n"
        f"Reply with ONLY this JSON (no other text):\n"
        f'[{{"symbol":"BTC","name":"Bitcoin","signal":"BUY","confidence":75,'
        f'"reason":"one sentence"}}, ...]\n'
        f"Exactly {top_n} items. signal=BUY/SELL/HOLD, confidence=0-100."
    )


def _extract_json_array(text: str) -> list:
    if not text or not text.strip():
        raise ValueError("Empty response")
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    pass
    raise ValueError(f"No JSON array found: {text[:200]}")


def _call_openrouter(model: str, system: str, user: str,
                     max_tokens: int = 500, retries: int = 2) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    payload = json.dumps({
        "model": model,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }).encode()
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/scalp-bot",
        "X-Title":       "ScalpBot",
    }
    last_exc: Exception = RuntimeError("no attempts")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=payload, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            content = data["choices"][0]["message"]["content"]
            if not content or not content.strip():
                raise ValueError(f"Empty response from {model}")
            return content.strip()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            body = ""
            try:
                body = exc.read().decode()[:200]
            except Exception:
                pass
            if exc.code == 429:
                wait = 15 * attempt
                log.warning("OpenRouter 429 [%s] — waiting %ds", model, wait)
                time.sleep(wait)
            elif exc.code in (502, 503, 504):
                log.warning("OpenRouter %d [%s] — retrying", exc.code, model)
                time.sleep(5)
            else:
                raise RuntimeError(f"HTTP {exc.code} [{model}]: {body}") from exc
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(3)
    raise last_exc


def _run_analyst_sync(name: str, model: str, coins: list[CoinData]) -> AnalystReport:
    try:
        prompt = _build_prompt(coins, SCAN_TOP_PICKS)
        raw    = _call_openrouter(model, _ANALYST_SYSTEM, prompt)
        items  = _extract_json_array(raw)
        picks  = [
            AnalystPick(
                symbol=p.get("symbol", "").upper(),
                name=p.get("name", ""),
                signal=p.get("signal", "HOLD").upper(),
                confidence=int(p.get("confidence", 50)),
                reason=str(p.get("reason", "")),
            )
            for p in items if isinstance(p, dict)
        ]
        return AnalystReport(analyst_name=name, picks=picks)
    except Exception as exc:
        log.error("Analyst %s failed: %s", name, exc)
        return AnalystReport(analyst_name=name, error=str(exc))


def _merge_reports(reports: list[AnalystReport]) -> dict:
    merged: dict = {}
    for report in reports:
        if report.error or not report.picks:
            continue
        for pick in report.picks:
            sym = pick.symbol
            if sym not in merged:
                merged[sym] = {
                    "name": pick.name, "signal": pick.signal,
                    "votes": 0, "confidence": 0,
                    "analysts": [], "reasons": [],
                }
            merged[sym]["votes"]      += 1
            merged[sym]["confidence"] += pick.confidence
            merged[sym]["analysts"].append(report.analyst_name)
            merged[sym]["reasons"].append(pick.reason)
    for data in merged.values():
        if data["votes"] > 0:
            data["confidence"] //= data["votes"]
    return merged


def _run_judge_sync(merged: dict, reports: list[AnalystReport]) -> list[FinalPick]:
    consensus = {s: d for s, d in merged.items() if d["votes"] >= 2}
    all_picks = consensus if consensus else merged

    lines = []
    for sym, d in list(all_picks.items())[:PROMPT_COIN_LIMIT]:
        lines.append(
            f"{sym} ({d['name']}): votes={d['votes']} conf={d['confidence']}% "
            f"signal={d['signal']} reason={d['reasons'][0] if d['reasons'] else ''}"
        )

    prompt = (
        f"These coins were picked by multiple AI analysts. "
        f"Choose the TOP {SCAN_FINAL_TOP} best BUY opportunities:\n\n"
        + "\n".join(lines)
        + f"\n\nReply ONLY with JSON array of {SCAN_FINAL_TOP} items:\n"
        f'[{{"rank":1,"symbol":"BTC","name":"Bitcoin","signal":"BUY",'
        f'"confidence":80,"reason":"one sentence","analysts":["LLaMA-70B"]}}]'
    )

    try:
        raw   = _call_openrouter(JUDGE_MODEL, _ANALYST_SYSTEM, prompt, max_tokens=600)
        items = _extract_json_array(raw)
        picks = []
        for p in items:
            if not isinstance(p, dict):
                continue
            picks.append(FinalPick(
                rank=int(p.get("rank", len(picks) + 1)),
                symbol=str(p.get("symbol", "")).upper(),
                name=str(p.get("name", "")),
                signal=str(p.get("signal", "HOLD")).upper(),
                confidence=int(p.get("confidence", 50)),
                reason=str(p.get("reason", "")),
                analysts=p.get("analysts", []),
            ))
        return picks[:SCAN_FINAL_TOP]
    except Exception as exc:
        log.error("Judge failed: %s — using fallback", exc)
        fallback = []
        for i, (sym, d) in enumerate(
            sorted(all_picks.items(), key=lambda x: (-x[1]["votes"], -x[1]["confidence"])),
            start=1,
        ):
            if i > SCAN_FINAL_TOP:
                break
            fallback.append(FinalPick(
                rank=i, symbol=sym, name=d["name"],
                signal=d["signal"], confidence=d["confidence"],
                reason=(d["reasons"][0] if d["reasons"] else ""),
                analysts=d["analysts"],
            ))
        return fallback


# ── Public async API ───────────────────────────────────────────────────────────

async def scan(coin_count: int = SCAN_COIN_COUNT) -> ScanResult:
    """Run full scan: fetch → filter → 3 analysts → judge → return result."""
    t0 = time.monotonic()

    coins = await asyncio.to_thread(_fetch_top_coins, coin_count)
    filtered = _prefilter(coins)

    reports: list[AnalystReport] = []
    for i, (name, model) in enumerate(ANALYST_MODELS):
        if i > 0:
            await asyncio.sleep(ANALYST_DELAY_S)
        report = await asyncio.to_thread(_run_analyst_sync, name, model, filtered)
        reports.append(report)

    merged      = _merge_reports(reports)
    final_picks = await asyncio.to_thread(_run_judge_sync, merged, reports)

    # Best BUY pick for auto-entry — format as BASE/USDT for ccxt
    top_symbol = None
    for pick in final_picks:
        if pick.signal == "BUY" and pick.symbol:
            sym = pick.symbol.upper().replace("USDT", "").strip()
            if sym:
                top_symbol = f"{sym}/USDT"
            break

    return ScanResult(
        coins_scanned=len(coins),
        scan_duration_s=time.monotonic() - t0,
        final_picks=final_picks,
        analyst_reports=reports,
        top_symbol=top_symbol,
    )
