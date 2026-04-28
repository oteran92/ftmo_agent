"""
Crisis Mode Skill
Activated when account drawdown approaches 4%. Enforces strict de-risking protocol.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from config import CRISIS_THRESHOLD_PCT, RISK_PER_TRADE_PCT
from state import load_state, save_state, push_alert, get_drawdown_pct


def activate_crisis_mode() -> dict[str, Any]:
    """Force-activate crisis mode regardless of drawdown level."""
    state = load_state()
    state["crisis_mode_active"] = True
    # Halt trading for 2 hours minimum
    halt_until = datetime.now() + timedelta(hours=2)
    state["trading_halted_until"] = halt_until.isoformat()
    save_state(state)

    push_alert("CRITICAL", "CRISIS MODE ACTIVATED — trading halted 2h, lot size halved.")

    balance  = state["account_balance"]
    dd_pct   = get_drawdown_pct(state) * 100
    max_dd   = 5.0  # FTMO hard limit
    buffer   = max_dd - dd_pct

    return {
        "status":        "CRISIS_MODE_ACTIVE",
        "balance":       balance,
        "drawdown_pct":  round(dd_pct, 2),
        "buffer_to_limit": round(buffer, 2),
        "trading_halted_until": halt_until.isoformat(),
        "risk_per_trade_pct": RISK_PER_TRADE_PCT / 2 * 100,  # halved
        "protocol": [
            "1. CLOSE all open positions at market immediately.",
            "2. Do NOT re-enter for at least 2 hours.",
            f"3. Risk per trade reduced to {RISK_PER_TRADE_PCT/2*100:.2f}% until equity recovers to $97,500.",
            "4. Trade ONLY your highest win-rate pair (1 instrument max).",
            "5. Run 'crisis status' before any new trade.",
            "6. Mandatory review: analyze last 5 trades before resuming.",
        ],
    }


def check_and_trigger_crisis() -> dict[str, Any] | None:
    """Auto-check: if drawdown ≥ threshold, activate crisis mode automatically."""
    state = load_state()
    dd = get_drawdown_pct(state)
    if dd >= CRISIS_THRESHOLD_PCT and not state.get("crisis_mode_active"):
        return activate_crisis_mode()
    return None


def crisis_status() -> dict[str, Any]:
    """Return current crisis mode status and recovery progress."""
    state = load_state()
    balance  = state["account_balance"]
    dd_pct   = get_drawdown_pct(state) * 100
    active   = state.get("crisis_mode_active", False)
    recovery_target = 97_500.0

    can_deactivate = balance >= recovery_target and active
    if can_deactivate:
        state["crisis_mode_active"] = False
        state["trading_halted_until"] = None
        state["current_lot_size_multiplier"] = 1.0
        save_state(state)
        push_alert("INFO", "Crisis mode deactivated — account recovered to $97,500.")

    return {
        "crisis_mode_active":    state.get("crisis_mode_active", False),
        "current_balance":       balance,
        "drawdown_pct":          round(dd_pct, 2),
        "recovery_target":       recovery_target,
        "recovery_gap":          round(max(0.0, recovery_target - balance), 2),
        "can_deactivate":        can_deactivate,
        "lot_multiplier":        state.get("current_lot_size_multiplier", 1.0),
        "trading_halted_until":  state.get("trading_halted_until"),
    }
