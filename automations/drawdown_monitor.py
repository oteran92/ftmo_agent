"""
Drawdown Monitor Automation
Polls account state every 60 seconds and triggers Crisis Mode automatically
if the drawdown approaches the 4% threshold.
"""

from __future__ import annotations

import time

from config import CRISIS_THRESHOLD_PCT
from skills.crisis_mode import check_and_trigger_crisis
from state import load_state, get_drawdown_pct, get_daily_drawdown_pct


def run_drawdown_monitor(interval_seconds: int = 60) -> None:
    """Blocking loop — run in a background thread."""
    print(f"[DrawdownMonitor] Started — checking every {interval_seconds}s")
    last_dd = 0.0

    while True:
        result = check_and_trigger_crisis()
        if result:
            print("\n" + "!" * 60)
            print("[DrawdownMonitor] 🚨 CRISIS MODE AUTO-TRIGGERED")
            for step in result.get("protocol", []):
                print(f"  {step}")
            print("!" * 60 + "\n")

        state = load_state()
        dd = get_drawdown_pct(state) * 100
        daily_dd = get_daily_drawdown_pct(state) * 100

        # Print warning as drawdown increases
        if dd >= 3.0 and dd > last_dd + 0.1:
            print(
                f"[DrawdownMonitor] ⚠️  Total drawdown: {dd:.2f}% | "
                f"Daily: {daily_dd:.2f}% | "
                f"Crisis at: {CRISIS_THRESHOLD_PCT*100:.0f}%"
            )
        last_dd = dd

        time.sleep(interval_seconds)
