###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import sys
import json
import logging
import datetime
import urllib.parse
import urllib.request

from user_store import load_users

## Repo root on sys.path so the shared `common` package is importable when alerts.py is run
## as a script (python alerts.py ...); when imported by read_puit/bot it is already there.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from common.logging_setup import setup_logging

log = logging.getLogger('alerts')

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

## Volume thresholds (m³)
ALERT_LOW_VOLUME_M3      = 3.0   # "volume bas": alert once per crossing
ALERT_CRITICAL_VOLUME_M3 = 0.5   # "volume critique": daily reminder at ALERT_HOUR while below
ALERT_HOUR = 18                  # local hour for daily reminders

## A threshold re-arms (and a recovery message is sent) only once the volume is back above
## threshold + hysteresis; 0.2 m³ = 5 cm, the sensor noise band tolerated by read_puit.py.
RECOVERY_HYSTERESIS_M3 = 0.2

## Per-threshold alert state, keyed by threshold value
ALERT_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.alert_state.json')

# -------------------------------------------------------------------------------------------------
# RECIPIENTS

def alert_recipients():
    return [
        chat_id
        for chat_id, user in load_users().items()
        if user.get("alerts") and user.get("role") and not user.get("banned")
    ]

def admin_recipients():
    """Admins receive ops notifications (e.g. deploys) regardless of their
    water-level alert opt-in, which only governs the volume-threshold alerts."""
    return [
        chat_id
        for chat_id, user in load_users().items()
        if user.get("role") == "admin" and not user.get("banned")
    ]

# -------------------------------------------------------------------------------------------------
# ALERT STATE

def load_alert_state():
    try:
        with open(ALERT_STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def save_alert_state(state):
    try:
        with open(ALERT_STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        log.warning(f"Could not save {ALERT_STATE_FILE} ({e}).")

# -------------------------------------------------------------------------------------------------
# THRESHOLD LOGIC

def daily_reminder_due(last_alert_iso, now):
    """A daily reminder is due on the first check at/after ALERT_HOUR not yet alerted today."""
    if now.hour < ALERT_HOUR:
        return False
    try:
        last_alert = datetime.datetime.fromisoformat(last_alert_iso)
    except (TypeError, ValueError):
        return True
    todays_reminder = now.replace(hour=ALERT_HOUR, minute=0, second=0, microsecond=0)
    return last_alert < todays_reminder

def check_threshold(volume_m3, threshold_m3, label, daily, state):
    """Evaluate one threshold against the measured volume.

    Mutates `state` (keyed by threshold value) and returns the message to send, or None.
    """
    key = str(threshold_m3)
    entry = state.get(key, {})
    was_below = entry.get("below", False)
    now = datetime.datetime.now().astimezone()

    if volume_m3 < threshold_m3:
        crossing = not was_below
        if crossing or (daily and daily_reminder_due(entry.get("last_alert"), now)):
            state[key] = {"below": True, "last_alert": now.isoformat()}
            icon = "🚨" if daily else "⚠️"
            return (f"{icon} Puit : {label} — {volume_m3:.2f} m³ ({volume_m3 * 1000:.0f} L), "
                    f"sous le seuil de {threshold_m3:g} m³.")
        state[key] = {"below": True, "last_alert": entry.get("last_alert")}
        return None

    if was_below and volume_m3 >= threshold_m3 + RECOVERY_HYSTERESIS_M3:
        state.pop(key, None)
        return (f"✅ Puit : volume remonté à {volume_m3:.2f} m³ ({volume_m3 * 1000:.0f} L), "
                f"au-dessus du seuil de {threshold_m3:g} m³.")
    return None

def check_thresholds(volume_m3):
    """Check the measured volume against all thresholds and notify opted-in users."""
    state = load_alert_state()
    messages = []
    for threshold_m3, label, daily in (
        (ALERT_LOW_VOLUME_M3, "volume bas", False),
        (ALERT_CRITICAL_VOLUME_M3, "volume critique", True),
    ):
        message = check_threshold(volume_m3, threshold_m3, label, daily, state)
        if message:
            messages.append(message)
    save_alert_state(state)

    if messages:
        send_telegram(alert_recipients(), "\n".join(messages))

# -------------------------------------------------------------------------------------------------
# TELEGRAM SENDING

def send_telegram(chat_ids, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set; alert not sent.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as resp:
                resp.read()
            log.info(f"Alert sent to chat {chat_id}.")
        except Exception as e:
            log.error(f"Could not send alert to chat {chat_id}: {e}")

# -------------------------------------------------------------------------------------------------
# DEPLOY NOTIFICATION

def notify_deploy(old, new, changelog=""):
    """Tell admins a new version was pulled and installation finished.

    Called by deploy/deploy.sh after the pull + service restarts succeed. It sends
    over Telegram directly (not through the bot process, which is being restarted).
    """
    recipients = admin_recipients()
    if not recipients:
        log.info("No admin recipients; deploy notification not sent.")
        return
    text = f"🚀 PiJardin mis à jour et redémarré.\nVersion : {old[:7]} → {new[:7]}"
    if changelog:
        # Keep well under Telegram's 4096-char cap even with a long history.
        lines = changelog.splitlines()
        shown = lines[:15]
        text += "\n\nChangements :\n" + "\n".join(f"• {line}" for line in shown)
        if len(lines) > len(shown):
            text += f"\n… (+{len(lines) - len(shown)} de plus)"
    send_telegram(recipients, text)

# -------------------------------------------------------------------------------------------------
# MANUAL TESTING
# Run a threshold check against a fake volume, without touching the Arduino:
#   python alerts.py 2.8
# Notify admins of a deploy (changelog read from stdin, one commit per line):
#   git log --oneline OLD..NEW | python alerts.py deploy OLD NEW

if __name__ == '__main__':
    setup_logging()
    if len(sys.argv) >= 2 and sys.argv[1] == 'deploy':
        old = sys.argv[2] if len(sys.argv) > 2 else "?"
        new = sys.argv[3] if len(sys.argv) > 3 else "?"
        # Changelog on stdin keeps commit subjects out of the argv quoting mess.
        changelog = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
        notify_deploy(old, new, changelog)
    elif len(sys.argv) == 2:
        check_thresholds(float(sys.argv[1]))
    else:
        print("Usage: python alerts.py <volume_m3>")
        print("       git log --oneline OLD..NEW | python alerts.py deploy OLD NEW")
        sys.exit(1)
