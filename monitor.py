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
                "contentType": "Text",
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
    """Format alert dict into a readable email body."""
    lines = [
        f"FTMO Agent — Market Alert",
        f"Time: {alert['timestamp']}",
        f"",
        f"STATUS: {alert['message']}",
        f"",
        f"PAIR SIGNALS:",
    ]
    for s in alert["signals"]:
        sig  = s.get("signal", "?")
        pair = s.get("pair", "?")
        bias = s.get("bias", "")
        note = s.get("note", "") or s.get("next_step", "")
        lines.append(f"  {pair}: {sig} ({bias}) — {note}")
        if "trade" in s:
            t = s["trade"]
            lines.append(
                f"    → Entry: {t['entry']} | SL: {t['sl']} | TP: {t['tp']} | "
                f"SL: {t['sl_pips']}p | TP: {t['tp_pips']}p | RRR: {t['rrr']}"
            )
        if s.get("news"):
            lines.append(f"    ⚠ {s['news']}")

    if alert.get("new_lessons"):
        lines += ["", "NEW TRADE LESSONS:"]
        for l in alert["new_lessons"]:
            lines.append(f"  [{l.get('pair')} {l.get('outcome')}] {l.get('lesson')}")

    lines += ["", "— FTMO Agent"]
    return "\n".join(lines)


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
