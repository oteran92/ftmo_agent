"""
backtest/data_loader.py
-----------------------
Cache-first OHLC loader for backtesting.

load_or_fetch(pair, interval, years) returns a list of candle dicts
[{t, o, h, l, c}] sorted oldest→newest. If a local cache file exists
in data/historical/, it is returned immediately. Otherwise TwelveData
is queried in reverse-paginated chunks, saved to disk, and returned.

TwelveData free plan limits: 800 req/day, 8 req/min.
Candles per interval over 5 years:
  1day   → ~1 300 candles  (1 request  per pair)
  4h     → ~11 000 candles (3 requests per pair, 5 000-candle pages)
  1week  → ~  260 candles  (1 request  per pair)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

# ── Paths ──────────────────────────────────────────────────────────────────────
_REPO_ROOT   = Path(__file__).parent.parent
_CACHE_DIR   = _REPO_ROOT / "data" / "historical"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── TwelveData settings ────────────────────────────────────────────────────────
_API_BASE       = "https://api.twelvedata.com"
_PAGE_SIZE      = 5000          # max candles per request (TwelveData)
_BETWEEN_REQS   = 8             # seconds between requests to stay ≤8 req/min
_API_KEY: str   = os.getenv("TWELVEDATA_API_KEY", "")

# TwelveData uses slash-separated forex symbols (EUR/USD, not EURUSD)
_SYMBOL_MAP: dict = {
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


def _to_td_symbol(pair: str) -> str:
    """Convert internal symbol (EURUSD) to TwelveData format (EUR/USD)."""
    return _SYMBOL_MAP.get(pair.upper(), pair)


def _cache_path(pair: str, interval: str) -> Path:
    return _CACHE_DIR / f"{pair}_{interval}.json"


def _load_cache(pair: str, interval: str) -> Optional[list]:
    """Return cached candles or None if cache missing."""
    p = _cache_path(pair, interval)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, list) and len(data) > 0:
                return data
        except Exception:
            pass
    return None


def _save_cache(pair: str, interval: str, candles: list[dict]) -> None:
    _cache_path(pair, interval).write_text(json.dumps(candles, separators=(",", ":")))


def _fetch_page(
    pair: str,
    interval: str,
    end_date: str,
    start_date: str,
) -> list[dict]:
    """
    Fetch one page from TwelveData for the given [start_date, end_date] window.
    Returns candles sorted oldest→newest, or [] on error.
    """
    if not _API_KEY:
        raise RuntimeError(
            "TWELVEDATA_API_KEY is not set. "
            "Export it in your shell or .env before running fetch_historical.py"
        )

    url = f"{_API_BASE}/time_series"
    params = {
        "symbol":       _to_td_symbol(pair),
        "interval":     interval,
        "start_date":   start_date,
        "end_date":     end_date,
        "outputsize":   _PAGE_SIZE,
        "format":       "JSON",
        "order":        "ASC",
        "apikey":       _API_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"  [data_loader] Request error for {pair}/{interval}: {exc}")
        return []

    if body.get("status") == "error":
        print(f"  [data_loader] API error for {pair}/{interval}: {body.get('message')}")
        return []

    raw_values = body.get("values", [])
    candles = []
    for v in raw_values:
        try:
            candles.append({
                "t": v["datetime"],
                "o": float(v["open"]),
                "h": float(v["high"]),
                "l": float(v["low"]),
                "c": float(v["close"]),
            })
        except (KeyError, ValueError):
            continue

    return candles


def fetch_all_pages(
    pair: str,
    interval: str,
    years: int = 5,
    verbose: bool = True,
) -> list[dict]:
    """
    Paginate TwelveData backwards to collect `years` of OHLC data.
    Adds a delay between requests to respect rate limits.
    Returns candles sorted oldest→newest.
    """
    from datetime import timedelta

    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=years * 365)

    end_date   = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    start_date = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    if verbose:
        print(f"  Fetching {pair} {interval} from {start_date[:10]} to {end_date[:10]} ...")

    # For most intervals a single page covers the window; for H4 we may need several.
    # We fetch the full window in one shot (TwelveData honours start/end properly),
    # then split into multiple requests only if count > _PAGE_SIZE.
    candles = _fetch_page(pair, interval, end_date, start_date)

    if not candles:
        return []

    # If we got a full page, there may be more data before the earliest candle
    page_count = 1
    while len(candles) % _PAGE_SIZE == 0:
        # Paginate: request data ending just before the oldest candle we have
        earliest_ts = candles[0]["t"]  # "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"
        if verbose:
            print(f"    Page {page_count}: got {len(candles)} candles, fetching earlier batch ...")
        time.sleep(_BETWEEN_REQS)
        older = _fetch_page(pair, interval, earliest_ts, start_date)
        if not older:
            break
        # Merge, drop potential duplicate at boundary
        candles = older + [c for c in candles if c["t"] > older[-1]["t"]]
        page_count += 1

    if verbose:
        print(f"    Total: {len(candles)} candles ({page_count} page(s))")

    return candles


def load_or_fetch(
    pair: str,
    interval: str,
    years: int = 5,
    verbose: bool = True,
    force_refresh: bool = False,
) -> list[dict]:
    """
    Return OHLC candles for `pair` + `interval` from disk cache if available,
    otherwise fetch from TwelveData, cache, and return.

    Parameters
    ----------
    pair          : e.g. "EURUSD"
    interval      : "1day", "4h", "1week"
    years         : how many years of history to fetch (default 5)
    verbose       : print progress to stdout
    force_refresh : ignore cache and re-fetch from API

    Returns list[{t, o, h, l, c}] sorted oldest→newest.
    """
    sym = pair.replace("/", "").upper()

    if not force_refresh:
        cached = _load_cache(sym, interval)
        if cached is not None:
            if verbose:
                print(f"  [cache hit] {sym}_{interval}.json → {len(cached)} candles")
            return cached

    candles = fetch_all_pages(sym, interval, years=years, verbose=verbose)
    if candles:
        _save_cache(sym, interval, candles)

    return candles
