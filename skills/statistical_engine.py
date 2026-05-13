"""
Statistical Engine — FTMO Academy Pillar 4.

Computes performance statistics from the trade log to inform future signal
conviction. As the trade history grows, these stats become increasingly reliable.

Key outputs:
  - Win rate per pair
  - Average pip gain/loss per trade and per direction
  - Session breakdown (Asian / London / NY)
  - Setup-type performance (to be enriched once analyst_snapshot fields are populated)

Data source: data/trade_log.json (local file, no API calls).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

BASE_DIR   = Path(__file__).parent.parent
TRADE_LOG  = BASE_DIR / "data" / "trade_log.json"

# Minimum trades required before a win-rate score is trusted
_MIN_TRADES_FOR_STATS = 3

# Session time ranges in CEST (UTC+2)
_SESSIONS = {
    "asian":   (23, 9),   # 23:00-09:00 CEST (crosses midnight)
    "london":  (7, 16),   # 07:00-16:00 CEST
    "ny":      (13, 22),  # 13:00-22:00 CEST (overlaps London)
}


def _load_trades() -> list[dict]:
    """Load and return the trade log, filtering out entries without a result."""
    if not TRADE_LOG.exists():
        return []
    try:
        trades = json.loads(TRADE_LOG.read_text())
        return [t for t in trades if t.get("result") in ("WIN", "LOSS")]
    except Exception:
        return []


def _parse_cest_hour(opened_at: str) -> int | None:
    """Extract hour (CEST) from opened_at string like '2026-05-11 09:44 CEST'."""
    try:
        return int(opened_at.split(" ")[1].split(":")[0])
    except (IndexError, ValueError, AttributeError):
        return None


def _get_session(hour: int | None) -> str:
    """Map an hour (0-23, CEST) to a trading session name."""
    if hour is None:
        return "unknown"
    # Asian: 23:00–09:00 (crosses midnight)
    if hour >= 23 or hour < 9:
        return "asian"
    # London: 07:00–16:00 (including pre-open overlap)
    if 7 <= hour < 16:
        # Within London hours — NY overlap starts at 13:00
        if hour >= 13:
            return "london_ny"
        return "london"
    # NY: 16:00–22:00 (post-London)
    return "ny"


def get_pair_stats(pair: str) -> dict[str, Any]:
    """
    Return historical performance stats for a specific pair.

    Returns a dict with:
      - total_trades: int
      - wins: int
      - losses: int
      - win_rate: float (0.0-1.0)  — None if insufficient data
      - avg_win_pips: float
      - avg_loss_pips: float
      - avg_rrr: float
      - score: int  — conviction contribution: +1 win_rate>50%, -1 win_rate<40%, 0 otherwise
      - note: str   — human-readable summary
    """
    trades = _load_trades()
    pair_trades = [t for t in trades if t.get("symbol", "").upper() == pair.upper()]

    wins   = [t for t in pair_trades if t.get("result") == "WIN"]
    losses = [t for t in pair_trades if t.get("result") == "LOSS"]
    total  = len(pair_trades)

    if total == 0:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": None, "avg_win_pips": None, "avg_loss_pips": None,
            "avg_rrr": None, "score": 0,
            "note": f"{pair}: No historical trades — neutral statistical score.",
        }

    win_rate     = len(wins) / total
    avg_win_pips  = (sum(t.get("pips", 0) for t in wins) / len(wins)) if wins else 0.0
    avg_loss_pips = (sum(t.get("pips", 0) for t in losses) / len(losses)) if losses else 0.0
    avg_rrr       = (
        sum(t.get("rrr", 0) for t in pair_trades if t.get("rrr")) / total
        if total > 0 else 0.0
    )

    # Only trust stats if we have enough trades
    if total < _MIN_TRADES_FOR_STATS:
        score = 0
        trust = f"(limited sample: {total} trade{'s' if total>1 else ''})"
    elif win_rate > 0.55:
        score = 1
        trust = f"({win_rate:.0%} win rate — positive edge)"
    elif win_rate < 0.40:
        score = -1
        trust = f"({win_rate:.0%} win rate — caution)"
    else:
        score = 0
        trust = f"({win_rate:.0%} win rate — neutral)"

    note = (
        f"{pair}: {total} trades, {len(wins)}W/{len(losses)}L "
        f"{trust} | avg win +{avg_win_pips:.1f}p, avg loss {avg_loss_pips:.1f}p"
    )

    return {
        "total_trades":  total,
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(win_rate, 4),
        "avg_win_pips":  round(avg_win_pips, 1),
        "avg_loss_pips": round(avg_loss_pips, 1),
        "avg_rrr":       round(avg_rrr, 2),
        "score":         score,
        "note":          note,
    }


def _get_session_stats() -> dict[str, Any]:
    """
    Return aggregate win/loss stats broken down by trading session.
    Useful for identifying which sessions our strategy performs best in.
    """
    trades = _load_trades()

    session_data: dict[str, dict] = {
        "asian":     {"wins": 0, "losses": 0, "pips": 0.0},
        "london":    {"wins": 0, "losses": 0, "pips": 0.0},
        "london_ny": {"wins": 0, "losses": 0, "pips": 0.0},
        "ny":        {"wins": 0, "losses": 0, "pips": 0.0},
        "unknown":   {"wins": 0, "losses": 0, "pips": 0.0},
    }

    for t in trades:
        hour = _parse_cest_hour(t.get("opened_at", ""))
        session = _get_session(hour)
        result  = t.get("result")
        pips    = t.get("pips", 0) or 0

        if result == "WIN":
            session_data[session]["wins"] += 1
        elif result == "LOSS":
            session_data[session]["losses"] += 1
        session_data[session]["pips"] += pips

    # Build readable summary
    summary = {}
    for sess, data in session_data.items():
        total = data["wins"] + data["losses"]
        if total == 0:
            continue
        summary[sess] = {
            "total":    total,
            "wins":     data["wins"],
            "losses":   data["losses"],
            "win_rate": round(data["wins"] / total, 4) if total > 0 else None,
            "net_pips": round(data["pips"], 1),
        }

    return summary


def get_overall_stats() -> dict[str, Any]:
    """
    Return portfolio-level statistics across all pairs.
    Used as a fallback when pair-specific data is insufficient.
    """
    trades = _load_trades()
    if not trades:
        return {"total": 0, "win_rate": None, "score": 0, "note": "No trade history yet."}

    wins   = [t for t in trades if t.get("result") == "WIN"]
    losses = [t for t in trades if t.get("result") == "LOSS"]
    total  = len(trades)
    wr     = len(wins) / total if total > 0 else 0.0

    avg_win_pips  = (sum(t.get("pips", 0) for t in wins) / len(wins)) if wins else 0.0
    avg_loss_pips = (sum(t.get("pips", 0) for t in losses) / len(losses)) if losses else 0.0

    score = 1 if wr > 0.55 else (-1 if wr < 0.40 else 0)

    return {
        "total":          total,
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(wr, 4),
        "avg_win_pips":   round(avg_win_pips, 1),
        "avg_loss_pips":  round(avg_loss_pips, 1),
        "score":          score,
        "note":           (
            f"Portfolio: {total} trades, {len(wins)}W/{len(losses)}L "
            f"({wr:.0%} win rate) | avg win +{avg_win_pips:.1f}p, avg loss {avg_loss_pips:.1f}p"
        ),
    }


def _get_session_score_for_trade(opened_at: str) -> int:
    """
    Given an opened_at string, check if this session has historically been profitable.

    Returns +1 if session net_pips > 0, -1 if negative, 0 if unknown/no data.
    """
    hour    = _parse_cest_hour(opened_at)
    session = _get_session(hour)
    stats   = _get_session_stats()

    if session not in stats:
        return 0

    s = stats[session]
    if s["total"] < 2:  # insufficient session data
        return 0
    return 1 if s["net_pips"] > 0 else -1
