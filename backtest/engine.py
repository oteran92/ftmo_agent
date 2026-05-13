"""
backtest/engine.py
------------------
Walk-forward backtest engine for the EMA Trend + Pullback strategy.

Each step slides forward one H4 candle, builds the exact same window
that the live monitor uses, calls _compute_signal (pure fn), and if
a signal fires simulates the trade on subsequent H4 candles.

Realism features:
  - Spread cost: subtract 1 pip from entry on long, add on short
  - Slippage: ±random 0–0.5 pip (seeded per pair for reproducibility)
  - Gap-aware SL/TP: if next candle opens through SL/TP → fill at open (worst-case gaps)
  - Weekend gap detection: Sunday open vs Friday close
  - Max bars held: 60 H4 candles (~10 trading days) → timeout close

Usage:
    from backtest.engine import run_backtest, BacktestConfig

    cfg = BacktestConfig()
    trades = run_backtest("EURUSD", cfg)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from backtest.data_loader import load_or_fetch
from skills.signal_engine import _compute_signal

# Pip sizes — mirrors config.py PIP_SIZE
_PIP_SIZE: dict = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "XAUUSD": 0.1,
    "EURJPY": 0.01,
    "GBPJPY": 0.01,
    "AUDUSD": 0.0001,
    "USDCAD": 0.0001,
    "USDCHF": 0.0001,
}

# Pip values in USD per standard lot (100k units)
_PIP_VALUE_USD: dict = {
    "EURUSD": 10.0,
    "GBPUSD": 10.0,
    "USDJPY": 7.14,   # approx at 140 JPY/USD
    "XAUUSD": 10.0,   # $1 per 0.1 pip (gold tick)
    "EURJPY": 7.14,
    "GBPJPY": 7.14,
    "AUDUSD": 10.0,
    "USDCAD": 7.50,
    "USDCHF": 11.0,
}

_SPREAD_PIPS = 1.0       # standard FTMO cost per trade
_MAX_SLIPPAGE_PIPS = 0.5 # max random slippage added to spread
_MAX_BARS_HELD = 60      # H4 bars → ~10 trading days timeout
_D1_LOOKBACK = 100       # D1 candles passed to _compute_signal
_H4_LOOKBACK = 100       # H4 candles passed to _compute_signal
_MIN_INDEX = max(_D1_LOOKBACK, _H4_LOOKBACK) + 10  # buffer before first signal


def _parse_ts(ts_str: str) -> datetime:
    """Parse TwelveData timestamp string to UTC-aware datetime."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.min.replace(tzinfo=timezone.utc)


def _is_weekend_gap(prev_ts: str, curr_ts: str) -> bool:
    """Return True if there is a weekend gap between two candles."""
    try:
        p = _parse_ts(prev_ts)
        c = _parse_ts(curr_ts)
        # Gap of more than 55 hours (Fri close → Sun open) signals a weekend
        return (c - p).total_seconds() > 55 * 3600
    except Exception:
        return False


def _corresponding_d1_slice(
    d1_candles: list[dict],
    h4_ts: str,
) -> list[dict]:
    """
    Return the D1 window visible at the time of this H4 candle.
    We include all D1 candles whose timestamp is <= h4_ts, up to _D1_LOOKBACK.
    """
    h4_dt = _parse_ts(h4_ts)
    visible = [c for c in d1_candles if _parse_ts(c["t"]) <= h4_dt]
    return visible[-_D1_LOOKBACK:]


@dataclass
class BacktestConfig:
    """Parameters that define a single backtest variant."""
    rrr: float = 2.0           # reward-to-risk ratio (TP = entry ± sl_dist × rrr)
    sl_buffer_pips: int = 5    # pips added beyond H4 high/low for SL
    day_filter: Optional[list] = None  # e.g. [1,2,3] = Mon/Tue/Wed; None = all days
    spread_pips: float = _SPREAD_PIPS
    max_bars_held: int = _MAX_BARS_HELD
    lot_size: float = 0.1      # fixed lot size for P&L calculation
    random_seed: int = 42      # for reproducible slippage


@dataclass
class Trade:
    """Result of a single simulated trade."""
    pair: str
    direction: str             # "LONG" or "SHORT"
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    exit_reason: str           # "TP", "SL", "timeout"
    sl: float
    tp: float
    sl_pips: float
    tp_pips: float
    pips: float                # positive = profit
    pnl_usd: float             # profit/loss in USD
    lot_size: float
    signal_snapshot: dict = field(default_factory=dict)


def simulate_trade(
    pair: str,
    direction: str,
    raw_entry: float,
    sl: float,
    tp: float,
    future_h4: list[dict],
    config: BacktestConfig,
    rng: random.Random,
    entry_time: str,
) -> Trade:
    """
    Simulate a trade given an entry price and the sequence of future H4 candles.

    Entry is adjusted by spread and slippage. Each subsequent candle is checked
    for SL/TP touch. Gap-aware: if next candle opens through SL → fill at open.
    """
    pip = _PIP_SIZE.get(pair, 0.0001)
    pv  = _PIP_VALUE_USD.get(pair, 10.0)

    slippage = rng.uniform(0, _MAX_SLIPPAGE_PIPS) * pip
    spread   = config.spread_pips * pip

    if direction == "LONG":
        entry = raw_entry + spread + slippage
    else:
        entry = raw_entry - spread - slippage

    exit_price  = entry
    exit_reason = "timeout"
    exit_time   = entry_time

    for i, bar in enumerate(future_h4[:config.max_bars_held]):
        bar_open = bar["o"]
        bar_high = bar["h"]
        bar_low  = bar["l"]
        ts       = bar["t"]

        is_gap = (i == 0) and _is_weekend_gap(entry_time, ts)

        if direction == "LONG":
            if is_gap and bar_open <= sl:
                # Gap through SL — fill at open (worst case)
                exit_price  = bar_open
                exit_reason = "SL"
                exit_time   = ts
                break
            if bar_low <= sl:
                exit_price  = sl
                exit_reason = "SL"
                exit_time   = ts
                break
            if bar_high >= tp:
                exit_price  = tp
                exit_reason = "TP"
                exit_time   = ts
                break
        else:  # SHORT
            if is_gap and bar_open >= sl:
                exit_price  = bar_open
                exit_reason = "SL"
                exit_time   = ts
                break
            if bar_high >= sl:
                exit_price  = sl
                exit_reason = "SL"
                exit_time   = ts
                break
            if bar_low <= tp:
                exit_price  = tp
                exit_reason = "TP"
                exit_time   = ts
                break
    else:
        # Timed out — close at last bar's close price
        if future_h4[:config.max_bars_held]:
            last = future_h4[min(config.max_bars_held - 1, len(future_h4) - 1)]
            exit_price = last["c"]
            exit_time  = last["t"]

    if direction == "LONG":
        pips   = (exit_price - entry) / pip
    else:
        pips   = (entry - exit_price) / pip

    pnl_usd = pips * pv * config.lot_size

    return Trade(
        pair=pair,
        direction=direction,
        entry_time=entry_time,
        entry_price=round(entry, 6),
        exit_time=exit_time,
        exit_price=round(exit_price, 6),
        exit_reason=exit_reason,
        sl=sl,
        tp=tp,
        sl_pips=round(abs(entry - sl) / pip, 1),
        tp_pips=round(abs(tp - entry) / pip, 1),
        pips=round(pips, 1),
        pnl_usd=round(pnl_usd, 2),
        lot_size=config.lot_size,
    )


def run_backtest(
    pair: str,
    config: Optional[BacktestConfig] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    verbose: bool = True,
) -> list[Trade]:
    """
    Run a walk-forward backtest for one pair using cached historical data.

    Parameters
    ----------
    pair        : e.g. "EURUSD"
    config      : BacktestConfig (default values if None)
    start_date  : ISO date string to start from (inclusive), or None for full history
    end_date    : ISO date string to end at (inclusive), or None for full history
    verbose     : print progress to stdout

    Returns a list of Trade objects.
    """
    if config is None:
        config = BacktestConfig()

    sym = pair.replace("/", "").upper()
    rng = random.Random(config.random_seed)

    if verbose:
        print(f"[backtest] Loading {sym} …")

    d1_all = load_or_fetch(sym, "1day",  verbose=verbose)
    h4_all = load_or_fetch(sym, "4h",    verbose=verbose)

    if not d1_all or not h4_all:
        if verbose:
            print(f"[backtest] No data for {sym} — skipping")
        return []

    # Apply date filter
    if start_date:
        h4_all = [c for c in h4_all if c["t"][:10] >= start_date[:10]]
    if end_date:
        h4_all = [c for c in h4_all if c["t"][:10] <= end_date[:10]]

    if len(h4_all) < _MIN_INDEX:
        if verbose:
            print(f"[backtest] Insufficient H4 data after date filter ({len(h4_all)} bars)")
        return []

    trades: list[Trade] = []
    skip_until_idx = 0  # avoid overlapping trades; skip until previous trade exits

    for i in range(_MIN_INDEX, len(h4_all)):
        if i < skip_until_idx:
            continue

        current_h4_bar = h4_all[i]
        bar_ts = current_h4_bar["t"]

        # Day-of-week filter (0=Mon … 6=Sun)
        if config.day_filter is not None:
            bar_dt = _parse_ts(bar_ts)
            if bar_dt.weekday() not in config.day_filter:
                continue

        # Build windows exactly as the live signal does
        h4_window = h4_all[i - _H4_LOOKBACK : i]
        d1_window = _corresponding_d1_slice(d1_all, bar_ts)

        if len(d1_window) < 55 or len(h4_window) < 25:
            continue

        sig = _compute_signal(
            d1_window,
            h4_window,
            sym,
            rrr_override=config.rrr,
            sl_buffer_pips=config.sl_buffer_pips,
        )

        if sig["signal"] not in ("GO_LONG", "GO_SHORT"):
            continue

        direction = "LONG" if sig["signal"] == "GO_LONG" else "SHORT"
        trade_info = sig["trade"]

        # Entry is at the OPEN of the NEXT H4 candle (realistic execution)
        if i + 1 >= len(h4_all):
            continue
        next_bar   = h4_all[i + 1]
        raw_entry  = next_bar["o"]
        entry_time = next_bar["t"]

        future_h4 = h4_all[i + 1 :]  # candles available from entry bar onward

        trade = simulate_trade(
            pair=sym,
            direction=direction,
            raw_entry=raw_entry,
            sl=trade_info["sl"],
            tp=trade_info["tp"],
            future_h4=future_h4,
            config=config,
            rng=rng,
            entry_time=entry_time,
        )
        trade.signal_snapshot = sig

        trades.append(trade)

        # Skip until the exit bar to avoid overlapping positions
        exit_ts  = trade.exit_time
        exit_idx = next((j for j in range(i + 1, len(h4_all)) if h4_all[j]["t"] >= exit_ts), i + 1)
        skip_until_idx = exit_idx + 1

    if verbose:
        wins = sum(1 for t in trades if t.pnl_usd > 0)
        print(f"[backtest] {sym}: {len(trades)} trades | {wins} wins | "
              f"P&L ${sum(t.pnl_usd for t in trades):+.0f}")

    return trades
