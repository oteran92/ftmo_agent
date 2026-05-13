"""
Market Analyst — FTMO Academy 4-Pillar Analysis Engine.

Implements all four analysis types described in the FTMO Academy lesson:
  "Technical, Fundamental, Sentiment and Statistical Analysis — which one is best?"
  https://academy.ftmo.com/lesson/technical-fundamental-sentiment-and-statistical-analysis-which-one-is-best/

Answer: ALL FOUR, used together. This module integrates them into a conviction
score that is attached to every GO signal from the signal engine.

─────────────────────────────────────────────────────────────────────────────
SCORING SYSTEM (each sub-check contributes -1, 0, or +1):

  TECHNICAL  (2 checks, max ±2)
    1. RSI(14) on H4 — not overbought/oversold relative to signal direction
    2. Weekly EMA20 direction — aligned with D1 bias

  FUNDAMENTAL  (2 checks, max ±2)
    3. Net central bank bias — base vs quote currency CB stance
    4. DXY trend (USD Index) — relevant for all USD pairs and XAU

  SENTIMENT  (1 check, max ±1)
    5. CFTC COT — large speculator net position for base currency

  STATISTICAL  (1 check, max ±1)
    6. Historical win rate for this pair from our trade log

  TOTAL: -6 to +6
    HIGH   ≥  4  — strong multi-pillar alignment
    MEDIUM 2–3  — partial alignment, proceed with caution
    LOW    ≤  1  — insufficient alignment (GO downgraded to WATCH)

─────────────────────────────────────────────────────────────────────────────
Data sources (all free-tier, no additional API keys required):
  - TwelveData (RSI + Weekly EMA) — uses the same key as signal_engine.py
  - data/central_banks.json       — manually maintained, updated after CB meetings
  - CFTC Public Reporting API     — weekly COT data (no auth required)
  - data/trade_log.json           — our own historical trades
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

BASE_DIR = Path(__file__).parent.parent

# TwelveData configuration (shared with signal_engine, no duplication of key)
_TD_BASE    = "https://api.twelvedata.com"
_TD_TIMEOUT = 10

# Symbol map for TwelveData (same as signal_engine)
_SYMBOL_MAP = {
    "EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY",
    "XAUUSD": "XAU/USD", "EURJPY": "EUR/JPY", "GBPJPY": "GBP/JPY",
    "AUDUSD": "AUD/USD", "USDCAD": "USD/CAD", "USDCHF": "USD/CHF",
    "DXY":    "DXY",
}

# Delay between API calls to stay within TwelveData free plan (8 req/min)
_API_DELAY = 8  # seconds

# RSI thresholds for the pullback strategy:
# For LONG entries: RSI below 55 is ideal (not overbought at pullback zone)
# For SHORT entries: RSI above 45 is ideal (not oversold at pullback zone)
_RSI_LONG_OK  = 55   # RSI must be BELOW this for LONG alignment
_RSI_SHORT_OK = 45   # RSI must be ABOVE this for SHORT alignment
_RSI_LONG_BAD  = 68  # RSI above this penalizes LONG (overbought)
_RSI_SHORT_BAD = 32  # RSI below this penalizes SHORT (oversold)

# Currency composition per pair: (base, quote)
_PAIR_CURRENCIES: dict[str, tuple[str, str]] = {
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"), "USDJPY": ("USD", "JPY"),
    "XAUUSD": ("XAU", "USD"), "EURJPY": ("EUR", "JPY"), "GBPJPY": ("GBP", "JPY"),
    "AUDUSD": ("AUD", "USD"), "USDCAD": ("USD", "CAD"), "USDCHF": ("USD", "CHF"),
}

# USD pairs where DXY is directly relevant
_USD_BASE_PAIRS  = {"USDJPY", "USDCAD", "USDCHF"}   # USD is base (DXY up → bullish for pair)
_USD_QUOTE_PAIRS = {"EURUSD", "GBPUSD", "AUDUSD", "XAUUSD"}  # USD is quote (DXY up → bearish for pair)

# In-memory request cache to save API credits during a scan cycle
_td_cache: dict[str, tuple[float, Any]] = {}
_TD_CACHE_TTL = 240  # 4 minutes


def _api_key() -> str:
    key = os.environ.get("TWELVEDATA_API_KEY", "")
    if not key:
        raise EnvironmentError("TWELVEDATA_API_KEY not set.")
    return key


def _td_get(endpoint: str, params: dict) -> dict | None:
    """GET request to TwelveData with in-memory TTL cache."""
    cache_key = endpoint + json.dumps(params, sort_keys=True)
    if cache_key in _td_cache:
        ts, data = _td_cache[cache_key]
        if time.time() - ts < _TD_CACHE_TTL:
            return data
    try:
        resp = requests.get(f"{_TD_BASE}/{endpoint}", params=params, timeout=_TD_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            return None
        _td_cache[cache_key] = (time.time(), data)
        return data
    except Exception:
        return None


# ── Pillar 1: Technical Analysis ───────────────────────────────────────────────

def _technical_context(pair: str, signal: str) -> dict[str, Any]:
    """
    Technical pillar — two checks:
      1. RSI(14) on H4: good pullback entry → not overbought for LONG, not oversold for SHORT
      2. Weekly EMA20: price position relative to weekly trend

    Returns score (-2 to +2) and details.
    """
    td_sym = _SYMBOL_MAP.get(pair)
    if not td_sym:
        return {"score": 0, "note": f"No TwelveData mapping for {pair}.", "rsi": None, "weekly_ema": None}

    score    = 0
    rsi_val  = None
    w_ema    = None
    w_price  = None
    details  = []

    # Check 1: RSI(14) on H4
    try:
        rsi_data = _td_get("rsi", {
            "symbol":      td_sym,
            "interval":    "4h",
            "time_period": 14,
            "outputsize":  1,
            "apikey":      _api_key(),
        })
        time.sleep(_API_DELAY)

        if rsi_data and "values" in rsi_data:
            rsi_val = float(rsi_data["values"][0]["rsi"])

            if signal == "GO_LONG":
                if rsi_val < _RSI_LONG_OK:
                    score += 1
                    details.append(f"RSI {rsi_val:.1f} < {_RSI_LONG_OK} — pullback not overbought ✓")
                elif rsi_val > _RSI_LONG_BAD:
                    score -= 1
                    details.append(f"RSI {rsi_val:.1f} > {_RSI_LONG_BAD} — overbought, risky LONG entry ✗")
                else:
                    details.append(f"RSI {rsi_val:.1f} — neutral zone")
            elif signal == "GO_SHORT":
                if rsi_val > _RSI_SHORT_OK:
                    score += 1
                    details.append(f"RSI {rsi_val:.1f} > {_RSI_SHORT_OK} — pullback not oversold ✓")
                elif rsi_val < _RSI_SHORT_BAD:
                    score -= 1
                    details.append(f"RSI {rsi_val:.1f} < {_RSI_SHORT_BAD} — oversold, risky SHORT entry ✗")
                else:
                    details.append(f"RSI {rsi_val:.1f} — neutral zone")
    except Exception as e:
        details.append(f"RSI fetch failed: {e}")

    # Check 2: Weekly EMA20 direction — is the weekly trend aligned?
    try:
        ema_data = _td_get("ema", {
            "symbol":      td_sym,
            "interval":    "1week",
            "time_period": 20,
            "outputsize":  2,
            "apikey":      _api_key(),
        })
        time.sleep(_API_DELAY)

        if ema_data and "values" in ema_data:
            vals    = ema_data["values"]
            w_ema   = float(vals[0]["ema"])           # most recent weekly EMA20
            w_ema_p = float(vals[1]["ema"]) if len(vals) > 1 else w_ema
            w_rising = w_ema > w_ema_p

            # Get the current weekly close to compare vs EMA
            w_candle = _td_get("time_series", {
                "symbol":     td_sym,
                "interval":   "1week",
                "outputsize": 1,
                "apikey":     _api_key(),
            })
            time.sleep(_API_DELAY)

            if w_candle and "values" in w_candle:
                w_price = float(w_candle["values"][0]["close"])
                above_w_ema = w_price > w_ema

                if signal == "GO_LONG" and above_w_ema and w_rising:
                    score += 1
                    details.append(f"Weekly: price {w_price:.5f} above EMA20 {w_ema:.5f} (rising) ✓")
                elif signal == "GO_SHORT" and not above_w_ema and not w_rising:
                    score += 1
                    details.append(f"Weekly: price {w_price:.5f} below EMA20 {w_ema:.5f} (falling) ✓")
                elif signal == "GO_LONG" and not above_w_ema:
                    score -= 1
                    details.append(f"Weekly: price {w_price:.5f} BELOW EMA20 {w_ema:.5f} — counter-trend LONG ✗")
                elif signal == "GO_SHORT" and above_w_ema:
                    score -= 1
                    details.append(f"Weekly: price {w_price:.5f} ABOVE EMA20 {w_ema:.5f} — counter-trend SHORT ✗")
                else:
                    details.append(f"Weekly EMA20: {w_ema:.5f} — mixed signal")
    except Exception as e:
        details.append(f"Weekly EMA fetch failed: {e}")

    return {
        "score":      score,
        "rsi_h4":     round(rsi_val, 1) if rsi_val else None,
        "weekly_ema": round(w_ema, 5) if w_ema else None,
        "weekly_price": round(w_price, 5) if w_price else None,
        "note":       " | ".join(details) if details else "No technical data.",
    }


# ── Pillar 2: Fundamental Analysis ─────────────────────────────────────────────

def _load_central_banks() -> dict:
    """Load the CB stance file. Returns empty dict on failure."""
    path = BASE_DIR / "data" / "central_banks.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _fundamental_context(pair: str, signal: str) -> dict[str, Any]:
    """
    Fundamental pillar — two checks:
      1. Net central bank bias: compare base vs quote currency CB stance
      2. DXY trend: USD Index direction for USD-involved pairs

    Returns score (-2 to +2) and details.
    """
    score   = 0
    details = []
    cb_data = _load_central_banks()

    currencies = _PAIR_CURRENCIES.get(pair)
    if not currencies:
        return {"score": 0, "note": f"Unknown pair composition for {pair}.", "cb_note": "N/A", "dxy_note": "N/A"}

    base_cur, quote_cur = currencies

    # Check 1: Central bank net bias
    base_cb  = cb_data.get(base_cur, {})
    quote_cb = cb_data.get(quote_cur, {})
    base_score  = base_cb.get("hawk_score", 0)
    quote_score = quote_cb.get("hawk_score", 0)

    # Net CB bias from the perspective of the base currency
    cb_net = base_score - quote_score

    # For XAU (gold), CB logic is inverted: hawkish CB → stronger USD → lower gold
    if base_cur == "XAU":
        # Higher USD hawkishness → bearish for gold
        cb_net = -quote_score  # inverse the USD score for gold

    if signal == "GO_LONG":
        if cb_net > 0:
            score += 1
            details.append(
                f"CB: {base_cur}({base_cb.get('stance','?')}) vs {quote_cur}({quote_cb.get('stance','?')}) "
                f"→ net +{cb_net} — base currency favored ✓"
            )
        elif cb_net < 0:
            score -= 1
            details.append(
                f"CB: {base_cur}({base_cb.get('stance','?')}) vs {quote_cur}({quote_cb.get('stance','?')}) "
                f"→ net {cb_net} — quote currency favored ✗"
            )
        else:
            details.append(
                f"CB: {base_cur}({base_cb.get('stance','?')}) vs {quote_cur}({quote_cb.get('stance','?')}) "
                f"→ neutral"
            )
    elif signal == "GO_SHORT":
        if cb_net < 0:
            score += 1
            details.append(
                f"CB: {base_cur}({base_cb.get('stance','?')}) vs {quote_cur}({quote_cb.get('stance','?')}) "
                f"→ net {cb_net} — base currency weak ✓"
            )
        elif cb_net > 0:
            score -= 1
            details.append(
                f"CB: {base_cur}({base_cb.get('stance','?')}) vs {quote_cur}({quote_cb.get('stance','?')}) "
                f"→ net +{cb_net} — quote currency weak ✗"
            )
        else:
            details.append(
                f"CB: {base_cur}({base_cb.get('stance','?')}) vs {quote_cur}({quote_cb.get('stance','?')}) "
                f"→ neutral"
            )

    cb_note = details[-1] if details else "N/A"

    # Check 2: DXY trend — USD Index context
    dxy_note = "DXY: not applicable (cross pair)"
    dxy_val  = None
    dxy_ema  = None

    usd_involved = pair in _USD_BASE_PAIRS or pair in _USD_QUOTE_PAIRS
    if usd_involved:
        try:
            td_sym = _SYMBOL_MAP.get("DXY")
            dxy_data = _td_get("ema", {
                "symbol":      td_sym,
                "interval":    "1day",
                "time_period": 20,
                "outputsize":  1,
                "apikey":      _api_key(),
            })
            time.sleep(_API_DELAY)

            dxy_price_data = _td_get("price", {"symbol": td_sym, "apikey": _api_key()})
            time.sleep(_API_DELAY)

            if dxy_data and dxy_price_data:
                dxy_ema  = float(dxy_data["values"][0]["ema"])
                dxy_val  = float(dxy_price_data.get("price", 0))
                dxy_bull = dxy_val > dxy_ema  # True = USD strengthening

                if signal == "GO_LONG" and pair in _USD_BASE_PAIRS and dxy_bull:
                    score += 1
                    dxy_note = f"DXY {dxy_val:.2f} > EMA20 {dxy_ema:.2f} — USD strong, LONG {pair} aligned ✓"
                elif signal == "GO_SHORT" and pair in _USD_BASE_PAIRS and not dxy_bull:
                    score += 1
                    dxy_note = f"DXY {dxy_val:.2f} < EMA20 {dxy_ema:.2f} — USD weak, SHORT {pair} aligned ✓"
                elif signal == "GO_LONG" and pair in _USD_QUOTE_PAIRS and not dxy_bull:
                    score += 1
                    dxy_note = f"DXY {dxy_val:.2f} < EMA20 {dxy_ema:.2f} — USD weak, LONG {pair} aligned ✓"
                elif signal == "GO_SHORT" and pair in _USD_QUOTE_PAIRS and dxy_bull:
                    score += 1
                    dxy_note = f"DXY {dxy_val:.2f} > EMA20 {dxy_ema:.2f} — USD strong, SHORT {pair} aligned ✓"
                elif signal == "GO_LONG" and pair in _USD_BASE_PAIRS and not dxy_bull:
                    score -= 1
                    dxy_note = f"DXY {dxy_val:.2f} < EMA20 {dxy_ema:.2f} — USD weak, LONG {pair} diverging ✗"
                elif signal == "GO_SHORT" and pair in _USD_BASE_PAIRS and dxy_bull:
                    score -= 1
                    dxy_note = f"DXY {dxy_val:.2f} > EMA20 {dxy_ema:.2f} — USD strong, SHORT {pair} diverging ✗"
                elif signal == "GO_LONG" and pair in _USD_QUOTE_PAIRS and dxy_bull:
                    score -= 1
                    dxy_note = f"DXY {dxy_val:.2f} > EMA20 {dxy_ema:.2f} — USD strong, LONG {pair} diverging ✗"
                elif signal == "GO_SHORT" and pair in _USD_QUOTE_PAIRS and not dxy_bull:
                    score -= 1
                    dxy_note = f"DXY {dxy_val:.2f} < EMA20 {dxy_ema:.2f} — USD weak, SHORT {pair} diverging ✗"
                else:
                    dxy_note = f"DXY {dxy_val:.2f} vs EMA20 {dxy_ema:.2f} — neutral"
        except Exception as e:
            dxy_note = f"DXY fetch failed: {e}"

    details.append(dxy_note)

    return {
        "score":    score,
        "cb_note":  cb_note,
        "dxy_note": dxy_note,
        "dxy":      round(dxy_val, 2) if dxy_val else None,
        "dxy_ema":  round(dxy_ema, 2) if dxy_ema else None,
        "note":     " | ".join(details),
    }


# ── Pillar 3: Sentiment Analysis ───────────────────────────────────────────────

def _sentiment_context(pair: str, signal: str) -> dict[str, Any]:
    """
    Sentiment pillar — one check:
      COT large speculator (non-commercial) net position for the base currency.
      Aligned with signal direction → +1. Opposing → -1.

    Also notes the contrarian retail indicator (non-reportable positions).
    """
    from skills.cot_fetcher import fetch_cot_data

    currencies = _PAIR_CURRENCIES.get(pair)
    if not currencies:
        return {"score": 0, "note": "Unknown pair."}

    base_cur = currencies[0]

    # Gold has no currency futures — use USD as proxy (inverse)
    cot_currency = "USD" if base_cur == "XAU" else base_cur

    cot = fetch_cot_data(cot_currency)
    score = 0

    if cot["source"] == "unavailable":
        return {"score": 0, "cot": cot, "note": f"COT data unavailable for {cot_currency}."}

    direction = cot["direction"]  # "bullish", "bearish", "neutral"

    if base_cur == "XAU":
        # For XAUUSD: USD bullish COT → bearish for gold price
        direction = {"bullish": "bearish", "bearish": "bullish", "neutral": "neutral"}[direction]

    if signal == "GO_LONG":
        if direction == "bullish":
            score = 1
        elif direction == "bearish":
            score = -1
    elif signal == "GO_SHORT":
        if direction == "bearish":
            score = 1
        elif direction == "bullish":
            score = -1

    arrow = "✓" if score == 1 else ("✗" if score == -1 else "—")
    note  = f"COT ({cot_currency}): {cot['note']} {arrow}"

    return {
        "score":     score,
        "cot":       cot,
        "direction": direction,
        "note":      note,
    }


# ── Pillar 4: Statistical Analysis ─────────────────────────────────────────────

def _statistical_context(pair: str) -> dict[str, Any]:
    """
    Statistical pillar — one check:
      Historical win rate for this specific pair.
      Falls back to overall portfolio stats if pair history is insufficient.
    """
    from skills.statistical_engine import get_pair_stats, get_overall_stats

    pair_stats = get_pair_stats(pair)

    # Fall back to portfolio stats if not enough pair-specific data
    if pair_stats["total_trades"] < 3:
        overall = get_overall_stats()
        return {
            "score": overall["score"],
            "win_rate": overall.get("win_rate"),
            "total_trades": pair_stats["total_trades"],
            "note": (
                f"{pair}: only {pair_stats['total_trades']} trade(s) — "
                f"using portfolio stats: {overall['note']}"
            ),
        }

    return {
        "score":        pair_stats["score"],
        "win_rate":     pair_stats["win_rate"],
        "total_trades": pair_stats["total_trades"],
        "note":         pair_stats["note"],
    }


# ── Conviction Scorer ──────────────────────────────────────────────────────────

def _score_to_conviction(total: int) -> str:
    """Map total score (-6 to +6) to conviction label."""
    if total >= 4:
        return "HIGH"
    if total >= 2:
        return "MEDIUM"
    return "LOW"


# ── Main public API ────────────────────────────────────────────────────────────

def run_full_analysis(pair: str, signal: str) -> dict[str, Any]:
    """
    Run the full 4-pillar analysis for a given pair and signal direction.

    Args:
        pair:   e.g. "EURUSD"
        signal: "GO_LONG" or "GO_SHORT"

    Returns a dict with:
        conviction:   "HIGH" | "MEDIUM" | "LOW"
        score:        int (-6 to +6)
        technical:    pillar 1 result
        fundamental:  pillar 2 result
        sentiment:    pillar 3 result
        statistical:  pillar 4 result
        summary:      one-line human-readable verdict
        snapshot:     flat dict for logging into trade_log.json analyst_snapshot field
    """
    pair = pair.upper().replace("/", "")

    # Run all 4 pillars — each is independent and degrades gracefully on error
    tech  = _safe_pillar(_technical_context,  pair, signal)
    fund  = _safe_pillar(_fundamental_context, pair, signal)
    sent  = _safe_pillar(_sentiment_context,  pair, signal)
    stat  = _safe_pillar(_statistical_context, pair)

    total = tech["score"] + fund["score"] + sent["score"] + stat["score"]
    conviction = _score_to_conviction(total)

    # Build one-line summary
    scores_str = (
        f"T:{tech['score']:+d} F:{fund['score']:+d} "
        f"S:{sent['score']:+d} St:{stat['score']:+d} = {total:+d}"
    )
    summary = f"[{conviction}] {pair} {signal} — {scores_str}"

    # Flat snapshot for attaching to trade log entry
    snapshot = {
        "conviction":        conviction,
        "total_score":       total,
        "rsi_h4":            tech.get("rsi_h4"),
        "weekly_ema":        tech.get("weekly_ema"),
        "cb_note":           fund.get("cb_note"),
        "dxy":               fund.get("dxy"),
        "cot_direction":     sent.get("direction"),
        "cot_net_ratio":     sent.get("cot", {}).get("net_ratio"),
        "win_rate":          stat.get("win_rate"),
        "total_trades_hist": stat.get("total_trades"),
    }

    return {
        "conviction":  conviction,
        "score":       total,
        "technical":   tech,
        "fundamental": fund,
        "sentiment":   sent,
        "statistical": stat,
        "summary":     summary,
        "snapshot":    snapshot,
    }


def _safe_pillar(fn, *args) -> dict:
    """Call a pillar function, returning a neutral result on any error."""
    try:
        return fn(*args)
    except Exception as e:
        return {"score": 0, "note": f"Pillar error: {e}"}
