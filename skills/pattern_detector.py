"""
Pattern Detector Skill
Analyzes the trade log for emotional and behavioral anti-patterns.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from state import load_trade_log


def analyze_patterns(trades: list[dict] | None = None) -> dict[str, Any]:
    """
    Identifies: revenge trading, overtrading, RRR decay, time-of-day bias,
    pair concentration risk, and win/loss streaks.
    """
    log = trades or load_trade_log()
    if not log:
        return {"patterns": [], "summary": "No trade data available yet."}

    patterns: list[dict] = []

    # ── Sort by timestamp ──────────────────────────────────────────────────────
    def parse_ts(t: dict) -> datetime:
        try:
            return datetime.fromisoformat(t.get("timestamp", "2000-01-01"))
        except Exception:
            return datetime(2000, 1, 1)

    log_sorted = sorted(log, key=parse_ts)

    # ── 1. Revenge trading: loss followed by immediate re-entry ───────────────
    for i in range(1, len(log_sorted)):
        prev, curr = log_sorted[i - 1], log_sorted[i]
        if prev.get("pnl", 0) < 0:
            try:
                t_prev = parse_ts(prev)
                t_curr = parse_ts(curr)
                mins_gap = (t_curr - t_prev).total_seconds() / 60
                if mins_gap < 15:
                    patterns.append({
                        "type":     "REVENGE_TRADING",
                        "severity": "HIGH",
                        "detail":   f"Trade entered {mins_gap:.0f}min after a losing trade at {t_curr.strftime('%H:%M')}.",
                        "trade_id": i,
                    })
            except Exception:
                pass

    # ── 2. Overtrading: >3 trades in a single session ────────────────────────
    by_date: dict[str, list[dict]] = defaultdict(list)
    for t in log_sorted:
        day = parse_ts(t).strftime("%Y-%m-%d")
        by_date[day].append(t)

    for day, day_trades in by_date.items():
        if len(day_trades) > 3:
            patterns.append({
                "type":     "OVERTRADING",
                "severity": "MEDIUM",
                "detail":   f"{len(day_trades)} trades on {day}. Maximum recommended: 3.",
                "day":      day,
            })

    # ── 3. RRR decay: average RRR trend declining ────────────────────────────
    rrr_values = [t.get("rrr", 0) for t in log_sorted if t.get("rrr")]
    if len(rrr_values) >= 5:
        first_half = sum(rrr_values[:len(rrr_values)//2]) / (len(rrr_values)//2)
        second_half = sum(rrr_values[len(rrr_values)//2:]) / (len(rrr_values) - len(rrr_values)//2)
        if second_half < first_half * 0.85:
            patterns.append({
                "type":     "RRR_DECAY",
                "severity": "MEDIUM",
                "detail":   f"Average RRR declining: {first_half:.2f} → {second_half:.2f}. Quality degrading over time.",
            })

    # ── 4. Time-of-day bias: losses concentrated in specific hours ───────────
    hour_pnl: dict[int, list[float]] = defaultdict(list)
    for t in log_sorted:
        hour = parse_ts(t).hour
        if "pnl" in t:
            hour_pnl[hour].append(t["pnl"])

    for hour, pnls in hour_pnl.items():
        if len(pnls) >= 3:
            avg = sum(pnls) / len(pnls)
            if avg < -50:
                patterns.append({
                    "type":     "TIME_BIAS",
                    "severity": "LOW",
                    "detail":   f"Consistently losing at {hour:02d}:00 UTC (avg P&L: ${avg:.2f}). Consider avoiding this hour.",
                })

    # ── 5. Pair concentration ─────────────────────────────────────────────────
    pair_counts: dict[str, int] = defaultdict(int)
    for t in log_sorted:
        pair_counts[t.get("pair", "UNKNOWN")] += 1
    total = len(log_sorted)
    for pair, count in pair_counts.items():
        if count / total > 0.6 and total > 5:
            patterns.append({
                "type":     "CONCENTRATION_RISK",
                "severity": "LOW",
                "detail":   f"{count}/{total} trades on {pair} ({count/total*100:.0f}%). Diversify across pairs.",
            })

    # ── 6. Win rate and expectancy ────────────────────────────────────────────
    wins   = [t for t in log_sorted if t.get("pnl", 0) > 0]
    losses = [t for t in log_sorted if t.get("pnl", 0) < 0]
    win_rate = len(wins) / len(log_sorted) if log_sorted else 0
    avg_win  = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(abs(t["pnl"]) for t in losses) / len(losses) if losses else 0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    # ── Summary ───────────────────────────────────────────────────────────────
    high_severity = [p for p in patterns if p["severity"] == "HIGH"]
    summary = (
        f"{len(log_sorted)} trades analyzed | "
        f"Win rate: {win_rate*100:.1f}% | "
        f"Expectancy: ${expectancy:.2f}/trade | "
        f"{len(patterns)} pattern(s) detected ({len(high_severity)} high-severity)"
    )

    return {
        "patterns":   patterns,
        "summary":    summary,
        "stats": {
            "total_trades": len(log_sorted),
            "win_rate_pct": round(win_rate * 100, 1),
            "avg_win_usd":  round(avg_win, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "expectancy":   round(expectancy, 2),
            "avg_rrr":      round(sum(rrr_values) / len(rrr_values), 2) if rrr_values else 0,
        },
    }
