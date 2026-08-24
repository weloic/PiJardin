###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import re
import io
import sys
import html
import asyncio
import logging
import datetime
import subprocess

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

## read_puit.py lives in sensors/; the shared `common` package lives at the repo root. Both
## on sys.path so this script can import them when run directly by the service.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, 'sensors'))

import read_puit
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
    'no_banner':            "L'Arduino ne démarre pas correctement. Un admin doit lancer /flash.",
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
        text += "\n(médiane de 5 échantillons)"
    if not db_ok:
        text += "\n⚠️ Could not write to InfluxDB — value NOT recorded in the database."
    await msg.edit_text(text)

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
    """Render a `sampling` burst as a diagnostic block.

    Works on a failed burst too: the four counts and the effective parameters are on every
    measurement reply, and they are what says whether the sensor is silent, blind, or aimed
    at the wrong thing. The per-ping detail only exists on a successful one.
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
    pulses = resp.get('pulse_us')
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
    """Relay one `sampling` burst, per ping — admin sensor diagnostics."""
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

## /help text (HTML). Literal <, >, & are pre-escaped; examples go in <pre> blocks. The admin
## section is appended only for admins.
HELP_GENERAL = (
    "<b>Commandes PiJardin</b>\n"
    "\n"
    "<b>/mesure</b> — mesure le niveau du puits maintenant (alias <b>/measure</b>)\n"
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
    "<b>/logs</b> [bot|sensors|deploy] [N | 2h|30m|3d] [since &lt;t&gt;] [until &lt;t&gt;] — "
    "journaux d'un service ; compact (≤ 15 lignes) en message, sinon en fichier .txt\n"
    "<pre>/logs                       (15 dernières lignes du bot)\n"
    "/logs sensors 50           (50 dernières lignes)\n"
    "/logs bot 2h               (2 dernières heures)\n"
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
# BOT

application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("alertes", alerts))
application.add_handler(CommandHandler(["mesure", "measure"], measure))
application.add_handler(CommandHandler("graphe24h", graphe24h))
application.add_handler(CommandHandler("graphe3j", graphe3j))
application.add_handler(CommandHandler("graphe7j", graphe7j))
application.add_handler(CommandHandler("logs", logs))
application.add_handler(CommandHandler("echantillons", samples))
application.add_handler(CommandHandler("flash", flash))
application.add_handler(CommandHandler("help", help_command))

# TODO: add command handler /status
log.info("Starting Telegram bot polling loop.")
application.run_polling()
