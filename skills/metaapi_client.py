"""
MetaApi Client — unified async wrapper around the MetaApi cloud SDK.

Provides the same public API that mt5_connector.py used to provide, so all
consumers (agent.py, agent_api.py, review_trade.py, trade_journal.py) can
import from this module without knowing the underlying transport.

All public functions are synchronous wrappers; async helpers are private.

Required env vars:
  METAAPI_TOKEN      — from app.metaapi.cloud → API Access
  METAAPI_ACCOUNT_ID — from app.metaapi.cloud → MT Accounts
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from config import CEST


# ── Internal async helpers ─────────────────────────────────────────────────────

def _token() -> str:
    t = os.environ.get("METAAPI_TOKEN", "")
    if not t:
        raise RuntimeError("METAAPI_TOKEN not set in .env")
    return t


def _account_id() -> str:
    a = os.environ.get("METAAPI_ACCOUNT_ID", "")
    if not a:
        raise RuntimeError("METAAPI_ACCOUNT_ID not set in .env")
    return a


def _get_api():
    try:
        from metaapi_cloud_sdk import MetaApi  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "metaapi-cloud-sdk not installed. Run: pip install metaapi-cloud-sdk"
        ) from exc
    return MetaApi(_token())


def _run(coro):
    """Run an async coroutine safely whether or not a loop is already running."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


async def _open_conn():
    """Connect and synchronize; returns (api, account, conn)."""
    api     = _get_api()
    account = await api.metatrader_account_api.get_account(_account_id())
    if account.state not in ("DEPLOYED", "DEPLOYING"):
        await account.deploy()
    await account.wait_connected(timeout_in_seconds=60)
    conn = account.get_rpc_connection()
    await conn.connect()
    await conn.wait_synchronized(timeout_in_seconds=60)
    return api, account, conn


async def _async_live_summary() -> dict:
    api, _, conn = await _open_conn()
    info      = await conn.get_account_information()
    positions = await conn.get_positions()
    await conn.close()
    api.close()
    return {
        "bridge_connected":       True,
        "data_age_seconds":       0,
        "account": {
            "login":       info.get("login"),
            "name":        info.get("name"),
            "server":      info.get("broker"),
            "balance":     float(info.get("balance",  0)),
            "equity":      float(info.get("equity",   0)),
            "margin":      float(info.get("margin",   0)),
            "free_margin": float(info.get("freeMargin", 0)),
            "profit":      float(info.get("equity", 0)) - float(info.get("balance", 0)),
            "currency":    info.get("currency", "USD"),
            "leverage":    info.get("leverage"),
            "server_time": datetime.now(CEST).isoformat(),
        },
        "open_positions":       positions or [],
        "open_positions_count": len(positions or []),
        "floating_pnl":         sum(
            float(p.get("unrealizedProfit", p.get("profit", 0)))
            for p in (positions or [])
        ),
    }


async def _async_get_positions() -> list[dict]:
    api, _, conn = await _open_conn()
    positions = await conn.get_positions()
    await conn.close()
    api.close()
    return positions or []


async def _async_get_closed_trades(days: int = 30) -> list[dict]:
    """Fetch closed deals from MetaApi history (last N days)."""
    api, _, conn = await _open_conn()
    start = datetime.now(CEST) - timedelta(days=days)
    deals = await conn.get_deals_by_time_range(start, datetime.now(CEST))
    await conn.close()
    api.close()

    # Normalize MetaApi deal format to match the old mt5 closed_trades schema
    trades = []
    for d in (deals or []):
        if d.get("entryType") != "DEAL_ENTRY_OUT":
            continue  # only closed positions (not entries or SL/TP partials)
        trades.append({
            "ticket":     d.get("dealId") or d.get("id"),
            "symbol":     d.get("symbol"),
            "type":       "buy" if d.get("type") == "DEAL_TYPE_BUY" else "sell",
            "volume":     d.get("volume"),
            "profit":     float(d.get("profit", 0)),
            "close_time": str(d.get("time", "")),
            "price":      d.get("price"),
            "comment":    d.get("comment", ""),
        })
    return trades


async def _async_get_price(symbol: str) -> dict | None:
    api, _, conn = await _open_conn()
    tick = await conn.get_symbol_price(symbol)
    await conn.close()
    api.close()
    if tick:
        return {"bid": float(tick.get("bid", 0)), "ask": float(tick.get("ask", 0))}
    return None


async def _async_send_order(
    direction: str, symbol: str, lots: float, sl: float, tp: float, comment: str
) -> dict:
    api, _, conn = await _open_conn()
    opts = {"comment": comment}
    if direction.upper() == "BUY":
        result = await conn.create_market_buy_order(symbol, lots, sl, tp, opts)
    else:
        result = await conn.create_market_sell_order(symbol, lots, sl, tp, opts)
    await conn.close()
    api.close()
    return result


async def _async_close_position(position_id: str) -> dict:
    api, _, conn = await _open_conn()
    result = await conn.close_position(position_id)
    await conn.close()
    api.close()
    return result or {}


async def _async_modify_position(position_id: str, sl: float, tp: float) -> dict:
    api, _, conn = await _open_conn()
    result = await conn.modify_position(position_id, sl, tp)
    await conn.close()
    api.close()
    return result or {}


# ── Public synchronous API ────────────────────────────────────────────────────

def live_account_summary() -> dict:
    """
    Return full account snapshot: balance, equity, margin, open positions.
    Raises RuntimeError if MetaApi is unreachable or env vars are missing.
    """
    try:
        return _run(_async_live_summary())
    except Exception as exc:
        return {
            "bridge_connected": False,
            "error": str(exc),
            "account": {},
            "open_positions": [],
            "open_positions_count": 0,
            "floating_pnl": 0.0,
        }


def get_positions() -> list[dict]:
    """Return list of open positions. Empty list if none or connection error."""
    try:
        return _run(_async_get_positions())
    except Exception:
        return []


def get_closed_trades(days: int = 30) -> list[dict]:
    """Return closed trades for the last N days, newest first."""
    try:
        trades = _run(_async_get_closed_trades(days))
        return list(reversed(trades))  # most recent first
    except Exception:
        return []


def get_price(symbol: str) -> dict | None:
    """Return {bid, ask} for a symbol, or None if unavailable."""
    try:
        return _run(_async_get_price(symbol))
    except Exception:
        return None


def is_connected() -> bool:
    """True if we can successfully reach MetaApi and the account is deployed."""
    try:
        summary = live_account_summary()
        return summary.get("bridge_connected", False)
    except Exception:
        return False


# Kept for backward compat with any code that called is_bridge_connected()
is_bridge_connected = is_connected


def send_order(
    action: str,
    symbol: str,
    lot_size: float,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    comment: str = "FTMO-Agent",
    magic_number: int = 0,  # unused — MetaApi doesn't use magic numbers via cloud
) -> dict:
    """
    Send a market order. action must be 'buy' or 'sell'.
    Returns dict with order_id and status.
    """
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got: {action!r}")
    try:
        result   = _run(_async_send_order(action.upper(), symbol, lot_size, stop_loss, take_profit, comment))
        order_id = result.get("orderId") or result.get("positionId") or "?"
        return {
            "status":   "order_placed",
            "order_id": order_id,
            "action":   action,
            "symbol":   symbol,
            "lot_size": lot_size,
            "sl":       stop_loss,
            "tp":       take_profit,
            "via":      "MetaApi",
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def close_position(position_id: str, close_volume: float = 0.0) -> dict:
    """
    Close an open position by its MetaApi position ID.
    close_volume is ignored (MetaApi requires a separate partial-close API).
    """
    try:
        return _run(_async_close_position(str(position_id))) or {"status": "closed"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def modify_position(
    position_id: Any, stop_loss: float = 0.0, take_profit: float = 0.0
) -> dict:
    """Modify SL/TP on an existing open position."""
    try:
        return _run(_async_modify_position(str(position_id), stop_loss, take_profit)) or {"status": "modified"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
