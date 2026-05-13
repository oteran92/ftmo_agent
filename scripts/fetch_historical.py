#!/usr/bin/env python3
"""
scripts/fetch_historical.py
---------------------------
One-shot script to pre-fetch 5 years of historical OHLC data for all
monitored pairs across 3 intervals (1day, 4h, 1week) and store them in
data/historical/ as JSON cache files.

Run once before backtesting:
    python scripts/fetch_historical.py

Options:
    --pairs   EUR/USD,GBP/USD,...   Override default pair list
    --years   5                     Years of history (default 5)
    --refresh                       Force re-fetch even if cache exists

Expected output: 27 files in data/historical/ (9 pairs × 3 intervals).
Total API cost: ~45 requests (within TwelveData free plan: 800 req/day).
Estimated time: ~7 minutes (8-second delay between requests).
"""

import argparse
import sys
import time
from pathlib import Path

# Allow running from repo root without installing package
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.data_loader import load_or_fetch, _BETWEEN_REQS
from config import MONITORED_PAIRS

_INTERVALS = ["1day", "4h", "1week"]

_MIN_CANDLES = {
    "1day":  200,   # min needed for 55-candle EMA50 + 100 lookback with buffer
    "4h":    500,   # same, but H4 resolution
    "1week":  50,   # weekly context
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-fetch OHLC history for backtesting")
    parser.add_argument("--pairs",   default="",    help="Comma-separated pairs (default: all MONITORED_PAIRS)")
    parser.add_argument("--years",   default=5,     type=int, help="Years of history (default 5)")
    parser.add_argument("--refresh", action="store_true",     help="Force re-fetch, ignore cache")
    args = parser.parse_args()

    pairs = [p.strip().replace("/", "").upper() for p in args.pairs.split(",") if p.strip()] \
            if args.pairs else MONITORED_PAIRS

    total  = len(pairs) * len(_INTERVALS)
    done   = 0
    errors = []

    print(f"=== fetch_historical.py ===")
    print(f"Pairs    : {pairs}")
    print(f"Intervals: {_INTERVALS}")
    print(f"Years    : {args.years}")
    print(f"Refresh  : {args.refresh}")
    print(f"Total requests (approx): {total}")
    print()

    for pair in pairs:
        print(f"── {pair} ──")
        for interval in _INTERVALS:
            candles = load_or_fetch(
                pair,
                interval,
                years=args.years,
                verbose=True,
                force_refresh=args.refresh,
            )
            done += 1
            minimum = _MIN_CANDLES.get(interval, 50)
            if len(candles) < minimum:
                msg = f"  [WARN] {pair}/{interval}: only {len(candles)} candles (need ≥{minimum})"
                print(msg)
                errors.append(msg)
            else:
                print(f"  [OK]  {pair}/{interval}: {len(candles)} candles")

            # Respect free-plan rate limit (8 req/min) — skip delay on cache hits
            # (load_or_fetch already skips API if cache exists, but delay is harmless)
            if done < total:
                time.sleep(_BETWEEN_REQS)

        print()

    print("=== Summary ===")
    print(f"Completed: {done}/{total}")
    if errors:
        print("Warnings:")
        for e in errors:
            print(" ", e)
    else:
        print("All pairs fetched successfully — ready for backtesting.")


if __name__ == "__main__":
    main()
