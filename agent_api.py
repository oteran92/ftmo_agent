"""
Agent API — callable from Cursor chat via Shell tool.
Each function prints JSON so Cursor AI can read and explain it conversationally.
Usage: python3 agent_api.py <command> [args]

Timezone note: user is in CEST (UTC+2). MT5 server runs UTC+3.
All timestamps in output are normalized to CEST.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

CEST = timezone(timedelta(hours=2))


def _now_cest() -> str:
    return datetime.now(CEST).strftime("%Y-%m-%d %H:%M CEST")

BASE_DIR = Path(__file__).parent


def cmd_status() -> dict:
    """Live account status from MT5."""
    from mt5_connector import live_account_summary
    from state import load_state, get_drawdown_pct
    mt5 = live_account_summary()
    state = load_state()
    dd = round(get_drawdown_pct(state) * 100, 2)
    return {
        "mt5": mt5,
        "risk_state": {
            "phase": state["phase"],
            "daily_budget_remaining": round(state["daily_risk_budget"] - state["daily_risk_used"], 2),
            "risk_per_trade": state["risk_per_trade"],
            "consecutive_losses": state["consecutive_losses"],
            "total_drawdown_pct": dd,
            "crisis_mode": state.get("crisis_mode_active", False),
        },
    }


def cmd_scan(pair: str = "all") -> dict | list:
    """Scan one pair or all Stage 1 pairs for setups."""
    from skills.signal_engine import analyze_setup, scan_all_pairs
    if pair.lower() == "all":
        results = scan_all_pairs()
        return {"scanned_at": _now_cest(), "results": results}
    result = analyze_setup(pair.upper().replace("/", ""))
    result["scanned_at"] = _now_cest()
    return result


def cmd_news() -> dict:
    """Upcoming high-impact news next 24h."""
    from skills.news_filter import fetch_upcoming_news
    return fetch_upcoming_news(hours_ahead=24)


def cmd_alerts() -> list:
    """Read latest alerts from monitor daemon."""
    alerts_file = BASE_DIR / "data" / "alerts.json"
    if not alerts_file.exists():
        return [{"message": "No alerts yet. Run monitor.py to start background monitoring."}]
    with open(alerts_file) as f:
        alerts = json.load(f)
    return alerts[-5:]  # last 5


def cmd_objectives() -> dict:
    """Current stage objectives."""
    obj_file = BASE_DIR / "data" / "stage_objectives.json"
    with open(obj_file) as f:
        return json.load(f)


def cmd_review(pair: str, entry: float, sl: float, tp: float) -> dict:
    """Full trade review — GO / CAUTION / NO-GO."""
    from skills.review_trade import review_trade
    return review_trade(pair=pair, entry=entry, sl=sl, tp=tp)


def cmd_positions() -> dict:
    """Live open positions from MT5."""
    from mt5_connector import get_positions, is_bridge_connected
    return {
        "bridge_connected": is_bridge_connected(),
        "positions": get_positions(),
    }


COMMANDS = {
    "status":     cmd_status,
    "scan":       cmd_scan,
    "news":       cmd_news,
    "alerts":     cmd_alerts,
    "objectives": cmd_objectives,
    "review":     cmd_review,
    "positions":  cmd_positions,
}


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print(json.dumps({"available": list(COMMANDS.keys())}))
        sys.exit(0)

    cmd = args[0]
    fn = COMMANDS[cmd]

    try:
        if cmd == "scan" and len(args) > 1:
            result = fn(args[1])
        elif cmd == "review" and len(args) >= 5:
            result = fn(args[1], float(args[2]), float(args[3]), float(args[4]))
        else:
            result = fn()
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
