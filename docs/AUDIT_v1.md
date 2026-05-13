# FTMO Agent — Production Readiness Audit v1.0

**Date:** 2026-05-13  
**Audited commit:** `2f05540` (tag `v1.0.0`)  
**Auditor:** Claude Sonnet (automated + manual review)  
**Purpose:** Pre-refactor baseline review. Documents bugs, dead code, config drift, endpoint status, and strategy health before the v2 cleanup cycle.

---

## Executive Summary

The system is **operationally functional** in its core loop:  
`monitor.py` (24/7, DigitalOcean) → `signal_engine` → `market_analyst` (4 pillars) → `auto_executor` (MetaApi cloud) → FTMO MT5.

Three categories of issues were identified:

| Severity | Count | Examples |
|---|---|---|
| Critical | 1 | `_is_bearish_engulfing` logic inverted (shorts degraded) |
| High | 3 | Inconsistent pip values, approximate risk aggregation, Wine stack still wired |
| Medium / Low | 8 | Orphan functions, config drift, duplicate constants, missing safeguards |

No issues threaten FTMO hard limits (daily loss, max drawdown) given current low lot sizes on demo.

---

## 1. Bugs

### 1.1 `_is_bearish_engulfing` — CRITICAL

**File:** `skills/signal_engine.py`, lines 177–184

**Problem:** The direction predicates for `prev` and `curr` candles are swapped. The function requires `curr` to be bullish and `prev` to be bearish — the opposite of a standard bearish engulfing pattern (which requires bullish prev, bearish curr).

```python
# Current (WRONG) — curr bullish, prev bearish
def _is_bearish_engulfing(prev: dict, curr: dict) -> bool:
    return (
        curr["c"] > curr["o"] and   # should be: curr bearish (c < o)
        prev["c"] < prev["o"] and   # should be: prev bullish (c > o)
        curr["o"] >= prev["c"] and
        curr["c"] <= prev["o"]
    )

# Correct fix:
def _is_bearish_engulfing(prev: dict, curr: dict) -> bool:
    return (
        prev["c"] > prev["o"] and   # prev bullish
        curr["c"] < curr["o"] and   # curr bearish
        curr["o"] >= prev["c"] and
        curr["c"] <= prev["o"]
    )
```

**Impact:** Bearish engulfing confirmation for `GO_SHORT` signals is never triggered. Short setups rely exclusively on bear pin bars. This makes the system less responsive on the short side and can miss valid bearish reversals.

**Fix effort:** 2 lines, 5 minutes.

---

### 1.2 Inconsistent pip values — HIGH

**Files:** `config.py` (line ~32) vs `skills/auto_executor.py` (line ~38)

| Pair | `config.PIP_VALUES` | `auto_executor._PIP_VALUES` | Correct (approx) |
|---|---|---|---|
| USDJPY | 9.09 | 6.5 | ~$6.5–$7 at 157 rate |
| EURJPY | ~8.3 | 8.0 | ~$6.4–$7 at 185 rate |
| GBPJPY | ~8.3 | 8.0 | ~$6.4–$7 at 213 rate |
| XAUUSD | 1.0 | 10.0 | $10/pip (standard) |

**Impact:** `auto_executor` lot sizing uses `_PIP_VALUES`. For JPY crosses at current rates (~157–213), the real pip value in USD is `$10 / (JPYUSD rate)` per lot, which is closer to $6.3–$6.4. The `auto_executor` value of 6.5 is reasonable. The `config.py` value of 9.09 for USDJPY is stale (assumes rate ~110). Having two tables risks silent divergence if one is updated without the other.

**Fix:** Consolidate into a single function in `config.py` that computes pip value dynamically from live USDJPY rate for JPY crosses. Static table acceptable as approximation if documented.

---

### 1.3 `_aggregate_open_risk` approximation — HIGH

**File:** `skills/auto_executor.py`, function `_aggregate_open_risk`

**Problem:** The function sums `0.5%` per open position as a proxy for risk. It does not use the actual SL distance or current P&L of each position.

```python
def _aggregate_open_risk(positions: list, balance: float) -> float:
    total_risk = 0.0
    for p in positions:
        total_risk += _RISK_FRACTION  # always 0.5%, regardless of SL distance
    return total_risk
```

**Impact:** The 2% aggregate risk cap is an approximation. If positions were entered with wider SLs (15+ pips), the actual dollar risk can exceed the cap. Conversely, if positions have moved in profit and SL was trailed, actual risk is lower than reported.

**Fix:** Compute true open risk per position as `abs(entry_price - sl_price) * lots * pip_value_per_lot`. MetaApi positions include `openPrice` and `stopLoss`; the denominator is derivable.

---

### 1.4 `USDCHF` duplicate in `_USD_PAIRS` — LOW (cosmetic)

**File:** `skills/auto_executor.py`, line ~36

```python
_USD_PAIRS = {"EURUSD", "GBPUSD", "AUDUSD", "USDCHF", "USDCAD", "USDJPY", "USDCHF"}
#                                                                              ^^^^^^ duplicate
```

Python sets deduplicate automatically, so no runtime effect. Remove the duplicate for clarity.

---

## 2. Dead Code and Obsolete Files

### 2.1 Wine / MT5 file-bridge stack — HIGH priority to migrate

These files are **obsolete after the MetaApi migration** but cannot be safely deleted yet because they are still imported by active modules.

| File | Status | Blocking imports |
|---|---|---|
| `mt5_connector.py` | Obsolete (Wine IPC) | `agent.py`, `agent_api.py`, `skills/review_trade.py` |
| `mt5/FTMO_Bridge.mq5` | Obsolete (MQL5 EA) | None (pure MQL5) |
| `deploy/mt5-wine.service` | Obsolete (systemd Wine) | None |
| `deploy/start_mt5.sh` | Obsolete (shell Wine startup) | None |

**Migration path for v2:**
1. Rewrite `skills/review_trade.py` → `get_positions` via MetaApi `_live_state()`
2. Rewrite `skills/trade_journal.py` → `get_closed_trades` via MetaApi history API
3. Rewrite `agent.py` tool calls that use `mt5_connector` → MetaApi equivalents
4. Rewrite `agent_api.py` `status` and `positions` commands → MetaApi
5. Then delete the 4 files above

---

### 2.2 Orphan functions (public API, never called externally)

| Function | File | Notes |
|---|---|---|
| `update_balance` | `skills/challenge_tracker.py` | Only `get_dashboard` is used externally |
| `refresh_all_currencies` | `skills/cot_fetcher.py` | Defined, never called |
| `get_session_stats` | `skills/statistical_engine.py` | Internal chain only |
| `get_session_score_for_trade` | `skills/statistical_engine.py` | Internal chain only |

**Action:** Mark private (`_prefix`) or remove in v2.

---

### 2.3 `economic-calendar.csv` — LOW

**File:** `economic-calendar.csv` (repo root)

No Python or shell references. The news filter uses the live Forex Factory JSON endpoint instead. Safe to delete in v2 if not needed for offline testing.

---

### 2.4 `scripts/create_droplet.py` — LOW

Standalone provisioning script with no imports from other modules. Useful for initial setup but not part of the trading loop. Consider moving to a `tools/` directory and documenting its purpose explicitly.

---

## 3. Configuration Drift

### 3.1 Variables in code but missing from `.env.example`

| Variable | Used in | Action |
|---|---|---|
| `DIGITALOCEAN_TOKEN` | `scripts/create_droplet.py` | Add to `.env.example` under Infrastructure section |

### 3.2 Variables in `.env.example` but unused in Python code

| Variable | Notes |
|---|---|
| `FTMO_ACADEMY_EMAIL` | Used by external Cursor skill, not this repo's Python |
| `FTMO_ACADEMY_PASSWORD` | Same — external Cursor skill only |
| `MT5_FILES_DIR` | Still referenced by `mt5_connector.py` (legacy); will be removable after Wine migration |

### 3.3 Droplet provisioning script missing MetaApi variables

**File:** `scripts/setup_droplet.sh`

The script writes the remote `.env` with `ANTHROPIC_API_KEY`, `TWELVEDATA_API_KEY`, and Microsoft Graph vars — but does **not** include `METAAPI_TOKEN` or `METAAPI_ACCOUNT_ID`. This means a fresh provisioning run would produce a Droplet where `auto_executor` fails silently.

**Fix:** Add `METAAPI_TOKEN` and `METAAPI_ACCOUNT_ID` to the env-writing section of `setup_droplet.sh`.

### 3.4 Duplicate PAIRS constant

`monitor.py` defines `PAIRS = [...]` and `skills/signal_engine.py` defines `_SYMBOL_MAP = {...}` — both maintain the same 9-pair universe. If a pair is added to one and not the other, the monitor will scan it but the signal engine won't have data mapping (or vice versa).

**Fix:** Define the pair list once in `config.py` and import from there.

### 3.5 Duplicate `CEST` timezone

`timezone(timedelta(hours=2))` is defined inline in 5 different files:
- `monitor.py`
- `agent_api.py`
- `skills/auto_executor.py`
- `skills/trade_journal.py`
- `skills/challenge_tracker.py`

**Fix:** Add `CEST = timezone(timedelta(hours=2))` to `config.py` and import.

---

## 4. External Endpoint Validation

Status as of audit date (2026-05-13):

| Endpoint | Purpose | Status | Notes |
|---|---|---|---|
| `api.twelvedata.com/time_series` | D1/H4/weekly OHLC | OK | ~200ms avg. Free plan: 800 req/day. 9 pairs × 3 timeframes = 54 req per scan cycle. |
| `api.twelvedata.com/price` | Live spot price | OK | Used in DXY check and slippage validation |
| `api.twelvedata.com/rsi` | H4 RSI(14) | OK | One call per pair per scan |
| `api.twelvedata.com/ema` | H4/D1/weekly EMA | OK | Multiple calls per scan |
| `nfs.faireconomy.media/ff_calendar_thisweek.json` | Economic calendar | OK | Forex Factory mirror. No auth required. |
| `cftc.gov/files/dea/history/fut_fin_xls_{year}.zip` | COT data | OK | Recently repaired. Was broken (OData 404). Now uses direct ZIP download + `xlrd`. Refreshed every 6h in-memory. |
| `login.microsoftonline.com/.../oauth2/v2.0/token` | MS Graph OAuth2 | OK | MSAL refresh token from local MCP cache. Token refreshed automatically. |
| `graph.microsoft.com/v1.0/users/{from}/sendMail` | Email alerts | OK | From `osmel@victoryswitzerland.com` to `vote@eroica.io` |
| MetaApi WebSocket (`london-a/london-b.agiliumtrade.ai`) | Trade execution + account state | OK | Connects in ~300ms. Sync in ~5s. |
| `api.anthropic.com` (SDK) | Claude analysis (trade journal, agent) | OK | `claude-sonnet-4-5` model |

**Risks:**
- TwelveData free plan quota (800 req/day) may be hit if scan frequency increases or pair universe expands. Each full scan uses ~50–60 API calls. At 15-min intervals: 24×4 = 96 scans/day × 6 calls/pair = ~576 req minimum for 1 pair. For 9 pairs: approaches limit.
- Microsoft Graph refresh token has no automatic rotation in code — if it expires (typically 90 days for work/school accounts), emails stop silently. Consider adding a fallback warning to the monitor log.
- MetaApi `$30/month` subscription — trial period: 17 days. Track expiry date to avoid service interruption.

---

## 5. Strategy Validation

### 5.1 Strategy specification

```
Trend:        D1 close vs EMA(50) → LONG bias if above, SHORT if below
Zone:         H4 close touches EMA(20) OR within 15 pips
Confirmation: Bull engulfing / Bull pin bar (LONG)
              Bear pin bar (SHORT) — engulfing broken, see Bug 1.1
Entry:        Last H4 close price
SL:           Long:  H4 low  - 5 pips
              Short: H4 high + 5 pips
TP:           2× SL distance from entry (RRR 1:2)
Gate:         4-pillar conviction ≥ MEDIUM (score ≥ 2)
              LOW conviction → downgrade to WATCH
News:         ±30 min hard block | 4h caution window
Sessions:     No Sunday | No Mon pre-03:00 CEST
```

### 5.2 4-Pillar conviction scoring

| Pillar | Max score | Scoring logic |
|---|---|---|
| Technical | ±2 | (1) H4 RSI vs direction thresholds ±1; (2) Weekly EMA20 alignment ±1 |
| Fundamental | ±2 | (1) Central bank stance differential ±1; (2) DXY alignment ±1 (skipped for cross pairs) |
| Sentiment | ±1 | CFTC COT Leveraged Funds net ratio: >5% OI bullish, <-5% bearish |
| Statistical | ±1 | Per-pair win rate if ≥3 trades: >55% → +1, <40% → -1; else portfolio WR |

**Conviction thresholds:** HIGH ≥ 4 | MEDIUM 2–3 | LOW ≤ 1

**Current calibration note:** With only 5 closed trades in history, the Statistical pillar contributes 0 for most pairs (insufficient per-pair data, portfolio WR 60% falls between 40–55% → score 0). Effective scoring range is ±5 from the first 3 pillars.

### 5.3 Trade history (v1 demo period)

| # | Pair | Direction | Result | Pips | P&L (est.) |
|---|---|---|---|---|---|
| 1 | EURJPY | LONG | WIN | +43.9 | +$350 |
| 2 | EURUSD | LONG | WIN | +47.5 | +$475 |
| 3 | EURUSD | LONG | LOSS | −36.0 | −$684 |
| 4 | USDCAD | SHORT | LOSS | −9.4 | −$362 |
| 5 | GBPJPY | LONG | WIN | +27.4 | +$219 |

**Totals:** 5 closed | 3W / 2L | **60% WR** | **+73 pips net** | **~−$2 net P&L** (small lots on early demo)

**Lessons captured in `data/trade_lessons.json`:**
1. EURUSD Sunday open: TwelveData returns stale Friday data; wide spread; avoid. → Monitor now skips Sunday.
2. USDCAD tight SL: slippage shrank effective SL to near-zero; loss despite market moving correctly. → `validate_entry()` added with 3-pip slippage tolerance.

### 5.4 Missing safeguards (v2 candidates)

| Gap | Risk | Recommended fix |
|---|---|---|
| No spread check before execution | Wide spread in low-liquidity sessions can make TP unreachable | Check `ask - bid > 2 pips` and reject or warn |
| No weekend position check | FTMO penalizes open positions over weekend gap risk | Add Friday 21:00 UTC auto-close or alert |
| `validate_entry()` not called in `auto_executor` | Auto-exec bypasses slippage check from signal engine | Wire `validate_entry` into `execute_trade` path |
| No trailing stop logic | Winners can reverse; TP at 2× only | Consider breakeven move after +1R |
| Lot size for JPY cross depends on static pip value | Pip USD value changes with rate | Consider dynamic calculation using live USDJPY |
| No MetaApi token expiry monitoring | Execution silently fails if token expires | Add expiry check in `_get_api()` |

---

## 6. Roadmap for v2

Priority-ordered list of changes for the next refactor session:

| Priority | Task | Effort | Impact |
|---|---|---|---|
| CRITICAL | Fix `_is_bearish_engulfing` direction predicates | 5 min | Restores short-side engulfing confirmation |
| HIGH | Unify `PIP_VALUES` into single `config.py` source | 30 min | Eliminates sizing inconsistency |
| HIGH | Wire `validate_entry()` into `auto_executor.execute_trade` | 20 min | Applies slippage guard to auto-exec |
| HIGH | Migrate `agent.py` + `review_trade.py` + `trade_journal.py` to MetaApi | 2–3 h | Enables deletion of Wine stack |
| HIGH | Delete Wine/MT5 stack after migration | 15 min | Removes ~500 lines of dead code |
| HIGH | Add `METAAPI_TOKEN/ACCOUNT_ID` to `setup_droplet.sh` | 5 min | Fresh provisioning works end-to-end |
| MEDIUM | Fix `_aggregate_open_risk` to use real SL distances | 30 min | Makes 2% risk cap accurate |
| MEDIUM | Add spread check to `auto_executor` | 20 min | Prevents entries in wide-spread conditions |
| MEDIUM | Add Friday 21:00 UTC position alert/close | 30 min | Protects against weekend gap risk |
| MEDIUM | Add `analyst_snapshot` field to `trade_log.json` entries | 20 min | Enables conviction-vs-outcome backtesting |
| LOW | Consolidate `CEST`, `PAIRS` into `config.py` | 20 min | DRY principle |
| LOW | Clean orphan functions (`update_balance`, `refresh_all_currencies`, etc.) | 15 min | Reduces surface area |
| LOW | Add `DIGITALOCEAN_TOKEN` to `.env.example`, remove academy vars | 5 min | Documentation accuracy |
| LOW | Remove `economic-calendar.csv` | 1 min | Unused artifact |
| LOW | Remove duplicate `USDCHF` from `_USD_PAIRS` | 1 min | Cosmetic |

---

## Appendix — Architecture Diagram (v1.0)

```
User / Cursor IDE
      │
      ├── agent.py (Claude orchestrator — uses mt5_connector [LEGACY])
      ├── agent_api.py (JSON commands — uses mt5_connector [LEGACY])
      └── cli.py (interactive shell)

DigitalOcean Droplet (46.101.206.29, $6/mo)
      │
      └── monitor.py [DAEMON, systemd]
            │
            ├── signal_engine.py
            │     ├── TwelveData API (D1/H4 OHLC, RSI, EMA)
            │     ├── market_analyst.py
            │     │     ├── Technical (RSI, weekly EMA)
            │     │     ├── Fundamental (central_banks.json, DXY)
            │     │     ├── Sentiment (cot_fetcher → CFTC XLS)
            │     │     └── Statistical (statistical_engine → trade_log.json)
            │     └── news_filter.py (ForexFactory calendar)
            │
            ├── auto_executor.py [MetaApi SDK]
            │     └── MetaApi WebSocket → FTMO MT5 Demo
            │
            ├── challenge_tracker.py (dashboard for email)
            └── Microsoft Graph API (email alerts → vote@eroica.io)
```

---

*This document is the authoritative audit record for v1.0. The v2 refactor session should start by implementing items in priority order above.*
