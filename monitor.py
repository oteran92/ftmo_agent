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
    """Format alert dict into a minimal, professional HTML email."""

    # ── signal colour map ──────────────────────────────────────────────────────
    # (accent bar colour, badge bg, badge text)
    SIG_STYLE = {
        "GO_LONG":      ("#16a34a", "#dcfce7", "#15803d"),
        "GO_SHORT":     ("#dc2626", "#fee2e2", "#b91c1c"),
        "WATCH":        ("#d97706", "#fef3c7", "#92400e"),
        "NEWS_CAUTION": ("#ea580c", "#ffedd5", "#9a3412"),
        "NEWS_BLOCK":   ("#dc2626", "#fce7f3", "#9d174d"),
        "WAIT":         ("#9ca3af", "#f3f4f6", "#6b7280"),
    }

    # Dominant signal determines the top accent bar
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

    accent_color = SIG_STYLE[dominant][0]

    STATUS_TITLE = {
        "GO_LONG":      "Action Required",
        "GO_SHORT":     "Action Required",
        "WATCH":        "Watching",
        "NEWS_CAUTION": "News Caution",
        "NEWS_BLOCK":   "News Block",
        "WAIT":         "No Setup",
    }
    STATUS_SUB = {
        "GO_LONG":      "GO LONG signal confirmed",
        "GO_SHORT":     "GO SHORT signal confirmed",
        "WATCH":        "Approaching setup zone — no entry yet",
        "NEWS_CAUTION": "Valid setup but major news event soon",
        "NEWS_BLOCK":   "Trading paused due to high-impact news",
        "WAIT":         "Capital protected · All pairs in wait",
    }

    def badge(sig: str) -> str:
        _, bg, fg = SIG_STYLE.get(sig, SIG_STYLE["WAIT"])
        label = sig.replace("_", " ")
        return (
            f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700;'
            f'letter-spacing:.4px;font-family:monospace;">{label}</span>'
        )

    def bias_pill(bias: str) -> str:
        if bias == "LONG":
            return '<span style="color:#16a34a;font-weight:700;font-size:12px;">▲ LONG</span>'
        if bias == "SHORT":
            return '<span style="color:#dc2626;font-weight:700;font-size:12px;">▼ SHORT</span>'
        return f'<span style="color:#9ca3af;font-size:12px;">{bias}</span>'

    # ── build pair rows ────────────────────────────────────────────────────────
    pair_rows = ""
    actionable_trades = []   # for the trade card below

    for s in alert.get("signals", []):
        sig   = s.get("signal", "WAIT")
        pair  = s.get("pair") or s.get("symbol") or "?"
        bias  = s.get("bias", "")
        note  = (s.get("note") or s.get("next_step") or "—")[:85]
        trade = s.get("trade")
        if trade:
            trade["pair"] = pair
            trade["signal"] = sig
            actionable_trades.append(trade)

        # Dim WAIT rows slightly
        row_opacity = '1' if sig != "WAIT" else '0.7'
        border = "border-bottom:1px solid #f3f4f6;"

        pair_rows += f"""
      <tr style="opacity:{row_opacity};">
        <td style="padding:11px 0 11px 20px;{border}">
          <span style="font-weight:700;font-family:monospace;font-size:14px;
                       color:#111;">{pair}</span>
        </td>
        <td style="padding:11px 8px;{border}">{badge(sig)}</td>
        <td style="padding:11px 8px;{border}">{bias_pill(bias)}</td>
        <td style="padding:11px 20px 11px 8px;{border}
                   font-size:12px;color:#6b7280;line-height:1.4;">{note}</td>
      </tr>"""

    # ── trade setup card (only for GO signals) ─────────────────────────────────
    trade_card = ""
    for t in actionable_trades:
        sig_color = "#16a34a" if t["signal"] == "GO_LONG" else "#dc2626"
        direction = "LONG" if t["signal"] == "GO_LONG" else "SHORT"
        trade_card += f"""
    <div style="margin:0 20px 20px;background:#f8fafc;border-radius:6px;
                border-left:3px solid {sig_color};padding:14px 16px;">
      <div style="font-size:11px;font-weight:700;color:{sig_color};
                  letter-spacing:.8px;margin-bottom:10px;">
        TRADE SETUP — {t['pair']} {direction}
      </div>
      <table style="border-collapse:collapse;width:100%;">
        <tr>
          <td style="padding:0 16px 0 0;text-align:center;">
            <div style="font-size:10px;color:#9ca3af;letter-spacing:.5px;
                        margin-bottom:3px;">ENTRY</div>
            <div style="font-size:17px;font-weight:700;font-family:monospace;
                        color:#111;">{t.get('entry')}</div>
          </td>
          <td style="padding:0 16px;text-align:center;border-left:1px solid #e5e7eb;">
            <div style="font-size:10px;color:#9ca3af;letter-spacing:.5px;
                        margin-bottom:3px;">STOP LOSS</div>
            <div style="font-size:17px;font-weight:700;font-family:monospace;
                        color:#dc2626;">{t.get('sl')}</div>
            <div style="font-size:11px;color:#9ca3af;">{t.get('sl_pips')} pips</div>
          </td>
          <td style="padding:0 16px;text-align:center;border-left:1px solid #e5e7eb;">
            <div style="font-size:10px;color:#9ca3af;letter-spacing:.5px;
                        margin-bottom:3px;">TAKE PROFIT</div>
            <div style="font-size:17px;font-weight:700;font-family:monospace;
                        color:#16a34a;">{t.get('tp')}</div>
            <div style="font-size:11px;color:#9ca3af;">{t.get('tp_pips')} pips</div>
          </td>
          <td style="padding:0 0 0 16px;text-align:center;border-left:1px solid #e5e7eb;">
            <div style="font-size:10px;color:#9ca3af;letter-spacing:.5px;
                        margin-bottom:3px;">RRR</div>
            <div style="font-size:17px;font-weight:700;font-family:monospace;
                        color:#111;">1:{t.get('rrr')}</div>
          </td>
        </tr>
      </table>
    </div>"""

    # ── news / lessons ─────────────────────────────────────────────────────────
    extras = ""
    for s in alert.get("signals", []):
        if s.get("news"):
            extras += (
                f'<p style="margin:0 20px 8px;font-size:12px;color:#92400e;'
                f'background:#fef3c7;padding:8px 12px;border-radius:4px;">'
                f'⚠ {s.get("news","")[:100]}</p>'
            )
    if alert.get("new_lessons"):
        extras += (
            '<p style="margin:16px 20px 4px;font-size:11px;font-weight:700;'
            'color:#6b7280;letter-spacing:.6px;text-transform:uppercase;">New Trade Lessons</p>'
        )
        for lesson in alert["new_lessons"]:
            outcome = lesson.get("outcome", "")
            c = "#16a34a" if outcome == "WIN" else "#dc2626"
            extras += (
                f'<p style="margin:2px 20px;font-size:12px;color:#374151;">'
                f'<span style="color:{c};font-weight:700;">[{lesson.get("pair")} {outcome}]</span>'
                f' {lesson.get("lesson","")[:110]}</p>'
            )

    # ── challenge mini-dashboard ───────────────────────────────────────────────
    challenge_strip = ""
    try:
        from skills.challenge_tracker import get_dashboard
        d = get_dashboard()
        bal      = d.get("balance", 0)
        pct      = d.get("profit_pct", 0)
        prog     = min(d.get("progress_pct", 0), 100)
        wins     = d.get("trades_won", 0)
        losses   = d.get("trades_lost", 0)
        days     = d.get("trading_days", 0)
        remaining = d.get("remaining_usd", 0)
        d_pct    = d.get("daily_loss_pct", 0)
        t_pct    = d.get("total_loss_pct", 0)
        pct_color = "#16a34a" if pct >= 0 else "#dc2626"
        d_warn   = d.get("daily_warning", False)
        t_warn   = d.get("total_warning", False)
        bar_w    = int(prog)

        challenge_strip = f"""
    <div style="margin:16px 20px 0;padding:16px;background:#f8fafc;
                border-radius:6px;border:1px solid #e5e7eb;">
      <div style="font-size:10px;font-weight:700;color:#9ca3af;
                  letter-spacing:.8px;margin-bottom:12px;">FTMO CHALLENGE</div>
      <!-- 4 stats -->
      <table style="border-collapse:collapse;width:100%;margin-bottom:12px;">
        <tr>
          <td style="width:25%;text-align:center;padding:0 6px 0 0;">
            <div style="font-size:18px;font-weight:700;font-family:monospace;
                        color:#111;">${bal:,.0f}</div>
            <div style="font-size:10px;color:#9ca3af;margin-top:2px;">Balance</div>
          </td>
          <td style="width:25%;text-align:center;padding:0 6px;
                     border-left:1px solid #e5e7eb;">
            <div style="font-size:18px;font-weight:700;font-family:monospace;
                        color:{pct_color};">{pct:+.2f}%</div>
            <div style="font-size:10px;color:#9ca3af;margin-top:2px;">Profit</div>
          </td>
          <td style="width:25%;text-align:center;padding:0 6px;
                     border-left:1px solid #e5e7eb;">
            <div style="font-size:18px;font-weight:700;font-family:monospace;
                        color:#111;">
              <span style="color:#16a34a;">{wins}W</span>
              <span style="color:#d1d5db;font-size:14px;">/</span>
              <span style="color:#dc2626;">{losses}L</span>
            </div>
            <div style="font-size:10px;color:#9ca3af;margin-top:2px;">Win / Loss</div>
          </td>
          <td style="width:25%;text-align:center;padding:0 0 0 6px;
                     border-left:1px solid #e5e7eb;">
            <div style="font-size:18px;font-weight:700;font-family:monospace;
                        color:#111;">{days}<span style="font-size:12px;color:#9ca3af;">/4</span></div>
            <div style="font-size:10px;color:#9ca3af;margin-top:2px;">Trading Days</div>
          </td>
        </tr>
      </table>
      <!-- progress bar -->
      <div style="margin-bottom:6px;">
        <div style="background:#e5e7eb;border-radius:99px;height:6px;">
          <div style="background:#2563eb;border-radius:99px;height:6px;
                      width:{bar_w}%;"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:4px;">
          <span style="font-size:10px;color:#9ca3af;">{prog:.0f}% to target</span>
          <span style="font-size:10px;color:#9ca3af;">${remaining:,.0f} remaining</span>
        </div>
      </div>
      <!-- risk indicators -->
      <table style="border-collapse:collapse;width:100%;margin-top:8px;
                    border-top:1px solid #e5e7eb;padding-top:8px;">
        <tr>
          <td style="padding-top:8px;font-size:11px;color:{'#dc2626' if d_warn else '#6b7280'};">
            Daily risk used: <strong>{d_pct:.1f}%</strong>{"&nbsp;⚠" if d_warn else ""}
          </td>
          <td style="padding-top:8px;font-size:11px;color:{'#dc2626' if t_warn else '#6b7280'};
                     text-align:right;">
            Total drawdown: <strong>{t_pct:.1f}%</strong>{"&nbsp;⚠" if t_warn else ""}
          </td>
        </tr>
      </table>
    </div>"""
    except Exception:
        pass

    # ── assemble full email ────────────────────────────────────────────────────
    ts_parts = alert.get("timestamp", "").split(" ")
    ts_display = " · ".join(ts_parts[:2]) if len(ts_parts) >= 2 else alert.get("timestamp", "")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f3f4f6;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f3f4f6;padding:28px 0 36px;">
  <tr><td align="center" style="padding:0 12px;">

    <table width="540" cellpadding="0" cellspacing="0"
           style="background:#fff;border-radius:10px;overflow:hidden;
                  max-width:540px;width:100%;">

      <!-- top accent bar -->
      <tr><td style="height:4px;background:{accent_color};font-size:0;">&nbsp;</td></tr>

      <!-- header -->
      <tr><td style="padding:20px 20px 16px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <span style="font-size:15px;font-weight:700;color:#111;
                           letter-spacing:-.2px;">FTMO Agent</span>
              <span style="font-size:13px;color:#9ca3af;margin-left:8px;">
                Market Monitor
              </span>
            </td>
            <td style="text-align:right;">
              <span style="font-size:12px;color:#9ca3af;font-family:monospace;">
                {ts_display}
              </span>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- status block -->
      <tr><td style="padding:0 20px 20px;">
        <div style="border-left:3px solid {accent_color};padding:10px 14px;
                    background:#f9fafb;border-radius:0 6px 6px 0;">
          <div style="font-size:17px;font-weight:700;color:#111;margin-bottom:3px;">
            {STATUS_TITLE[dominant]}
          </div>
          <div style="font-size:13px;color:#6b7280;">
            {STATUS_SUB[dominant]}
          </div>
        </div>
      </td></tr>

      <!-- trade setup card (GO signals only) -->
      {trade_card}

      <!-- pair table -->
      <tr><td>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;border-top:1px solid #f3f4f6;">
          <thead>
            <tr style="background:#f9fafb;">
              <th style="padding:7px 0 7px 20px;text-align:left;font-size:10px;
                         color:#9ca3af;font-weight:600;letter-spacing:.5px;
                         text-transform:uppercase;">Pair</th>
              <th style="padding:7px 8px;text-align:left;font-size:10px;
                         color:#9ca3af;font-weight:600;letter-spacing:.5px;
                         text-transform:uppercase;">Signal</th>
              <th style="padding:7px 8px;text-align:left;font-size:10px;
                         color:#9ca3af;font-weight:600;letter-spacing:.5px;
                         text-transform:uppercase;">Bias</th>
              <th style="padding:7px 20px 7px 8px;text-align:left;font-size:10px;
                         color:#9ca3af;font-weight:600;letter-spacing:.5px;
                         text-transform:uppercase;">Note</th>
            </tr>
          </thead>
          <tbody>{pair_rows}</tbody>
        </table>
      </td></tr>

      <!-- extras (news warnings, lessons) -->
      {f'<tr><td style="padding:12px 0 0;">{extras}</td></tr>' if extras else ''}

      <!-- challenge mini-dashboard -->
      {f'<tr><td style="padding-bottom:20px;">{challenge_strip}</td></tr>' if challenge_strip else ''}

      <!-- footer -->
      <tr><td style="padding:14px 20px;border-top:1px solid #f3f4f6;">
        <p style="margin:0;font-size:11px;color:#d1d5db;line-height:1.6;">
          Automated signal · EMA Trend + H4 Pullback · 0.5% risk per trade<br>
          Do not trade without your own confirmation.
        </p>
      </td></tr>

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
