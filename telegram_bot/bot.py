###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import sys
import asyncio
import logging
import subprocess

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

## systemd captures stdout block-buffered; line-buffer it so read_puit's print()
## diagnostics reach the journal immediately (readable remotely via /logs bot).
sys.stdout.reconfigure(line_buffering=True)

## read_puit.py lives in sensors/; when run as a script, only this folder is on sys.path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sensors'))

import read_puit
# graph pulls in matplotlib; guard the import so a missing/broken dependency only
# disables the /graphe* commands rather than crashing the whole bot on startup.
try:
    import graph
except Exception as e:
    graph = None
    logging.getLogger(__name__).warning(f"graph module unavailable ({e}); /graphe* commands disabled.")
from user_store import load_users, save_users
from alerts import ALERT_LOW_VOLUME_M3, ALERT_CRITICAL_VOLUME_M3

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

## httpx logs every polling request at INFO — including the bot token in the URL.
## Keep it (and the journal read via /logs) quiet unless something goes wrong.
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

PASSWORD_ADMIN  = os.getenv("TELEGRAM_PASSWORD_ADMIN")
PASSWORD_VIEWER = os.getenv("TELEGRAM_PASSWORD_VIEWER")
PASSWORD_TO_ROLE = {
    password: role
    for role, password in (("admin", PASSWORD_ADMIN), ("viewer", PASSWORD_VIEWER))
    if password
}

MAX_FAILED_ATTEMPTS = 10

## Journal units readable via /logs — fixed whitelist, never user-supplied unit names.
LOG_UNITS = {
    "bot": "telegram-bot.service",
    "sensors": "sensors.service",
    "deploy": "deploy.service",
}
LOG_DEFAULT_LINES = 40
LOG_MAX_LINES = 100

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
    except Exception as e:
        log.warning(f"/mesure failed for chat {chat_id}: {e}")
        await msg.edit_text(f"Measurement failed: {e}")
        return

    if height is None:
        await msg.edit_text("Sensor did not return a valid reading.")
        return

    volume = read_puit.height_to_volume(height)
    text = f"Volume: {volume:.2f} m³ ({volume * 1000:.0f} L)"
    if resampled:
        text += "\n(médiane de 5 échantillons)"
    if not db_ok:
        text += "\n⚠️ Could not write to InfluxDB — value NOT recorded in the database."
    await msg.edit_text(text)

async def send_graph(update: Update, context: ContextTypes.DEFAULT_TYPE, range_start, title):
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

    png = await asyncio.to_thread(graph.render_volume_chart, times, volumes, title)
    await update.message.reply_photo(photo=png, caption=title)
    await msg.delete()

async def samples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay the Arduino's raw ping array (SAMPLING) — admin sensor diagnostics."""
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    if user is None or user.get("banned") or user.get("role") != "admin":
        await update.message.reply_text("Admin only.")
        return

    msg = await update.message.reply_text("Lecture des échantillons bruts, patience")

    try:
        raw = await asyncio.to_thread(read_puit.raw_samples_once, 20)
    except TimeoutError:
        await msg.edit_text("Une mesure ordinaire est déjà en cours, veuillez recommencer plus tard")
        return
    except Exception as e:
        log.warning(f"/echantillons failed for chat {chat_id}: {e}")
        await msg.edit_text(f"Sampling failed: {e}")
        return

    if raw is None:
        await msg.edit_text("Le capteur n'a pas répondu.")
        return

    await msg.edit_text(f"Échantillons bruts (cm) :\n{raw}")

async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply with the tail of a service journal. Admin-only remote diagnostics."""
    chat_id = str(update.effective_chat.id)
    user = load_users().get(chat_id)

    if user is None or user.get("banned") or user.get("role") != "admin":
        await update.message.reply_text("Admin only.")
        return

    unit_alias = "bot"
    lines = LOG_DEFAULT_LINES
    for arg in context.args:
        if arg.lower() in LOG_UNITS:
            unit_alias = arg.lower()
        elif arg.isdigit():
            lines = min(int(arg), LOG_MAX_LINES)
        else:
            await update.message.reply_text(
                f"Usage: /logs [{'|'.join(LOG_UNITS)}] [lines]"
            )
            return

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["journalctl", "-u", LOG_UNITS[unit_alias], "-n", str(lines),
             "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=15,
        )
        # On permission problems journalctl often exits 0 with a hint on stderr;
        # show whatever it produced, it is the diagnostic.
        output = (result.stdout.strip() or result.stderr.strip()
                  or f"(journalctl exited {result.returncode} with no output)")
    except Exception as e:
        await update.message.reply_text(f"Could not read journal: {e}")
        return

    # Telegram caps messages at 4096 chars; send the most recent chunks as plain
    # text (no Markdown parse mode: journal content would break entity parsing).
    chunks = [output[i:i + 4000] for i in range(0, len(output), 4000)]
    for chunk in chunks[-3:]:
        await update.message.reply_text(chunk)

async def graphe24h(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_graph(update, context, "-24h", "Historique 24 heures")

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

# TODO: add command handler /status
log.info("Starting Telegram bot polling loop.")
application.run_polling()
