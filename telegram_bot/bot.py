###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import re
import io
import sys
import html
import time
import asyncio
import logging
import datetime
import tempfile
import subprocess

from telegram import Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes

## read_puit.py lives in sensors/; the shared `common` package lives at the repo root. Both
## on sys.path so this script can import them when run directly by the service.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, 'sensors'))

import read_puit
import read_pump
import pump_volume
from common import boards
from common.logging_setup import setup_logging

## Configure logging first (stdout -> journald, line-buffered, httpx muzzled). The bot does
## NOT attach the InfluxDB handler: it is an add-on and never writes to the DB — its logs live
## in journald only (readable via /logs bot). A dead bot is caught by the systemd OnFailure hook.
setup_logging()
log = logging.getLogger(__name__)

# graph pulls in matplotlib; guard the import so a missing/broken dependency only
# disables the /graphe* commands rather than crashing the whole bot on startup.
try:
    import graph
except Exception as e:
    graph = None
    log.warning(f"graph module unavailable ({e}); /graphe* commands disabled.")

# export pulls in nothing beyond the standard library and read_puit (already imported above),
# so this guard is not about a missing dependency like graph's — it is about a broken import
# never costing the whole bot. On a Pi with no SSH the bot is the only way back in, so one
# add-on command failing to load must stay one command failing to load.
try:
    import export
except Exception as e:
    export = None
    log.warning(f"export module unavailable ({e}); /export disabled.")
from user_store import load_users, save_users
from alerts import ALERT_LOW_VOLUME_M3, ALERT_CRITICAL_VOLUME_M3

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

PASSWORD_ADMIN  = os.getenv("TELEGRAM_PASSWORD_ADMIN")
PASSWORD_VIEWER = os.getenv("TELEGRAM_PASSWORD_VIEWER")
PASSWORD_TO_ROLE = {
    password: role
    for role, password in (("admin", PASSWORD_ADMIN), ("viewer", PASSWORD_VIEWER))
    if password
}

MAX_FAILED_ATTEMPTS = 10

## How long to stay quiet after logging one transient Telegram API failure. A single 502 blip
## arrives as a burst of retries (~8 in 30 s), and /logs shows only 15 lines inline — without
## this, one upstream hiccup crowds every real message out of the window we can actually read.
TRANSIENT_ERROR_MUTE_S = 300

## What to tell the user for each firmware error code (the contract lives in read_puit.py).
## The split that matters here is "try again in a minute" vs "someone has to go to the well"
## vs "this is our bug" — the sensor is fine in only one of the three.
SENSOR_ERROR_FR = {
    'echo_timeout':         "Le capteur n'a reçu aucun écho (surface agitée ou oblique). "
                            "Réessayez dans un instant.",
    'insufficient_samples': "Trop peu de mesures valides pour être fiable. "
                            "Réessayez dans un instant.",
    'no_reply':             "L'Arduino n'a pas répondu. Réessayez ; si cela persiste, /flash.",
    'out_of_range':         "Le capteur répond mais toutes les mesures sont hors plage : "
                            "probablement mal orienté ou obstrué. Intervention nécessaire au puits.",
    'sensor_fault':         "Le capteur ne réagit plus au déclenchement : alimentation, câblage "
                            "ou module HS. Intervention nécessaire au puits.",
    'proto_mismatch':       "Le firmware de l'Arduino ne correspond pas au logiciel du Pi. "
                            "Un admin doit lancer /flash.",
    ## Historical code name (it once meant "printed no boot banner"); the condition it now
    ## reports is that the board answered nothing at all when asked to identify itself.
    'no_banner':            "L'Arduino ne répond plus du tout : carte bloquée ou firmware "
                            "incompatible. Un admin doit lancer /flash.",
}

## sensor_fault covers two faults at opposite ends of the wiring, told apart by n_stuck: echo
## held high means the trigger was never even fired (echo side), no stuck pings means triggers
## went out and nothing answered (power / module / trigger side). Keyed by whether n_stuck is
## non-zero; absent (firmware before 2.1.0) falls back to the undifferentiated text above
## rather than guessing a side.
SENSOR_FAULT_FR = {
    True:  "La ligne echo reste haute : aucun déclenchement n'est émis. À vérifier côté "
           "echo (D7). Intervention nécessaire au puits.",
    False: "Les déclenchements sont émis mais le capteur ne répond jamais : alimentation, "
           "module HS ou ligne trigger (D8). Intervention nécessaire au puits.",
}

def sensor_error_text(error):
    """The French explanation for a PuitError, refined by the response when it says more."""
    if error.code == 'sensor_fault':
        stuck = error.resp.get('n_stuck')
        if stuck is not None:
            return SENSOR_FAULT_FR[bool(stuck)]
    return SENSOR_ERROR_FR.get(error.code, f"Mesure impossible ({error.code}).")

## Journal units readable via /logs — fixed whitelist, never user-supplied unit names.
LOG_UNITS = {
    "bot": "telegram-bot.service",
    "sensors": "sensors.service",
    "deploy": "deploy.service",
    "pump": "pump.service",
}
LOG_DEFAULT_LINES = 15   # compact default tail when no line count / time range is given
LOG_MESSAGE_MAX_LINES = 15  # above this many lines, deliver as a .txt file instead of a message

## Secret env values that must never reach Telegram in a /logs dump.
LOG_SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN", "INFLUXDB_TOKEN",
    "TELEGRAM_PASSWORD_ADMIN", "TELEGRAM_PASSWORD_VIEWER",
)

def redact_secrets(text):
    """Strip known/likely secrets from journal text before it is sent to Telegram.

    Logs can contain the bot token (httpx URLs) or the InfluxDB token (a client traceback),
    which must never leave the Pi. We replace the exact secret values we hold at runtime, plus
    two token-shaped patterns as defense in depth. This is a strong mitigation, not a proof:
    it cannot catch an unknown secret embedded in arbitrary third-party output.
    """
    for var in LOG_SECRET_ENV_VARS:
        value = os.getenv(var)
        if value:
            text = text.replace(value, "***")
    # Telegram bot-token shape (<digits>:<35+ url-safe chars>) and token=... query params.
    text = re.sub(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b", "***", text)
    text = re.sub(r"(?i)(token=)[^&\s]+", r"\1***", text)
    return text

def build_logs_payload(output, unit_alias, header):
    """Decide how to deliver a journal tail to Telegram.

    Returns either {'kind': 'message', 'text': <html>} for a compact tail, or
    {'kind': 'file', 'data': bytes, 'filename': str, 'caption': str} otherwise. A message is
    kept compact — at most LOG_MESSAGE_MAX_LINES lines and within Telegram's 4096-char cap
    (checked on the escaped+tagged text so '<'/'&'/'>' can't break parsing or overflow);
    anything larger is sent as a .txt file.
    """
    body = f"<b>{html.escape(header)}</b>\n<pre>{html.escape(output)}</pre>"
    if len(output.splitlines()) <= LOG_MESSAGE_MAX_LINES and len(body) <= 4096:
        return {"kind": "message", "text": body}
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    return {"kind": "file", "data": output.encode("utf-8"),
            "filename": f"{unit_alias}_{stamp}.log", "caption": header}

## How far back /export reaches by default. The dashboard opens on `now-7d`, so a shorter
## export leaves « Historique semaine » visibly truncated when it is replayed — which reads as
## a hole in the data rather than as a narrow request.
EXPORT_DEFAULT_WINDOW = datetime.timedelta(days=8)

## And the most it will reach. Not a guess at what is useful: the export queries InfluxDB one
## day at a time, so the window sets the number of round trips, and a fat-fingered `/export
## 3650d` would put 21 900 queries through influxd on a Pi 3 that is already swapping.
EXPORT_MAX_WINDOW = datetime.timedelta(days=90)

## Duration suffixes /export accepts. Seconds are deliberately absent — `range()` would be
## narrower than the 5-minute cadence and always come back empty, which looks like a fault.
EXPORT_UNITS = {'m': 'minutes', 'h': 'hours', 'd': 'days'}

def parse_export_window(token):
    """`30m` / `12h` / `8d` as a timedelta, or None if the token is not a duration."""
    match = re.fullmatch(r"(\d+)([mhd])", token.lower())
    if not match:
        return None
    return datetime.timedelta(**{EXPORT_UNITS[match.group(2)]: int(match.group(1))})

def export_window_label(window):
    """The window as a short filename-safe label, at its coarsest exact unit."""
    if window.days and not window.seconds:
        return f"{window.days}d"
    hours = window.days * 24 + window.seconds // 3600
    if hours and not window.seconds % 3600:
        return f"{hours}h"
    return f"{window.days * 1440 + window.seconds // 60}m"

# -------------------------------------------------------------------------------------------------
# STARTUP CHECK

if not TOKEN:
    log.info(
        "TELEGRAM_BOT_TOKEN not set in .env — bot is not configured. "
        "Add the token to .env, then restart the service: "
        "sudo systemctl restart telegram-bot.service"
    )
    sys.exit(0)

if not PASSWORD_TO_ROLE:
    log.warning(
        "Neither TELEGRAM_PASSWORD_ADMIN nor TELEGRAM_PASSWORD_VIEWER is set — "
        "self-registration via /start will reject everyone."
    )

# -------------------------------------------------------------------------------------------------
# COMMAND HANDLERS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    users = load_users()
    previous = users.get(chat_id, {})

    if previous.get("banned"):
        log.info(f"Ignored /start from banned chat {chat_id}.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /start <password>")
        return

    role = PASSWORD_TO_ROLE.get(context.args[0])
    if role is None:
        fails = previous.get("fails", 0) + 1
        if fails >= MAX_FAILED_ATTEMPTS:
            previous["fails"] = fails
            previous["banned"] = True
            users[chat_id] = previous
            save_users(users)
            log.warning(f"Banned chat {chat_id} after {fails} failed password attempts.")
            await update.message.reply_text("Too many failed attempts. You have been blocked.")
            return

        previous["fails"] = fails
        users[chat_id] = previous
        save_users(users)
        log.info(f"Rejected registration attempt from chat {chat_id}: invalid password ({fails}/{MAX_FAILED_ATTEMPTS}).")
        await update.message.reply_text("Invalid password.")
        return

    users[chat_id] = {
        "role": role,
        "username": update.effective_user.username or update.effective_user.first_name,
        "alerts": previous.get("alerts", False),
    }
    save_users(users)

    if previous.get("role") and previous["role"] != role:
        await update.message.reply_text(f"Role updated: {previous['role']} → {role}.")
    else:
        await update.message.reply_text(f"Registered as {role}.")

async def alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    users = load_users()
    user = users.get(chat_id)

    if user is None:
        await update.message.reply_text("You need to register first: /start <password>")
        return

    thresholds = (
        f"Seuils: {ALERT_LOW_VOLUME_M3:g} m³ (une fois) "
        f"et {ALERT_CRITICAL_VOLUME_M3:g} m³ (rappel quotidien)."
    )

    if not context.args:
        status = "on" if user.get("alerts") else "off"
        await update.message.reply_text(
            f"Alerts are currently {status}. Usage: /alertes on|off\n{thresholds}"
        )
        return

    choice = context.args[0].lower()
    if choice not in ("on", "off"):
        await update.message.reply_text("Usage: /alertes on|off")
        return

    user["alerts"] = (choice == "on")
    save_users(users)
    text = f"Alerts turned {choice}."
    if choice == "on":
        text += f"\n{thresholds}"
    await update.message.reply_text(text)

async def measure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    # Any registered role (admin or viewer) may measure; banned entries have no role.
    if user is None or user.get("banned") or not user.get("role"):
        await update.message.reply_text("You need to register first: /start <password>")
        return

    msg = await update.message.reply_text("Mesure en cours, patience")

    try:
        height, resampled, db_ok = await asyncio.to_thread(read_puit.measure_once, 20)
    except TimeoutError:
        await msg.edit_text("Une mesure ordinaire est déjà en cours, veuillez recommencer plus tard")
        return
    except read_puit.PuitError as e:
        # The firmware says *why* it could not measure; relay that instead of a generic
        # failure, since what the user should do about it differs per code.
        log.warning(f"/mesure failed for chat {chat_id}: {e}")
        await msg.edit_text(f"❌ {sensor_error_text(e)}")
        return
    except Exception as e:
        log.warning(f"/mesure failed for chat {chat_id}: {e}")
        await msg.edit_text(f"Measurement failed: {e}")
        return

    volume = read_puit.height_to_volume(height)
    text = f"Volume: {volume:.2f} m³ ({volume * 1000:.0f} L)"
    if resampled:
        # Derived from the constants rather than written out: this line used to claim "5
        # échantillons" long after the real figure had moved, because nothing tied the two
        # together. `resampled` means the extra round was taken — for a moving surface or an
        # implausible jump — so the reading rests on that many bursts instead of BURSTS.
        text += (f"\n(surface agitée ou saut inattendu : médiane élargie à "
                 f"{read_puit.BURSTS + read_puit.EXTRA_BURSTS} salves au lieu de "
                 f"{read_puit.BURSTS})")
    if not db_ok:
        text += "\n⚠️ Could not write to InfluxDB — value NOT recorded in the database."
    await msg.edit_text(text)

## How each pump state reads to a human. `fault` is deliberately not "arrêtée": the module
## stopped reporting, so the pump's state is unknown — and a disconnected signal wire and a
## stopped pump produce the same low reading, which is exactly the confusion to avoid here.
PUMP_STATE_TEXT = {
    "on":      "🟢 La pompe tourne",
    "off":     "⚪ La pompe est arrêtée",
    "fault":   "🚨 Capteur en défaut — état de la pompe inconnu (câble de signal, module)",
    "unknown": "❔ État inconnu (la carte vient de démarrer)",
}

def _format_duration_fr(seconds):
    """A duration in French, at the coarsest unit that still says something useful."""
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {(seconds % 3600) // 60:02d}"
    return f"{seconds // 86400} j {(seconds % 86400) // 3600} h"

async def pompe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report what the pump is doing, from the listener's own record.

    Reads the state file pump.service writes after every recorded line — it never opens the
    serial port, and could not: the listener holds that port open for its whole life. So this
    answers instantly, works while the board is unplugged, and cannot disturb the detector.

    The cost is that it reports a memory rather than a fresh observation, which is why `age`
    is shown whenever it is stale: a state nobody has confirmed for minutes is exactly the
    case where the number on its own would mislead.
    """
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    # Any registered role (admin or viewer); banned entries have no role.
    if user is None or user.get("banned") or not user.get("role"):
        await update.message.reply_text("You need to register first: /start <password>")
        return

    info = await asyncio.to_thread(read_pump.current_state)
    if info is None:
        await update.message.reply_text(
            "Aucune donnée de pompe enregistrée pour l'instant.\n"
            "Le service vient peut-être de démarrer, ou la carte n'est pas détectée "
            "(voir /boards et /logs pump).")
        return

    text = PUMP_STATE_TEXT.get(info["state"], f"État : {info['state']}")
    if info["since_s"] is not None:
        text += f" depuis {_format_duration_fr(info['since_s'])}"

    if info["stale"]:
        text += (f"\n\n⚠️ Dernier signe de vie de la carte il y a "
                 f"{_format_duration_fr(info['age_s'])} — l'état ci-dessus est le dernier "
                 f"connu, pas une mesure actuelle. Voir /logs pump.")
    await update.message.reply_text(text)

## How far back /pertes computes. Wider than the sweep read_pump triggers on each pump stop,
## because this one is the catch-up path: it is what answers for the runs that happened while
## the volume code was not deployed yet, or during a stretch when nothing swept. A week of
## height_measure is a couple of thousand points — cheap, and it only ever writes what is
## genuinely missing.
PERTES_SWEEP_S = 7 * 24 * 3600

## How the reliability of a costed run reads to someone who did not write the estimator.
PERTES_QUALITY_FR = {
    'ok':       '',
    'coarse':   ' ≈',
    'degraded': ' ⚠️',
}

async def pertes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """How much water each of the last pump cycles used or lost.

    Sweeps first, then reads. The sweep is what makes this answer on a Pi where the pump has
    not run since the last deploy: costing a run is idempotent, so calling it here computes
    whatever is outstanding and does nothing at all once everything is up to date. It is also
    the manual way to close the one gap in triggering off pump stops — nothing runs while the
    pump is idle.
    """
    chat_id = str(update.effective_chat.id)
    users = load_users()
    user = users.get(chat_id)

    # Any registered role (admin or viewer); banned entries have no role.
    if user is None or user.get("banned") or not user.get("role"):
        await update.message.reply_text("You need to register first: /start <password>")
        return

    # /pertes on|off toggles the per-cycle message; /pertes on its own shows the history. One
    # command per subject, the same shape as /alertes.
    if context.args:
        choice = context.args[0].lower()
        if choice not in ("on", "off"):
            await update.message.reply_text("Usage: /pertes [on|off]")
            return
        user["pump_notify"] = (choice == "on")
        save_users(users)
        await update.message.reply_text(
            "Notifications de pompage activées : un message après chaque cycle."
            if choice == "on" else
            "Notifications de pompage désactivées.")
        return

    try:
        await asyncio.to_thread(pump_volume.sweep, PERTES_SWEEP_S)
    except Exception as e:
        # Not fatal to the command: whatever was already costed is still worth showing.
        log.warning(f"/pertes could not sweep: {e}")

    try:
        rows = await asyncio.to_thread(pump_volume.query_recent, 5)
    except Exception as e:
        log.error(f"/pertes could not read volumes: {e}")
        await update.message.reply_text("Impossible de lire les volumes (voir /logs bot).")
        return

    if not rows:
        await update.message.reply_text(
            "Aucun cycle de pompage chiffré pour l'instant.\n"
            "Le volume est mesuré à partir du niveau du puits de part et d'autre d'un cycle, "
            "il faut donc qu'une pompe ait tourné depuis l'installation. Voir /pompe et "
            "/logs pump.")
        return

    lines = ["💧 <b>Eau utilisée / perdue</b> — derniers cycles\n"]
    for row in reversed(rows):                      # newest first for a phone screen
        when = row['time'].astimezone().strftime('%d/%m %H:%M')
        volume, sigma = row.get('volume_l'), row.get('volume_sigma_l')
        rate, duration = row.get('rate_l_per_h'), row.get('duration_s')

        if volume is None:
            continue
        # Round a hair below zero to zero rather than printing "-0 L", which reads as a bug.
        # A tiny negative is normal: the level moved less than the sensor can resolve.
        if abs(volume) < 0.5:
            volume = 0.0
        detail = f"{volume:.0f} L"
        if sigma is not None:
            detail += f" ± {sigma:.0f}"
            if sigma >= abs(volume):
                # Saying "5 L" when the method cannot resolve 5 L is the one way this display
                # could actively mislead.
                detail += " (dans le bruit)"
        if rate is not None:
            detail += f", {rate:.0f} L/h"

        lines.append(f"• {when} — {_format_duration_fr(duration)} : {detail}"
                     f"{PERTES_QUALITY_FR.get(row.get('quality'), '')}")

    state = "activées" if user.get("pump_notify") else "désactivées"
    lines.append(f"\n<i>≈ mesure approximative, ⚠️ peu fiable. En circuit fermé, ce chiffre "
                 f"est la perte du circuit.\nNotifications par cycle : {state} "
                 f"(/pertes on|off).</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def send_graph(update: Update, context: ContextTypes.DEFAULT_TYPE, range_start, title, autoscale_y=False):
    """Query the volume history for `range_start` and reply with a rendered chart."""
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    # Any registered role (admin or viewer) may request a graph; banned entries have no role.
    if user is None or user.get("banned") or not user.get("role"):
        await update.message.reply_text("You need to register first: /start <password>")
        return

    if graph is None:
        await update.message.reply_text("Graphique indisponible (dépendance manquante).")
        return

    msg = await update.message.reply_text("Génération du graphique, patience")

    try:
        times, volumes = await asyncio.to_thread(read_puit.query_volume_history, range_start)
    except Exception as e:
        log.warning(f"graph query failed for chat {chat_id}: {e}")
        await msg.edit_text(f"Could not read history: {e}")
        return

    if not times:
        await msg.edit_text("Aucune donnée sur cette période.")
        return

    png = await asyncio.to_thread(graph.render_volume_chart, times, volumes, title, autoscale_y)
    await update.message.reply_photo(photo=png, caption=title)
    await msg.delete()

def format_sampling(resp):
    """Render one measurement burst, ping by ping, as a diagnostic block.

    Works on a failed burst too: the four counts and the effective parameters are on every
    measurement reply, and they are what says whether the sensor is silent, blind, or aimed
    at the wrong thing. The per-ping detail only exists on a successful one.

    Every array is read defensively, which also covers a board still on firmware < 2.3.0: the
    per-ping arrays arrived on the ordinary reply with that version, and before it this block
    degrades to the counts rather than breaking.
    """
    lines = []
    if resp.get('status') == 'ok':
        lines.append(f"Médiane : {resp['value']:.1f} cm  "
                     f"(min {resp['min']:.1f} / max {resp['max']:.1f}, "
                     f"écart {resp['spread']:.1f})")
    else:
        lines.append(f"Échec : {resp.get('code', '?')}"
                     + (f" (champ {resp['field']})" if 'field' in resp else ""))

    # n_stuck is a subset of "sans réponse", not a fifth bucket, so it is shown inside it —
    # the four counts must still visibly add up to n.
    no_response = f"{resp.get('n_no_response')} sans réponse"
    if resp.get('n_stuck'):
        no_response += f" (dont {resp['n_stuck']} bloqué{'s' if resp['n_stuck'] > 1 else ''})"
    lines.append(f"Pings : {resp.get('n_valid')} valides · {resp.get('n_timeout')} sans écho · "
                 f"{resp.get('n_rejected')} hors plage · {no_response} (sur {resp.get('n')})")
    if resp.get('ping_status'):
        lines.append(f"Détail : {resp['ping_status']}   "
                     "(V valide · R hors plage · T sans écho · N sans réponse · "
                     "S echo bloqué haut)")

    samples_cm = resp.get('samples')
    if isinstance(samples_cm, list):
        lines.append("cm : " + ", ".join('—' if s is None else f"{s:.1f}" for s in samples_cm))
    # `pulses_us`, plural: `pulse_us` on the same reply is the scalar median, the raw
    # counterpart of `value`. Separate keys because InfluxDB stores that scalar as a float and
    # would refuse the field for ever if it ever arrived as an array.
    pulses = resp.get('pulses_us')
    if isinstance(pulses, list):
        lines.append("µs : " + ", ".join('—' if p is None else f"{p:.0f}" for p in pulses))
    # Present for the T pings too, unlike the two arrays above: a dash in µs beside a number
    # here reads as "le module a répondu, mais n'a rien vu" — no data, yet alive.
    acks = resp.get('ack_us')
    if isinstance(acks, list):
        lines.append("ack µs : " + ", ".join('—' if a is None else f"{a:.0f}" for a in acks))

    lines.append(f"Fenêtre {resp.get('min_cm')}–{resp.get('max_cm')} cm · {resp.get('temp_c')} °C · "
                 f"timeout {resp.get('timeout_us')} µs")
    # The module's reaction time against the deadline it is judged by. Absent before fw 2.1.0.
    if resp.get('ack_max_us') is not None:
        lines.append(f"Réaction : {resp['ack_max_us']} µs max "
                     f"(limite {resp.get('ack_timeout_us')} µs)")
    return "\n".join(lines)

async def samples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay one measurement burst, ping by ping — admin sensor diagnostics."""
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    if user is None or user.get("banned") or user.get("role") != "admin":
        await update.message.reply_text("Admin only.")
        return

    # Order-independent tokens: a bare integer is the ping count, `ack <µs>` widens the wait
    # for echo to rise. Both are passed through unvalidated on purpose — the board clamps out
    # of range values and echoes what it actually used, so a clamp shows up in the reply
    # instead of being argued about here.
    usage = "Usage: /echantillons [N] [ack <µs>]"
    n = ack_timeout_us = None
    args = list(context.args)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.isdigit():
            n = int(arg)
        elif arg.lower() == "ack" and i + 1 < len(args) and args[i + 1].isdigit():
            i += 1
            ack_timeout_us = int(args[i])
        else:
            await update.message.reply_text(usage)
            return
        i += 1

    msg = await update.message.reply_text("Lecture des échantillons bruts, patience")

    try:
        resp = await asyncio.to_thread(read_puit.raw_samples_once, 20, n, ack_timeout_us)
    except TimeoutError:
        await msg.edit_text("Une mesure ordinaire est déjà en cours, veuillez recommencer plus tard")
        return
    except read_puit.PuitError as e:
        # A failed burst is a diagnostic result, not an error to swallow: show the counts
        # the board reported (they are on every measurement reply) when we have them.
        log.warning(f"/echantillons failed for chat {chat_id}: {e}")
        detail = format_sampling(e.resp) if e.resp.get('n') is not None else str(e)
        await msg.edit_text(f"❌ {sensor_error_text(e)}\n\n{detail}")
        return
    except Exception as e:
        log.warning(f"/echantillons failed for chat {chat_id}: {e}")
        await msg.edit_text(f"Sampling failed: {e}")
        return

    await msg.edit_text(format_sampling(resp))

async def flash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flash the committed firmware.bin onto the Arduino — admin-only ops action.

    Runs arduino/flash_firmware.py as a subprocess (the same entry point the deploy
    migration uses) and relays its output. The script stops sensors.timer and takes
    the serial-port flock itself, so a concurrent /mesure just waits."""
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    if user is None or user.get("banned") or user.get("role") != "admin":
        await update.message.reply_text("Admin only.")
        return

    msg = await update.message.reply_text("Flash du firmware Arduino en cours (~1 min), patience")

    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "arduino", "flash_firmware.py")
    try:
        # sys.executable is the venv python (the service runs the bot with it).
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, script],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        await msg.edit_text(
            "Flash timed out; the board may be in the bootloader. /flash again or check /logs bot.")
        return
    except Exception as e:
        log.warning(f"/flash failed to start for chat {chat_id}: {e}")
        await msg.edit_text(f"Flash failed to start: {e}")
        return

    output = (result.stdout + result.stderr).strip() or f"(exited {result.returncode}, no output)"
    # Plain text (no Markdown: bossac output would break entity parsing); chunk under
    # Telegram's 4096-char cap. The final FLASH OK|WARN|FAIL line lands in the last chunk.
    chunks = [output[i:i + 4000] for i in range(0, len(output), 4000)]
    await msg.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await update.message.reply_text(chunk)

def _format_port(p):
    """One line describing a USB serial port, as `describe()` reports it."""
    ids = f"{p['vid']:04x}:{p['pid']:04x}" if p['vid'] is not None else "?"
    product = html.escape(p['product'] or '?')
    manufacturer = html.escape(p['manufacturer'] or '?')
    return f"  <code>{html.escape(p['device'])}</code>  {ids}  {manufacturer} / {product}"

async def board_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report which board resolves to which device node. Admin-only diagnostics.

    Reads the USB layer only and never opens a port, so it is safe to run at any time,
    including during a measurement — opening would assert DTR and reset the board. That
    restriction is also what makes it useful: the SAMD21 USB stack is interrupt-driven, so a
    board whose main loop has hung still shows up here, which is what separates "wedged" from
    "unplugged". Firmware-level detail (fw, proto) comes from /mesure and /logs instead.
    """
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    if user is None or user.get("banned") or user.get("role") != "admin":
        await update.message.reply_text("Admin only.")
        return

    lines = []
    for board in boards.boards():
        info = boards.describe(board)
        lines.append(f"<b>{html.escape(board)}</b> — attendu : "
                     f"<code>{html.escape(info['usb_product'])}</code>")

        if info['error']:
            lines.append(f"  ❌ {html.escape(info['error'])}")
        elif info['fallback_used']:
            lines.append(f"  ⚠️ <code>{html.escape(info['device'])}</code> — repli : aucun "
                         f"périphérique n'annonce cette chaîne. Reflasher la carte.")
        else:
            lines.append(f"  ✅ <code>{html.escape(info['device'])}</code>")

        lines.extend(_format_port(p) for p in info['matches'])

        # Only worth listing what else is on the bus when the board was not found by its
        # descriptor — that is when knowing what *is* connected actually helps.
        if (info['error'] or info['fallback_used']) and info['others']:
            lines.append("  <i>Autres ports USB série :</i>")
            lines.extend(_format_port(p) for p in info['others'])
        lines.append("")

    await update.message.reply_text("\n".join(lines).strip(), parse_mode="HTML")

async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply with the tail of a service journal. Admin-only remote diagnostics."""
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    if user is None or user.get("banned") or user.get("role") != "admin":
        await update.message.reply_text("Admin only.")
        return

    usage = (f"Usage: /logs [{'|'.join(LOG_UNITS)}] [N | 2h|30m|3d] "
             "[since <t>] [until <t>]")

    # Order-independent tokens: a unit alias, a bare integer (line count), a duration like
    # 2h/30m/3d (relative window), or `since`/`until` each followed by a time token.
    unit_alias = "bot"
    lines = LOG_DEFAULT_LINES
    since = until = None
    args = list(context.args)
    i = 0
    while i < len(args):
        arg = args[i]
        low = arg.lower()
        if low in LOG_UNITS:
            unit_alias = low
        elif arg.isdigit():
            lines = int(arg)
        elif re.fullmatch(r"\d+[smhd]", low):
            since = f"-{low}"  # systemd reads "-2h" as "2 hours ago"; s/m/h/d = sec/min/hour/day
        elif low in ("since", "until") and i + 1 < len(args):
            i += 1
            value = args[i]
            # A bare duration means "ago"; otherwise pass the token through (HH:MM, YYYY-MM-DD…).
            value = f"-{value.lower()}" if re.fullmatch(r"\d+[smhd]", value.lower()) else value
            if low == "since":
                since = value
            else:
                until = value
        else:
            await update.message.reply_text(usage)
            return
        i += 1

    # Build the argument list (no shell → --since/--until values can't inject). A time range
    # shows the whole window; otherwise fall back to the last `lines` entries.
    cmd = ["journalctl", "-u", LOG_UNITS[unit_alias], "--no-pager", "--no-hostname",
           "-o", "short-iso"]
    if since or until:
        if since:
            cmd += ["--since", since]
        if until:
            cmd += ["--until", until]
        window = " → ".join(x for x in (since, until) if x) if (since and until) else (since or until)
        header = f"{LOG_UNITS[unit_alias]} — {window}"
    else:
        cmd += ["-n", str(lines)]
        header = f"{LOG_UNITS[unit_alias]} — last {lines} lines"

    try:
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=15,
        )
        # On permission problems journalctl often exits 0 with a hint on stderr;
        # show whatever it produced, it is the diagnostic.
        output = (result.stdout.strip() or result.stderr.strip()
                  or f"(journalctl exited {result.returncode} with no output)")
    except Exception as e:
        await update.message.reply_text(f"Could not read journal: {e}")
        return

    # Strip secrets, then render: a monospace message if it fits, else a .txt file attachment.
    output = redact_secrets(output)
    payload = build_logs_payload(output, unit_alias, header)
    if payload["kind"] == "message":
        await update.message.reply_text(payload["text"], parse_mode="HTML")
    else:
        buf = io.BytesIO(payload["data"])
        buf.name = payload["filename"]
        await update.message.reply_document(
            document=buf, filename=payload["filename"], caption=payload["caption"])

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the measurements as gzipped line protocol. Admin-only remote diagnostics.

    The point of it is that the file reloads into any InfluxDB, and `grafana/` in this repo
    provisions the real dashboard against it — same panel JSON, same Flux, same datasource uid.
    So this is how those panels get looked at from off the Pi, without reimplementing them and
    without a picture that could disagree with what the data actually says.

    Admin-only because it is a dump of the whole bucket, not a reading from it.
    """
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    if user is None or user.get("banned") or user.get("role") != "admin":
        await update.message.reply_text("Admin only.")
        return

    if export is None:
        await update.message.reply_text("Export indisponible (voir /logs bot).")
        return

    usage = "Usage: /export [30m|12h|8d] [nohb]"

    # Order-independent tokens, the same shape as /logs: a duration sets the window, `nohb`
    # drops the pump heartbeats.
    window = EXPORT_DEFAULT_WINDOW
    include_heartbeats = True
    for arg in context.args:
        if arg.lower() == "nohb":
            include_heartbeats = False
            continue
        parsed = parse_export_window(arg)
        if parsed is None:
            await update.message.reply_text(usage)
            return
        window = parsed

    if not window or window > EXPORT_MAX_WINDOW:
        await update.message.reply_text(
            f"Fenêtre hors limites (1 minute à {EXPORT_MAX_WINDOW.days} jours).")
        return

    label = export_window_label(window)
    msg = await update.message.reply_text(
        f"Export sur {label} en cours, patience (quelques dizaines de secondes)")

    ## A temporary file, not an in-memory buffer. export.py streams and compresses record by
    ## record precisely so nothing grows with the window asked for, and collecting the result
    ## into a BytesIO here would put that straight back — up to the 45 MB cap, on a Pi with
    ## ~380 MB available. Debian leaves /tmp on disk (systemd's tmp.mount is masked), so this
    ## really is off the heap, and TemporaryFile unlinks itself on close.
    with tempfile.TemporaryFile() as sink:
        try:
            counts = await asyncio.to_thread(
                export.build_export, sink, window, include_heartbeats, redact_secrets)
        except export.ExportTooLarge as e:
            await msg.edit_text(
                f"Export trop volumineux ({e}).\nRéduisez la fenêtre, ou ajoutez "
                "<code>nohb</code> pour retirer les battements de la pompe — ils dominent le "
                "volume (un par minute, contre une mesure toutes les 5 min).",
                parse_mode="HTML")
            return
        except Exception as e:
            log.warning(f"/export failed for chat {chat_id}: {e}")
            await msg.edit_text(f"Export impossible : {e}")
            return

        total = sum(counts.values())
        if not total:
            await msg.edit_text(f"Aucune donnée sur les {label} écoulés.")
            return

        size = sink.tell()
        sink.seek(0)

        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        filename = f"pijardin_{label}_{stamp}.lp.gz"
        caption = (f"{total} lignes · {size / 1024 / 1024:.1f} Mo\n"
                   f"{export.format_counts(counts)}")
        if not include_heartbeats:
            # Worth saying on the file itself: a week later nothing in it explains why the pump
            # trace has gaps, and the README is explicit that without the heartbeats "pump off"
            # and "board dead" are the same picture.
            caption += "\n⚠️ sans les battements de pompe"

        ## An explicit write timeout, unlike the .txt files /logs sends. Those are a few kB;
        ## this is megabytes going up a domestic uplink, where python-telegram-bot's default
        ## (a handful of seconds) expires mid-upload and reports a network error for what is
        ## really a file doing exactly what it should. Generous rather than tuned: the handler
        ## is async, so a slow upload costs this command time and no other.
        await update.message.reply_document(
            document=sink, filename=filename, caption=caption, write_timeout=600)

    await msg.delete()

## /help text (HTML). Literal <, >, & are pre-escaped; examples go in <pre> blocks. The admin
## section is appended only for admins.
HELP_GENERAL = (
    "<b>Commandes PiJardin</b>\n"
    "\n"
    "<b>/mesure</b> — mesure le niveau du puits maintenant (alias <b>/measure</b>)\n"
    "<b>/pompe</b> — la pompe tourne-t-elle, et depuis combien de temps ; signale si la "
    "carte ne donne plus signe de vie (alias <b>/pump</b>)\n"
    "<b>/pertes</b> [on|off] — eau utilisée ou perdue lors des derniers cycles de pompage, "
    "mesurée sur le niveau du puits (en circuit fermé : les pertes du circuit). "
    "<b>on</b>/<b>off</b> active ou coupe le message envoyé après chaque cycle\n"
    "<b>/alertes</b> [on|off] — active/désactive les alertes de volume bas ; "
    "sans argument, affiche l'état actuel et les seuils\n"
    "<b>/graphe24h</b> · <b>/graphe3j</b> · <b>/graphe7j</b> — graphique du volume "
    "sur 24 h / 3 jours / 7 jours\n"
    "<b>/help</b> — affiche cette aide\n"
    "\n"
    "<b>Inscription</b>\n"
    "<b>/start</b> &lt;mot de passe&gt; — s'enregistrer ; le mot de passe détermine le rôle "
    "(admin ou viewer)\n"
    "<pre>/start monMotDePasse</pre>"
)

HELP_ADMIN = (
    "\n<b>Admin</b>\n"
    "<b>/echantillons</b> [N] [ack &lt;µs&gt;] — un tir de mesures ping par ping : médiane, "
    "compteurs (valides / sans écho / hors plage / sans réponse, dont bloqués), détail par "
    "ping et temps de réaction du module (diagnostic capteur)\n"
    "<pre>/echantillons              (tir standard)\n"
    "/echantillons 25           (25 pings)\n"
    "/echantillons 5 ack 60000  (fenêtre de réaction élargie :\n"
    "                            si un sensor_fault redevient\n"
    "                            valide, c'est la fenêtre qui\n"
    "                            était trop serrée, pas le capteur)</pre>"
    "<b>/flash</b> — reflashe le firmware Arduino (~1 min)\n"
    "<b>/boards</b> — quelle carte est branchée sur quel port : chaîne USB attendue, port "
    "résolu, et repli éventuel. N'ouvre jamais le port, donc sans risque pendant une mesure ; "
    "une carte figée apparaît quand même comme présente\n"
    "<b>/export</b> [30m|12h|8d] [nohb] — les mesures en line protocol gzippé, à recharger "
    "dans un InfluxDB local pour rejouer le vrai dashboard Grafana hors du Pi. Par défaut "
    "8 jours (le dashboard s'ouvre sur 7). <b>nohb</b> retire les battements de la pompe, qui "
    "dominent le volume du fichier — au prix de ne plus distinguer « pompe arrêtée » de "
    "« carte muette »\n"
    "<pre>/export            (8 derniers jours)\n"
    "/export 30d        (30 jours)\n"
    "/export 12h nohb   (12 h, sans les battements)</pre>"
    "<b>/logs</b> [bot|sensors|deploy|pump] [N | 2h|30m|3d] [since &lt;t&gt;] [until &lt;t&gt;] — "
    "journaux d'un service ; compact (≤ 15 lignes) en message, sinon en fichier .txt\n"
    "<pre>/logs                       (15 dernières lignes du bot)\n"
    "/logs sensors 50           (50 dernières lignes)\n"
    "/logs bot 2h               (2 dernières heures)\n"
    "/logs pump 30              (écoute de la pompe)\n"
    "/logs deploy since 10:00 until 11:00</pre>"
)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List the available commands, tailored to the caller's role."""
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id) or {}

    text = HELP_GENERAL
    if user.get("role") == "admin" and not user.get("banned"):
        text += HELP_ADMIN
    await update.message.reply_text(text, parse_mode="HTML")

async def graphe24h(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_graph(update, context, "-24h", "Historique 24 heures", autoscale_y=True)

async def graphe3j(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_graph(update, context, "-3d", "Historique 3 jours")

async def graphe7j(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_graph(update, context, "-7d", "Historique 7 jours")

# -------------------------------------------------------------------------------------------------
# ERROR HANDLING

## Last transient failure logged, so a burst collapses to one line: the exception class, how
## many further ones were swallowed, and when to speak up again.
_transient = {'name': None, 'suppressed': 0, 'mute_until': 0.0}

def _is_transient(error):
    """True for failures python-telegram-bot's own retry loop recovers from unaided.

    `BadRequest` subclasses `NetworkError` in python-telegram-bot but means the opposite
    thing — the API rejected *our* request (bad parse_mode, oversized message, stale chat
    id) — so it is excluded here and keeps its traceback. `Conflict` and `InvalidToken` are
    not transient either and fall through to the same treatment.
    """
    if isinstance(error, BadRequest):
        return False
    return isinstance(error, (NetworkError, TimedOut, RetryAfter))

def _describe_update(update):
    """Short, safe identifier for whatever was being handled when an error surfaced.

    Only the chat id and the command word: the rest of the message is deliberately dropped
    because `/start <password>` would otherwise write a live password into the journal.
    """
    if not isinstance(update, Update):
        return "a polling cycle (no update)"
    chat = update.effective_chat
    text = (update.message.text or '') if update.message else ''
    command = text.split(maxsplit=1)[0] if text else '(no text)'
    return f"chat {chat.id if chat else '?'} {command}"

async def on_error(update, context):
    """Log Telegram errors without burying the journal in stack traces.

    Without an error handler registered, python-telegram-bot logs every unhandled exception
    with full `exc_info`. One transient Telegram 502 arrives as a burst of retries, so a
    single upstream blip became ~200 lines of traceback — and since `/logs` is the only way
    into this Pi and shows 15 lines inline, that reliably hid whatever was actually wrong.

    Transient API failures therefore collapse to one throttled line. Everything else keeps
    its traceback, because that is a bug in our code and the stack is the useful part.
    """
    error = context.error

    if not _is_transient(error):
        log.error(f"Unhandled error while processing {_describe_update(update)}", exc_info=error)
        return

    name = type(error).__name__
    now = time.monotonic()

    if _transient['name'] == name and now < _transient['mute_until']:
        _transient['suppressed'] += 1
        return

    swallowed = _transient['suppressed']
    extra = f" (+{swallowed} more suppressed)" if swallowed else ""
    log.warning(f"Telegram API unreachable: {name}: {error}{extra} — "
                f"retrying automatically, no action needed.")
    _transient.update(name=name, suppressed=0, mute_until=now + TRANSIENT_ERROR_MUTE_S)

# -------------------------------------------------------------------------------------------------
# BOT

application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("alertes", alerts))
application.add_handler(CommandHandler(["mesure", "measure"], measure))
application.add_handler(CommandHandler(["pompe", "pump"], pompe))
application.add_handler(CommandHandler("pertes", pertes))
application.add_handler(CommandHandler("graphe24h", graphe24h))
application.add_handler(CommandHandler("graphe3j", graphe3j))
application.add_handler(CommandHandler("graphe7j", graphe7j))
application.add_handler(CommandHandler("logs", logs))
application.add_handler(CommandHandler("export", export_command))
application.add_handler(CommandHandler("echantillons", samples))
application.add_handler(CommandHandler("flash", flash))
application.add_handler(CommandHandler("boards", board_status))
application.add_handler(CommandHandler("help", help_command))
application.add_error_handler(on_error)

# TODO: add command handler /status
log.info("Starting Telegram bot polling loop.")
application.run_polling()
