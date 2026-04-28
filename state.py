"""
Persistent state manager — reads/writes account_state.json and trade_log.json.
All mutations go through this module so there is a single source of truth.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any

from config import (
    ACCOUNT_BALANCE_INITIAL,
    DAILY_BUDGET_PCT,
    RISK_PER_TRADE_PCT,
    STATE_FILE,
    LOG_FILE,
    ALERT_FILE,
    DATA_DIR,
)

# ── Default state structure ────────────────────────────────────────────────────
_DEFAULT_STATE: dict[str, Any] = {
    "phase": "trial",                           # trial | challenge | funded
    "account_balance": ACCOUNT_BALANCE_INITIAL,
    "equity_high_water_mark": ACCOUNT_BALANCE_INITIAL,
    "daily_pnl": 0.0,
    "daily_risk_used": 0.0,
    "daily_risk_budget": ACCOUNT_BALANCE_INITIAL * DAILY_BUDGET_PCT,
    "risk_per_trade": ACCOUNT_BALANCE_INITIAL * RISK_PER_TRADE_PCT,
    "consecutive_losses": 0,
    "total_trades_today": 0,
    "trading_halted_until": None,               # ISO datetime string or null
    "crisis_mode_active": False,
    "current_lot_size_multiplier": 1.0,
    "last_reset_date": str(date.today()),
    "profit_splits_taken": 0,
    "total_pnl": 0.0,
    "sessions": [],
}


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_state() -> dict[str, Any]:
    _ensure_dirs()
    if not os.path.exists(STATE_FILE):
        save_state(_DEFAULT_STATE.copy())
        return _DEFAULT_STATE.copy()
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict[str, Any]) -> None:
    _ensure_dirs()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def load_trade_log() -> list[dict[str, Any]]:
    _ensure_dirs()
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return json.load(f)


def append_trade(trade: dict[str, Any]) -> None:
    log = load_trade_log()
    trade["timestamp"] = datetime.now().isoformat()
    log.append(trade)
    _ensure_dirs()
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


def load_alerts() -> list[dict[str, Any]]:
    _ensure_dirs()
    if not os.path.exists(ALERT_FILE):
        return []
    with open(ALERT_FILE) as f:
        return json.load(f)


def push_alert(level: str, message: str) -> None:
    """level: INFO | WARN | CRITICAL"""
    alerts = load_alerts()
    alerts.append({"level": level, "message": message, "time": datetime.now().isoformat()})
    # Keep last 200 alerts
    alerts = alerts[-200:]
    _ensure_dirs()
    with open(ALERT_FILE, "w") as f:
        json.dump(alerts, f, indent=2)


def daily_reset_if_needed(state: dict[str, Any]) -> dict[str, Any]:
    """Reset daily counters if it's a new trading day."""
    today = str(date.today())
    if state.get("last_reset_date") != today:
        balance = state["account_balance"]
        state["daily_pnl"]          = 0.0
        state["daily_risk_used"]    = 0.0
        state["daily_risk_budget"]  = balance * DAILY_BUDGET_PCT
        state["risk_per_trade"]     = balance * RISK_PER_TRADE_PCT
        state["consecutive_losses"] = 0
        state["total_trades_today"] = 0
        state["trading_halted_until"] = None
        state["last_reset_date"]    = today
        state["crisis_mode_active"] = False
        save_state(state)
    return state


def is_trading_halted(state: dict[str, Any]) -> bool:
    halt_until = state.get("trading_halted_until")
    if not halt_until:
        return False
    return datetime.now() < datetime.fromisoformat(halt_until)


def get_drawdown_pct(state: dict[str, Any]) -> float:
    hwm = state["equity_high_water_mark"]
    balance = state["account_balance"]
    return (hwm - balance) / hwm if hwm > 0 else 0.0


def get_daily_drawdown_pct(state: dict[str, Any]) -> float:
    return abs(min(state["daily_pnl"], 0.0)) / state["account_balance"]
