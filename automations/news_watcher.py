"""
News Watcher Automation
Polls the economic calendar every N minutes during trading hours and prints
alerts to the terminal when a high-impact event is approaching.
"""

from __future__ import annotations

import time
from datetime import datetime

from skills.news_filter import fetch_upcoming_news
from state import push_alert

_ALREADY_ALERTED: set[str] = set()


def watch_once() -> list[dict]:
    """Single poll — returns any new alerts."""
    events = fetch_upcoming_news(hours_ahead=2)
    new_alerts = []

    for ev in events:
        key = f"{ev['currency']}_{ev['time_utc']}"
        if key in _ALREADY_ALERTED:
            continue

        mins = ev["minutes_away"]
        if mins <= 5:
            level = "CRITICAL"
            tag   = "🚨 IMMINENT"
        elif mins <= 15:
            level = "WARN"
            tag   = "⚠️  UPCOMING"
        elif mins <= 30:
            level = "INFO"
            tag   = "📅 SCHEDULED"
        else:
            continue

        msg = f"{tag} | {ev['currency']} — {ev['title']} in {mins}min"
        push_alert(level, msg)
        _ALREADY_ALERTED.add(key)
        new_alerts.append({**ev, "tag": tag, "level": level})

    return new_alerts


def run_news_watcher(interval_minutes: int = 15) -> None:
    """Blocking loop — run in a background thread."""
    print(f"[NewsWatcher] Started — polling every {interval_minutes}min")
    while True:
        alerts = watch_once()
        for a in alerts:
            print(f"[NewsWatcher] {a['tag']} {a['currency']} {a['title']} in {a['minutes_away']}min")
        time.sleep(interval_minutes * 60)
