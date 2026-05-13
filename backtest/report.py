"""
backtest/report.py
------------------
Generates docs/BACKTEST_v3.md from ablation + per-pair backtest results.

Usage:
    python -m backtest.report              # full run, all pairs, writes docs/BACKTEST_v3.md
    python -m backtest.report --pair EURUSD  # single pair smoke test
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from backtest.ablation import (
    _VARIANTS,
    _aggregate_metrics,
    format_ablation_table,
    get_per_pair_table,
    run_ablation,
)
from backtest.engine import BacktestConfig, run_backtest
from backtest.metrics import compute_metrics
from config import MONITORED_PAIRS

_DOCS_DIR = Path(__file__).parent.parent / "docs"
_DOCS_DIR.mkdir(exist_ok=True)
_REPORT_PATH = _DOCS_DIR / "BACKTEST_v3.md"


def _edge_verdict(agg: dict) -> str:
    """Return YES / MARGINAL / NO based on aggregate metrics."""
    exp = agg.get("expectancy_usd", 0)
    pf  = agg.get("profit_factor", 0)
    if exp > 5 and pf > 1.3:
        return "**YES** — The strategy has a quantifiable edge."
    if exp > 0:
        return "**MARGINAL** — Positive expectancy but below the $5/trade threshold for safe scaling."
    return "**NO** — Negative expectancy. Strategy redesign required before live trading."


def _recommendations(per_pair: list, variant_id: str = "baseline") -> str:
    """Build concrete recommendations from per-pair results."""
    go     = [vr.pair for vr in per_pair if vr.metrics["expectancy_usd"] > 5 and vr.metrics["profit_factor"] > 1.3]
    watch  = [vr.pair for vr in per_pair if 0 < vr.metrics["expectancy_usd"] <= 5]
    no_go  = [vr.pair for vr in per_pair if vr.metrics["expectancy_usd"] <= 0]

    lines = []
    if go:
        lines.append(f"- **Keep trading:** {', '.join(go)} — expectancy positive and PF > 1.3")
    if watch:
        lines.append(f"- **Monitor:** {', '.join(watch)} — marginal expectancy, needs more data")
    if no_go:
        lines.append(f"- **Consider disabling:** {', '.join(no_go)} — negative expectancy over 5 years")

    best = max(per_pair, key=lambda vr: vr.metrics["expectancy_usd"]) if per_pair else None
    if best:
        lines.append(f"- **Best performing pair:** {best.pair} "
                     f"(E=${best.metrics['expectancy_usd']:+.2f}/trade, "
                     f"PF={best.metrics['profit_factor']:.2f})")

    return "\n".join(lines) if lines else "Insufficient data for recommendations."


def generate_report(pairs: list[str] | None = None, verbose: bool = True) -> str:
    """
    Run full ablation and generate the markdown report.
    Returns the report content as a string.
    """
    if pairs is None:
        pairs = MONITORED_PAIRS

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print("\n=== Running ablation study (this may take a few minutes) ===\n")
    results = run_ablation(pairs=pairs, verbose=verbose)

    # Aggregate across all pairs for executive summary
    baseline_vrs  = results.get("baseline", [])
    baseline_agg  = _aggregate_metrics(baseline_vrs)
    verdict       = _edge_verdict(baseline_agg)
    recs          = _recommendations(baseline_vrs)

    # Build winner variant (best expectancy in ablation)
    best_variant_id = "baseline"
    best_exp = baseline_agg.get("expectancy_usd", 0.0)
    for vid, _, _ in _VARIANTS:
        agg = _aggregate_metrics(results.get(vid, []))
        if agg.get("expectancy_usd", 0) > best_exp:
            best_exp = agg["expectancy_usd"]
            best_variant_id = vid

    # ── Build document ─────────────────────────────────────────────────────────
    sections = []

    sections.append(f"""# FTMO Agent — Backtest Report v3.0

**Generated:** {now}
**Strategy:** EMA Trend + Pullback (D1 EMA50 bias | H4 EMA20 pullback | engulfing/pin entry)
**Data:** TwelveData historical OHLC, 5 years, 9 pairs × 3 intervals
**Execution model:** Entry at next H4 open + 1 pip spread + 0–0.5 pip slippage
**Lot size for P&L:** 0.1 lot (normalized to a $10k nominal account)

---

## 1. Executive Summary

| Metric | Value |
|--------|-------|
| Total trades (baseline) | {baseline_agg['total_trades']} |
| Win rate | {baseline_agg['win_rate']:.1f}% |
| Expectancy per trade | ${baseline_agg['expectancy_usd']:+.2f} |
| Profit factor | {baseline_agg['profit_factor']:.2f} |
| Max drawdown | {baseline_agg['max_drawdown_pct']:.1f}% of $10k |
| Total P&L (5y, 0.1 lot) | ${baseline_agg['total_pnl_usd']:+.0f} |

### Does the bot have edge?

{verdict}

---

## 2. Per-Pair Breakdown (Baseline variant)

{get_per_pair_table(results, 'baseline')}

---

## 3. Ablation Results

{format_ablation_table(results)}

### Winner variant

**{best_variant_id}** — highest aggregate expectancy (${best_exp:+.2f}/trade).
""")

    # Per-pair detail for best variant (if different from baseline)
    if best_variant_id != "baseline":
        sections.append(f"""
{get_per_pair_table(results, best_variant_id)}
""")

    sections.append(f"""---

## 4. Recommendations

{recs}

---

## 5. Caveats & Limitations

1. **News filter not included** — the live bot blocks/downgrades signals near high-impact news events. The backtest may generate trades that the live system would skip, inflating trade count and potentially distorting win rate.
2. **4-Pillar conviction filter not included** — only the Technical pillar is reproducible offline. The live bot requires MEDIUM+ conviction (COT + fundamentals + stats); roughly 30–40% of GO signals are downgraded to WATCH in live mode.
3. **Slippage is randomized uniformly** — in live markets, slippage on fast-moving pairs (GBPJPY, USDJPY) can exceed 2 pips. Results on those pairs may be optimistic.
4. **Fixed 0.1 lot size** — live position sizing scales with account balance (1% risk). P&L numbers are for illustration; scale proportionally.
5. **Walk-forward, not fitted** — no parameter optimization was performed on the data used for evaluation. The strategy uses the same parameters live and in backtest, making results honest out-of-sample proxies.
6. **Overnight/weekend gaps** — modeled conservatively (worst-case open fill through SL), but extreme flash crash events (2015 CHF unpeg, etc.) are not fully captured.

---

*This report is generated automatically by `backtest/report.py`. Re-run after any strategy change.*
""")

    return "\n".join(sections)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Generate BACKTEST_v3.md report")
    parser.add_argument("--pair",  default="", help="Single pair for smoke test")
    parser.add_argument("--quiet", action="store_true", help="Suppress verbose output")
    args = parser.parse_args()

    pairs = [args.pair.strip().upper()] if args.pair.strip() else None

    content = generate_report(pairs=pairs, verbose=not args.quiet)
    _REPORT_PATH.write_text(content)
    print(f"\n[report] Written to {_REPORT_PATH}")


if __name__ == "__main__":
    _main()
