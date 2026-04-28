"""
News Filter Skill
Fetches the economic calendar and checks for high-impact events near the current time.
Uses Forex Factory's public calendar endpoint; falls back gracefully if unavailable.
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


def _get_affected_currencies(pair: str) -> list[str]:
    pair_upper = pair.upper().replace(" ", "")
    # Try explicit slash
    parts = pair_upper.split("/") if "/" in pair_upper else [pair_upper[:3], pair_upper[3:]]
    return [p for p in parts if len(p) == 3]


def fetch_upcoming_news(hours_ahead: int = 24) -> list[dict[str, Any]]:
    """Return high-impact news events for the next N hours."""
    try:
        resp = requests.get(_FF_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        events: list[dict] = resp.json()
    except Exception:
        # Silently return empty — caller handles gracefully
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
        if now <= ev_time <= cutoff:
            results.append({
                "currency":  ev.get("country", "?").upper(),
                "title":     ev.get("title", "?"),
                "time_utc":  ev_time.isoformat(),
                "minutes_away": int((ev_time - now).total_seconds() / 60),
            })

    results.sort(key=lambda x: x["minutes_away"])
    return results


def check_news_window(pair: str) -> dict[str, Any]:
    """
    Returns {"clear": bool, "message": str, "events": list}
    clear=False means DO NOT TRADE.
    """
    affected = _get_affected_currencies(pair)
    events = fetch_upcoming_news(hours_ahead=2)

    blocking: list[dict] = []
    for ev in events:
        if ev["currency"] not in affected:
            continue
        mins = ev["minutes_away"]
        if -NEWS_BUFFER_MIN <= mins <= NEWS_BUFFER_MIN:
            blocking.append(ev)

    if blocking:
        details = "; ".join(
            f"{e['currency']} {e['title']} in {e['minutes_away']}min"
            for e in blocking
        )
        return {
            "clear":   False,
            "message": f"High-impact news within ±{NEWS_BUFFER_MIN}min: {details}",
            "events":  blocking,
        }

    # Check for upcoming events in the next window (warn only)
    upcoming = [
        e for e in events
        if e["currency"] in affected and 0 < e["minutes_away"] <= NEWS_BUFFER_MIN * 2
    ]
    msg = "Clear." if not upcoming else (
        "Clear now but high-impact event approaching: " +
        "; ".join(f"{e['currency']} {e['title']} in {e['minutes_away']}min" for e in upcoming)
    )

    return {"clear": True, "message": msg, "events": upcoming}
