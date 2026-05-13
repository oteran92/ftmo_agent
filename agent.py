"""
FTMO Agent — Claude-powered orchestrator.
Exposes all skills as tools and maintains a persistent conversation loop.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import anthropic

from config import CLAUDE_MODEL
from state import load_state, get_drawdown_pct, daily_reset_if_needed, save_state
from skills.review_trade    import review_trade
from skills.lot_calculator  import calculate_lot_size
from skills.news_filter     import fetch_upcoming_news, check_news_window
from skills.end_of_day      import process_end_of_day
from skills.crisis_mode     import activate_crisis_mode, crisis_status, check_and_trigger_crisis
from skills.pattern_detector import analyze_patterns
from skills.metaapi_client import (
    live_account_summary,
    get_positions,
    get_price,
    send_order,
    close_position,
    modify_position,
    is_connected as is_bridge_connected,
)
from skills.signal_engine import analyze_setup, scan_all_pairs

# ── Tool schemas (Claude tool_use) ────────────────────────────────────────────
TOOLS: list[dict] = [
    {
        "name": "review_trade",
        "description": (
            "Audit a proposed trade against the full FTMO methodology. "
            "Returns GO / CAUTION / NO-GO verdict, exact lot size, RRR, "
            "news check, and all checklist results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pair":      {"type": "string",  "description": "Currency pair e.g. EUR/USD"},
                "entry":     {"type": "number",  "description": "Entry price"},
                "sl":        {"type": "number",  "description": "Stop loss price"},
                "tp":        {"type": "number",  "description": "Take profit price"},
                "direction": {"type": "string",  "description": "long or short (auto-detected if omitted)"},
                "pip_value_override": {"type": "number", "description": "Override pip value if non-standard broker"},
            },
            "required": ["pair", "entry", "sl", "tp"],
        },
    },
    {
        "name": "calculate_lot_size",
        "description": "Calculate the exact lot size for a given pair, entry, and stop loss so risk never exceeds 0.5% of account balance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pair":   {"type": "string", "description": "Currency pair"},
                "entry":  {"type": "number", "description": "Entry price"},
                "sl":     {"type": "number", "description": "Stop loss price"},
                "risk_pct": {"type": "number", "description": "Override risk % (default 0.005 = 0.5%)"},
            },
            "required": ["pair", "entry", "sl"],
        },
    },
    {
        "name": "check_news",
        "description": "Check if there are high-impact news events within the trading buffer window for a given pair.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pair": {"type": "string", "description": "Currency pair to check"},
            },
            "required": ["pair"],
        },
    },
    {
        "name": "upcoming_news",
        "description": "Fetch all upcoming high-impact news events for the next 24 hours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours_ahead": {"type": "integer", "description": "Hours to look ahead (default 24)"},
            },
        },
    },
    {
        "name": "end_of_day",
        "description": "Process end-of-day P&L, update account state, apply scaling rules, check payday trigger, and return tomorrow's risk capacity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "daily_pnl": {"type": "number",  "description": "Net P&L for the day (negative = loss)"},
                "notes":     {"type": "string",  "description": "Optional session notes"},
                "trades":    {"type": "array",   "description": "List of individual trades (optional)", "items": {"type": "object"}},
            },
            "required": ["daily_pnl"],
        },
    },
    {
        "name": "crisis_mode",
        "description": "Activate crisis mode immediately (emergency de-risk protocol).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "crisis_status",
        "description": "Check current crisis mode status and recovery progress.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "analyze_patterns",
        "description": "Analyze the full trade log for emotional and behavioral anti-patterns (revenge trading, overtrading, RRR decay, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "account_status",
        "description": "Return current account balance, drawdown, daily risk remaining, and phase.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "mt5_live_account",
        "description": (
            "Fetch LIVE account data directly from MT5 via the bridge EA: "
            "real balance, equity, margin, floating P&L, and all open positions. "
            "Use this instead of account_status when the bridge is running."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "mt5_price",
        "description": "Get the current live bid/ask price for a symbol directly from MT5.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol e.g. EURUSD, XAUUSD"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "mt5_positions",
        "description": "List all currently open positions in the MT5 account.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "mt5_send_order",
        "description": (
            "Send a market order (buy or sell) directly to MT5 via the bridge EA. "
            "ONLY call this after review_trade returns GO verdict. "
            "Requires exact lot_size, stop_loss, and take_profit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action":      {"type": "string",  "description": "buy or sell"},
                "symbol":      {"type": "string",  "description": "Symbol e.g. EURUSD"},
                "lot_size":    {"type": "number",  "description": "Lot size from calculate_lot_size"},
                "stop_loss":   {"type": "number",  "description": "Stop loss price"},
                "take_profit": {"type": "number",  "description": "Take profit price"},
                "comment":     {"type": "string",  "description": "Order comment (optional)"},
            },
            "required": ["action", "symbol", "lot_size", "stop_loss", "take_profit"],
        },
    },
    {
        "name": "mt5_close_position",
        "description": "Close an open MT5 position by ticket number. Partial close supported.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket":       {"type": "integer", "description": "Position ticket number"},
                "close_volume": {"type": "number",  "description": "Volume to close (0 = full close)"},
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "find_setup",
        "description": (
            "Scan MT5 OHLC data (D1 + H4) and detect a high-probability trade setup "
            "using the EMA Trend + Pullback methodology. "
            "Returns signal (GO_LONG / GO_SHORT / WATCH / WAIT / NO_DATA), "
            "exact entry, SL, and TP levels, and full trend analysis. "
            "Call this when the user asks 'find a setup', 'any signals?', or 'what should I trade?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Symbol to analyze e.g. EURUSD, GBPUSD, XAUUSD. Defaults to EURUSD.",
                },
                "scan_all": {
                    "type": "boolean",
                    "description": "If true, scan all Stage 1 pairs (EURUSD, GBPUSD, XAUUSD).",
                },
            },
        },
    },
    {
        "name": "stage_objectives",
        "description": "Return the current stage objectives, strategy rules, and what to validate before paying for the real FTMO challenge.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

# ── Tool dispatcher ────────────────────────────────────────────────────────────
def dispatch_tool(name: str, inputs: dict) -> Any:
    if name == "review_trade":
        return review_trade(**inputs)
    if name == "calculate_lot_size":
        return calculate_lot_size(**inputs)
    if name == "check_news":
        return check_news_window(inputs["pair"])
    if name == "upcoming_news":
        return fetch_upcoming_news(hours_ahead=inputs.get("hours_ahead", 24))
    if name == "end_of_day":
        return process_end_of_day(**inputs)
    if name == "crisis_mode":
        return activate_crisis_mode()
    if name == "crisis_status":
        return crisis_status()
    if name == "analyze_patterns":
        return analyze_patterns()
    if name == "account_status":
        state = load_state()
        dd = get_drawdown_pct(state) * 100
        balance = state["account_balance"]
        return {
            "phase":               state["phase"],
            "balance":             balance,
            "total_pnl":           state.get("total_pnl", 0.0),
            "profit_pct":          round(state.get("total_pnl", 0.0) / 100_000.0 * 100, 2),
            "total_drawdown_pct":  round(dd, 2),
            "daily_pnl":           state["daily_pnl"],
            "daily_budget_remaining": round(state["daily_risk_budget"] - state["daily_risk_used"], 2),
            "risk_per_trade":      state["risk_per_trade"],
            "consecutive_losses":  state["consecutive_losses"],
            "crisis_mode_active":  state.get("crisis_mode_active", False),
            "lot_multiplier":      state.get("current_lot_size_multiplier", 1.0),
            "high_water_mark":     state["equity_high_water_mark"],
        }

    # ── MT5 Live Bridge tools ──────────────────────────────────────────────────
    if name == "mt5_live_account":
        return live_account_summary()

    if name == "mt5_price":
        symbol = inputs.get("symbol", "").replace("/", "")
        price = get_price(symbol)
        if price is None:
            return {
                "error": f"No price data for {symbol}. "
                         "Is the bridge EA running with this symbol configured?",
                "bridge_connected": is_bridge_connected(),
            }
        return price

    if name == "mt5_positions":
        return {
            "positions": get_positions(),
            "bridge_connected": is_bridge_connected(),
        }

    if name == "mt5_send_order":
        # Safety guard: bridge must be live before sending any order
        if not is_bridge_connected():
            return {
                "error": "Cannot send order — bridge EA is not connected. "
                         "Open MT5 and attach FTMO_Bridge EA to a chart first."
            }
        return send_order(
            action=inputs["action"],
            symbol=inputs["symbol"].replace("/", ""),
            lot_size=inputs["lot_size"],
            stop_loss=inputs.get("stop_loss", 0.0),
            take_profit=inputs.get("take_profit", 0.0),
            comment=inputs.get("comment", "FTMO-Agent"),
        )

    if name == "mt5_close_position":
        if not is_bridge_connected():
            return {"error": "Cannot close — bridge EA is not connected."}
        return close_position(
            ticket=inputs["ticket"],
            close_volume=inputs.get("close_volume", 0.0),
        )

    if name == "find_setup":
        if inputs.get("scan_all"):
            return scan_all_pairs()
        sym = inputs.get("symbol", "EURUSD")
        return analyze_setup(sym)

    if name == "stage_objectives":
        import json as _json
        obj_path = os.path.join(os.path.dirname(__file__), "data", "stage_objectives.json")
        try:
            with open(obj_path, encoding="utf-8") as f:
                return _json.load(f)
        except OSError:
            return {"error": "stage_objectives.json not found"}

    return {"error": f"Unknown tool: {name}"}


# ── System prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are the FTMO Risk Manager Agent for a $100,000 prop firm challenge account.
Your personality: professional, data-driven, disciplined, unemotional. Like a senior prop trader mentor.
The trader you assist has NO prior trading experience. Explain every decision clearly and educationally.

── WHAT THE DATA SAYS ABOUT FTMO FAILURES ────────────────────────────────────
92% of traders fail. The top reasons:
  45% → Excessive risk per trade (risking 3-5% instead of 0.5%)
  30% → Revenge trading (doubling size after a loss to recover)
  15% → Strategy hopping (switching systems mid-challenge)
  10% → Overtrading (30-50 trades in week 1)
Pass rate by risk level: 0.5%→67% | 1%→35% | 2%→12% | 3%→5%
This is why we lock at 0.5% risk. It is not arbitrary — it is the highest-probability path.

── FTMO RULE MECHANICS (know these cold) ─────────────────────────────────────
Daily Loss Limit: 5% calculated from PREVIOUS day's closing balance (not current equity).
  Trap: if account was $102k at midnight and loses $5,100 intraday → FAIL, even if trade recovers.
  Our soft stop: $800 (0.8%). Our hard stop: $1,000 (1.0%). Agent halts before FTMO's limit.

Max Total Drawdown: 10% from initial balance. Floor is always $90,000. Never drops lower.
  Trap: grow to $108k then lose $18,001 → challenge over, even though you were profitable.
  Our crisis threshold: 4% ($4,000) → Crisis Mode, lot size halved.

Consistency Review: FTMO reviews BEHAVIOR after you pass — not just P&L.
  Flags: single day >70% of profits | lot size spikes | trading only 1-2 days | NFP-only profits.
  Consequence: funded account refused even after passing both phases.

No time limit (since 2024). Patience is a legal and optimal strategy.

── CORE RULES (non-negotiable) ───────────────────────────────────────────────
1. Max 0.5% risk per trade ($500 on $100k). Reject any request to increase this.
2. Max 1.0% daily risk budget ($1,000 / 2 trades). Halt when consumed.
3. After 2 consecutive losses → halt for 24 hours. No exceptions. No override.
4. Minimum 2:1 RRR. Reject setups below this.
5. No trading within ±30 min of high-impact news. Block automatically.
6. At 4% total drawdown → Crisis Mode (lot size halved until recovery).
7. Max 1-2 trades per day. More than 3 = overtrading alert.
8. Trade only London (08:00-12:00 UTC) or New York (13:00-17:00 UTC) sessions.
9. Never move a stop loss further from entry. Only trail toward profit.
10. Friday after 18:00 → warn about weekend gap risk on open positions.

── BEHAVIORAL ALERTS (enforce these) ─────────────────────────────────────────
- Lot size increased after a loss? → Revenge trading detected. Block and warn.
- Daily profit already >1%? → Recommend stopping for the day (protect gains).
- Same setup rejected but user asks again? → Maintain NO-GO. Explain why consistency matters.
- User wants to "make back" a loss? → Hard educational response: this is how 30% of accounts blow.

── PHASE-SPECIFIC GUIDANCE ───────────────────────────────────────────────────
Trial (now):    Validate process. 5-7 trades max. Rules > profit. Every trade needs review_trade.
Phase 1:        Need $10k (+10%). ~20 winning trades at 0.5% risk / 2:1 RRR. Unlimited time.
Phase 2:        Need $5k (+5%). DROP to 0.5% risk. Only 10 wins needed. MORE patience, not less.
                60% of Phase 1 passers fail Phase 2 due to overconfidence. You will warn about this.
Funded:         Consistency is everything. FTMO monitors behavior. No big swings.

When the user says "Review Trade" or provides entry/SL/TP, always call review_trade first.
When the user says "End of Day" or provides a P&L figure, call end_of_day.
When the user says "Crisis Mode", call crisis_mode immediately.
When asked about news, call check_news or upcoming_news.
Start every session by calling mt5_live_account first (falls back to account_status if bridge is offline).

MT5 Bridge tools (use when bridge EA is running):
- mt5_live_account: real-time balance, equity, open positions from MT5.
- mt5_price: live bid/ask for any symbol.
- mt5_positions: list all open trades in MT5.
- mt5_send_order: ONLY after review_trade returns GO. Sends order to MT5.
- mt5_close_position: close or partial-close a position by ticket.

Setup detection (EMA Trend + Pullback methodology):
- find_setup: scans D1 + H4 OHLC data from MT5 and returns GO_LONG / GO_SHORT / WATCH / WAIT.
  Use when the user asks "find a setup", "any signals?", "what should I trade?", or "scan the market".
  If signal is GO_LONG or GO_SHORT, immediately follow up with review_trade using the returned levels.
- stage_objectives: returns current stage goals and strategy rules.

NEVER call mt5_send_order unless review_trade explicitly returned GO verdict.
The trader has NO prior experience — explain every decision clearly and educationally.

Format your responses:
- Lead with the verdict/status clearly (GO ✅, CAUTION ⚠️, NO-GO ❌, CRISIS 🚨)
- Show the numbers (lot size, risk $, potential reward $)
- State the reason for any rejection concisely
- End with one actionable next step

Never say "I think" or "maybe" — only state facts from the data.
"""


def _build_system_prompt() -> str:
    """
    Build the system prompt dynamically, injecting recent trade lessons
    from trade_lessons.json so Claude learns from real trade history.
    """
    from skills.trade_journal import format_lessons_for_prompt
    lessons_section = format_lessons_for_prompt(n=10)
    if lessons_section:
        return SYSTEM_PROMPT + f"\n\n{lessons_section}"
    return SYSTEM_PROMPT


class FTMOAgent:
    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("[ERROR] ANTHROPIC_API_KEY not set. Run: export ANTHROPIC_API_KEY=your_key")
            sys.exit(1)
        self.client = anthropic.Anthropic(api_key=api_key)
        self.history: list[dict] = []
        # Rebuild each session so new lessons are always included
        self.system_prompt = _build_system_prompt()

        # Auto-trigger drawdown check on startup
        check_and_trigger_crisis()

        # Reset daily counters if new day
        state = load_state()
        state = daily_reset_if_needed(state)
        save_state(state)

    def chat(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})

        response = self.client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=self.history,
        )

        # ── Agentic tool-use loop ──────────────────────────────────────────────
        while response.stop_reason == "tool_use":
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            tool_results = []

            for tool_use in tool_uses:
                result = dispatch_tool(tool_use.name, tool_use.input)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tool_use.id,
                    "content":     json.dumps(result, default=str),
                })

            # Add assistant turn with tool uses
            self.history.append({"role": "assistant", "content": response.content})
            # Add tool results as user turn
            self.history.append({"role": "user", "content": tool_results})

            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=2048,
                system=self.system_prompt,
                tools=TOOLS,
                messages=self.history,
            )

        # Extract final text
        text_blocks = [b.text for b in response.content if hasattr(b, "text")]
        final = "\n".join(text_blocks)

        self.history.append({"role": "assistant", "content": response.content})
        return final
