"""
Lot Size Calculator
Computes the exact lot size so that hitting the SL never exceeds the risk budget.
"""

from __future__ import annotations

from config import PIP_VALUES, RISK_PER_TRADE_PCT
from state import load_state


def calculate_lot_size(
    pair: str,
    entry: float,
    sl: float,
    account_balance: float | None = None,
    risk_pct: float | None = None,
    pip_value_override: float | None = None,
) -> dict:
    """
    Returns lot size and full breakdown.

    For JPY pairs: 1 pip = 0.01.  For all others: 1 pip = 0.0001.
    For Gold (XAU/USD): 1 pip = 0.10.
    For Crypto: 1 pip = 1.00 (dollar-based, adjust pip_value_override).
    """
    state = load_state()
    balance = account_balance or state["account_balance"]
    rpt_pct = risk_pct or RISK_PER_TRADE_PCT

    risk_amount = balance * rpt_pct

    # Detect pip size
    pair_upper = pair.upper()
    if "JPY" in pair_upper:
        pip_size = 0.01
    elif "XAU" in pair_upper or "GOLD" in pair_upper:
        pip_size = 0.10
    elif "BTC" in pair_upper or "ETH" in pair_upper:
        pip_size = 1.00
    else:
        pip_size = 0.0001

    sl_distance_price = abs(entry - sl)
    if sl_distance_price == 0:
        return {"error": "SL cannot equal entry price."}

    sl_pips = sl_distance_price / pip_size

    pip_val = pip_value_override or PIP_VALUES.get(pair_upper)
    if pip_val is None:
        pip_val = 10.0  # default to standard USD pair

    # Apply crisis mode reduction (50% of normal size)
    if state.get("crisis_mode_active"):
        rpt_pct = rpt_pct / 2
        risk_amount = balance * rpt_pct
        crisis_active = True
    else:
        crisis_active = False

    # Apply lot size multiplier (scaling plan)
    multiplier = state.get("current_lot_size_multiplier", 1.0)

    lot_size_raw = risk_amount / (sl_pips * pip_val)
    lot_size = round(lot_size_raw * multiplier, 2)
    lot_size = max(0.01, lot_size)  # broker minimum

    return {
        "pair":             pair_upper,
        "account_balance":  balance,
        "risk_pct":         rpt_pct * 100,
        "risk_amount_usd":  round(risk_amount, 2),
        "entry":            entry,
        "sl":               sl,
        "sl_pips":          round(sl_pips, 1),
        "pip_value":        pip_val,
        "pip_size":         pip_size,
        "lot_size":         lot_size,
        "lot_multiplier":   multiplier,
        "crisis_mode":      crisis_active,
        "max_loss_if_sl_hit": round(lot_size * sl_pips * pip_val, 2),
    }
