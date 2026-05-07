"""
News Filter Skill
Fetches the economic calendar and checks for high-impact events near the current time.
Uses Forex Factory's public calendar endpoint; falls back gracefully if unavailable.

Two protection levels:
  BLOCK   — high-impact event within ±NEWS_BUFFER_MIN (30 min). Do NOT trade.
  CAUTION — high-impact event within the next 4 hours. Setup valid but entry is risky.
  CLEAR   — no relevant high-impact events. Trading allowed.

Note: CAUTION window was reduced from 24h to 4h (2026-05-07).
A 24h window was blocking too many valid setups given the frequency of macro events.
The hard BLOCK (±30 min) remains unchanged — that is the real protection.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from config import NEWS_BUFFER_MIN

_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

_CURRENCY_TO_PAIRS: dict[str, list[str]] = {
    "USD": ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "NZD/USD", "USD/CAD", "USD/CHF", "XAU/USD", "BTC/USD"],
    "EUR": ["EUR/USD", "EUR/JPY", "EUR/GBP"],
    "GBP": ["GBP/USD", "GBP/JPY", "EUR/GBP"],
    "JPY": ["USD/JPY", "GBP/JPY", "EUR/JPY"],
    "AUD": ["AUD/USD"],
    "NZD": ["NZD/USD"],
    "CAD": ["USD/CAD"],
    "CHF": ["USD/CHF"],
}

# Hours before a major event to downgrade GO signals to CAUTION.
# 4h is enough to warn the trader; 24h was blocking too many valid setups.
_CAUTION_HOURS = 4


def _get_affected_currencies(pair: str) -> list[str]:
    pair_upper = pair.upper().replace(" ", "")
    parts = pair_upper.split("/") if "/" in pair_upper else [pair_upper[:3], pair_upper[3:]]
    return [p for p in parts if len(p) == 3]


def _fetch_events(hours_ahead: int = 24) -> list[dict]:
    """Fetch high-impact events from ForexFactory calendar for the next N hours."""
    try:
        resp = requests.get(_FF_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        events: list[dict] = resp.json()
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    results = []

    for ev in events:
        if ev.get("impact", "").lower() != "high":
            continue
        try:
            ev_time = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        except Exception:
            continue
        if now - timedelta(hours=1) <= ev_time <= cutoff:
            results.append({
                "currency":     ev.get("country", "?").upper(),
                "title":        ev.get("title", "?"),
                "time_utc":     ev_time.isoformat(),
                "minutes_away": int((ev_time - now).total_seconds() / 60),
            })

    results.sort(key=lambda x: x["minutes_away"])
    return results


def fetch_upcoming_news(hours_ahead: int = 24) -> list[dict[str, Any]]:
    """Return all high-impact news events for the next N hours (public API)."""
    return _fetch_events(hours_ahead=hours_ahead)


def check_news_window(pair: str) -> dict[str, Any]:
    """
    Legacy function kept for compatibility.
    Returns {"clear": bool, "message": str, "events": list}
    """
    result = check_news_block(pair)
    return {
        "clear":   result["status"] != "BLOCK",
        "message": result["message"],
        "events":  result["events"],
    }


def check_news_block(pair: str) -> dict[str, Any]:
    """
    Full news protection check for a pair. Returns:
      {"status": "BLOCK"|"CAUTION"|"CLEAR", "message": str, "events": list}

    BLOCK   — event within ±NEWS_BUFFER_MIN minutes. No entry allowed.
    CAUTION — major event within _CAUTION_HOURS hours. Setup valid but high risk.
    CLEAR   — safe to enter.
    """
    affected = _get_affected_currencies(pair)
    all_events = _fetch_events(hours_ahead=_CAUTION_HOURS)

    # Filter to only events affecting this pair's currencies
    pair_events = [e for e in all_events if e["currency"] in affected]

    if not pair_events:
        return {"status": "CLEAR", "message": f"No high-impact news in next {_CAUTION_HOURS}h.", "events": []}

    # BLOCK: event within the ±30 min buffer
    blocking = [e for e in pair_events if abs(e["minutes_away"]) <= NEWS_BUFFER_MIN]
    if blocking:
        details = "; ".join(
            f"{e['currency']} {e['title']} in {e['minutes_away']}min" for e in blocking
        )
        return {
            "status":  "BLOCK",
            "message": f"BLOCKED — high-impact news within {NEWS_BUFFER_MIN}min: {details}",
            "events":  blocking,
        }

    # CAUTION: event within 24 hours
    upcoming = [e for e in pair_events if e["minutes_away"] > 0]
    if upcoming:
        nearest = upcoming[0]
        hours = nearest["minutes_away"] // 60
        mins  = nearest["minutes_away"] % 60
        time_str = f"{hours}h {mins}m" if hours else f"{mins}m"
        details = "; ".join(
            f"{e['currency']} {e['title']} in {e['minutes_away']//60}h{e['minutes_away']%60:02d}m"
            for e in upcoming
        )
        return {
            "status":  "CAUTION",
            "message": f"CAUTION — major news in {time_str}: {details}",
            "events":  upcoming,
        }

    return {"status": "CLEAR", "message": "No upcoming high-impact news.", "events": []}
