# FTMO Agent — Backtest Report v3.0

**Generated:** 2026-05-13 04:31 UTC
**Strategy:** EMA Trend + Pullback (D1 EMA50 bias | H4 EMA20 pullback | engulfing/pin entry)
**Data:** TwelveData historical OHLC, 5 years, 9 pairs × 3 intervals
**Execution model:** Entry at next H4 open + 1 pip spread + 0–0.5 pip slippage
**Lot size for P&L:** 0.1 lot (normalized to a $10k nominal account)

---

## 1. Executive Summary

| Metric | Value |
|--------|-------|
| Total trades (baseline) | 2854 |
| Win rate | 41.4% |
| Expectancy per trade | $+3.29 |
| Profit factor | 1.05 |
| Max drawdown | 48.1% of $10k |
| Total P&L (5y, 0.1 lot) | $+9378 |

### Does the bot have edge?

**MARGINAL** — Positive expectancy but below the $5/trade threshold for safe scaling.

---

## 2. Per-Pair Breakdown (Baseline variant)

## Per-Pair Breakdown — baseline

| Pair | Trades | WR% | E$/trade | PF | MaxDD% | Total P&L | Rating |
|------|--------|-----|----------|----|--------|-----------|--------|
| EURUSD | 317 | 38.2% | $+0.29 | 1.01 | 7.5% | $+91 | ⚠️ WATCH |
| GBPUSD | 329 | 39.8% | $-1.38 | 0.94 | 16.0% | $-455 | ❌ NO-GO |
| USDJPY | 304 | 43.4% | $+1.19 | 1.05 | 9.0% | $+360 | ⚠️ WATCH |
| XAUUSD | 273 | 50.9% | $+32.63 | 1.38 | 48.1% | $+8908 | ✅ GO |
| EURJPY | 317 | 42.0% | $-2.22 | 0.91 | 16.0% | $-703 | ❌ NO-GO |
| GBPJPY | 301 | 42.2% | $+3.90 | 1.16 | 7.6% | $+1174 | ⚠️ WATCH |
| AUDUSD | 367 | 39.0% | $-0.73 | 0.96 | 8.5% | $-268 | ❌ NO-GO |
| USDCAD | 324 | 39.5% | $-1.73 | 0.89 | 7.8% | $-562 | ❌ NO-GO |
| USDCHF | 322 | 39.8% | $+2.59 | 1.14 | 8.6% | $+833 | ⚠️ WATCH |

---

## 3. Ablation Results

## Ablation Results

| Variant | Trades | WR% | E$/trade | PF | MaxDD% | Total P&L |
|---------|--------|-----|----------|----|--------|-----------|
| **baseline** | 2854 | 41.4% | $+3.29 | 1.05 | 48.1% | $+9378 |
| **rrr_1_5** | 3082 | 45.4% | $+1.37 | 1.02 | 49.1% | $+4216 |
| **rrr_3** | 2542 | 35.1% | $+6.08 | 1.10 | 46.0% | $+15449 |
| **sl_3p** | 2952 | 42.0% | $+3.52 | 1.07 | 47.9% | $+10390 |
| **tue_wed_thu** | 1936 | 42.3% | $+2.85 | 1.04 | 36.7% | $+5513 |

*Trades, P&L and Sharpe at 0.1 lot size. MaxDD as % of $10k nominal account.*

### Winner variant

**rrr_3** — highest aggregate expectancy ($+6.08/trade).


## Per-Pair Breakdown — rrr_3

| Pair | Trades | WR% | E$/trade | PF | MaxDD% | Total P&L | Rating |
|------|--------|-----|----------|----|--------|-----------|--------|
| EURUSD | 284 | 32.4% | $+0.43 | 1.02 | 7.8% | $+122 | ⚠️ WATCH |
| GBPUSD | 294 | 34.7% | $+0.65 | 1.02 | 18.2% | $+191 | ⚠️ WATCH |
| USDJPY | 271 | 36.2% | $+2.52 | 1.10 | 6.6% | $+684 | ⚠️ WATCH |
| XAUUSD | 252 | 46.4% | $+50.45 | 1.53 | 46.0% | $+12712 | ✅ GO |
| EURJPY | 284 | 34.5% | $+1.15 | 1.04 | 11.8% | $+328 | ⚠️ WATCH |
| GBPJPY | 266 | 36.1% | $+4.76 | 1.16 | 9.6% | $+1267 | ⚠️ WATCH |
| AUDUSD | 313 | 31.0% | $-1.14 | 0.94 | 8.8% | $-357 | ❌ NO-GO |
| USDCAD | 286 | 32.5% | $-2.17 | 0.88 | 10.1% | $-621 | ❌ NO-GO |
| USDCHF | 292 | 34.2% | $+3.85 | 1.18 | 9.7% | $+1123 | ⚠️ WATCH |

---

## 4. Recommendations

- **Keep trading:** XAUUSD — expectancy positive and PF > 1.3
- **Monitor:** EURUSD, USDJPY, GBPJPY, USDCHF — marginal expectancy, needs more data
- **Consider disabling:** GBPUSD, EURJPY, AUDUSD, USDCAD — negative expectancy over 5 years
- **Best performing pair:** XAUUSD (E=$+32.63/trade, PF=1.38)

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
