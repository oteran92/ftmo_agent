"""
Signal Engine — EMA Trend + Pullback methodology.
Fetches OHLC candles from TwelveData REST API (no MT5 dependency).

Methodology (professional swing trading, FTMO-safe):
  1. Trend filter  (D1):  Price vs EMA50 → defines bias (long or short only)
  2. Setup trigger (H4):  Pullback to EMA20 zone
  3. Confirmation  (H4):  Engulfing or pin bar candle at the EMA zone
  4. Entry:               Close of confirmation candle (or next open)
  5. SL:                  Beyond the pullback extreme (candle low/high)
  6. TP:                  Entry ± (SL distance × 2.0) minimum

Data source: TwelveData API (https://twelvedata.com)
  Free plan: 800 req/day. Usage: ~108 req/day for 9 pairs x 6 H4 closes.
  Set TWELVEDATA_API_KEY in .env
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

# ── TwelveData configuration ───────────────────────────────────────────────────
_TD_BASE = "https://api.twelvedata.com"
_TD_TIMEOUT = 10  # seconds

# Symbol mapping: internal → TwelveData format
_SYMBOL_MAP = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "XAUUSD": "XAU/USD",
    "EURJPY": "EUR/JPY",
    "GBPJPY": "GBP/JPY",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",
    "USDCHF": "USD/CHF",
}

# Pip size per symbol for distance calculations
_PIP_SIZE = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "XAUUSD": 0.1,
    "EURJPY": 0.01,
    "GBPJPY": 0.01,
    "AUDUSD": 0.0001,
    "USDCAD": 0.0001,
    "USDCHF": 0.0001,
}

# In-memory cache to avoid redundant API calls within the same cycle
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 240  # 4 minutes — safe buffer inside an H4 cycle

# Delay between pair requests to stay under TwelveData free plan rate limit (8 req/min).
# 15s between pairs + 5s between D1/H4 = 4 pairs in ~80s, max 2 credits per 15s window.
_REQUEST_DELAY = 15  # seconds between pairs during a full scan
_INTRA_PAIR_DELAY = 5  # seconds between D1 and H4 fetch for the same pair


def _api_key() -> str:
    key = os.environ.get("TWELVEDATA_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "TWELVEDATA_API_KEY not set. Add it to your .env file. "
            "Get a free key at https://twelvedata.com"
        )
    return key


def _cached_get(url: str, params: dict) -> dict | None:
    """GET request with TTL cache to preserve daily API quota."""
    cache_key = url + json.dumps(params, sort_keys=True)
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return data
    try:
        resp = requests.get(url, params=params, timeout=_TD_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            return None
        _cache[cache_key] = (time.time(), data)
        return data
    except (requests.RequestException, ValueError):
        return None


def _fetch_candles(symbol: str, interval: str, count: int = 100) -> list[dict] | None:
    """
    Fetch OHLC candles from TwelveData.
    Returns list of dicts {t, o, h, l, c} sorted oldest first.
    """
    td_sym = _SYMBOL_MAP.get(symbol)
    if not td_sym:
        return None

    data = _cached_get(
        f"{_TD_BASE}/time_series",
        {
            "symbol":     td_sym,
            "interval":   interval,
            "outputsize": count,
            "apikey":     _api_key(),
            "order":      "ASC",  # oldest first — required for EMA calculation
        },
    )
    if not data or "values" not in data:
        return None

    candles = []
    for v in data["values"]:
        try:
            fmt = "%Y-%m-%d %H:%M:%S" if " " in v["datetime"] else "%Y-%m-%d"
            candles.append({
                "t": int(time.mktime(time.strptime(v["datetime"], fmt))),
                "o": float(v["open"]),
                "h": float(v["high"]),
                "l": float(v["low"]),
                "c": float(v["close"]),
            })
        except (KeyError, ValueError):
            continue
    return candles if candles else None


def _fetch_price(symbol: str) -> float | None:
    """
    Fetch latest price from TwelveData.
    Only called when not rate-limited — falls back to last H4 close otherwise.
    """
    td_sym = _SYMBOL_MAP.get(symbol)
    if not td_sym:
        return None
    data = _cached_get(
        f"{_TD_BASE}/price",
        {"symbol": td_sym, "apikey": _api_key()},
    )
    if not data or "price" not in data:
        return None
    try:
        return float(data["price"])
    except (ValueError, TypeError):
        return None


# ── EMA calculation ────────────────────────────────────────────────────────────
def _ema(closes: list[float], period: int) -> list[float]:
    """Calculate EMA for a list of closing prices (oldest first)."""
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


# ── Candle pattern detection ───────────────────────────────────────────────────
def _is_bullish_engulfing(prev: dict, curr: dict) -> bool:
    """Prev is bearish, curr is bullish and engulfs prev body."""
    return (
        prev["o"] > prev["c"] and       # prev bearish
        curr["c"] > curr["o"] and       # curr bullish
        curr["o"] <= prev["c"] and
        curr["c"] >= prev["o"]
    )


def _is_bearish_engulfing(prev: dict, curr: dict) -> bool:
    """Prev is bullish, curr is bearish and engulfs prev body."""
    return (
        curr["c"] > curr["o"] and       # prev bullish (note: prev = h4_prev)
        prev["c"] < prev["o"] and       # curr bearish
        curr["o"] >= prev["c"] and
        curr["c"] <= prev["o"]
    )


def _is_pin_bar(candle: dict, direction: str) -> bool:
    """
    Pin bar: small body, long wick in the rejection direction.
    direction='bull' → long lower wick; direction='bear' → long upper wick.
    """
    body        = abs(candle["c"] - candle["o"])
    total_range = candle["h"] - candle["l"]
    if total_range == 0:
        return False
    body_ratio = body / total_range
    if direction == "bull":
        lower_wick = min(candle["o"], candle["c"]) - candle["l"]
        return body_ratio < 0.35 and lower_wick > total_range * 0.55
    else:
        upper_wick = candle["h"] - max(candle["o"], candle["c"])
        return body_ratio < 0.35 and upper_wick > total_range * 0.55


# ── Core analysis ──────────────────────────────────────────────────────────────
def analyze_setup(symbol: str = "EURUSD") -> dict[str, Any]:
    """
    Full setup analysis for a symbol using D1 trend and H4 entry.
    Data is fetched from TwelveData — no MT5 or local files required.
    """
    sym      = symbol.replace("/", "").upper()
    pip_size = _PIP_SIZE.get(sym, 0.0001)

    # ── Fetch data (D1 + H4 = 2 API credits per pair, within free plan budget) ──
    d1_candles = _fetch_candles(sym, "1day", count=100)
    time.sleep(_INTRA_PAIR_DELAY)  # gap between D1 and H4 to avoid rate limit bursts
    h4_candles = _fetch_candles(sym, "4h",   count=100)

    if d1_candles is None:
        return {
            "signal": "NO_DATA",
            "symbol": sym,
            "error": f"TwelveData returned no D1 data for {sym}. Check TWELVEDATA_API_KEY.",
        }
    if h4_candles is None:
        return {
            "signal": "NO_DATA",
            "symbol": sym,
            "error": f"TwelveData returned no H4 data for {sym}.",
        }
    if len(d1_candles) < 55 or len(h4_candles) < 25:
        return {
            "signal": "INSUFFICIENT_DATA",
            "symbol": sym,
            "d1_candles": len(d1_candles),
            "h4_candles": len(h4_candles),
            "error": "Need at least 55 D1 candles and 25 H4 candles.",
        }

    # ── Step 1: D1 trend via EMA50 ─────────────────────────────────────────────
    d1_closes     = [c["c"] for c in d1_candles]
    d1_ema50      = _ema(d1_closes, 50)
    d1_ema50_val  = d1_ema50[-1]
    d1_ema50_prev = d1_ema50[-2]

    # Use last H4 close as effective price (avoids extra API credit; fine for swing trading)
    live_price    = None  # skip separate price call to stay within 8 req/min free limit
    current_price = d1_candles[-1]["c"]

    trend_up      = current_price > d1_ema50_val
    ema_rising    = d1_ema50_val > d1_ema50_prev
    bias          = "LONG" if trend_up else "SHORT"
    distance_pips = abs(current_price - d1_ema50_val) / pip_size

    # ── Step 2: H4 EMA20 pullback zone ────────────────────────────────────────
    h4_closes    = [c["c"] for c in h4_candles]
    h4_ema20     = _ema(h4_closes, 20)
    h4_ema20_val = h4_ema20[-1]
    h4_last      = h4_candles[-1]
    h4_prev      = h4_candles[-2]

    effective_price = h4_last["c"]  # last closed H4 candle — accurate for swing entry
    in_ema_zone     = (h4_last["l"] <= h4_ema20_val <= h4_last["h"]) or \
                      (abs(effective_price - h4_ema20_val) / pip_size < 15)
    near_ema        = abs(effective_price - h4_ema20_val) / pip_size < 15

    # ── Step 3: Confirmation candle ────────────────────────────────────────────
    bull_engulf = _is_bullish_engulfing(h4_prev, h4_last)
    bear_engulf = _is_bearish_engulfing(h4_prev, h4_last)
    bull_pin    = _is_pin_bar(h4_last, "bull")
    bear_pin    = _is_pin_bar(h4_last, "bear")

    confirmation_long  = bull_engulf or bull_pin
    confirmation_short = bear_engulf or bear_pin

    # ── Step 4: Signal ─────────────────────────────────────────────────────────
    signal = "WAIT"
    pattern = "none"
    entry = sl = tp = rrr = 0.0

    if bias == "LONG" and (in_ema_zone or near_ema) and confirmation_long:
        signal  = "GO_LONG"
        pattern = "bullish engulfing" if bull_engulf else "bull pin bar"
        entry   = h4_last["c"]
        sl      = h4_last["l"] - (pip_size * 5)
        sl_dist = entry - sl
        tp      = entry + (sl_dist * 2.0)
        rrr     = (tp - entry) / sl_dist

    elif bias == "SHORT" and (in_ema_zone or near_ema) and confirmation_short:
        signal  = "GO_SHORT"
        pattern = "bearish engulfing" if bear_engulf else "bear pin bar"
        entry   = h4_last["c"]
        sl      = h4_last["h"] + (pip_size * 5)
        sl_dist = sl - entry
        tp      = entry - (sl_dist * 2.0)
        rrr     = (entry - tp) / sl_dist

    elif (in_ema_zone or near_ema) and bias == "LONG" and not confirmation_long:
        signal = "WATCH"
    elif (in_ema_zone or near_ema) and bias == "SHORT" and not confirmation_short:
        signal = "WATCH"

    # ── Build result ───────────────────────────────────────────────────────────
    result: dict[str, Any] = {
        "symbol": sym,
        "signal": signal,
        "bias":   bias,
        "analysis": {
            "d1_trend":                 f"Price {'above' if trend_up else 'below'} EMA50 — {'uptrend' if trend_up else 'downtrend'}",
            "d1_ema50":                 round(d1_ema50_val, 5),
            "d1_ema_direction":         "rising" if ema_rising else "falling",
            "distance_from_ema50_pips": round(distance_pips, 1),
            "h4_ema20":                 round(h4_ema20_val, 5),
            "live_price":               round(effective_price, 5),
            "h4_last_close":            round(h4_last["c"], 5),
            "in_ema20_zone":            in_ema_zone or near_ema,
            "confirmation":             pattern,
        },
    }

    if signal in ("GO_LONG", "GO_SHORT"):
        result["trade"] = {
            "entry":   round(entry, 5),
            "sl":      round(sl, 5),
            "tp":      round(tp, 5),
            "rrr":     round(rrr, 2),
            "sl_pips": round(abs(entry - sl) / pip_size, 1),
            "tp_pips": round(abs(tp - entry) / pip_size, 1),
        }
        result["next_step"] = (
            f"SETUP CONFIRMED — {signal} on {sym}. "
            f"Entry {round(entry,5)} | SL {round(sl,5)} | TP {round(tp,5)} | RRR {round(rrr,2)}"
        )
    elif signal == "WATCH":
        result["next_step"] = (
            f"Price at H4 EMA20 zone ({round(h4_ema20_val,5)}) but no confirmation candle yet. "
            f"Check again after next H4 close."
        )
    else:
        result["next_step"] = (
            f"No setup. Price not near H4 EMA20 ({round(h4_ema20_val,5)}). "
            f"Live: {round(effective_price,5)}. Wait for pullback."
        )

    # ── News filter: check calendar AFTER building the technical signal ─────────
    # Runs last so technical analysis is always complete regardless of news.
    try:
        from skills.news_filter import check_news_block
        news = check_news_block(sym)
        result["news"] = news

        if news["status"] == "BLOCK" and signal in ("GO_LONG", "GO_SHORT", "WATCH"):
            # Override: hard block — active news window
            result["signal"]    = "NEWS_BLOCK"
            result["next_step"] = f"Entry blocked — {news['message']}"

        elif news["status"] == "CAUTION" and signal in ("GO_LONG", "GO_SHORT"):
            # Downgrade GO → NEWS_CAUTION — setup is valid but entry is risky before major news
            result["signal"]    = "NEWS_CAUTION"
            result["next_step"] = (
                f"Setup valid ({signal}) but entry NOT recommended — {news['message']}. "
                f"Wait until after the event, then re-evaluate."
            )
    except Exception:
        pass  # news filter is non-critical — never block a result over a calendar fetch error

    return result


def scan_all_pairs() -> list[dict]:
    """
    Scan all monitored pairs via TwelveData — no MT5 required.
    Adds inter-pair delay to stay within free plan rate limit (8 req/min).
    """
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD",
             "EURJPY", "GBPJPY", "AUDUSD", "USDCAD", "USDCHF"]
    results = []
    for i, pair in enumerate(pairs):
        if i > 0:
            time.sleep(_REQUEST_DELAY)  # stay under 8 req/min free plan limit
        results.append(analyze_setup(pair))
    return results


def scan_all_pairs_cached() -> list[dict]:
    """
    Scan all pairs using the in-process cache.
    When called from the monitor (long-running process), the cache avoids redundant
    API calls between H4 cycles — only fetches fresh data when cache expires.
    """
    return scan_all_pairs()
