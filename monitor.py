"""
FTMO Monitor — autonomous background daemon.
Runs at every H4 candle close (6x/day), scans all pairs via TwelveData,
sends email alerts via Microsoft Graph API when actionable signals are detected,
and journals any new closed trades for continuous learning.

Designed to run 24/7 on a DigitalOcean Droplet (or locally with nohup).
No MT5 required for monitoring — only for trade execution.

Required environment variables:
  TWELVEDATA_API_KEY    — TwelveData free plan key
  ANTHROPIC_API_KEY     — Claude API key (for trade journal analysis)
  MS_CLIENT_ID          — Azure app client ID (public client, no secret needed)
  MS_TENANT_ID          — Microsoft 365 tenant ID
  MS_REFRESH_TOKEN      — OAuth2 refresh token for the sender account (auto-rotates on use)
  ALERT_EMAIL_FROM      — sender email address
  ALERT_EMAIL_TO        — recipient email
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
ALERTS_FILE = DATA_DIR / "alerts.json"

# User timezone: CEST (UTC+2). MT5 server runs UTC+3.
CEST = timezone(timedelta(hours=2))

# H4 candle close hours in CEST (UTC+2): MT5 server UTC+3 minus 1h.
# Closes at 00/04/08/12/16/20 UTC+3 → 23/03/07/11/15/19 CEST.
H4_CLOSE_HOURS_CEST = {23, 3, 7, 11, 15, 19}
H4_CHECK_MINUTE     = 3    # fire 3 minutes after close (TwelveData needs a moment to update)
POLL_INTERVAL       = 30   # wake every 30s to check if it's time to run
PAIRS               = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD",
                       "EURJPY", "GBPJPY", "AUDUSD", "USDCAD", "USDCHF"]


# ── Time helpers ───────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(CEST)


def _now_str() -> str:
    return _now().strftime("%Y-%m-%d %H:%M CEST")


def _is_h4_check_time(now: datetime) -> bool:
    return now.hour in H4_CLOSE_HOURS_CEST and now.minute == H4_CHECK_MINUTE


def _market_is_open(now: datetime) -> bool:
    """
    Forex market is closed Saturday and most of Sunday.
    Opens Sunday ~23:00 CEST (21:00 UTC, New York open).
    Closes Friday ~22:00 CEST.

    The Sunday 23:03 scan is intentionally skipped because TwelveData
    returns stale Friday close data. The first reliable scan is Monday 03:03.
    """
    weekday = now.weekday()  # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    if weekday == 5:          # Saturday — always closed
        return False
    if weekday == 6:          # Sunday — skip all scans (stale data risk)
        return False
    if weekday == 0 and now.hour < 3:  # Monday before 03:00 — still unreliable
        return False
    if weekday == 4 and now.hour >= 22:  # Friday after 22:00 CEST
        return False
    return True


# ── Microsoft Graph email ──────────────────────────────────────────────────────

# File where the rotating refresh token is persisted between restarts.
# The env var MS_REFRESH_TOKEN is used on first boot; subsequent boots read this file.
_RT_FILE = DATA_DIR / ".ms_refresh_token"


def _load_refresh_token() -> str:
    """Return the current refresh token, preferring the persisted file over env."""
    if _RT_FILE.exists():
        return _RT_FILE.read_text().strip()
    return os.environ.get("MS_REFRESH_TOKEN", "")


def _save_refresh_token(token: str) -> None:
    """Persist the new refresh token so the next restart can use it."""
    DATA_DIR.mkdir(exist_ok=True)
    _RT_FILE.write_text(token)


def _get_ms_token() -> str | None:
    """
    Obtain an OAuth2 access token via the refresh-token grant (public client flow).
    Microsoft public clients do NOT require a client_secret.
    Automatically rotates the stored refresh token on each successful call.
    """
    client_id = os.environ.get("MS_CLIENT_ID", "")
    tenant_id = os.environ.get("MS_TENANT_ID", "")
    refresh_token = _load_refresh_token()

    if not all([client_id, tenant_id, refresh_token]):
        return None  # email not configured — skip silently

    try:
        resp = requests.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type":    "refresh_token",
                "client_id":     client_id,
                "refresh_token": refresh_token,
                "scope":         "https://graph.microsoft.com/Mail.Send offline_access",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # Rotate: save the new refresh token Microsoft returns
        new_rt = data.get("refresh_token", "")
        if new_rt:
            _save_refresh_token(new_rt)
        return data.get("access_token")
    except Exception as e:
        print(f"[Monitor] MS token error: {e}", flush=True)
        return None


def send_email_alert(subject: str, body: str) -> bool:
    """Send an email alert via Microsoft Graph API. Returns True on success."""
    from_email = os.environ.get("ALERT_EMAIL_FROM", "")
    to_email   = os.environ.get("ALERT_EMAIL_TO", "")

    if not from_email or not to_email:
        print("[Monitor] Email not configured (ALERT_EMAIL_FROM/TO missing)", flush=True)
        return False

    token = _get_ms_token()
    if not token:
        print("[Monitor] Could not obtain MS token — email skipped", flush=True)
        return False

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": body,
            },
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": True,
    }

    try:
        resp = requests.post(
            f"https://graph.microsoft.com/v1.0/users/{from_email}/sendMail",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        print(f"[Monitor] Email sent: {subject}", flush=True)
        return True
    except Exception as e:
        print(f"[Monitor] Email send error: {e}", flush=True)
        return False


# ── Alert persistence ──────────────────────────────────────────────────────────

def _load_alerts() -> list:
    if not ALERTS_FILE.exists():
        return []
    try:
        return json.loads(ALERTS_FILE.read_text())
    except Exception:
        return []


def _save_alerts(alerts: list) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ALERTS_FILE.write_text(json.dumps(alerts[-50:], indent=2))


# ── Core check ─────────────────────────────────────────────────────────────────

def run_check() -> dict:
    """Run a full market check: scan pairs, journal trades, build alert."""
    from skills.signal_engine import scan_all_pairs
    from skills.trade_journal import check_and_journal_new_trades

    alert: dict = {
        "timestamp":      _now_str(),
        "signals":        [],
        "new_lessons":    [],
        "action_required": False,
        "message":        "",
    }

    # 1 — Scan all pairs (uses TwelveData API)
    actionable = []
    try:
        results = scan_all_pairs()
        for r in results:
            sig   = r.get("signal", "UNKNOWN")
            entry = {"pair": r.get("symbol"), "signal": sig, "bias": r.get("bias", "")}

            if sig in ("GO_LONG", "GO_SHORT") and "trade" in r:
                entry["trade"]     = r["trade"]
                entry["next_step"] = r.get("next_step", "")
                actionable.append(entry)
                alert["action_required"] = True
            elif sig == "WATCH":
                entry["note"] = r.get("next_step", "In zone, awaiting confirmation")
            elif sig == "NEWS_CAUTION":
                entry["note"] = r.get("next_step", "")
                entry["news"] = r.get("news", {}).get("message", "")
            elif sig == "NEWS_BLOCK":
                entry["note"] = r.get("next_step", "")

            alert["signals"].append(entry)
    except Exception as e:
        alert["signals"].append({"error": str(e)})

    # 2 — Journal any new closed trades (learns from MT5 history)
    try:
        new_lessons = check_and_journal_new_trades()
        alert["new_lessons"] = [
            {"pair": l.get("pair"), "outcome": l.get("outcome"), "lesson": l.get("lesson", "")}
            for l in new_lessons if "lesson" in l
        ]
    except Exception:
        pass  # trade journal is non-critical

    # 3 — Build summary message
    if actionable:
        pairs_str = ", ".join(e["pair"] for e in actionable)
        alert["message"] = f"SETUP ALERT: {pairs_str} — actionable signal. Open MT5 and review."
    elif any(s.get("signal") == "NEWS_CAUTION" for s in alert["signals"]):
        caution = [s["pair"] for s in alert["signals"] if s.get("signal") == "NEWS_CAUTION"]
        alert["message"] = f"NEWS CAUTION: {', '.join(caution)} have valid setups blocked by upcoming major news. Wait for event to pass."
    elif any(s.get("signal") == "WATCH" for s in alert["signals"]):
        watch = [s["pair"] for s in alert["signals"] if s.get("signal") == "WATCH"]
        alert["message"] = f"WATCH: {', '.join(watch)} approaching setup zone. No action yet."
    else:
        alert["message"] = "No setups. All pairs in WAIT. Capital protected."

    return alert


def _build_email_body(alert: dict) -> str:
    """Format alert dict into a clean HTML email."""

    # ── color palette ──────────────────────────────────────────────────────────
    STATUS_COLORS = {
        "GO_LONG":      ("#0d6b2e", "#e6f4ea"),   # dark-green text, light-green bg
        "GO_SHORT":     ("#8b1a1a", "#fdecea"),   # dark-red text, light-red bg
        "WATCH":        ("#7a5500", "#fff8e1"),   # amber
        "NEWS_CAUTION": ("#6d3b00", "#fff3e0"),   # orange
        "NEWS_BLOCK":   ("#5c0000", "#fce4ec"),   # deep red
        "WAIT":         ("#555",    "#f5f5f5"),   # grey
    }

    # Dominant signal for the header banner
    dominant = "WAIT"
    for s in alert.get("signals", []):
        sig = s.get("signal", "WAIT")
        if sig in ("GO_LONG", "GO_SHORT"):
            dominant = sig
            break
        if sig == "WATCH" and dominant == "WAIT":
            dominant = "WATCH"
        if sig in ("NEWS_CAUTION", "NEWS_BLOCK") and dominant == "WAIT":
            dominant = sig

    banner_fg, banner_bg = STATUS_COLORS.get(dominant, ("#333", "#f0f0f0"))

    # ── banner label ───────────────────────────────────────────────────────────
    BANNER_LABELS = {
        "GO_LONG":      "ACTION REQUIRED — GO LONG",
        "GO_SHORT":     "ACTION REQUIRED — GO SHORT",
        "WATCH":        "WATCH — Approaching Setup Zone",
        "NEWS_CAUTION": "NEWS CAUTION — Major Event Soon",
        "NEWS_BLOCK":   "NEWS BLOCK — No Trading Now",
        "WAIT":         "No Setup — Capital Protected",
    }
    banner_label = BANNER_LABELS.get(dominant, dominant)

    # ── helpers ────────────────────────────────────────────────────────────────
    def badge(sig: str) -> str:
        fg, bg = STATUS_COLORS.get(sig, ("#555", "#eee"))
        return (
            f'<span style="background:{bg};color:{fg};padding:2px 8px;'
            f'border-radius:4px;font-size:11px;font-weight:700;'
            f'font-family:monospace;white-space:nowrap;">{sig}</span>'
        )

    def bias_arrow(bias: str) -> str:
        if bias == "LONG":
            return '<span style="color:#0d6b2e;font-weight:700;">&#9650; LONG</span>'
        if bias == "SHORT":
            return '<span style="color:#8b1a1a;font-weight:700;">&#9660; SHORT</span>'
        return bias

    # ── pair rows ──────────────────────────────────────────────────────────────
    pair_rows_html = ""
    for s in alert.get("signals", []):
        sig  = s.get("signal", "WAIT")
        pair = s.get("pair") or s.get("symbol") or "?"
        bias = s.get("bias", "")
        note = s.get("note", "") or s.get("next_step", "")
        trade = s.get("trade")
        news_msg = s.get("news", "")

        # Row background: highlight actionable pairs
        row_bg = "#fff"
        if sig in ("GO_LONG", "GO_SHORT"):
            row_bg = STATUS_COLORS[sig][1]

        pair_rows_html += f"""
        <tr style="background:{row_bg};">
          <td style="padding:10px 12px;font-weight:700;font-family:monospace;font-size:14px;">{pair}</td>
          <td style="padding:10px 12px;">{badge(sig)}</td>
          <td style="padding:10px 12px;">{bias_arrow(bias)}</td>
          <td style="padding:10px 12px;color:#555;font-size:13px;">{note[:90]}</td>
        </tr>"""

        # Trade parameters sub-row
        if trade:
            t = trade
            pair_rows_html += f"""
        <tr style="background:{row_bg};">
          <td colspan="4" style="padding:4px 12px 12px 24px;">
            <table style="border-collapse:collapse;font-size:13px;font-family:monospace;">
              <tr>
                <td style="padding:4px 16px 4px 0;"><strong>Entry</strong><br>{t.get('entry')}</td>
                <td style="padding:4px 16px 4px 0;color:#8b1a1a;"><strong>Stop Loss</strong><br>{t.get('sl')} &nbsp;({t.get('sl_pips')}p)</td>
                <td style="padding:4px 16px 4px 0;color:#0d6b2e;"><strong>Take Profit</strong><br>{t.get('tp')} &nbsp;({t.get('tp_pips')}p)</td>
                <td style="padding:4px 0;"><strong>RRR</strong><br>1 : {t.get('rrr')}</td>
              </tr>
            </table>
          </td>
        </tr>"""

        if news_msg:
            pair_rows_html += f"""
        <tr style="background:#fff8e1;">
          <td colspan="4" style="padding:4px 12px 10px 24px;font-size:12px;color:#7a5500;">
            ⚠&nbsp; {news_msg}
          </td>
        </tr>"""

    # ── lessons block ──────────────────────────────────────────────────────────
    lessons_html = ""
    if alert.get("new_lessons"):
        lessons_html = """
        <tr><td colspan="4" style="padding:16px 12px 4px;font-size:13px;font-weight:700;
                color:#333;border-top:1px solid #eee;">NEW TRADE LESSONS</td></tr>"""
        for lesson in alert["new_lessons"]:
            outcome = lesson.get("outcome", "")
            outcome_color = "#0d6b2e" if outcome == "WIN" else "#8b1a1a"
            lessons_html += f"""
        <tr><td colspan="4" style="padding:4px 12px 4px 24px;font-size:13px;color:#444;">
          <span style="color:{outcome_color};font-weight:700;">[{lesson.get('pair')} {outcome}]</span>
          &nbsp;{lesson.get('lesson','')[:120]}
        </td></tr>"""

    # ── challenge tracker ──────────────────────────────────────────────────────
    challenge_html = ""
    try:
        from skills.challenge_tracker import get_dashboard
        d = get_dashboard()
        # Use the exact keys returned by get_dashboard()
        profit_pct   = d.get("profit_pct", 0)
        progress_pct = d.get("progress_pct", 0)
        balance      = d.get("balance", 0)          # key is "balance", not "current_balance"
        target       = d.get("target_usd", 110000)  # key is "target_usd"
        remaining    = d.get("remaining_usd", 0)
        daily_pct    = d.get("daily_loss_pct", 0)   # key is "daily_loss_pct"
        total_pct    = d.get("total_loss_pct", 0)   # key is "total_loss_pct"
        days_done    = d.get("trading_days", 0)      # key is "trading_days"
        wins         = d.get("trades_won", 0)
        losses       = d.get("trades_lost", 0)
        phase        = d.get("phase", "")
        bar_width    = min(int(progress_pct), 100)
        profit_color = "#0d6b2e" if profit_pct >= 0 else "#8b1a1a"
        daily_color  = "#8b1a1a" if d.get("daily_warning") else "#333333"
        total_color  = "#8b1a1a" if d.get("total_warning") else "#333333"

        challenge_html = f"""
        <tr><td colspan="4" style="padding:0;"></td></tr>
        <tr>
          <td colspan="4" style="padding:16px 12px 0;border-top:2px solid #e0e0e0;">
            <div class="force-light" style="background:#f8f9fa;border-radius:6px;padding:14px 18px;">
              <div class="text-muted" style="font-size:12px;color:#888888;text-transform:uppercase;
                          letter-spacing:1px;margin-bottom:8px;">
                FTMO Challenge — {phase}
              </div>
              <table style="width:100%;border-collapse:collapse;font-size:13px;">
                <tr>
                  <td class="text-dark" style="padding:3px 0;color:#555555;">Balance</td>
                  <td style="padding:3px 0;font-weight:700;font-family:monospace;color:#111111;">
                    ${balance:,.2f}
                    <span class="{'text-green' if profit_pct>=0 else 'text-red'}"
                          style="color:{profit_color};margin-left:8px;">
                      ({profit_pct:+.2f}%)
                    </span>
                  </td>
                  <td class="text-dark" style="padding:3px 0 3px 24px;color:#555555;">Trading Days</td>
                  <td style="padding:3px 0;font-weight:700;color:#111111;">{days_done} / 4 min</td>
                </tr>
                <tr>
                  <td class="text-dark" style="padding:3px 0;color:#555555;">Target</td>
                  <td style="padding:3px 0;font-family:monospace;color:#111111;">
                    ${target:,.0f} &nbsp;(needs ${remaining:,.0f} more)
                  </td>
                  <td class="text-dark" style="padding:3px 0 3px 24px;color:#555555;">Win / Loss</td>
                  <td style="padding:3px 0;font-weight:700;">
                    <span class="text-green" style="color:#0d6b2e;">{wins}W</span>
                    <span style="color:#555555;"> / </span>
                    <span class="text-red" style="color:#8b1a1a;">{losses}L</span>
                  </td>
                </tr>
                <tr>
                  <td class="text-dark" style="padding:3px 0;color:#555555;">Daily risk used</td>
                  <td style="padding:3px 0;color:{daily_color};">{daily_pct:.1f}%</td>
                  <td class="text-dark" style="padding:3px 0 3px 24px;color:#555555;">Total drawdown</td>
                  <td style="padding:3px 0;color:{total_color};">{total_pct:.1f}%</td>
                </tr>
              </table>
              <!-- progress bar -->
              <div style="margin-top:10px;">
                <div class="text-muted" style="font-size:11px;color:#888888;margin-bottom:4px;">
                  Progress to target: {progress_pct:.1f}%
                </div>
                <div style="background:#e0e0e0;border-radius:4px;height:8px;width:100%;">
                  <div style="background:#1a73e8;border-radius:4px;height:8px;
                              width:{bar_width}%;"></div>
                </div>
              </div>
            </div>
          </td>
        </tr>"""
    except Exception:
        pass

    # ── full HTML ──────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <!-- Force light mode in all email clients that support it -->
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <style>
    :root {{ color-scheme: light; }}
    /* Override dark-mode inversion for Gmail/iOS/Outlook */
    @media (prefers-color-scheme: dark) {{
      body, table, td, th, div, span, p {{
        background-color: inherit !important;
        color: inherit !important;
      }}
      .force-white {{ background-color: #ffffff !important; }}
      .force-light  {{ background-color: #f8f9fa !important; }}
      .force-dark-header {{ background-color: #1a1a2e !important; }}
      .text-white  {{ color: #ffffff !important; }}
      .text-muted  {{ color: #aaaaaa !important; }}
      .text-dark   {{ color: #333333 !important; }}
      .text-green  {{ color: #0d6b2e !important; }}
      .text-red    {{ color: #8b1a1a !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif;color-scheme:light;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#f0f2f5;padding:24px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           class="force-white"
           style="background:#ffffff;border-radius:8px;overflow:hidden;
                  box-shadow:0 2px 8px rgba(0,0,0,.10);">

      <!-- Header -->
      <tr style="background:#1a1a2e;" class="force-dark-header">
        <td style="padding:20px 24px;">
          <span class="text-white" style="color:#ffffff;font-size:18px;font-weight:700;
                       letter-spacing:1px;">FTMO Agent</span>
          <span class="text-muted" style="color:#aaaaaa;font-size:13px;margin-left:12px;">Market Monitor</span>
          <div class="text-muted" style="color:#aaaaaa;font-size:12px;margin-top:4px;">{alert['timestamp']}</div>
        </td>
      </tr>

      <!-- Status banner -->
      <tr style="background:{banner_bg};">
        <td style="padding:14px 24px;">
          <span style="color:{banner_fg};font-size:16px;font-weight:700;">
            {banner_label}
          </span>
          <div style="color:{banner_fg};font-size:13px;margin-top:4px;opacity:.8;">
            {alert.get('message','')}
          </div>
        </td>
      </tr>

      <!-- Pair table -->
      <tr><td style="padding:0 12px;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;margin:16px 0;">
          <thead>
            <tr style="background:#f5f5f5;border-bottom:2px solid #e0e0e0;" class="force-light">
              <th style="padding:8px 12px;text-align:left;font-size:12px;
                         color:#888888;text-transform:uppercase;letter-spacing:.5px;">Pair</th>
              <th style="padding:8px 12px;text-align:left;font-size:12px;
                         color:#888888;text-transform:uppercase;letter-spacing:.5px;">Signal</th>
              <th style="padding:8px 12px;text-align:left;font-size:12px;
                         color:#888888;text-transform:uppercase;letter-spacing:.5px;">Bias</th>
              <th style="padding:8px 12px;text-align:left;font-size:12px;
                         color:#888888;text-transform:uppercase;letter-spacing:.5px;">Note</th>
            </tr>
          </thead>
          <tbody>
            {pair_rows_html}
            {lessons_html}
            {challenge_html}
          </tbody>
        </table>
      </td></tr>

      <!-- Footer -->
      <tr style="background:#f8f9fa;border-top:1px solid #e0e0e0;" class="force-light">
        <td style="padding:14px 24px;font-size:12px;color:#aaaaaa;">
          Automated signal — do not trade without your own confirmation.<br>
          Strategy: EMA Trend + H4 Pullback &nbsp;|&nbsp; Risk: 0.5% per trade
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

    return html


# ── Main loop ──────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"[Monitor] Started at {_now_str()}", flush=True)
    print(f"[Monitor] Fires at H4 closes: {sorted(H4_CLOSE_HOURS_CEST)}:{H4_CHECK_MINUTE:02d} CEST", flush=True)
    print(f"[Monitor] Polling every {POLL_INTERVAL}s", flush=True)

    # Send startup confirmation email so we know the service is live
    send_email_alert(
        subject="FTMO Monitor — Service started",
        body=(
            f"FTMO Monitor is running on the cloud server.\n"
            f"Started: {_now_str()}\n\n"
            f"Scanning: {', '.join(PAIRS)}\n"
            f"Schedule: H4 closes at {sorted(H4_CLOSE_HOURS_CEST)}:03 CEST\n\n"
            f"— FTMO Agent"
        ),
    )

    last_check_hour = -1

    while True:
        try:
            now = _now()

            if _is_h4_check_time(now) and now.hour != last_check_hour:
                last_check_hour = now.hour

                # Skip scans when forex market is closed (weekends)
                if not _market_is_open(now):
                    print(f"[Monitor] {_now_str()} — Market closed (weekend). Skipping scan.", flush=True)
                    time.sleep(POLL_INTERVAL)
                    continue

                print(f"\n[Monitor] {_now_str()} — H4 close detected, running check...", flush=True)

                alert = run_check()

                # Persist alert
                alerts = _load_alerts()
                alerts.append(alert)
                _save_alerts(alerts)

                print(f"[Monitor] {alert['message']}", flush=True)

                # Send email for actionable signals, CAUTION alerts, and WATCH alerts
                if alert["action_required"]:
                    subject = f"FTMO SETUP: {alert['message'][:60]}"
                    send_email_alert(subject, _build_email_body(alert))
                elif any(s.get("signal") == "NEWS_CAUTION" for s in alert["signals"]):
                    subject = f"FTMO NEWS CAUTION: {alert['message'][:60]}"
                    send_email_alert(subject, _build_email_body(alert))
                elif any(s.get("signal") == "WATCH" for s in alert["signals"]):
                    subject = f"FTMO WATCH: {alert['message'][:60]}"
                    send_email_alert(subject, _build_email_body(alert))

        except KeyboardInterrupt:
            print("\n[Monitor] Stopped.", flush=True)
            sys.exit(0)
        except Exception as e:
            print(f"[Monitor] Error: {e}", flush=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
