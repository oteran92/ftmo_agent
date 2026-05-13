"""
backtest/metrics.py
-------------------
Performance metrics for a list of Trade objects.

compute_metrics(trades, start_date, end_date) -> dict with:
  - win_rate          : % of profitable trades
  - expectancy_usd    : avg P&L per trade (the metric that matters most)
  - profit_factor     : gross wins / gross losses (>1.5 good, >2 excellent)
  - max_drawdown_pct  : peak-to-valley equity drawdown as % of starting equity
  - max_daily_dd_pct  : worst single-day loss as % of starting equity
  - avg_duration_h    : mean trade duration in hours
  - sharpe_annualized : annualized Sharpe ratio (using per-trade returns)
  - trades_per_month  : throughput over the backtest window
  - total_pnl_usd     : net P&L over the period
  - total_trades      : count of trades
  - wins / losses     : sub-counts
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backtest.engine import Trade


def _parse_ts(ts: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.min.replace(tzinfo=timezone.utc)


def compute_metrics(
    trades: list,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Compute all performance metrics for a list of Trade objects.

    start_date / end_date are used only to calculate the backtest window length
    (for trades_per_month). Trades are not re-filtered here.
    """
    if not trades:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "expectancy_usd": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "max_daily_dd_pct": 0.0,
            "avg_duration_h": 0.0,
            "sharpe_annualized": 0.0,
            "trades_per_month": 0.0,
            "total_pnl_usd": 0.0,
        }

    pnls     = [t.pnl_usd for t in trades]
    wins     = [p for p in pnls if p > 0]
    losses   = [p for p in pnls if p <= 0]

    n        = len(pnls)
    win_rate = len(wins) / n if n else 0.0

    expectancy = sum(pnls) / n if n else 0.0

    gross_win  = sum(wins)   or 0.0
    gross_loss = abs(sum(losses)) or 1e-9   # avoid /0
    profit_factor = gross_win / gross_loss

    # Equity curve (starts at 0 → cumulative P&L)
    equity = []
    running = 0.0
    for p in pnls:
        running += p
        equity.append(running)

    # Max drawdown — peak-to-valley on cumulative equity
    peak = equity[0]
    max_dd_abs = 0.0
    for e in equity:
        peak = max(peak, e)
        dd   = peak - e
        max_dd_abs = max(max_dd_abs, dd)

    # Express as % of gross wins (proxy for account exposure)
    # Using a nominal $10 000 account to normalize
    nominal_account = 10_000.0
    max_drawdown_pct = (max_dd_abs / nominal_account) * 100

    # Max daily drawdown
    # Group trades by exit date, sum P&L per day
    daily: dict = {}
    for t in trades:
        day = t.exit_time[:10]
        daily[day] = daily.get(day, 0.0) + t.pnl_usd

    max_daily_dd_pct = 0.0
    if daily:
        worst_day = min(daily.values())
        if worst_day < 0:
            max_daily_dd_pct = abs(worst_day) / nominal_account * 100

    # Average trade duration
    durations_h = []
    for t in trades:
        try:
            dt_in  = _parse_ts(t.entry_time)
            dt_out = _parse_ts(t.exit_time)
            hours  = (dt_out - dt_in).total_seconds() / 3600
            durations_h.append(hours)
        except Exception:
            pass
    avg_duration_h = sum(durations_h) / len(durations_h) if durations_h else 0.0

    # Sharpe ratio (annualized, per-trade)
    # Treat each trade P&L as a return; no risk-free rate (simplification for private account)
    mean_pnl = expectancy
    if n > 1:
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (n - 1)
        std_pnl  = math.sqrt(variance)
    else:
        std_pnl = 1e-9

    # Annualize: assume ~4 trades/month → ~48 trades/year
    trades_per_year_factor = math.sqrt(48)
    sharpe_annualized = (mean_pnl / std_pnl) * trades_per_year_factor if std_pnl > 0 else 0.0

    # Trades per month
    if start_date and end_date:
        dt_start = _parse_ts(start_date)
        dt_end   = _parse_ts(end_date)
        months   = max((dt_end - dt_start).days / 30.4375, 1.0)
    elif trades:
        dt_start = _parse_ts(trades[0].entry_time)
        dt_end   = _parse_ts(trades[-1].exit_time)
        months   = max((dt_end - dt_start).days / 30.4375, 1.0)
    else:
        months = 1.0

    trades_per_month = n / months

    return {
        "total_trades":      n,
        "wins":              len(wins),
        "losses":            len(losses),
        "win_rate":          round(win_rate * 100, 1),        # %
        "expectancy_usd":    round(expectancy, 2),
        "profit_factor":     round(profit_factor, 3),
        "max_drawdown_pct":  round(max_drawdown_pct, 2),
        "max_daily_dd_pct":  round(max_daily_dd_pct, 2),
        "avg_duration_h":    round(avg_duration_h, 1),
        "sharpe_annualized": round(sharpe_annualized, 3),
        "trades_per_month":  round(trades_per_month, 1),
        "total_pnl_usd":     round(sum(pnls), 2),
    }
