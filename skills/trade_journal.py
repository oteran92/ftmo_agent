"""
Trade Journal — autonomous post-trade analysis with Claude.

After each trade closes, this module:
  1. Detects new closed trades by comparing MT5 closed_trades.json with the known log
  2. Calls Claude to analyze the trade: methodology, outcome, lessons learned
  3. Writes structured lessons to data/trade_lessons.json

These lessons are later injected into the agent's system prompt so Claude
learns from real trade history in every future session.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import anthropic

BASE_DIR  = Path(__file__).parent.parent
DATA_DIR  = BASE_DIR / "data"
LESSONS_FILE   = DATA_DIR / "trade_lessons.json"
KNOWN_TRADES_FILE = DATA_DIR / "known_closed_trades.json"

# MT5 closed trades file (written by FTMO_Bridge EA)
_DEFAULT_MT5_FILES = (
    Path.home()
    / "Library/Application Support/net.metaquotes.wine.metatrader5"
    / "drive_c/Program Files/MetaTrader 5/MQL5/Files"
)

CEST = timezone(timedelta(hours=2))


def _mt5_files_dir() -> Path:
    env = os.environ.get("MT5_FILES_DIR")
    return Path(env) if env else _DEFAULT_MT5_FILES


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_json(path: Path, data: Any) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_lessons(n: int = 10) -> list[dict]:
    """Return the last N trade lessons (most recent first)."""
    lessons = _load_json(LESSONS_FILE) or []
    return list(reversed(lessons[-n:])) if lessons else []


def _get_known_ticket_ids() -> set[str]:
    known = _load_json(KNOWN_TRADES_FILE) or []
    return {str(t["ticket"]) for t in known}


def _mark_trades_known(trades: list[dict]) -> None:
    existing = _load_json(KNOWN_TRADES_FILE) or []
    existing_ids = {str(t["ticket"]) for t in existing}
    for t in trades:
        if str(t.get("ticket", "")) not in existing_ids:
            existing.append({"ticket": t.get("ticket"), "symbol": t.get("symbol")})
    _save_json(KNOWN_TRADES_FILE, existing[-500:])  # cap at 500


def _analyze_trade_with_claude(trade: dict) -> str:
    """
    Call Claude to analyze a closed trade and extract a concise lesson.
    Returns a 1-2 sentence lesson string.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""You are an FTMO trading coach analyzing a completed trade.

Trade details:
- Symbol: {trade.get('symbol')}
- Direction: {trade.get('type')}
- Volume: {trade.get('volume')} lots
- Profit/Loss: ${trade.get('profit', 0):.2f}
- Close time: {trade.get('close_time')}
- Comment: {trade.get('comment', 'none')}

The trader uses an EMA Trend + Pullback methodology:
- D1 EMA50 defines bias (long/short)
- H4 EMA20 defines pullback entry zone
- Confirmation required: bullish/bearish engulfing or pin bar

Based on the trade data, write ONE concise lesson (2 sentences max) that captures:
1. What likely happened (did the setup work as expected?)
2. One actionable insight for the next trade

Be specific, direct, and honest. No generic advice."""

    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def check_and_journal_new_trades() -> list[dict]:
    """
    Main entry point: detect new closed trades and journal them.
    Returns list of newly created lessons.
    """
    # Read closed trades from MT5 bridge file
    mt5_data = _load_json(_mt5_files_dir() / "closed_trades.json")
    if not mt5_data or not mt5_data.get("trades"):
        return []

    closed_trades = mt5_data["trades"]
    known_ids     = _get_known_ticket_ids()

    # Find trades not yet analyzed
    new_trades = [
        t for t in closed_trades
        if str(t.get("ticket", "")) not in known_ids
    ]

    if not new_trades:
        return []

    new_lessons = []
    lessons = _load_json(LESSONS_FILE) or []

    for trade in new_trades:
        try:
            lesson_text = _analyze_trade_with_claude(trade)
            outcome     = "WIN" if trade.get("profit", 0) > 0 else "LOSS"

            lesson = {
                "ticket":     trade.get("ticket"),
                "date":       datetime.now(CEST).strftime("%Y-%m-%d"),
                "time":       datetime.now(CEST).strftime("%H:%M CEST"),
                "pair":       trade.get("symbol", "UNKNOWN"),
                "direction":  trade.get("type", "unknown").upper(),
                "outcome":    outcome,
                "profit_usd": round(trade.get("profit", 0), 2),
                "volume":     trade.get("volume", 0),
                "lesson":     lesson_text,
                "raw_trade":  {
                    "close_time": trade.get("close_time"),
                    "price":      trade.get("price"),
                    "comment":    trade.get("comment", ""),
                },
            }
            lessons.append(lesson)
            new_lessons.append(lesson)
        except Exception as e:
            # Non-fatal: log and continue with next trade
            new_lessons.append({
                "ticket":  trade.get("ticket"),
                "error":   str(e),
                "outcome": "ERROR",
            })

    if new_lessons:
        _save_json(LESSONS_FILE, lessons[-200:])  # keep last 200 lessons
        _mark_trades_known(new_trades)

    return new_lessons


def format_lessons_for_prompt(n: int = 10) -> str:
    """
    Return the last N lessons formatted for injection into the system prompt.
    Returns empty string if no lessons exist yet.
    """
    lessons = load_lessons(n)
    if not lessons:
        return ""

    lines = ["LESSONS FROM YOUR RECENT TRADES (learn from these):"]
    for l in lessons:
        if "lesson" in l:
            outcome_emoji = "WIN" if l.get("outcome") == "WIN" else "LOSS"
            lines.append(
                f"- [{l.get('date')} {l.get('pair')} {l.get('direction')} {outcome_emoji} "
                f"${l.get('profit_usd', 0):+.0f}] {l['lesson']}"
            )
    return "\n".join(lines)
