###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import sys
import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

## read_puit.py lives in sensors/; when run as a script, only this folder is on sys.path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sensors'))

import read_puit
from user_store import load_users, save_users
from alerts import ALERT_LOW_VOLUME_M3, ALERT_CRITICAL_VOLUME_M3

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

PASSWORD_ADMIN  = os.getenv("TELEGRAM_PASSWORD_ADMIN")
PASSWORD_VIEWER = os.getenv("TELEGRAM_PASSWORD_VIEWER")
PASSWORD_TO_ROLE = {
    password: role
    for role, password in (("admin", PASSWORD_ADMIN), ("viewer", PASSWORD_VIEWER))
    if password
}

MAX_FAILED_ATTEMPTS = 10

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

# -------------------------------------------------------------------------------------------------
# BOT

application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("alertes", alerts))
application.add_handler(CommandHandler(["mesure", "measure"], measure))

# TODO: add command handler /status
log.info("Starting Telegram bot polling loop.")
application.run_polling()
