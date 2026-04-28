"""
End of Day Skill
Processes daily P&L, updates account state, applies hard stop rules,
detects payday threshold, and returns tomorrow's risk capacity.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from config import (
    HARD_STOP_LOSSES,
    PAYDAY_TRIGGER_PCT,
    DAILY_BUDGET_PCT,
    RISK_PER_TRADE_PCT,
    SCALE_UP_EQUITY_PCT,
    SCALE_UP_INCREMENT,
    DE_SCALE_LOSS_PCT,
)
from state import (
    load_state,
    save_state,
    append_trade,
    push_alert,
    get_drawdown_pct,
    get_daily_drawdown_pct,
)


def process_end_of_day(
    daily_pnl: float,
    trades: list[dict] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """
    Call this at end of each session.
    daily_pnl: net P&L for the day (negative = loss).
    trades: list of individual trade records (optional, for log).
    """
    state = load_state()
    balance_before = state["account_balance"]

    # ── Update balance ─────────────────────────────────────────────────────────
    state["daily_pnl"]       = daily_pnl
    state["account_balance"] = balance_before + daily_pnl
    state["total_pnl"]       = state.get("total_pnl", 0.0) + daily_pnl

    # ── Update high-water mark ─────────────────────────────────────────────────
    if state["account_balance"] > state["equity_high_water_mark"]:
        state["equity_high_water_mark"] = state["account_balance"]

    # ── Log session ────────────────────────────────────────────────────────────
    session = {
        "date":          str(date.today()),
        "pnl":           daily_pnl,
        "balance_end":   state["account_balance"],
        "trades":        trades or [],
        "notes":         notes,
    }
    state.setdefault("sessions", []).append(session)

    # ── Lot scaling logic ──────────────────────────────────────────────────────
    reports: list[str] = []
    balance      = state["account_balance"]
    initial      = 100_000.0  # baseline
    growth_pct   = (balance - initial) / initial

    if daily_pnl < 0 and abs(daily_pnl) / balance_before > DE_SCALE_LOSS_PCT:
        old_mult = state["current_lot_size_multiplier"]
        # Revert one scale step
        state["current_lot_size_multiplier"] = max(1.0, old_mult - SCALE_UP_INCREMENT)
        if state["current_lot_size_multiplier"] < old_mult:
            reports.append(
                f"⚠️  DE-SCALE triggered: lot multiplier {old_mult:.2f}x → "
                f"{state['current_lot_size_multiplier']:.2f}x "
                f"(daily loss {abs(daily_pnl)/balance_before*100:.2f}% > {DE_SCALE_LOSS_PCT*100:.2f}%)"
            )
    elif growth_pct >= SCALE_UP_EQUITY_PCT:
        # Check last 10 trades for positive expectancy before scaling
        all_trades = trades or []
        last_10 = all_trades[-10:] if len(all_trades) >= 10 else []
        if last_10:
            expectancy = sum(t.get("pnl", 0) for t in last_10)
            if expectancy > 0:
                old_mult = state["current_lot_size_multiplier"]
                state["current_lot_size_multiplier"] = round(old_mult + SCALE_UP_INCREMENT, 2)
                reports.append(
                    f"📈 SCALE-UP available: equity +{growth_pct*100:.1f}% | "
                    f"10-trade expectancy positive. "
                    f"Lot multiplier → {state['current_lot_size_multiplier']:.2f}x. "
                    f"Confirm before trading tomorrow."
                )

    # ── Payday check ───────────────────────────────────────────────────────────
    profit_pct = state["total_pnl"] / 100_000.0
    payday_triggered = profit_pct >= PAYDAY_TRIGGER_PCT
    if payday_triggered:
        reports.append(
            f"💰 PAYDAY TRIGGER: Total profit {profit_pct*100:.1f}% ≥ {PAYDAY_TRIGGER_PCT*100:.0f}%. "
            f"Submit withdrawal request NOW. Secure fee refund."
        )
        push_alert("INFO", f"Payday trigger reached: {profit_pct*100:.1f}%")

    # ── Tomorrow's risk capacity ───────────────────────────────────────────────
    new_balance   = state["account_balance"]
    daily_budget  = new_balance * DAILY_BUDGET_PCT
    risk_per_trade = new_balance * RISK_PER_TRADE_PCT

    state["daily_risk_budget"] = daily_budget
    state["risk_per_trade"]    = risk_per_trade
    # Reset daily counters for tomorrow
    state["daily_pnl"]         = 0.0
    state["daily_risk_used"]   = 0.0
    state["consecutive_losses"] = 0
    state["total_trades_today"] = 0
    state["trading_halted_until"] = None
    state["last_reset_date"]   = str(date.today())

    save_state(state)

    # Log trades individually
    if trades:
        for t in trades:
            t.setdefault("date", str(date.today()))
            append_trade(t)

    # ── Drawdown status ────────────────────────────────────────────────────────
    total_dd  = get_drawdown_pct(state) * 100
    daily_dd  = abs(min(daily_pnl, 0)) / balance_before * 100

    return {
        "date":                    str(date.today()),
        "daily_pnl":               daily_pnl,
        "balance_before":          balance_before,
        "balance_after":           new_balance,
        "total_pnl":               state["total_pnl"],
        "total_drawdown_pct":      round(total_dd, 2),
        "daily_drawdown_pct":      round(daily_dd, 2),
        "tomorrow_daily_budget":   round(daily_budget, 2),
        "tomorrow_risk_per_trade": round(risk_per_trade, 2),
        "lot_multiplier":          state["current_lot_size_multiplier"],
        "payday_triggered":        payday_triggered,
        "profit_pct":              round(profit_pct * 100, 2),
        "phase":                   state["phase"],
        "reports":                 reports,
    }
