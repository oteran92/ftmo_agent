"""
FTMO Challenge Tracker
Reads challenge_state.json and computes real-time progress metrics.
Called by monitor.py to include a dashboard block in every email.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from config import CEST as _CEST

_STATE_FILE = Path(__file__).parent.parent / "data" / "challenge_state.json"

# FTMO Challenge thresholds
_PROFIT_TARGET_PCT  = 0.10   # +10% to pass
_DAILY_LOSS_MAX_PCT = 0.05   # -5%/day hard limit
_TOTAL_LOSS_MAX_PCT = 0.10   # -10% total hard limit


def load_state() -> dict:
    if _STATE_FILE.exists():
        return json.loads(_STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def _update_balance(new_balance: float) -> dict:
    """
    Call after each trade closes or at each scan to refresh metrics.
    Updates high_water_mark, daily_pnl, and last_updated.
    """
    state = load_state()
    today = datetime.now(_CEST).strftime("%Y-%m-%d")

    prev_balance = state.get("current_balance", state.get("initial_balance", 100_000.0))
    state["current_balance"] = new_balance
    state["high_water_mark"] = max(state.get("high_water_mark", new_balance), new_balance)
    state["last_updated"]    = datetime.now(_CEST).strftime("%Y-%m-%d %H:%M CEST")

    # Accumulate today's P&L
    daily_pnl = state.setdefault("daily_pnl", {})
    daily_pnl[today] = round(new_balance - state["initial_balance"] -
                             sum(v for d, v in daily_pnl.items() if d < today), 2)

    save_state(state)
    return state


def get_dashboard() -> dict[str, Any]:
    """
    Returns a structured snapshot of challenge progress.
    Fields are ready to embed directly into an email.
    """
    state = load_state()
    if not state:
        return {"error": "challenge_state.json not found"}

    initial  = state.get("initial_balance", 100_000.0)
    current  = state.get("current_balance", initial)
    target   = state.get("challenge_target", initial * (1 + _PROFIT_TARGET_PCT))
    today    = datetime.now(_CEST).strftime("%Y-%m-%d")

    profit_usd  = current - initial
    profit_pct  = profit_usd / initial * 100
    progress    = profit_pct / (_PROFIT_TARGET_PCT * 100) * 100   # % of the 10% target done
    remaining   = target - current

    daily_pnl   = state.get("daily_pnl", {}).get(today, 0.0)
    daily_used  = abs(min(daily_pnl, 0))     # only negative matters
    daily_left  = state["daily_loss_limit"] - daily_used
    daily_pct   = daily_used / initial * 100

    drawdown    = state["high_water_mark"] - current
    total_used  = drawdown
    total_left  = state["total_loss_limit"] - total_used
    total_pct   = total_used / initial * 100

    # Status flags
    daily_warning = daily_used >= state["daily_loss_limit"] * 0.70  # >70% of daily limit used
    total_warning = total_used >= state["total_loss_limit"] * 0.60  # >60% of total limit used

    return {
        "phase":             state.get("phase", "DEMO_PRACTICE"),
        "balance":           round(current, 2),
        "profit_usd":        round(profit_usd, 2),
        "profit_pct":        round(profit_pct, 2),
        "target_usd":        round(target, 2),
        "remaining_usd":     round(remaining, 2),
        "progress_pct":      round(progress, 1),     # how far through the 10% target
        "trading_days":      state.get("trading_days_done", 0),
        "trades_won":        state.get("trades_won", 0),
        "trades_lost":       state.get("trades_lost", 0),
        "daily_pnl":         round(daily_pnl, 2),
        "daily_loss_used":   round(daily_used, 2),
        "daily_loss_left":   round(daily_left, 2),
        "daily_loss_pct":    round(daily_pct, 2),
        "total_drawdown":    round(total_used, 2),
        "total_loss_left":   round(total_left, 2),
        "total_loss_pct":    round(total_pct, 2),
        "daily_warning":     daily_warning,
        "total_warning":     total_warning,
    }


def format_email_block(d: dict | None = None) -> str:
    """Returns a formatted text block ready to append to monitor emails."""
    if d is None:
        d = get_dashboard()
    if "error" in d:
        return f"[Challenge tracker: {d['error']}]"

    warn_daily = " ⚠ NEAR LIMIT" if d["daily_warning"] else ""
    warn_total = " ⚠ NEAR LIMIT" if d["total_warning"] else ""

    bar_filled = int(d["progress_pct"] / 5)   # 20 chars = 100%
    bar = "█" * bar_filled + "░" * (20 - bar_filled)

    return (
        f"\n{'─'*48}\n"
        f"FTMO CHALLENGE TRACKER ({d['phase']})\n"
        f"{'─'*48}\n"
        f"  Balance       : ${d['balance']:>10,.2f}\n"
        f"  Profit        : ${d['profit_usd']:>+10,.2f}  ({d['profit_pct']:+.2f}%)\n"
        f"  Target        : ${d['target_usd']:>10,.2f}  (+10.00%)\n"
        f"  Remaining     : ${d['remaining_usd']:>10,.2f}\n"
        f"  Progress      : [{bar}] {d['progress_pct']:.1f}%\n"
        f"\n"
        f"  Today's P&L   : ${d['daily_pnl']:>+10,.2f}\n"
        f"  Daily limit   : -${d['daily_loss_left']:,.2f} remaining  "
        f"({d['daily_loss_pct']:.1f}% used){warn_daily}\n"
        f"  Total limit   : -${d['total_loss_left']:,.2f} remaining  "
        f"({d['total_loss_pct']:.1f}% used){warn_total}\n"
        f"\n"
        f"  Trading days  : {d['trading_days']}/4 minimum\n"
        f"  Win/Loss      : {d['trades_won']}W / {d['trades_lost']}L\n"
        f"{'─'*48}"
    )
