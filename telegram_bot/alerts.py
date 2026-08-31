###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import sys
import json
import logging
import datetime
import urllib.error
import urllib.parse
import urllib.request

from user_store import load_users, forget_user, rekey_user

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

def pump_recipients():
    """Who wants a note after each pump cycle. Opt-in, separate from the volume alerts.

    A different question from `alerts`: that one is "tell me when the cistern is running dry",
    this one is "tell me what every cycle cost". Someone may well want one and not the other.
    Any role, admins included — it is a report, not an operation.
    """
    return [
        chat_id
        for chat_id, user in load_users().items()
        if user.get("pump_notify") and user.get("role") and not user.get("banned")
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

## Telegram error descriptions that mean this chat will never accept a message again: the bot was
## removed, the group was deleted, or the user blocked it. Matched on the description and not on
## the status code, because a 400 also covers our own mistakes — a text over the 4096-char cap,
## say — and those must not cost us a recipient.
DEAD_CHAT_ERRORS = (
    "chat not found",
    "bot was kicked",
    "bot was blocked",
    "bot is not a member",
    "user is deactivated",
    "group chat was deactivated",
)

def post_message(url, chat_id, text):
    """Send one message. Returns (ok, description, migrate_to_chat_id).

    The description is Telegram's own wording when it answers with an error, which is the only
    thing that says whether the failure is worth retrying — the status code alone does not.
    """
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as resp:
            resp.read()
        return True, None, None
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode())
        except Exception:
            pass  # Not JSON (a proxy error page, say); fall back to the status line.
        description = body.get("description") or str(e)
        return False, description, (body.get("parameters") or {}).get("migrate_to_chat_id")
    except Exception as e:
        ## Timeout, DNS, connection reset: transient, and the chat is not to blame.
        return False, str(e), None

def send_telegram(chat_ids, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set; alert not sent.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        ok, description, migrate_to = post_message(url, chat_id, text)

        if not ok and migrate_to:
            ## Upgraded to a supergroup, hence a new id. Follow it and deliver, so the group keeps
            ## receiving its alerts instead of looking dead.
            log.warning(f"Chat {chat_id} was upgraded to a supergroup ({migrate_to}); following it.")
            rekey_user(chat_id, migrate_to)
            chat_id = migrate_to
            ok, description, _ = post_message(url, chat_id, text)

        if ok:
            log.info(f"Alert sent to chat {chat_id}.")
            continue

        if any(dead in description.lower() for dead in DEAD_CHAT_ERRORS):
            ## Left in the registry, this chat would fail on every alert forever, and the failure
            ## is only visible in the log — so a threshold alert nobody receives would look sent.
            log.warning(f"Chat {chat_id} is permanently unreachable ({description}); "
                        f"removing it from the registry. It can register again with /start.")
            forget_user(chat_id)
            continue

        log.error(f"Could not send alert to chat {chat_id}: {description}")

# -------------------------------------------------------------------------------------------------
# PUMP CYCLE NOTIFICATION

def notify_pump_run(volume_l, sigma_l, duration_s, rate_l_per_h, quality):
    """Tell the opted-in users what the cycle that just finished cost, in litres.

    Sent from sensors/pump_volume.py, which runs inside pump.service — not the bot process — so
    it goes out over the HTTP API like every other notification here.

    Sent once per run, when it is first costed, seconds after the pump stops. A run is costed a
    second time later (see pump_volume.sweep) once the reading that vouches for its closing level
    exists; that recheck deliberately does NOT notify again, because two messages about one cycle
    would be worse than a number that improves quietly.
    """
    recipients = pump_recipients()
    if not recipients:
        return

    minutes = duration_s / 60.0
    text = (f"💧 Cycle de pompage terminé ({minutes:.0f} min)\n"
            f"Eau utilisée / perdue : {volume_l:.0f} L")
    if sigma_l is not None:
        text += f" ± {sigma_l:.0f}"
        if sigma_l >= abs(volume_l):
            # Below the sensor's resolution. Saying "3 L" here would invent a precision the
            # measurement does not have, and this is the case a closed loop lands in.
            text += "\n(trop faible pour être mesuré : dans le bruit)"
    if rate_l_per_h is not None:
        text += f"\nDébit moyen : {rate_l_per_h:.0f} L/h"
    if quality == 'degraded':
        text += "\n⚠️ Mesure peu fiable."

    text += "\n\n/pertes pour l'historique, /pertes off pour arrêter ces messages."
    send_telegram(recipients, text)

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
