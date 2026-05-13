"""
backtest/ablation.py
--------------------
Strategy ablation: run 5 variants of the EMA Trend + Pullback strategy
across all monitored pairs and compare expectancy, profit factor, and
max drawdown to identify the optimal configuration.

Variants tested:
  baseline   - RRR 1:2,   SL +5 pips, all trading days
  rrr_1_5    - RRR 1:1.5, SL +5 pips, all trading days
  rrr_3      - RRR 1:3,   SL +5 pips, all trading days
  sl_3p      - RRR 1:2,   SL +3 pips, all trading days
  tue_wed_thu- RRR 1:2,   SL +5 pips, Tue/Wed/Thu only

Usage:
    python -m backtest.ablation --all

    Or from Python:
        from backtest.ablation import run_ablation
        results = run_ablation(["EURUSD", "GBPUSD"])
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

from backtest.engine import BacktestConfig, run_backtest
from backtest.metrics import compute_metrics
from config import MONITORED_PAIRS

# ── Variant definitions ────────────────────────────────────────────────────────
# Each tuple: (variant_id, label, BacktestConfig overrides)
_VARIANTS: list[tuple[str, str, dict]] = [
    (
        "baseline",
        "Baseline (RRR 1:2, SL +5p, all days)",
        {"rrr": 2.0, "sl_buffer_pips": 5, "day_filter": None},
    ),
    (
        "rrr_1_5",
        "RRR 1:1.5 (closer TP, higher WR hypothesis)",
        {"rrr": 1.5, "sl_buffer_pips": 5, "day_filter": None},
    ),
    (
        "rrr_3",
        "RRR 1:3 (wider TP, patience trade)",
        {"rrr": 3.0, "sl_buffer_pips": 5, "day_filter": None},
    ),
    (
        "sl_3p",
        "SL +3 pips (tighter SL, smaller loss size)",
        {"rrr": 2.0, "sl_buffer_pips": 3, "day_filter": None},
    ),
    (
        "tue_wed_thu",
        "Tue/Wed/Thu only (FTMO Academy day filter)",
        # weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
        {"rrr": 2.0, "sl_buffer_pips": 5, "day_filter": [1, 2, 3]},
    ),
]


@dataclass
class VariantResult:
    variant_id: str
    label: str
    pair: str
    metrics: dict
    trade_count: int


def run_variant(pair: str, variant_id: str, overrides: dict, verbose: bool = False) -> VariantResult:
    """Run a single backtest variant for one pair."""
    cfg = BacktestConfig(**overrides)
    trades = run_backtest(pair, config=cfg, verbose=verbose)
    m = compute_metrics(trades)
    return VariantResult(
        variant_id=variant_id,
        label="",
        pair=pair,
        metrics=m,
        trade_count=len(trades),
    )


def run_ablation(
    pairs: list[str] | None = None,
    verbose: bool = True,
) -> dict[str, list[VariantResult]]:
    """
    Run all 5 variants for every pair.

    Returns a dict keyed by variant_id, each containing a list of
    VariantResult objects (one per pair), plus an "__aggregate__" entry.
    """
    if pairs is None:
        pairs = MONITORED_PAIRS

    # results[variant_id] = list of VariantResult (one per pair)
    results: dict[str, list[VariantResult]] = {v[0]: [] for v in _VARIANTS}

    for pair in pairs:
        if verbose:
            print(f"\n══ {pair} ══")
        for variant_id, label, overrides in _VARIANTS:
            if verbose:
                print(f"  variant: {variant_id} …", end=" ", flush=True)
            vr = run_variant(pair, variant_id, overrides, verbose=False)
            vr.label = label
            results[variant_id].append(vr)
            if verbose:
                m = vr.metrics
                print(f"trades={m['total_trades']:3d}  "
                      f"WR={m['win_rate']:5.1f}%  "
                      f"E=${m['expectancy_usd']:+7.2f}  "
                      f"PF={m['profit_factor']:.2f}  "
                      f"maxDD={m['max_drawdown_pct']:.1f}%")

    return results


def _aggregate_metrics(variant_results: list[VariantResult]) -> dict:
    """Compute aggregate metrics across all pairs for a variant (sum P&L, avg ratios)."""
    all_trades_count = sum(r.trade_count for r in variant_results)
    if all_trades_count == 0:
        return {"total_trades": 0, "expectancy_usd": 0.0, "profit_factor": 0.0,
                "win_rate": 0.0, "max_drawdown_pct": 0.0}

    total_pnl    = sum(r.metrics["total_pnl_usd"] for r in variant_results)
    total_wins   = sum(r.metrics["wins"] for r in variant_results)
    total_losses = sum(r.metrics["losses"] for r in variant_results)
    total_n      = sum(r.metrics["total_trades"] for r in variant_results)

    avg_expectancy = total_pnl / total_n if total_n else 0.0
    avg_wr         = total_wins / total_n * 100 if total_n else 0.0
    avg_pf         = sum(r.metrics["profit_factor"] for r in variant_results) / len(variant_results)
    avg_dd         = max(r.metrics["max_drawdown_pct"] for r in variant_results)

    return {
        "total_trades":     total_n,
        "wins":             total_wins,
        "losses":           total_losses,
        "win_rate":         round(avg_wr, 1),
        "expectancy_usd":   round(avg_expectancy, 2),
        "profit_factor":    round(avg_pf, 3),
        "max_drawdown_pct": round(avg_dd, 2),
        "total_pnl_usd":    round(total_pnl, 2),
    }


def format_ablation_table(results: dict[str, list[VariantResult]]) -> str:
    """Return a markdown-formatted ablation comparison table."""
    lines = [
        "## Ablation Results\n",
        "| Variant | Trades | WR% | E$/trade | PF | MaxDD% | Total P&L |",
        "|---------|--------|-----|----------|----|--------|-----------|",
    ]

    for variant_id, label, _ in _VARIANTS:
        vr_list = results.get(variant_id, [])
        agg = _aggregate_metrics(vr_list)
        lines.append(
            f"| **{variant_id}** | {agg['total_trades']} "
            f"| {agg['win_rate']:.1f}% "
            f"| ${agg['expectancy_usd']:+.2f} "
            f"| {agg['profit_factor']:.2f} "
            f"| {agg['max_drawdown_pct']:.1f}% "
            f"| ${agg['total_pnl_usd']:+.0f} |"
        )

    lines.append("")
    lines.append("*Trades, P&L and Sharpe at 0.1 lot size. MaxDD as % of $10k nominal account.*")
    return "\n".join(lines)


def get_per_pair_table(results: dict[str, list[VariantResult]], variant_id: str = "baseline") -> str:
    """Return a per-pair breakdown table for a given variant."""
    vr_list = results.get(variant_id, [])
    if not vr_list:
        return f"No results for variant '{variant_id}'"

    lines = [
        f"## Per-Pair Breakdown — {variant_id}\n",
        "| Pair | Trades | WR% | E$/trade | PF | MaxDD% | Total P&L | Rating |",
        "|------|--------|-----|----------|----|--------|-----------|--------|",
    ]

    for vr in vr_list:
        m = vr.metrics
        # Rating: GO if expectancy > $5 and PF > 1.3, NO-GO if expectancy < 0
        if m["expectancy_usd"] > 5 and m["profit_factor"] > 1.3:
            rating = "✅ GO"
        elif m["expectancy_usd"] < 0:
            rating = "❌ NO-GO"
        else:
            rating = "⚠️ WATCH"

        lines.append(
            f"| {vr.pair} "
            f"| {m['total_trades']} "
            f"| {m['win_rate']:.1f}% "
            f"| ${m['expectancy_usd']:+.2f} "
            f"| {m['profit_factor']:.2f} "
            f"| {m['max_drawdown_pct']:.1f}% "
            f"| ${m['total_pnl_usd']:+.0f} "
            f"| {rating} |"
        )

    return "\n".join(lines)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Run strategy ablation across all pairs")
    parser.add_argument("--all",   action="store_true", help="Run all pairs (default if no --pairs)")
    parser.add_argument("--pairs", default="",          help="Comma-separated pair list")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-trade progress")
    args = parser.parse_args()

    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()] \
            if args.pairs else None

    print("=== v3.0 Strategy Ablation ===\n")
    results = run_ablation(pairs=pairs, verbose=not args.quiet)

    print("\n" + "=" * 60)
    print(format_ablation_table(results))
    print()
    print(get_per_pair_table(results, "baseline"))


if __name__ == "__main__":
    _main()
