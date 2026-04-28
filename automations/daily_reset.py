"""
Daily Reset Automation
Runs at the configured session start time and resets all daily risk counters.
"""

from __future__ import annotations

import time
from datetime import datetime

from config import TRADING_SESSION_START
from state import load_state, save_state, daily_reset_if_needed, push_alert


def _time_until_reset() -> float:
    """Seconds until the next session start."""
    now = datetime.now()
    h, m = map(int, TRADING_SESSION_START.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target = target.replace(day=target.day + 1)
    return (target - now).total_seconds()


def run_daily_reset_watcher() -> None:
    """Blocking loop — waits until session start, then resets daily counters."""
    while True:
        wait = _time_until_reset()
        print(f"[DailyReset] Next reset in {wait/3600:.1f}h (at {TRADING_SESSION_START})")
        time.sleep(wait)

        state = load_state()
        state = daily_reset_if_needed(state)
        save_state(state)

        balance = state["account_balance"]
        budget  = state["daily_risk_budget"]
        push_alert(
            "INFO",
            f"Daily reset — new budget: ${budget:.2f} | balance: ${balance:,.2f}"
        )
        print(
            f"[DailyReset] ✅ Reset complete — "
            f"balance: ${balance:,.2f} | daily budget: ${budget:.2f}"
        )
