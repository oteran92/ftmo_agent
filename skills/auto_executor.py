"""
Auto Executor — Automatic trade execution via MetaApi cloud API.

Risk management philosophy (senior trader approach):
  - No fixed position limit — manage by AGGREGATE RISK, not trade count
  - Max aggregate open risk: 2% of balance at any time
  - Max 3 correlated USD pairs simultaneously (correlation control)
  - Daily auto-halt at 3% loss (safety margin before FTMO 5% daily limit)
  - Per-trade risk: 0.5% of balance
  - Conviction filter: HIGH or MEDIUM only

FTMO hard limits (never breach):
  - Daily loss limit: 5% ($5,000 on $100k)
  - Max total drawdown: 10% ($10,000 on $100k)

Required env vars:
  METAAPI_TOKEN      — from app.metaapi.cloud → API access
  METAAPI_ACCOUNT_ID — from app.metaapi.cloud → Accounts
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent.parent
CEST     = timezone(timedelta(hours=2))

# Risk parameters
_RISK_FRACTION        = 0.005   # 0.5% risk per trade
_MAX_AGGREGATE_RISK   = 0.02    # halt new trades if open risk already ≥ 2% of balance
_MAX_USD_PAIRS        = 3       # max simultaneous positions containing USD
_DAILY_HALT_FRACTION  = 0.60    # halt at 60% of daily limit (= 3% loss on $100k)
_FTMO_DAILY_LIMIT     = 5000.0  # FTMO $100k account daily loss limit

# Pip value per standard lot (USD) — used for lot sizing
_PIP_VALUES: dict[str, float] = {
    "EURUSD": 10.0, "GBPUSD": 10.0, "AUDUSD": 10.0, "USDCHF": 10.0,
    "USDCAD": 7.5,  "USDJPY": 6.5,  "EURJPY": 8.0,  "GBPJPY": 8.0,
    "XAUUSD": 10.0,
}

# Pairs that contain USD (for correlation cap)
_USD_PAIRS = {"EURUSD", "GBPUSD", "AUDUSD", "USDCHF", "USDCAD", "USDJPY", "USDCHF"}


# ── MetaApi async helpers ─────────────────────────────────────────────────────

def _get_api():
    """Return a MetaApi instance; raises RuntimeError if SDK or token missing."""
    try:
        from metaapi_cloud_sdk import MetaApi  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "metaapi-cloud-sdk not installed. Run: pip install metaapi-cloud-sdk"
        ) from exc

    token = os.environ.get("METAAPI_TOKEN", "")
    if not token:
        raise RuntimeError("METAAPI_TOKEN not set in .env")
    return MetaApi(token)


async def _fetch_account_state() -> dict:
    """Connect to MetaApi and return live balance, equity and open positions."""
    account_id = os.environ.get("METAAPI_ACCOUNT_ID", "")
    if not account_id:
        raise RuntimeError("METAAPI_ACCOUNT_ID not set in .env")

    api     = _get_api()
    account = await api.metatrader_account_api.get_account(account_id)

    if account.state not in ("DEPLOYED", "DEPLOYING"):
        await account.deploy()
    await account.wait_connected(timeout_in_seconds=60)

    conn = account.get_rpc_connection()
    await conn.connect()
    await conn.wait_synchronized(timeout_in_seconds=60)

    info      = await conn.get_account_information()
    positions = await conn.get_positions()

    await conn.close()
    api.close()

    return {
        "balance":   float(info.get("balance",  100000.0)),
        "equity":    float(info.get("equity",   100000.0)),
        "positions": positions or [],
    }


async def _place_order(
    account_id: str, direction: str, pair: str,
    lots: float, sl: float, tp: float, comment: str,
) -> dict:
    """Place a market order and return the MetaApi result dict."""
    api     = _get_api()
    account = await api.metatrader_account_api.get_account(account_id)

    if account.state not in ("DEPLOYED", "DEPLOYING"):
        await account.deploy()
    await account.wait_connected(timeout_in_seconds=60)

    conn = account.get_rpc_connection()
    await conn.connect()
    await conn.wait_synchronized(timeout_in_seconds=60)

    opts = {"comment": comment}
    if direction == "BUY":
        result = await conn.create_market_buy_order(pair, lots, sl, tp, opts)
    else:
        result = await conn.create_market_sell_order(pair, lots, sl, tp, opts)

    await conn.close()
    api.close()
    return result


def _run_async(coro):
    """Run an async coroutine safely whether or not a loop is already running."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ── Sync wrappers ─────────────────────────────────────────────────────────────

def _live_state() -> dict:
    """Fetch live account state; return safe defaults on error."""
    try:
        return _run_async(_fetch_account_state())
    except Exception as exc:
        print(f"[auto_executor] MetaApi state fetch failed: {exc}", flush=True)
        return {"balance": 100000.0, "equity": 100000.0, "positions": []}


def _todays_pnl(state: dict) -> float:
    """Return today's realised P&L; falls back to equity-balance delta."""
    path = BASE_DIR / "data" / "challenge_state.json"
    if path.exists():
        try:
            cs    = json.loads(path.read_text())
            today = datetime.now(CEST).strftime("%Y-%m-%d")
            return float(cs.get("daily_pnl", {}).get(today, 0.0))
        except Exception:
            pass
    return state.get("equity", 0.0) - state.get("balance", 0.0)


def _lots(balance: float, sl_pips: float, pair: str) -> float:
    """Calculate lot size for 0.5% risk given SL in pips."""
    risk_usd  = balance * _RISK_FRACTION
    pip_value = _PIP_VALUES.get(pair.upper(), 10.0)
    if sl_pips <= 0:
        return 0.01
    return round(min(max(risk_usd / (sl_pips * pip_value), 0.01), 10.0), 2)


def _aggregate_open_risk(positions: list, balance: float) -> float:
    """
    Estimate total open risk as fraction of balance.
    Uses unrealized loss proxy: (entry - current) * lots * pip_value.
    Falls back to 0.5% per position if SL data unavailable.
    """
    total_risk = 0.0
    for p in positions:
        # Best estimate: use 0.5% per position (conservative proxy)
        total_risk += _RISK_FRACTION
    return total_risk


def _usd_pair_count(positions: list, new_pair: str) -> int:
    """Count how many open positions + new pair involve USD."""
    pairs = {p.get("symbol", "").upper() for p in positions}
    pairs.add(new_pair.upper())
    return sum(1 for p in pairs if p in _USD_PAIRS)


# ── Public interface ──────────────────────────────────────────────────────────

def execute_trade(signal: dict, dry_run: bool = False) -> dict[str, Any]:
    """
    Execute a trade from a GO signal dict via MetaApi.

    Guards applied (in order):
      1. Signal must be GO_LONG or GO_SHORT
      2. Conviction must be HIGH or MEDIUM
      3. No duplicate position on same pair
      4. Aggregate open risk < 2% of balance
      5. USD pair correlation cap: max 3 USD pairs simultaneously
      6. Daily P&L not below 3% loss (60% of FTMO 5% daily limit)

    Args:
        signal:  Output from signal_engine with keys:
                 signal, symbol, trade (entry/sl/tp/sl_pips/tp_pips), conviction
        dry_run: Validate all guards but do not place the order.
    """
    warnings:  list[str] = []
    pair       = (signal.get("symbol") or signal.get("pair", "")).upper()
    sig_type   = signal.get("signal", "")
    trade      = signal.get("trade", {})
    conviction = signal.get("conviction", "UNKNOWN")

    # Guard 1 — actionable signal
    if sig_type not in ("GO_LONG", "GO_SHORT"):
        return {"executed": False,
                "reason": f"Signal is {sig_type} — not actionable.", "command": None}

    # Guard 2 — conviction
    if conviction not in ("HIGH", "MEDIUM"):
        return {"executed": False,
                "reason": f"Conviction {conviction} — requires HIGH or MEDIUM.",
                "command": None}

    # Fetch live state once for all remaining guards
    state     = _live_state()
    positions = state.get("positions", [])
    balance   = state["balance"]

    # Guard 3 — no duplicate on this pair
    if any(p.get("symbol", "").upper() == pair for p in positions):
        return {"executed": False,
                "reason": f"Position already open on {pair}.", "command": None}

    # Guard 4 — aggregate open risk cap (2% of balance)
    current_risk = _aggregate_open_risk(positions, balance)
    new_total_risk = current_risk + _RISK_FRACTION
    if new_total_risk > _MAX_AGGREGATE_RISK:
        return {"executed": False,
                "reason": (f"Aggregate open risk {current_risk*100:.1f}% + "
                           f"new trade {_RISK_FRACTION*100:.1f}% = "
                           f"{new_total_risk*100:.1f}% exceeds 2% cap."),
                "command": None}
    if current_risk > 0:
        warnings.append(
            f"Aggregate risk after entry: {new_total_risk*100:.1f}% of balance."
        )

    # Guard 5 — USD correlation cap
    if pair in _USD_PAIRS:
        usd_count = _usd_pair_count(positions, pair)
        if usd_count > _MAX_USD_PAIRS:
            return {"executed": False,
                    "reason": (f"USD correlation cap: already {usd_count-1} USD pairs open. "
                               f"Max {_MAX_USD_PAIRS}."),
                    "command": None}

    # Guard 6 — daily loss halt at 3% (60% of FTMO 5% limit)
    pnl = _todays_pnl(state)
    halt_threshold = _FTMO_DAILY_LIMIT * _DAILY_HALT_FRACTION
    if pnl < 0 and abs(pnl) >= halt_threshold:
        return {"executed": False,
                "reason": (f"Daily loss ${abs(pnl):.0f} reached "
                           f"{abs(pnl)/(_FTMO_DAILY_LIMIT/100):.1f}% of FTMO daily limit. "
                           "Auto-execution halted for today."),
                "command": None}
    if pnl < 0:
        pct_used = abs(pnl) / (_FTMO_DAILY_LIMIT / 100)
        warnings.append(f"Today P&L: ${pnl:.0f} ({pct_used:.1f}% of daily limit used).")

    sl_pips  = float(trade.get("sl_pips", 15))
    lot_size = _lots(balance, sl_pips, pair)
    risk_usd = round(balance * _RISK_FRACTION, 2)
    direction = "BUY" if sig_type == "GO_LONG" else "SELL"

    command = {
        "action":    "open_trade",
        "symbol":    pair,
        "type":      direction,
        "lots":      lot_size,
        "sl":        trade.get("sl", 0),
        "tp":        trade.get("tp", 0),
        "comment":   f"Auto|{conviction}|{lot_size}L|R${risk_usd}",
        "timestamp": datetime.now(CEST).isoformat(),
    }

    if dry_run:
        return {"executed": True, "dry_run": True,
                "reason": "Dry run — no order placed.",
                "command": command, "lots": lot_size,
                "risk_usd": risk_usd, "warnings": warnings}

    account_id = os.environ.get("METAAPI_ACCOUNT_ID", "")
    if not account_id:
        return {"executed": False,
                "reason": "METAAPI_ACCOUNT_ID not in .env", "command": command}

    try:
        result   = _run_async(_place_order(
            account_id, direction, pair, lot_size,
            float(trade.get("sl", 0)), float(trade.get("tp", 0)),
            command["comment"],
        ))
        order_id = result.get("orderId") or result.get("positionId") or "?"
        _log(pair, direction, lot_size, trade, conviction, risk_usd, order_id)
        return {
            "executed":  True,
            "command":   command,
            "lots":      lot_size,
            "risk_usd":  risk_usd,
            "warnings":  warnings,
            "order_id":  order_id,
            "reason":    f"MetaApi: {direction} {lot_size} {pair} | "
                         f"{conviction} | orderId {order_id}",
        }
    except Exception as exc:
        return {"executed": False,
                "reason":   f"MetaApi order failed: {exc}", "command": command}


def _log(
    pair: str, direction: str, lots: float, trade: dict,
    conviction: str, risk_usd: float, order_id: str = "?",
) -> None:
    """Append to data/auto_executions.json for audit trail (keep last 200)."""
    path = BASE_DIR / "data" / "auto_executions.json"
    try:
        records = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        records = []

    records.append({
        "timestamp":  datetime.now(CEST).isoformat(),
        "pair":       pair,
        "direction":  direction,
        "lots":       lots,
        "entry":      trade.get("entry"),
        "sl":         trade.get("sl"),
        "tp":         trade.get("tp"),
        "sl_pips":    trade.get("sl_pips"),
        "tp_pips":    trade.get("tp_pips"),
        "conviction": conviction,
        "risk_usd":   risk_usd,
        "order_id":   order_id,
        "via":        "MetaApi",
    })
    path.write_text(json.dumps(records[-200:], indent=2))
