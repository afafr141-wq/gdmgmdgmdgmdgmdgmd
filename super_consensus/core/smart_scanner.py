"""
Smart Scanner — scans top N coins by market cap and finds the best opportunities.

Flow:
  1. Fetch top N coins from CoinGecko (free, no API key needed)
  2. Run 3 AI analyst models in parallel via OpenRouter (free tier)
     Each analyst picks its top 5 BUY candidates with confidence + reason
  3. Merge results: coins appearing in 2+ lists are strong candidates
  4. A 4th AI Judge synthesises all 3 reports into a final top-3 list
  5. Return structured ScanResult for Telegram display

Models used (all free on OpenRouter):
  Analyst 1: nvidia/nemotron-3-super-120b-a12b:free
  Analyst 2: meta-llama/llama-3.3-70b-instruct:free
  Analyst 3: qwen/qwen3-235b-a22b:free
  Judge    : meta-llama/llama-3.3-70b-instruct:free  (reliable JSON output)
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
from typing import Dict, List, Optional, Tuple

from config.settings import OPENROUTER_API_KEY

# In-process cache: avoids repeated CoinGecko calls within the same process
_coingecko_cache: Dict[int, tuple] = {}   # count → (monotonic_ts, raw_list)
COINGECKO_CACHE_TTL = 300                 # reuse data for 5 minutes

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

SCAN_COIN_COUNT = 50          # how many top coins to fetch from CoinGecko
SCAN_TOP_PICKS  = 5           # each analyst picks this many coins
SCAN_FINAL_TOP  = 3           # judge outputs this many final coins

# Free models — ordered by reliability. All from different providers to
# spread rate-limit load. GPT-OSS-20B confirmed working; others are fallbacks.
# Primary analysts: try each in order, skip on failure
ANALYST_MODELS: List[Tuple[str, str]] = [
    ("GPT-OSS-20B",  "openai/gpt-oss-20b:free"),
    ("Qwen3-80B",    "qwen/qwen3-next-80b-a3b-instruct:free"),
    ("Nemotron-30B", "nvidia/nemotron-3-nano-30b-a3b:free"),
]
JUDGE_MODEL = "openai/gpt-oss-120b:free"

# Fallback analysts tried if primary ones all fail
FALLBACK_ANALYST_MODELS: List[Tuple[str, str]] = [
    ("LLaMA-3B",    "meta-llama/llama-3.2-3b-instruct:free"),
    ("Gemma4-26B",  "google/gemma-4-26b-a4b-it:free"),
    ("Hermes-405B", "nousresearch/hermes-3-llama-3.1-405b:free"),
]

# Send analysts sequentially with a gap to avoid simultaneous 429s
ANALYST_DELAY_SECONDS = 5   # wait between each analyst call

# Max coins to include in the AI prompt (pre-filtered by data quality)
PROMPT_COIN_LIMIT = 20

# Minimum successful analysts needed before calling the judge
MIN_SUCCESSFUL_ANALYSTS = 1

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HEADERS = {
    "Content-Type": "application/json",
    "HTTP-Referer": "https://github.com/mejodmando-art/orderbook",
    "X-Title":      "SuperConsensus SmartScanner",
}

# CoinGecko free endpoint — no key required
COINGECKO_MARKETS_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd"
    "&order=market_cap_desc"
    "&per_page={count}"
    "&page=1"
    "&sparkline=false"
    "&price_change_percentage=24h,7d"
)

# CoinPaprika fallback — completely free, no key, generous rate limits
COINPAPRIKA_TICKERS_URL = "https://api.coinpaprika.com/v1/tickers?limit={count}"

# Stablecoins to exclude from analysis
STABLECOINS = {
    "usdt", "usdc", "busd", "dai", "tusd", "usdp", "usdd",
    "frax", "lusd", "susd", "gusd", "fdusd", "pyusd",
    "usd1", "usde", "usds", "crvusd", "mkr", "xaut", "paxg",
    "wbtc", "steth", "weth", "cbbtc",  # wrapped/staked assets
}


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class CoinData:
    id:            str
    symbol:        str
    name:          str
    price:         float
    market_cap:    float
    volume_24h:    float
    change_24h:    float
    change_7d:     float
    rank:          int

    def summary_line(self) -> str:
        return (
            f"{self.rank}. {self.name} ({self.symbol.upper()}): "
            f"${self.price:.4f}, MCap=${self.market_cap/1e9:.2f}B, "
            f"Vol=${self.volume_24h/1e6:.1f}M, "
            f"24h={self.change_24h:+.1f}%, 7d={self.change_7d:+.1f}%"
        )


@dataclass
class AnalystPick:
    symbol:     str
    name:       str
    signal:     str   # BUY | SELL | HOLD
    confidence: int   # 0-100
    reason:     str


@dataclass
class AnalystReport:
    analyst_name: str
    model:        str
    picks:        List[AnalystPick] = field(default_factory=list)
    error:        Optional[str] = None


@dataclass
class FinalPick:
    rank:       int
    symbol:     str
    name:       str
    signal:     str
    confidence: int
    reason:     str
    analysts:   List[str]   # which analysts agreed


@dataclass
class ScanResult:
    coins_scanned:    int
    final_picks:      List[FinalPick]
    analyst_reports:  List[AnalystReport]
    scan_duration_s:  float
    timestamp:        float = field(default_factory=time.time)


# ── Market data fetch (CoinGecko → CoinPaprika fallback) ──────────────────────

def _http_get_json(url: str, timeout: int = 25) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "SuperConsensusBot/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _fetch_coingecko_raw(count: int, retries: int = 3) -> list:
    """CoinGecko /coins/markets — retries on 429 with backoff."""
    url = COINGECKO_MARKETS_URL.format(count=count)
    last_exc: Exception = RuntimeError("No attempts")
    for attempt in range(1, retries + 1):
        try:
            return _http_get_json(url)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                wait = 10 * attempt          # 10s, 20s, 30s
                logger.warning("CoinGecko 429 — sleeping %ds (attempt %d/%d)", wait, attempt, retries)
                time.sleep(wait)
            else:
                raise
        except Exception as exc:
            last_exc = exc
            time.sleep(3)
    raise last_exc


def _parse_coingecko(raw: list) -> List[CoinData]:
    coins = []
    for i, item in enumerate(raw, start=1):
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
            rank=i,
        ))
    return coins


def _fetch_coinpaprika_fallback(count: int) -> List[CoinData]:
    """
    CoinPaprika /v1/tickers — free, no key, 25 000 req/month.
    Used when CoinGecko is rate-limiting.
    """
    logger.info("Falling back to CoinPaprika for market data")
    # CoinPaprika returns all tickers sorted by rank; we slice after filtering
    url = "https://api.coinpaprika.com/v1/tickers"
    raw = _http_get_json(url, timeout=30)

    coins = []
    rank = 0
    for item in raw:
        sym = (item.get("symbol") or "").lower()
        if sym in STABLECOINS:
            continue
        rank += 1
        if rank > count:
            break
        quotes = item.get("quotes", {}).get("USD", {})
        coins.append(CoinData(
            id=item.get("id", ""),
            symbol=sym,
            name=item.get("name", ""),
            price=float(quotes.get("price") or 0),
            market_cap=float(quotes.get("market_cap") or 0),
            volume_24h=float(quotes.get("volume_24h") or 0),
            change_24h=float(quotes.get("percent_change_24h") or 0),
            change_7d=float(quotes.get("percent_change_7d") or 0),
            rank=rank,
        ))
    return coins


# Shared cache — keyed by count, stores (monotonic_ts, List[CoinData])
_market_cache: Dict[int, tuple] = {}
MARKET_CACHE_TTL = 300   # 5 minutes


def _fetch_top_coins(count: int = SCAN_COIN_COUNT) -> List[CoinData]:
    # Serve from cache if fresh
    cached = _market_cache.get(count)
    if cached:
        cached_at, coins = cached
        age = time.monotonic() - cached_at
        if age < MARKET_CACHE_TTL:
            logger.info("Market cache hit (age=%.0fs, %d coins)", age, len(coins))
            return coins

    # Try CoinGecko first, fall back to CoinPaprika
    try:
        raw = _fetch_coingecko_raw(count)
        coins = _parse_coingecko(raw)
        source = "CoinGecko"
    except Exception as cg_exc:
        logger.warning("CoinGecko failed (%s) — trying CoinPaprika", cg_exc)
        try:
            coins = _fetch_coinpaprika_fallback(count)
            source = "CoinPaprika"
        except Exception as cp_exc:
            raise RuntimeError(
                f"Both market data sources failed. "
                f"CoinGecko: {cg_exc} | CoinPaprika: {cp_exc}"
            ) from cp_exc

    _market_cache[count] = (time.monotonic(), coins)
    logger.info("%s: fetched %d coins, cached for %ds", source, len(coins), MARKET_CACHE_TTL)
    return coins


# ── OpenRouter call (sync, runs in thread) ─────────────────────────────────────

def _call_openrouter(
    model: str, system: str, user: str,
    max_tokens: int = 800, retries: int = 2
) -> str:
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

    headers = {**OPENROUTER_HEADERS, "Authorization": f"Bearer {OPENROUTER_API_KEY}"}

    last_exc: Exception = RuntimeError("No attempts")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                OPENROUTER_URL, data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())

            content = data["choices"][0]["message"]["content"]
            if not content or not content.strip():
                raise ValueError(f"Model {model} returned empty content")
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
                logger.warning("OpenRouter 429 [%s] — waiting %ds (attempt %d/%d)", model, wait, attempt, retries)
                time.sleep(wait)
            elif exc.code in (502, 503, 504):
                logger.warning("OpenRouter %d [%s] — retrying (attempt %d/%d)", exc.code, model, attempt, retries)
                time.sleep(5)
            else:
                raise RuntimeError(f"HTTP {exc.code} from OpenRouter [{model}]: {body}") from exc
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(3)

    raise last_exc


def _extract_json_array(text: str) -> list:
    """
    Extract the first JSON array from a model response.
    Handles markdown code fences, leading text, and partial responses.
    """
    if not text or not text.strip():
        raise ValueError("Empty response from model")

    # Strip markdown code fences
    text = re.sub(r"```(?:json)?", "", text).strip()

    # Try direct parse first
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Find the outermost [...] block (handles nested objects inside)
    depth = 0
    start = None
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

    raise ValueError(f"No valid JSON array found in response (first 200 chars): {text[:200]}")


# ── Analyst prompt & parsing ───────────────────────────────────────────────────

def _prefilter_coins(coins: List[CoinData], limit: int = PROMPT_COIN_LIMIT) -> List[CoinData]:
    """
    Reduce the coin list before sending to AI to keep prompts short.
    Scores each coin by volume/mcap ratio and 24h momentum, keeps top `limit`.
    """
    def score(c: CoinData) -> float:
        vol_ratio = (c.volume_24h / c.market_cap) if c.market_cap > 0 else 0
        momentum  = c.change_24h + (c.change_7d * 0.3)
        return vol_ratio * 100 + momentum

    ranked = sorted(coins, key=score, reverse=True)
    return ranked[:limit]


_ANALYST_SYSTEM = (
    "You are a crypto trading analyst. "
    "Respond with ONLY a valid JSON array. No markdown, no text outside the JSON."
)

def _build_analyst_prompt(coins: List[CoinData], top_n: int) -> str:
    coin_lines = "\n".join(c.summary_line() for c in coins)
    return (
        f"Pick the TOP {top_n} best BUY opportunities from this list.\n\n"
        f"{coin_lines}\n\n"
        f"Reply with ONLY this JSON (no other text):\n"
        f'[{{"symbol":"BTC","name":"Bitcoin","signal":"BUY","confidence":75,'
        f'"reason":"one sentence"}}, ...]\n'
        f"Exactly {top_n} items. signal=BUY/SELL/HOLD, confidence=0-100."
    )


async def _run_analyst(
    analyst_name: str,
    model: str,
    coins: List[CoinData],
) -> AnalystReport:
    filtered = _prefilter_coins(coins, PROMPT_COIN_LIMIT)
    prompt   = _build_analyst_prompt(filtered, SCAN_TOP_PICKS)
    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None, _call_openrouter, model, _ANALYST_SYSTEM, prompt, 500
        )
        logger.debug("Analyst %s raw: %s", analyst_name, raw[:300])

        items = _extract_json_array(raw)
        picks = []
        for item in items[:SCAN_TOP_PICKS]:
            picks.append(AnalystPick(
                symbol=str(item.get("symbol", "")).upper(),
                name=str(item.get("name", "")),
                signal=str(item.get("signal", "HOLD")).upper(),
                confidence=int(item.get("confidence", 50)),
                reason=str(item.get("reason", "")),
            ))
        logger.info("Analyst %s picked: %s", analyst_name, [p.symbol for p in picks])
        return AnalystReport(analyst_name=analyst_name, model=model, picks=picks)

    except Exception as exc:
        logger.error("Analyst %s failed: %s", analyst_name, exc)
        return AnalystReport(analyst_name=analyst_name, model=model, error=str(exc))


# ── Consensus merge ────────────────────────────────────────────────────────────

def _merge_analyst_reports(reports: List[AnalystReport]) -> Dict[str, dict]:
    """
    Aggregate picks across analysts.
    Returns dict keyed by symbol with vote count, avg confidence, names, reasons.
    """
    merged: Dict[str, dict] = {}
    for report in reports:
        if report.error or not report.picks:
            continue
        for pick in report.picks:
            sym = pick.symbol
            if sym not in merged:
                merged[sym] = {
                    "name":       pick.name,
                    "signal":     pick.signal,
                    "votes":      0,
                    "confidence": 0,
                    "analysts":   [],
                    "reasons":    [],
                }
            merged[sym]["votes"]      += 1
            merged[sym]["confidence"] += pick.confidence
            merged[sym]["analysts"].append(report.analyst_name)
            merged[sym]["reasons"].append(f"{report.analyst_name}: {pick.reason}")

    # Average confidence
    for sym, data in merged.items():
        data["confidence"] = data["confidence"] // data["votes"]

    return merged


# ── Judge prompt & parsing ─────────────────────────────────────────────────────

_JUDGE_SYSTEM = (
    "You are the chief crypto trading judge. "
    "You receive analysis reports from multiple AI analysts and must synthesize them "
    "into a final ranked list of the best trading opportunities. "
    "Always respond with ONLY a valid JSON array — no markdown, no explanation outside JSON."
)

def _build_judge_prompt(
    reports: List[AnalystReport],
    merged: Dict[str, dict],
    final_n: int,
) -> str:
    # Analyst summaries
    analyst_blocks = []
    for r in reports:
        if r.error:
            analyst_blocks.append(f"[{r.analyst_name}] ERROR: {r.error}")
            continue
        picks_str = ", ".join(
            f"{p.symbol}({p.signal},{p.confidence}%)" for p in r.picks
        )
        analyst_blocks.append(f"[{r.analyst_name}] Picks: {picks_str}")

    # Consensus summary
    consensus_lines = []
    for sym, data in sorted(merged.items(), key=lambda x: -x[1]["votes"]):
        consensus_lines.append(
            f"  {sym} ({data['name']}): {data['votes']} votes, "
            f"avg confidence {data['confidence']}%, "
            f"analysts: {', '.join(data['analysts'])}"
        )

    return (
        f"Three AI analysts have reviewed the crypto market. Here are their reports:\n\n"
        + "\n".join(analyst_blocks)
        + f"\n\nConsensus Summary (coins appearing in multiple lists):\n"
        + "\n".join(consensus_lines or ["No consensus found"])
        + f"\n\nBased on all reports and consensus, select the FINAL TOP {final_n} "
        f"best opportunities. Prioritize coins with multiple analyst agreement.\n\n"
        f"Respond with ONLY a JSON array of exactly {final_n} objects:\n"
        f'[{{"rank":1,"symbol":"BTC","name":"Bitcoin","signal":"BUY","confidence":80,'
        f'"reason":"one clear unified reason","analysts":["Nemotron","LLaMA-70B"]}}, ...]\n\n'
        f"rank is 1 (best) to {final_n}. signal must be BUY, SELL, or HOLD."
    )


async def _run_judge(
    reports: List[AnalystReport],
    merged: Dict[str, dict],
) -> List[FinalPick]:
    prompt = _build_judge_prompt(reports, merged, SCAN_FINAL_TOP)
    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None, _call_openrouter, JUDGE_MODEL, _JUDGE_SYSTEM, prompt, 800
        )
        logger.debug("Judge raw: %s", raw[:400])

        items = _extract_json_array(raw)
        picks = []
        for item in items[:SCAN_FINAL_TOP]:
            picks.append(FinalPick(
                rank=int(item.get("rank", len(picks) + 1)),
                symbol=str(item.get("symbol", "")).upper(),
                name=str(item.get("name", "")),
                signal=str(item.get("signal", "HOLD")).upper(),
                confidence=int(item.get("confidence", 50)),
                reason=str(item.get("reason", "")),
                analysts=list(item.get("analysts", [])),
            ))
        logger.info("Judge final picks: %s", [p.symbol for p in picks])
        return picks

    except Exception as exc:
        logger.error("Judge failed: %s", exc)
        # Fallback: return top merged coins by votes
        fallback = []
        for i, (sym, data) in enumerate(
            sorted(merged.items(), key=lambda x: (-x[1]["votes"], -x[1]["confidence"])),
            start=1,
        ):
            if i > SCAN_FINAL_TOP:
                break
            fallback.append(FinalPick(
                rank=i,
                symbol=sym,
                name=data["name"],
                signal=data["signal"],
                confidence=data["confidence"],
                reason=f"Consensus pick ({data['votes']} analysts agreed). Judge unavailable: {exc}",
                analysts=data["analysts"],
            ))
        return fallback


# ── Main scanner entry point ───────────────────────────────────────────────────

class SmartScanner:
    """
    Orchestrates the full market scan:
      fetch → 3 analysts in parallel → merge → judge → ScanResult
    """

    async def scan(self, coin_count: int = SCAN_COIN_COUNT) -> ScanResult:
        t0 = time.monotonic()

        # 1. Fetch market data
        logger.info("SmartScanner: fetching top %d coins from CoinGecko…", coin_count)
        try:
            coins = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_top_coins, coin_count
            )
        except Exception as exc:
            logger.error("CoinGecko fetch failed: %s", exc)
            raise RuntimeError(f"Failed to fetch market data: {exc}") from exc

        logger.info("SmartScanner: got %d coins (after filtering stablecoins)", len(coins))

        # 2. Run analysts sequentially with a delay between each.
        # If a primary analyst fails, try a fallback model in its slot.
        reports: List[AnalystReport] = []
        fallback_iter = iter(FALLBACK_ANALYST_MODELS)

        for i, (name, model) in enumerate(ANALYST_MODELS):
            if i > 0:
                await asyncio.sleep(ANALYST_DELAY_SECONDS)
            report = await _run_analyst(name, model, coins)

            # If primary failed, try one fallback in its place
            if report.error:
                try:
                    fb_name, fb_model = next(fallback_iter)
                    logger.info(
                        "Primary analyst %s failed — trying fallback %s", name, fb_name
                    )
                    await asyncio.sleep(2)
                    fb_report = await _run_analyst(fb_name, fb_model, coins)
                    if not fb_report.error:
                        report = fb_report
                except StopIteration:
                    pass  # no more fallbacks

            reports.append(report)

        successful = sum(1 for r in reports if not r.error)
        logger.info(
            "SmartScanner: %d/%d analysts succeeded", successful, len(reports)
        )

        # 3. Merge results
        merged = _merge_analyst_reports(reports)
        logger.info(
            "SmartScanner: merged %d unique coins, consensus coins: %s",
            len(merged),
            [s for s, d in merged.items() if d["votes"] >= 2],
        )

        # 4. Judge
        final_picks = await _run_judge(reports, merged)

        duration = time.monotonic() - t0
        logger.info("SmartScanner: scan complete in %.1fs", duration)

        return ScanResult(
            coins_scanned=len(coins),
            final_picks=final_picks,
            analyst_reports=reports,
            scan_duration_s=duration,
        )


# Singleton
SMART_SCANNER = SmartScanner()
