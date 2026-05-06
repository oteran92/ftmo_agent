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
    """Format alert dict into a clean, elegant HTML email."""

    # ── signal metadata ────────────────────────────────────────────────────────
    SIG_META = {
        "GO_LONG":      {"color": "#0a7c42", "light": "#eaf7ef", "icon": "▲", "label": "GO LONG"},
        "GO_SHORT":     {"color": "#c0392b", "light": "#fdf0ef", "icon": "▼", "label": "GO SHORT"},
        "WATCH":        {"color": "#b07d00", "light": "#fffbf0", "icon": "◉", "label": "WATCH"},
        "NEWS_CAUTION": {"color": "#c05a00", "light": "#fff4ec", "icon": "⚠", "label": "NEWS CAUTION"},
        "NEWS_BLOCK":   {"color": "#8b1a1a", "light": "#fdf0ef", "icon": "✕", "label": "NEWS BLOCK"},
        "WAIT":         {"color": "#8a8a8a", "light": "#f7f7f7", "icon": "·", "label": "NO SETUP"},
    }

    # Dominant signal priority
    PRIORITY = ["GO_LONG", "GO_SHORT", "NEWS_BLOCK", "NEWS_CAUTION", "WATCH", "WAIT"]
    dominant = "WAIT"
    for s in alert.get("signals", []):
        sig = s.get("signal", "WAIT")
        if PRIORITY.index(sig if sig in PRIORITY else "WAIT") < PRIORITY.index(dominant):
            dominant = sig

    meta     = SIG_META.get(dominant, SIG_META["WAIT"])
    acc      = meta["color"]    # accent color
    acc_bg   = meta["light"]    # tinted background
    is_action = dominant in ("GO_LONG", "GO_SHORT")

    # ── inline badge ───────────────────────────────────────────────────────────
    def badge(sig: str) -> str:
        m = SIG_META.get(sig, SIG_META["WAIT"])
        return (
            f'<span style="display:inline-block;background:{m["light"]};color:{m["color"]};'
            f'border:1px solid {m["color"]}33;padding:2px 7px;border-radius:3px;'
            f'font-size:10px;font-weight:700;letter-spacing:.5px;'
            f'font-family:monospace;">{sig}</span>'
        )

    def bias_pill(bias: str) -> str:
        if bias == "LONG":
            return ('<span style="color:#0a7c42;font-size:12px;font-weight:700;">'
                    '&#9650;&nbsp;LONG</span>')
        if bias == "SHORT":
            return ('<span style="color:#c0392b;font-size:12px;font-weight:700;">'
                    '&#9660;&nbsp;SHORT</span>')
        return f'<span style="color:#aaa;font-size:12px;">{bias}</span>'

    # ── pair rows (only non-WAIT, or WAIT if <5 non-WAIT) ─────────────────────
    signals = alert.get("signals", [])
    actionable = [s for s in signals if s.get("signal") in ("GO_LONG", "GO_SHORT")]
    watch_sigs = [s for s in signals if s.get("signal") in ("WATCH", "NEWS_CAUTION", "NEWS_BLOCK")]
    wait_sigs  = [s for s in signals if s.get("signal") not in
                  ("GO_LONG", "GO_SHORT", "WATCH", "NEWS_CAUTION", "NEWS_BLOCK")]

    # Show: actionable first, then watch, then wait (collapsed to save space)
    display_sigs = actionable + watch_sigs + wait_sigs

    pair_rows_html = ""
    for s in display_sigs:
        sig   = s.get("signal", "WAIT")
        pair  = s.get("pair") or s.get("symbol") or "?"
        bias  = s.get("bias", "")
        note  = s.get("note", "") or s.get("next_step", "")
        trade = s.get("trade")
        news_msg = s.get("news", "")
        m = SIG_META.get(sig, SIG_META["WAIT"])

        # Left-border accent stripe — strong for actionable, subtle for wait
        border_color = m["color"] if sig not in ("WAIT",) else "#e0e0e0"
        row_bg = m["light"] if sig in ("GO_LONG", "GO_SHORT") else "#fff"

        pair_rows_html += f"""
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:10px 0 10px 12px;border-left:3px solid {border_color};
                         width:70px;">
                <span style="font-family:monospace;font-size:13px;font-weight:700;
                             color:#222;">{pair}</span>
              </td>
              <td style="padding:10px 8px;width:110px;">{badge(sig)}</td>
              <td style="padding:10px 8px;width:70px;">{bias_pill(bias)}</td>
              <td style="padding:10px 8px 10px 0;font-size:12px;color:#666;
                         line-height:1.4;">{note[:85]}</td>
            </tr>"""

        if trade:
            t = trade
            sl_pips = t.get("sl_pips", "")
            tp_pips = t.get("tp_pips", "")
            pair_rows_html += f"""
            <tr style="background:{m['light']};border-bottom:1px solid #f0f0f0;">
              <td colspan="4" style="padding:6px 12px 12px 15px;
                                     border-left:3px solid {border_color};">
                <table style="border-collapse:collapse;width:100%;">
                  <tr>
                    <td style="padding:0 20px 0 0;">
                      <div style="font-size:10px;color:#999;text-transform:uppercase;
                                  letter-spacing:.5px;margin-bottom:2px;">Entry</div>
                      <div style="font-family:monospace;font-size:13px;font-weight:700;
                                  color:#222;">{t.get("entry")}</div>
                    </td>
                    <td style="padding:0 20px 0 0;">
                      <div style="font-size:10px;color:#999;text-transform:uppercase;
                                  letter-spacing:.5px;margin-bottom:2px;">Stop Loss</div>
                      <div style="font-family:monospace;font-size:13px;font-weight:700;
                                  color:#c0392b;">{t.get("sl")}
                        <span style="font-size:11px;font-weight:400;">({sl_pips}p)</span>
                      </div>
                    </td>
                    <td style="padding:0 20px 0 0;">
                      <div style="font-size:10px;color:#999;text-transform:uppercase;
                                  letter-spacing:.5px;margin-bottom:2px;">Take Profit</div>
                      <div style="font-family:monospace;font-size:13px;font-weight:700;
                                  color:#0a7c42;">{t.get("tp")}
                        <span style="font-size:11px;font-weight:400;">({tp_pips}p)</span>
                      </div>
                    </td>
                    <td>
                      <div style="font-size:10px;color:#999;text-transform:uppercase;
                                  letter-spacing:.5px;margin-bottom:2px;">RRR</div>
                      <div style="font-size:13px;font-weight:700;color:#222;">
                        1&nbsp;:&nbsp;{t.get("rrr")}
                      </div>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>"""

        if news_msg:
            pair_rows_html += f"""
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td colspan="4"
                  style="padding:5px 12px 8px 15px;border-left:3px solid #c05a00;
                         font-size:11px;color:#c05a00;background:#fffbf0;">
                ⚠&nbsp; {news_msg}
              </td>
            </tr>"""

    # ── lessons ────────────────────────────────────────────────────────────────
    lessons_html = ""
    if alert.get("new_lessons"):
        items = ""
        for lesson in alert["new_lessons"]:
            outcome = lesson.get("outcome", "")
            oc = "#0a7c42" if outcome == "WIN" else "#c0392b"
            items += f"""<div style="padding:6px 0;border-bottom:1px solid #f5f5f5;
                                      font-size:12px;color:#444;">
              <span style="color:{oc};font-weight:700;">[{lesson.get('pair')} {outcome}]</span>
              &nbsp;{lesson.get('lesson','')[:130]}
            </div>"""
        lessons_html = f"""
        <div style="margin:0 20px 16px;padding:14px;border:1px solid #e8e8e8;
                    border-radius:6px;background:#fafafa;">
          <div style="font-size:11px;text-transform:uppercase;letter-spacing:.8px;
                      color:#aaa;font-weight:700;margin-bottom:8px;">New Trade Lessons</div>
          {items}
        </div>"""

    # ── challenge tracker ──────────────────────────────────────────────────────
    challenge_html = ""
    try:
        from skills.challenge_tracker import get_dashboard
        d = get_dashboard()
        profit_pct   = d.get("profit_pct", 0)
        progress_pct = min(d.get("progress_pct", 0), 100)
        balance      = d.get("balance", 0)
        target       = d.get("target_usd", 110000)
        remaining    = d.get("remaining_usd", 0)
        daily_pct    = d.get("daily_loss_pct", 0)
        total_pct    = d.get("total_loss_pct", 0)
        days_done    = d.get("trading_days", 0)
        wins         = d.get("trades_won", 0)
        losses       = d.get("trades_lost", 0)
        daily_pnl    = d.get("daily_pnl", 0)
        phase        = d.get("phase", "DEMO")
        bar_w        = int(progress_pct)
        pc           = "#0a7c42" if profit_pct >= 0 else "#c0392b"
        dpnl_c       = "#0a7c42" if daily_pnl >= 0 else "#c0392b"
        daily_warn   = daily_pct > 70
        total_warn   = total_pct > 60

        challenge_html = f"""
        <div style="margin:0 20px 20px;border:1px solid #e8e8e8;border-radius:6px;
                    overflow:hidden;">
          <!-- tracker header -->
          <div style="background:#f7f7f7;padding:10px 16px;border-bottom:1px solid #e8e8e8;">
            <span style="font-size:10px;text-transform:uppercase;letter-spacing:1px;
                         color:#888;font-weight:700;">FTMO Challenge</span>
            <span style="font-size:11px;color:#bbb;margin-left:8px;">{phase}</span>
          </div>
          <!-- balance hero -->
          <div style="padding:14px 16px 0;text-align:center;">
            <div style="font-size:28px;font-weight:700;font-family:monospace;color:#222;">
              ${balance:,.2f}
            </div>
            <div style="font-size:13px;color:{pc};font-weight:600;margin-top:2px;">
              {profit_pct:+.2f}% &nbsp;·&nbsp; Target ${target:,.0f}
            </div>
          </div>
          <!-- progress bar -->
          <div style="padding:10px 16px 14px;">
            <div style="display:flex;justify-content:space-between;
                        font-size:10px;color:#aaa;margin-bottom:4px;">
              <span>Progress to +10%</span>
              <span style="color:{pc};font-weight:700;">{progress_pct:.1f}%</span>
            </div>
            <div style="background:#ebebeb;border-radius:3px;height:6px;">
              <div style="background:{acc};border-radius:3px;height:6px;
                          width:{bar_w}%;"></div>
            </div>
          </div>
          <!-- stats grid -->
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;
                      border-top:1px solid #f0f0f0;text-align:center;">
            <div style="padding:10px 8px;border-right:1px solid #f0f0f0;">
              <div style="font-size:10px;color:#aaa;text-transform:uppercase;
                          letter-spacing:.5px;margin-bottom:3px;">Today</div>
              <div style="font-size:13px;font-weight:700;color:{dpnl_c};
                          font-family:monospace;">{daily_pnl:+,.0f}</div>
            </div>
            <div style="padding:10px 8px;border-right:1px solid #f0f0f0;">
              <div style="font-size:10px;color:#aaa;text-transform:uppercase;
                          letter-spacing:.5px;margin-bottom:3px;">Days</div>
              <div style="font-size:13px;font-weight:700;color:#222;">
                {days_done}<span style="font-size:10px;color:#bbb;">/4</span>
              </div>
            </div>
            <div style="padding:10px 8px;border-right:1px solid #f0f0f0;">
              <div style="font-size:10px;color:#aaa;text-transform:uppercase;
                          letter-spacing:.5px;margin-bottom:3px;">W / L</div>
              <div style="font-size:13px;font-weight:700;">
                <span style="color:#0a7c42;">{wins}</span>
                <span style="color:#ddd;font-size:10px;">/</span>
                <span style="color:#c0392b;">{losses}</span>
              </div>
            </div>
            <div style="padding:10px 8px;">
              <div style="font-size:10px;color:#aaa;text-transform:uppercase;
                          letter-spacing:.5px;margin-bottom:3px;">DD</div>
              <div style="font-size:13px;font-weight:700;
                          color:{'#c0392b' if total_warn else '#222'};">
                {total_pct:.1f}<span style="font-size:10px;color:#bbb;">%</span>
              </div>
            </div>
          </div>
        </div>"""
    except Exception:
        pass

    # ── action button (only for GO signals) ───────────────────────────────────
    action_html = ""
    if is_action:
        action_html = f"""
        <div style="margin:0 20px 20px;text-align:center;">
          <div style="display:inline-block;background:{acc};color:#fff;
                      padding:12px 32px;border-radius:5px;font-size:14px;
                      font-weight:700;letter-spacing:.5px;">
            {meta['icon']}&nbsp; {meta['label']} — Open MT5 Now
          </div>
        </div>"""

    # ── top accent line color ──────────────────────────────────────────────────
    accent_line = f'<div style="height:4px;background:{acc};"></div>'

    # ── full HTML ──────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f2f3f5;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:20px 0;">
  <tr><td align="center">
  <table width="580" cellpadding="0" cellspacing="0"
         style="background:#ffffff;border-radius:10px;overflow:hidden;
                border:1px solid #e4e4e4;">

    <!-- top accent line -->
    <tr><td>{accent_line}</td></tr>

    <!-- header -->
    <tr>
      <td style="padding:18px 20px 14px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <span style="font-size:15px;font-weight:700;color:#1a1a1a;
                           letter-spacing:.3px;">FTMO Agent</span>
              <span style="font-size:12px;color:#bbb;margin-left:8px;">
                Market Monitor
              </span>
            </td>
            <td align="right">
              <span style="font-size:11px;color:#bbb;">{alert['timestamp']}</span>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- status hero -->
    <tr>
      <td style="padding:0 20px 16px;">
        <div style="background:{acc_bg};border-left:4px solid {acc};
                    border-radius:4px;padding:12px 16px;">
          <div style="font-size:18px;font-weight:700;color:{acc};
                      letter-spacing:.3px;">
            {meta['icon']}&nbsp; {meta['label']}
          </div>
          <div style="font-size:12px;color:#666;margin-top:4px;line-height:1.5;">
            {alert.get('message','')}
          </div>
        </div>
      </td>
    </tr>

    <!-- pair table -->
    <tr>
      <td style="padding:0 20px 8px;">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:.8px;
                    color:#bbb;font-weight:700;margin-bottom:6px;">
          Market Scan — 9 Pairs
        </div>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border:1px solid #f0f0f0;border-radius:5px;overflow:hidden;">
          <thead>
            <tr style="background:#f9f9f9;">
              <th style="padding:7px 0 7px 12px;text-align:left;font-size:10px;
                         color:#bbb;text-transform:uppercase;letter-spacing:.5px;
                         font-weight:600;width:68px;border-bottom:1px solid #f0f0f0;">
                Pair</th>
              <th style="padding:7px 8px;text-align:left;font-size:10px;
                         color:#bbb;text-transform:uppercase;letter-spacing:.5px;
                         font-weight:600;width:100px;border-bottom:1px solid #f0f0f0;">
                Signal</th>
              <th style="padding:7px 8px;text-align:left;font-size:10px;
                         color:#bbb;text-transform:uppercase;letter-spacing:.5px;
                         font-weight:600;width:65px;border-bottom:1px solid #f0f0f0;">
                Bias</th>
              <th style="padding:7px 8px 7px 0;text-align:left;font-size:10px;
                         color:#bbb;text-transform:uppercase;letter-spacing:.5px;
                         font-weight:600;border-bottom:1px solid #f0f0f0;">
                Note</th>
            </tr>
          </thead>
          <tbody>
            {pair_rows_html}
          </tbody>
        </table>
      </td>
    </tr>

    <!-- action CTA -->
    {action_html}

    <!-- lessons -->
    {lessons_html}

    <!-- challenge tracker -->
    {challenge_html}

    <!-- footer -->
    <tr>
      <td style="padding:12px 20px;border-top:1px solid #f0f0f0;">
        <p style="margin:0;font-size:11px;color:#ccc;line-height:1.6;">
          Automated signal &mdash; confirm before execution.
          &nbsp;Strategy: EMA Trend + H4 Pullback &nbsp;|&nbsp; Risk: 0.5% / trade
        </p>
      </td>
    </tr>

  </table>
  </td></tr>
</table>
</body>
</html>"""


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
