"""
Pre-mortem Risk Analysis — Claude-powered trade veto layer.

Called AFTER all 7 deterministic guards pass in auto_executor.execute_trade,
BEFORE the order is placed on MetaApi.

Claude acts as a senior risk officer reviewing the trade proposal.
Hard veto: if failure probability exceeds 60%, the trade is blocked.
Fail-open: on API errors, returns GO (never blocks trades due to infra issues).

Cost: ~$0.005/call at Sonnet 4.6 pricing (~1.5k in + 250 out tokens).
Frequency: ~3-5 times/week with the current 5-pair universe.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from config import CLAUDE_MODEL, CEST

_LOG_FILE = Path(__file__).parent.parent / "data" / "premortem_log.json"

_SYSTEM_PROMPT = """You are a senior risk officer at a proprietary trading firm.
A junior trader proposes a trade. Your job is to run a PRE-MORTEM:
imagine the trade has already FAILED, and identify why.

Rules:
- Identify the 3 most likely failure modes for THIS SPECIFIC trade (not generic risks).
- Decide: GO (proceed) or VETO (block).
- VETO ONLY when you estimate combined failure probability > 60%.
- Factors to consider: recent loss streaks on this pair, time-of-day/week,
  overextended price, conflicting fundamental/sentiment signals, correlated exposure,
  proximity to major news events, pair-specific volatility patterns.
- Be CONCRETE and specific. Reference the actual numbers provided.

Output ONLY valid JSON — no markdown, no explanation outside the JSON:
{"verdict": "GO" or "VETO", "reason": "<one sentence justification>", "risks": ["<risk 1>", "<risk 2>", "<risk 3>"]}"""


def _build_user_prompt(
    signal: dict,
    account_state: dict,
    recent_trades: list[dict],
) -> str:
    """Assemble the context payload Claude needs to evaluate the trade."""
    trade = signal.get("trade", {})
    analyst = signal.get("analyst", {})
    pillars = analyst.get("pillars", {})

    now = datetime.now(tz=CEST)
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    # Format recent trade history for this pair
    pair = signal.get("symbol", signal.get("pair", ""))
    pair_history = [t for t in recent_trades if t.get("symbol", "").upper() == pair.upper()][-5:]
    history_lines = []
    for t in pair_history:
        pnl = t.get("profit", 0)
        outcome = "WIN" if pnl > 0 else "LOSS"
        history_lines.append(f"  - {outcome} ${pnl:+.2f} on {t.get('close_time', '?')}")

    history_str = "\n".join(history_lines) if history_lines else "  No recent trades on this pair."

    # Format open positions
    positions = account_state.get("positions", [])
    pos_lines = []
    for p in positions:
        sym = p.get("symbol", "?")
        pnl = p.get("unrealizedProfit", p.get("profit", 0))
        pos_lines.append(f"  - {sym}: ${pnl:+.2f}")
    pos_str = "\n".join(pos_lines) if pos_lines else "  No open positions."

    return f"""TRADE PROPOSAL:
- Pair: {pair}
- Direction: {signal.get('signal', '?')}
- Entry: {trade.get('entry', '?')}
- SL: {trade.get('sl', '?')} ({trade.get('sl_pips', '?')} pips)
- TP: {trade.get('tp', '?')} ({trade.get('tp_pips', '?')} pips)
- RRR: {trade.get('rrr', '?')}

CONVICTION: {signal.get('conviction', 'UNKNOWN')} (score: {analyst.get('score', '?')}/6)
ANALYST SUMMARY: {analyst.get('summary', 'N/A')}

PILLAR SCORES:
- Technical: {pillars.get('technical', {}).get('score', '?')}/2 — {pillars.get('technical', {}).get('detail', '')}
- Fundamental: {pillars.get('fundamental', {}).get('score', '?')}/2 — {pillars.get('fundamental', {}).get('detail', '')}
- Sentiment: {pillars.get('sentiment', {}).get('score', '?')}/1 — {pillars.get('sentiment', {}).get('detail', '')}
- Statistical: {pillars.get('statistical', {}).get('score', '?')}/1 — {pillars.get('statistical', {}).get('detail', '')}

ACCOUNT STATE:
- Balance: ${account_state.get('balance', 0):,.0f}
- Equity: ${account_state.get('equity', 0):,.0f}
- Today P&L: ${account_state.get('today_pnl', 0):+.2f}

OPEN POSITIONS:
{pos_str}

RECENT TRADE HISTORY ON {pair} (last 5):
{history_str}

CONTEXT:
- Day: {day_names[now.weekday()]}
- Time (CEST): {now.strftime('%H:%M')}
- Trading session: {'London' if 8 <= now.hour < 16 else 'New York' if 12 <= now.hour < 21 else 'Off-hours'}

Based on this data, run a pre-mortem analysis. Output ONLY the JSON verdict."""


def evaluate_setup(
    signal: dict,
    account_state: dict,
    recent_trades: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Ask Claude to evaluate a trade setup before execution.

    Returns:
        {"verdict": "GO"|"VETO", "reason": str, "risks": list[str], "tokens_used": int}

    On any failure (API down, parsing error), returns GO with a warning reason
    to avoid blocking trades on infrastructure issues.
    """
    if recent_trades is None:
        recent_trades = []

    try:
        import anthropic
    except ImportError:
        return {"verdict": "GO", "reason": "anthropic SDK not installed — premortem skipped", "risks": [], "tokens_used": 0}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"verdict": "GO", "reason": "ANTHROPIC_API_KEY not set — premortem skipped", "risks": [], "tokens_used": 0}

    user_prompt = _build_user_prompt(signal, account_state, recent_trades)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = msg.content[0].text.strip()
        tokens_used = (msg.usage.input_tokens or 0) + (msg.usage.output_tokens or 0)

        # Parse JSON response — Claude may wrap in markdown fences
        text = raw_text
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(text)
        verdict = result.get("verdict", "GO").upper()
        if verdict not in ("GO", "VETO"):
            verdict = "GO"

        return {
            "verdict": verdict,
            "reason": result.get("reason", ""),
            "risks": result.get("risks", []),
            "tokens_used": tokens_used,
        }

    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        return {"verdict": "GO", "reason": f"premortem parse error: {exc}", "risks": [], "tokens_used": 0}
    except Exception as exc:
        return {"verdict": "GO", "reason": f"premortem unavailable: {exc}", "risks": [], "tokens_used": 0}


def log_verdict(
    pair: str,
    direction: str,
    verdict_data: dict,
    signal: dict | None = None,
) -> None:
    """Append a pre-mortem verdict to the persistent log for future v4.0 analysis."""
    entry = {
        "ts": datetime.now(tz=CEST).isoformat(),
        "pair": pair,
        "direction": direction,
        "verdict": verdict_data.get("verdict"),
        "reason": verdict_data.get("reason"),
        "risks": verdict_data.get("risks", []),
        "tokens": verdict_data.get("tokens_used", 0),
    }

    if signal:
        entry["analyst_snapshot"] = {
            "conviction": signal.get("conviction"),
            "score": signal.get("analyst", {}).get("score"),
            "summary": signal.get("analyst", {}).get("summary", ""),
        }

    try:
        existing = []
        if _LOG_FILE.exists():
            existing = json.loads(_LOG_FILE.read_text())
        existing.append(entry)
        # Cap at 500 entries to avoid unbounded growth
        _LOG_FILE.write_text(json.dumps(existing[-500:], indent=2))
    except Exception:
        pass  # logging is non-critical
