"""
FTMO $100K Challenge — Central Configuration
All thresholds, limits, and constants live here.
"""

from __future__ import annotations

ACCOUNT_BALANCE_INITIAL = 100_000.0

# ── Phase definitions ──────────────────────────────────────────────────────────
PHASES = {
    "trial":     {"profit_target_pct": 0.0,  "max_daily_loss_pct": 0.05, "max_total_loss_pct": 0.05},
    "challenge": {"profit_target_pct": 0.10, "max_daily_loss_pct": 0.05, "max_total_loss_pct": 0.10},
    "funded":    {"profit_target_pct": 0.05, "max_daily_loss_pct": 0.05, "max_total_loss_pct": 0.10},
}

# ── Risk parameters ────────────────────────────────────────────────────────────
DAILY_BUDGET_PCT        = 0.010   # 1.0% of account balance
RISK_PER_TRADE_PCT      = 0.005   # 0.5% per trade
MIN_RRR                 = 2.0     # Minimum reward:risk ratio
HARD_STOP_LOSSES        = 2       # Consecutive losses before forced stop
NEWS_BUFFER_MIN         = 30      # Minutes before/after high-impact news
CRISIS_THRESHOLD_PCT    = 0.04    # 4% drawdown → Crisis Mode
SCALE_UP_EQUITY_PCT     = 0.05    # Equity must grow 5% before lot scaling
SCALE_UP_INCREMENT      = 0.25    # +25% on lot size per scale step
DE_SCALE_LOSS_PCT       = 0.0075  # Single-day loss > 0.75% → revert lot size
PAYDAY_TRIGGER_PCT      = 0.05    # 5% profit → trigger payout reminder
SOFT_DAILY_STOP_PCT     = 0.008   # $800 floating loss → close all, day over

# ── Pip values per standard lot (USD account) ──────────────────────────────────
PIP_VALUES: dict[str, float] = {
    "EUR/USD":  10.00,
    "GBP/USD":  10.00,
    "AUD/USD":  10.00,
    "NZD/USD":  10.00,
    "USD/JPY":   9.09,
    "USD/CHF":  10.00,
    "USD/CAD":   7.70,
    "GBP/JPY":   9.09,
    "EUR/JPY":   9.09,
    "EUR/GBP":  12.50,
    "XAU/USD":  10.00,  # Gold: 1 pip = $0.01, standard lot = 100oz → ~$10/pip
    "BTC/USD":   1.00,  # Crypto: depends on broker; override manually
    "ETH/USD":   1.00,
}

# ── Scheduling (24-hour UTC or local, configure per broker) ───────────────────
TRADING_SESSION_START = "08:00"   # When daily budget resets
TRADING_SESSION_END   = "22:00"   # When EOD report fires
NEWS_CHECK_INTERVAL_MIN = 15      # How often to poll the news calendar

# ── Data paths ─────────────────────────────────────────────────────────────────
import os
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "account_state.json")
LOG_FILE   = os.path.join(DATA_DIR, "trade_log.json")
ALERT_FILE = os.path.join(DATA_DIR, "alerts.json")

# ── Trading session windows (UTC hours) ───────────────────────────────────────
# Academy: London + NY sessions have the highest volume and best setups.
# Asian session has the LOWEST volume — patterns fail more often.
TRADING_SESSIONS_UTC: list[tuple[int, int]] = [
    (8, 16),   # London session (08:00–16:00 UTC)
    (12, 21),  # New York session (12:00–21:00 UTC)
]

# ── Preferred trading days (0=Monday … 6=Sunday) ──────────────────────────────
# Academy: Tuesday, Wednesday, Thursday have highest volume and volatility.
# Monday morning is slow; Friday afternoon dries up after London close.
BEST_TRADING_DAYS: set[int] = {1, 2, 3}         # Tue=1, Wed=2, Thu=3
CAUTION_DAYS: dict[str, tuple[int, int]] = {
    "monday_morning":   (0, 12),  # weekday=0, hours before 12:00 UTC
    "friday_afternoon": (4, 16),  # weekday=4, hours at or after 16:00 UTC
}

# ── Correlation map (pairs sharing strong directional USD exposure) ────────────
# Academy: EURUSD + GBPUSD open simultaneously = double USD short exposure.
# Gold (XAUUSD) has negative correlation with USD.
CORRELATED_PAIRS: dict[str, list[str]] = {
    "EURUSD": ["GBPUSD", "AUDUSD", "NZDUSD"],
    "GBPUSD": ["EURUSD", "AUDUSD", "NZDUSD"],
    "AUDUSD": ["EURUSD", "GBPUSD", "NZDUSD"],
    "NZDUSD": ["EURUSD", "GBPUSD", "AUDUSD"],
    "XAUUSD": ["EURUSD", "GBPUSD"],  # Gold negatively correlated with USD strength
    "USDCAD": [],                     # Oil-correlated but no direct pair conflict
    "USDCHF": [],
    "USDJPY": [],
}

# ── Claude model ───────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-6"
