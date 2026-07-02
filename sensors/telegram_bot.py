###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import sys
import json
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.telegram_users.json')

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
# USER REGISTRY

def load_users():
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not load {USERS_FILE} ({e}); starting with no registered users.")
        return {}

def save_users(users):
    try:
        with open(USERS_FILE, 'w') as f:
            json.dump(users, f)
    except Exception as e:
        log.warning(f"Could not save {USERS_FILE} ({e}).")

# -------------------------------------------------------------------------------------------------
# COMMAND HANDLERS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text("Usage: /start <password>")
        return

    role = PASSWORD_TO_ROLE.get(context.args[0])
    if role is None:
        log.info(f"Rejected registration attempt from chat {chat_id}: invalid password.")
        await update.message.reply_text("Invalid password.")
        return

    users = load_users()
    previous = users.get(chat_id, {})
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

    if not context.args:
        status = "on" if user.get("alerts") else "off"
        await update.message.reply_text(f"Alerts are currently {status}. Usage: /alerts on|off")
        return

    choice = context.args[0].lower()
    if choice not in ("on", "off"):
        await update.message.reply_text("Usage: /alerts on|off")
        return

    user["alerts"] = (choice == "on")
    save_users(users)
    await update.message.reply_text(f"Alerts turned {choice}.")

# -------------------------------------------------------------------------------------------------
# BOT

application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("alerts", alerts))

# TODO: add command handlers (/measure, /status)
# TODO: add proactive alert logic (call from read_puit.py when level is low;
#       recipients = [cid for cid, u in load_users().items() if u.get("alerts")])
log.info("Starting Telegram bot polling loop.")
application.run_polling()
