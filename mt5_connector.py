"""
MT5 File Bridge Connector — macOS compatible.
Reads JSON files written by the FTMO_Bridge EA and writes trade commands.
No MetaTrader5 Python package required; pure file I/O over MQL5/Files dir.

File layout (all inside MT5_FILES_DIR):
  account_info.json   — live balance, equity, margin (updated every 2s)
  positions.json      — open positions (updated every 2s)
  closed_trades.json  — last 30 days of closed trades (updated every 10s)
  {SYM}_price.json    — bid/ask snapshot (updated every 1s)
  {SYM}_tick.json     — full tick (updated every 1s)
  commands.json       — Python writes here; EA reads and executes
  trade_results.txt   — EA appends execution results here
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

# ── Path resolution ────────────────────────────────────────────────────────────
_DEFAULT_MT5_FILES = (
    Path.home()
    / "Library/Application Support/net.metaquotes.wine.metatrader5"
    / "drive_c/Program Files/MetaTrader 5/MQL5/Files"
)

def _get_files_dir() -> Path:
    """Return MT5 Files directory from env var or macOS default."""
    env = os.environ.get("MT5_FILES_DIR")
    return Path(env) if env else _DEFAULT_MT5_FILES


# ── Low-level helpers ──────────────────────────────────────────────────────────
def _read_json(filename: str) -> dict | None:
    """Read and parse a JSON file from MT5/Files. Returns None if missing/stale."""
    path = _get_files_dir() / filename
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _bridge_age_seconds(filename: str) -> float | None:
    """Return how many seconds ago the file was last modified. None if missing."""
    path = _get_files_dir() / filename
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


# ── Public read API ────────────────────────────────────────────────────────────
def get_account_info() -> dict:
    """
    Return live account data from the bridge.
    Raises RuntimeError if MT5 is not running or bridge EA is not active.
    """
    data = _read_json("account_info.json")
    age = _bridge_age_seconds("account_info.json")

    if data is None:
        raise RuntimeError(
            "account_info.json not found. "
            "Make sure MT5 is open and FTMO_Bridge EA is attached to a chart."
        )
    if age is not None and age > 60:
        raise RuntimeError(
            f"account_info.json is {age:.0f}s old — bridge EA may be paused or disconnected."
        )
    return data


def get_positions() -> list[dict]:
    """Return list of open positions. Empty list if none or bridge not running."""
    data = _read_json("positions.json")
    if data is None:
        return []
    return data.get("positions", [])


def get_price(symbol: str) -> dict | None:
    """Return latest bid/ask for a symbol. None if not available."""
    # The EA names the file after the symbol it is configured for.
    # Try exact match, then common FTMO suffix variants.
    for candidate in [symbol, symbol.replace("/", ""), symbol + "!", symbol + "."]:
        data = _read_json(f"{candidate}_price.json")
        if data:
            return data
    return None


def get_closed_trades(limit: int = 50) -> list[dict]:
    """Return recent closed trades (last 30 days, newest first)."""
    data = _read_json("closed_trades.json")
    if data is None:
        return []
    trades = data.get("trades", [])
    return trades[:limit]


def is_bridge_connected() -> bool:
    """True if the bridge EA wrote account_info.json within the last 30 seconds."""
    age = _bridge_age_seconds("account_info.json")
    return age is not None and age <= 30


# ── Trade command API ──────────────────────────────────────────────────────────
def _write_command(payload: dict) -> str:
    """Serialize payload to commands.json and return a trade_id."""
    trade_id = payload.setdefault("trade_id", str(uuid.uuid4())[:8])
    cmd_path = _get_files_dir() / "commands.json"
    with open(cmd_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return trade_id


def send_order(
    action: str,
    symbol: str,
    lot_size: float,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    comment: str = "FTMO-Agent",
    magic_number: int = 20260427,
) -> dict:
    """
    Send a buy or sell market order via the bridge EA.
    action must be 'buy' or 'sell'.
    Returns dict with trade_id and confirmation message.
    """
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got: {action!r}")

    payload: dict[str, Any] = {
        "action":       action,
        "symbol":       symbol,
        "lot_size":     lot_size,
        "stop_loss":    stop_loss,
        "take_profit":  take_profit,
        "comment":      comment,
        "magic_number": magic_number,
    }
    trade_id = _write_command(payload)
    return {
        "status":   "command_sent",
        "trade_id": trade_id,
        "action":   action,
        "symbol":   symbol,
        "lot_size": lot_size,
        "sl":       stop_loss,
        "tp":       take_profit,
        "note":     "EA will execute within 1 second. Check trade_results.txt for confirmation.",
    }


def close_position(ticket: int, close_volume: float = 0.0) -> dict:
    """
    Close an open position by ticket number.
    close_volume=0 means full close; any positive value triggers partial close.
    """
    payload: dict[str, Any] = {
        "action":       "close",
        "ticket":       ticket,
        "close_volume": close_volume,
        "comment":      "FTMO-Agent close",
    }
    trade_id = _write_command(payload)
    return {"status": "command_sent", "trade_id": trade_id, "ticket": ticket}


def modify_position(ticket: int, stop_loss: float = 0.0, take_profit: float = 0.0) -> dict:
    """Modify SL/TP on an existing open position."""
    payload: dict[str, Any] = {
        "action":      "modify",
        "ticket":      ticket,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
    }
    trade_id = _write_command(payload)
    return {"status": "command_sent", "trade_id": trade_id, "ticket": ticket}


# ── Convenience summary ────────────────────────────────────────────────────────
def live_account_summary() -> dict:
    """
    Single call that returns everything the agent needs:
    account info + open positions + bridge status.
    Used by the mt5_live_account tool in agent.py.
    """
    age = _bridge_age_seconds("account_info.json")
    connected = age is not None and age <= 30

    # Read whatever data is available even if slightly stale
    data = _read_json("account_info.json")
    if data is None:
        return {
            "bridge_connected": False,
            "error": (
                "account_info.json not found. Open MT5, attach FTMO_Bridge EA to any chart, "
                "enable 'Allow AutoTrading' (green button in toolbar), and try again."
            ),
        }

    warning = None
    if not connected and age is not None:
        warning = (
            f"Data is {age:.0f}s old — EA may not be updating. "
            "Check that 'Algo Trading' button in MT5 toolbar is GREEN."
        )

    try:
        account = get_account_info()
    except RuntimeError as e:
        account = data  # fall back to raw file if age check fails

    positions = get_positions()

    result = {
        "bridge_connected": connected,
        "data_age_seconds": round(age, 1) if age else None,
        "account": {
            "login":        account.get("login"),
            "name":         account.get("name"),
            "server":       account.get("server"),
            "balance":      account.get("balance"),
            "equity":       account.get("equity"),
            "margin":       account.get("margin"),
            "free_margin":  account.get("free_margin"),
            "profit":       account.get("profit"),
            "currency":     account.get("currency"),
            "leverage":     account.get("leverage"),
            "server_time":  account.get("server_time"),
        },
        "open_positions": positions,
        "open_positions_count": len(positions),
        "floating_pnl": sum(p.get("profit", 0) for p in positions),
    }
    if warning:
        result["warning"] = warning
    return result


# ── Programmatic EA symbol control ─────────────────────────────────────────────

def set_bridge_symbol(symbol: str, write_interval: int = 1, enable_trading: bool = True) -> dict:
    """Write bridge_config.json so the EA monitors the given symbol on next restart.

    The EA reads this file in OnInit, so you must restart/remove+add the EA on the
    chart after calling this, OR the symbol takes effect on the next EA load.

    Returns a status dict.
    """
    config = {
        "symbol": symbol.upper(),
        "write_interval": write_interval,
        "enable_trading": enable_trading,
    }
    path = _get_files_dir() / "bridge_config.json"
    try:
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return {"ok": True, "config_written": str(path), "symbol": symbol.upper(),
                "note": "Restart the EA on the MT5 chart to apply (remove + re-add it)"}
    except OSError as e:
        return {"ok": False, "error": str(e)}
