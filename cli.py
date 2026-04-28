"""
FTMO Agent — Main CLI entry point.
Starts the agent + background automations and opens an interactive chat loop.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime

# ── Rich terminal output ───────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.text import Text
    RICH = True
except ImportError:
    RICH = False

from agent import FTMOAgent
from automations.news_watcher    import run_news_watcher
from automations.daily_reset     import run_daily_reset_watcher
from automations.drawdown_monitor import run_drawdown_monitor
from config import NEWS_CHECK_INTERVAL_MIN

console = Console() if RICH else None


def _print(msg: str, style: str = "") -> None:
    if RICH and console:
        if style:
            console.print(msg, style=style)
        else:
            console.print(Markdown(msg))
    else:
        print(msg)


def _header() -> None:
    banner = (
        "╔══════════════════════════════════════════════════════╗\n"
        "║          FTMO $100K Challenge — Risk Manager         ║\n"
        "║          Powered by Claude · claude-sonnet-4-6       ║\n"
        "╚══════════════════════════════════════════════════════╝"
    )
    _print(banner, style="bold cyan" if RICH else "")


def _start_background_automations() -> None:
    """Launch all automation threads as daemons."""
    threads = [
        threading.Thread(
            target=run_news_watcher,
            args=(NEWS_CHECK_INTERVAL_MIN,),
            daemon=True,
            name="NewsWatcher",
        ),
        threading.Thread(
            target=run_daily_reset_watcher,
            daemon=True,
            name="DailyReset",
        ),
        threading.Thread(
            target=run_drawdown_monitor,
            args=(60,),
            daemon=True,
            name="DrawdownMonitor",
        ),
    ]
    for t in threads:
        t.start()
    _print(
        f"✅ Background automations started: {', '.join(t.name for t in threads)}",
        style="green" if RICH else "",
    )


def _hint() -> None:
    _print(
        "\n**Quick commands:**\n"
        "- `Review Trade: EUR/USD Entry 1.0850 SL 1.0810 TP 1.0930`\n"
        "- `End of Day: +320` or `End of Day: -480`\n"
        "- `Crisis Mode`\n"
        "- `Status` — account overview (live MT5 if bridge running)\n"
        "- `Positions` — list open MT5 trades\n"
        "- `Price EURUSD` — live bid/ask from MT5\n"
        "- `News` — upcoming high-impact events\n"
        "- `Patterns` — analyze my trade log\n"
        "- `exit` — quit\n"
    )


def main() -> None:
    _header()
    _start_background_automations()

    agent = FTMOAgent()

    # Auto-greet: try live MT5 data first, fall back to local state
    _print("\n**Initializing — connecting to MT5 bridge…**", style="dim" if RICH else "")
    greeting = agent.chat(
        "Start session. Call mt5_live_account to get real-time MT5 data. "
        "If the bridge is not connected, fall back to account_status. "
        "Give me a concise briefing: balance, equity, open positions, "
        "today's risk budget, drawdown, and phase. End with today's date and time."
    )
    _print(greeting)
    _hint()

    # ── Interactive loop ───────────────────────────────────────────────────────
    while True:
        try:
            if RICH:
                user_input = console.input("[bold yellow]You:[/bold yellow] ").strip()
            else:
                user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            _print("\nSession ended. Stay disciplined.", style="bold" if RICH else "")
            sys.exit(0)

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "q"):
            _print("Session ended. Remember: consistency > perfection.", style="bold cyan" if RICH else "")
            sys.exit(0)

        response = agent.chat(user_input)
        _print(f"\n**Agent:** {response}\n")


if __name__ == "__main__":
    main()
