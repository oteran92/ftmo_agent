"""
COT Fetcher — FTMO Academy Pillar 3 (Sentiment Analysis).

Fetches CFTC Commitments of Traders (COT) data to determine institutional
positioning (large speculators / leveraged funds) for FX currency futures.

Data source: CFTC Public Reporting API (free, no authentication required)
  https://publicreporting.cftc.gov/api/odata/v1/VisualizationData_COT_FuturesOnly_AllYears

Cache: data/cot_cache.json — refreshed once per week (COT published every Friday 3:30 PM ET).
Stale data is acceptable for swing trading — positions rarely flip in one week.

Sentiment signal interpretation:
  - Leveraged funds (hedge funds, CTAs) NET LONG → bullish for base currency
  - Leveraged funds NET SHORT → bearish for base currency
  - Contrarian note: Non-reportable (retail) positions tend to be wrong at extremes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

BASE_DIR  = Path(__file__).parent.parent
COT_CACHE = BASE_DIR / "data" / "cot_cache.json"

# CFTC direct ZIP download (Traders in Financial Futures — legacy futures-only)
# More reliable than the OData API which changes endpoints frequently.
# Published every Friday ~3:30 PM ET. Contains full year-to-date data.
_CFTC_ZIP_URL = "https://www.cftc.gov/files/dea/history/fut_fin_xls_{year}.zip"
_CFTC_CSV_NAME = "FinFutYY.txt"  # filename inside the zip

# COT data is published weekly (Friday). Refresh cache if older than 7 days.
_CACHE_TTL_DAYS = 7

# Request timeout (CFTC API can be slow)
_TIMEOUT = 20

# Map internal currency code → CFTC market name substring for filtering
# These are the instrument names as they appear in the CFTC XLS database
_CURRENCY_MARKET_NAMES: dict[str, str] = {
    "EUR": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "GBP": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
    "JPY": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
    "CHF": "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE",
    "CAD": "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "AUD": "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "NZD": "NZ DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "DXY": "U.S. DOLLAR INDEX - ICE FUTURES U.S.",
}

# COT net direction thresholds: positions are in contracts (not units)
# We use the ratio (net / open_interest) to normalize across instruments
_THRESHOLD_BULLISH = 0.05   # >5% of OI net long → bullish signal
_THRESHOLD_BEARISH = -0.05  # <-5% of OI net short → bearish signal


def _load_cache() -> dict:
    """Load cached COT data from disk."""
    if not COT_CACHE.exists():
        return {}
    try:
        return json.loads(COT_CACHE.read_text())
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    """Persist COT data to disk."""
    COT_CACHE.parent.mkdir(exist_ok=True)
    COT_CACHE.write_text(json.dumps(data, indent=2))


def _is_cache_fresh(entry: dict) -> bool:
    """Return True if cache entry is less than _CACHE_TTL_DAYS old."""
    try:
        fetched_at = entry.get("fetched_at", "")
        if not fetched_at:
            return False
        dt = datetime.fromisoformat(fetched_at)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age.days < _CACHE_TTL_DAYS
    except Exception:
        return False


def _load_cftc_zip() -> dict[str, dict] | None:
    """
    Download the CFTC yearly XLS ZIP and parse it.
    Returns dict mapping market name (uppercase) → most recent row dict, or None.
    """
    import io, zipfile

    year = datetime.now(timezone.utc).year
    url  = _CFTC_ZIP_URL.format(year=year)
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[COT] Failed to download CFTC ZIP: {e}", flush=True)
        return None

    try:
        import xlrd  # type: ignore
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        xls_name = next((n for n in zf.namelist() if n.lower().endswith(".xls")), None)
        if not xls_name:
            print("[COT] No XLS found in CFTC ZIP", flush=True)
            return None

        wb = xlrd.open_workbook(file_contents=zf.read(xls_name))
        ws = wb.sheet_by_index(0)
        headers = [str(ws.cell_value(0, c)).strip() for c in range(ws.ncols)]

        # Build lookup: market_name → latest row (file sorted ascending by date, last wins)
        markets: dict[str, dict] = {}
        for r in range(1, ws.nrows):
            row_vals = ws.row_values(r)
            row_dict = {headers[c]: row_vals[c] for c in range(len(headers))}
            name = str(row_dict.get("Market_and_Exchange_Names", "")).strip().upper()
            if name:
                markets[name] = row_dict
        return markets

    except ImportError:
        print("[COT] xlrd not installed — run: pip install xlrd", flush=True)
        return None
    except Exception as e:
        print(f"[COT] ZIP parse error: {e}", flush=True)
        return None


# Module-level cache: (fetched_at_epoch, data_dict)
_zip_cache: tuple[float, dict] = (0.0, {})
_ZIP_TTL = 6 * 3600  # re-download every 6 hours


def _get_zip_data() -> dict[str, dict]:
    """Return cached ZIP data, refreshing if stale."""
    global _zip_cache
    now = time.time()
    if now - _zip_cache[0] > _ZIP_TTL or not _zip_cache[1]:
        data = _load_cftc_zip()
        if data:
            _zip_cache = (now, data)
    return _zip_cache[1]


def _fetch_from_cftc(market_name: str) -> dict | None:
    """
    Look up the most recent COT row for market_name from the CFTC yearly CSV.
    Returns parsed dict or None on failure.
    """
    try:
        all_markets = _get_zip_data()
        if not all_markets:
            return None

        key = market_name.upper()
        row = all_markets.get(key)
        if not row:
            print(f"[COT] Market '{market_name}' not found in CFTC data", flush=True)
            return None

        # TFF report uses Lev_Money (Leveraged Funds = hedge funds, CTAs)
        # which is the best proxy for institutional speculator sentiment
        nc_long  = int(row.get("Lev_Money_Positions_Long_All",
                               row.get("NonComm_Positions_Long_All", 0)) or 0)
        nc_short = int(row.get("Lev_Money_Positions_Short_All",
                               row.get("NonComm_Positions_Short_All", 0)) or 0)
        oi        = int(row.get("Open_Interest_All", 1) or 1)
        net       = nc_long - nc_short
        net_ratio = net / oi if oi > 0 else 0.0
        report_date = row.get("Report_Date_as_MM_DD_YYYY",
                     row.get("Report_Date_as_YYYY_MM_DD", "unknown"))

        return {
            "market":        market_name,
            "report_date":   report_date,
            "nc_long":       nc_long,
            "nc_short":      nc_short,
            "open_interest": oi,
            "net":           net,
            "net_ratio":     round(net_ratio, 4),
            "fetched_at":    datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        print(f"[COT] Parse error for '{market_name}': {e}", flush=True)
        return None


def fetch_cot_data(currency: str) -> dict[str, Any]:
    """
    Return COT positioning data for a currency (e.g. "EUR", "GBP", "JPY").

    Checks cache first; fetches from CFTC API if stale or missing.

    Returns a dict with:
      - currency: str
      - net: int         — non-commercial net contracts (positive = net long)
      - net_ratio: float — net as fraction of open interest
      - direction: str   — "bullish" | "bearish" | "neutral"
      - score: int       — +1 bullish, -1 bearish, 0 neutral
      - report_date: str
      - source: str      — "cache" | "live" | "unavailable"
    """
    cur = currency.upper()
    market_name = _CURRENCY_MARKET_NAMES.get(cur)

    if not market_name:
        return {
            "currency": cur, "net": 0, "net_ratio": 0.0,
            "direction": "unknown", "score": 0,
            "report_date": "N/A", "source": "unavailable",
            "note": f"No CFTC market mapping for {cur}.",
        }

    # Check cache
    cache = _load_cache()
    if cur in cache and _is_cache_fresh(cache[cur]):
        entry = cache[cur]
        return _build_result(entry, source="cache")

    # Fetch fresh data from CFTC
    print(f"[COT] Fetching fresh data for {cur} from CFTC...", flush=True)
    raw = _fetch_from_cftc(market_name)

    if raw:
        cache[cur] = raw
        _save_cache(cache)
        return _build_result(raw, source="live")

    # Fall back to stale cache if available
    if cur in cache:
        print(f"[COT] Using stale cache for {cur} (live fetch failed).", flush=True)
        return _build_result(cache[cur], source="cache_stale")

    return {
        "currency": cur, "net": 0, "net_ratio": 0.0,
        "direction": "unknown", "score": 0,
        "report_date": "N/A", "source": "unavailable",
        "note": f"CFTC data unavailable for {cur}. Sentiment score = 0.",
    }


def _build_result(entry: dict, source: str) -> dict:
    """Convert a raw COT cache entry into a structured result dict."""
    net_ratio = entry.get("net_ratio", 0.0)

    if net_ratio > _THRESHOLD_BULLISH:
        direction = "bullish"
        score     = 1
    elif net_ratio < _THRESHOLD_BEARISH:
        direction = "bearish"
        score     = -1
    else:
        direction = "neutral"
        score     = 0

    net = entry.get("net", 0)
    pct = abs(net_ratio) * 100
    note = (
        f"Speculators net {'+' if net >= 0 else ''}{net:,} contracts "
        f"({pct:.1f}% of OI) → {direction.upper()} as of {entry.get('report_date','?')}"
    )

    return {
        "currency":    entry.get("currency", "?") if "currency" in entry else "?",
        "net":         entry.get("net", 0),
        "net_ratio":   net_ratio,
        "direction":   direction,
        "score":       score,
        "report_date": entry.get("report_date", "unknown"),
        "source":      source,
        "note":        note,
    }


def refresh_all_currencies() -> dict[str, dict]:
    """
    Force-refresh COT data for all tracked currencies.
    Should be called once per week (Saturday) to keep cache current.
    Includes delay between calls to avoid rate-limiting the CFTC API.
    """
    results = {}
    for cur in _CURRENCY_MARKET_NAMES:
        cache = _load_cache()
        # Clear cache entry to force live fetch
        cache.pop(cur, None)
        _save_cache(cache)

        results[cur] = fetch_cot_data(cur)
        time.sleep(2)  # be polite to CFTC servers

    return results
