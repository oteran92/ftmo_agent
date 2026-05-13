"""
Trade Review Skill
Validates a proposed trade against the full FTMO methodology checklist.
"""

from __future__ import annotations

from datetime import datetime, timezone

from config import (
    MIN_RRR,
    HARD_STOP_LOSSES,
    TRADING_SESSIONS_UTC,
    CAUTION_DAYS,
    CORRELATED_PAIRS,
)
from state import load_state, is_trading_halted
from skills.lot_calculator import calculate_lot_size
from skills.news_filter import check_news_window


def review_trade(
    pair: str,
    entry: float,
    sl: float,
    tp: float,
    direction: str = "auto",  # "long" | "short" | "auto"
    pip_value_override: float | None = None,
) -> dict:
    """
    Full pre-trade audit. Returns verdict + breakdown.
    """
    state = load_state()
    issues: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}

    # ── Direction inference ────────────────────────────────────────────────────
    if direction == "auto":
        direction = "long" if tp > entry else "short"

    # ── 1. Trading halt check ──────────────────────────────────────────────────
    if is_trading_halted(state):
        halt_until = state.get("trading_halted_until", "unknown")
        return {
            "verdict": "BLOCKED",
            "reason": f"Trading halted until {halt_until} — 2 consecutive losses rule.",
            "checks": {},
            "lot_size": None,
        }

    # ── 2. Consecutive losses check ────────────────────────────────────────────
    consec = state.get("consecutive_losses", 0)
    checks["consecutive_losses_ok"] = consec < HARD_STOP_LOSSES
    if not checks["consecutive_losses_ok"]:
        issues.append(f"HARD STOP: {consec} consecutive losses today. No more trades.")

    # ── 3. Daily budget check ──────────────────────────────────────────────────
    budget_remaining = state["daily_risk_budget"] - state["daily_risk_used"]
    risk_amount      = state["account_balance"] * 0.005
    checks["budget_ok"] = budget_remaining >= risk_amount
    if not checks["budget_ok"]:
        issues.append(
            f"Daily budget exhausted. Remaining: ${budget_remaining:.2f} | "
            f"Trade needs: ${risk_amount:.2f}"
        )
    else:
        warnings.append(
            f"Budget remaining after this trade: ${(budget_remaining - risk_amount):.2f}"
        ) if (budget_remaining - risk_amount) < risk_amount else None

    # ── 4. RRR check ──────────────────────────────────────────────────────────
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    rrr     = tp_dist / sl_dist if sl_dist > 0 else 0.0
    checks["rrr_ok"] = rrr >= MIN_RRR
    if not checks["rrr_ok"]:
        issues.append(f"RRR {rrr:.2f}:1 is below minimum {MIN_RRR}:1. Widen TP or tighten SL.")

    # ── 5. Directional consistency ────────────────────────────────────────────
    if direction == "long":
        checks["direction_ok"] = tp > entry > sl
    else:
        checks["direction_ok"] = tp < entry < sl
    if not checks["direction_ok"]:
        issues.append(
            f"Price levels inconsistent with {direction} trade. "
            f"Check entry={entry}, SL={sl}, TP={tp}."
        )

    # ── 6. SL is not too tight (min 5 pips for FX) ────────────────────────────
    pair_upper = pair.upper()
    if "JPY" in pair_upper:
        min_sl_pips, pip_size = 5, 0.01
    elif "XAU" in pair_upper:
        min_sl_pips, pip_size = 10, 0.10
    elif "BTC" in pair_upper or "ETH" in pair_upper:
        min_sl_pips, pip_size = 50, 1.0
    else:
        min_sl_pips, pip_size = 5, 0.0001

    sl_pips = sl_dist / pip_size
    checks["sl_not_too_tight"] = sl_pips >= min_sl_pips
    if not checks["sl_not_too_tight"]:
        warnings.append(f"SL only {sl_pips:.1f} pips — may be hit by normal spread/volatility.")

    # ── 7. News window check ──────────────────────────────────────────────────
    news_result = check_news_window(pair)
    checks["news_clear"] = news_result["clear"]
    if not checks["news_clear"]:
        issues.append(f"NEWS ALERT: {news_result['message']}")

    # ── 8. Crisis mode check ──────────────────────────────────────────────────
    checks["not_in_crisis"] = not state.get("crisis_mode_active", False)
    if not checks["not_in_crisis"]:
        warnings.append(
            "CRISIS MODE active — lot size is 50% of normal. "
            "Only trade highest-conviction setups."
        )

    # ── 9. Session filter ─────────────────────────────────────────────────────
    # Academy: London (08–16 UTC) and NY (12–21 UTC) have highest volume.
    # Asian session has LOWEST volume — patterns fail more often (FTMO Academy).
    now_utc = datetime.now(timezone.utc)
    hour    = now_utc.hour
    in_valid_session = any(start <= hour < end for start, end in TRADING_SESSIONS_UTC)
    checks["session_ok"] = in_valid_session
    if not in_valid_session:
        warnings.append(
            f"Hour {hour:02d}:00 UTC is outside London (08–16) and NY (12–21) sessions. "
            "Asian session has the lowest volume — setups fail more often. "
            "Wait for London open or trade later."
        )

    # ── 10. Day-of-week filter ────────────────────────────────────────────────
    # Academy: Tue/Wed/Thu are best days. Monday AM and Friday PM are low-quality.
    weekday = now_utc.weekday()  # 0=Mon … 6=Sun
    mon_day, mon_hour = CAUTION_DAYS["monday_morning"]
    fri_day, fri_hour = CAUTION_DAYS["friday_afternoon"]

    if weekday == mon_day and hour < mon_hour:
        warnings.append(
            f"Monday morning ({hour:02d}:00 UTC) — market slow until NY session opens at 12:00 UTC. "
            "Volume is minimal; best setups appear Tue–Thu."
        )
    elif weekday == fri_day and hour >= fri_hour:
        warnings.append(
            f"Friday after {fri_hour:02d}:00 UTC — London closed, volume drying up. "
            "Weekend gap risk if holding a position into the close."
        )

    # ── 11. Correlation guard ─────────────────────────────────────────────────
    # Academy: EURUSD + GBPUSD open simultaneously = double USD exposure.
    # Opening correlated pairs multiplies risk beyond the 0.5% per-trade rule.
    pair_clean = pair_upper.replace("/", "").replace("-", "").replace("_", "")
    correlated = CORRELATED_PAIRS.get(pair_clean, [])

    if correlated:
        try:
            from mt5_connector import get_positions
            open_positions = get_positions() or []
            open_symbols = {
                p.get("symbol", "").replace("/", "").replace("-", "").replace("_", "").upper()
                for p in open_positions
            }
            conflicts = [c for c in correlated if c in open_symbols]
            checks["correlation_ok"] = len(conflicts) == 0
            if conflicts:
                warnings.append(
                    f"Correlation risk: already holding {', '.join(conflicts)}. "
                    f"Adding {pair_clean} doubles USD directional exposure — "
                    "consider sizing down or skipping this trade."
                )
        except Exception:
            # Fail open — if MT5 is unavailable, don't block the trade
            checks["correlation_ok"] = True
    else:
        checks["correlation_ok"] = True

    # ── Lot size calculation ───────────────────────────────────────────────────
    lot_data = calculate_lot_size(
        pair=pair,
        entry=entry,
        sl=sl,
        pip_value_override=pip_value_override,
    )

    # ── Verdict ────────────────────────────────────────────────────────────────
    hard_failures = [k for k, v in checks.items() if not v and k not in ("news_clear", "sl_not_too_tight", "not_in_crisis")]
    if issues and any("HARD STOP" in i or "BLOCKED" in i or "RRR" in i for i in issues):
        verdict = "NO-GO"
    elif hard_failures:
        verdict = "NO-GO"
    elif warnings or not checks["news_clear"]:
        verdict = "CAUTION"
    else:
        verdict = "GO"

    return {
        "verdict":    verdict,
        "pair":       pair_upper,
        "direction":  direction,
        "entry":      entry,
        "sl":         sl,
        "tp":         tp,
        "rrr":        round(rrr, 2),
        "sl_pips":    round(sl_pips, 1),
        "lot_size":   lot_data.get("lot_size"),
        "risk_usd":   lot_data.get("max_loss_if_sl_hit"),
        "reward_usd": round((lot_data.get("lot_size", 0) or 0) * (tp_dist / pip_size) * (lot_data.get("pip_value", 10)), 2),
        "checks":     checks,
        "issues":     issues,
        "warnings":   warnings,
        "lot_detail": lot_data,
    }
