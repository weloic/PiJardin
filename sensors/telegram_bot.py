###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import sys
import logging

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# -------------------------------------------------------------------------------------------------
# STARTUP CHECK

if not TOKEN:
    log.info(
        "TELEGRAM_BOT_TOKEN not set in .env — bot is not configured. "
        "Add the token (and TELEGRAM_CHAT_ID) to .env, then restart the service: "
        "sudo systemctl restart telegram-bot.service"
    )
    sys.exit(0)

# -------------------------------------------------------------------------------------------------
# BOT

log.info("Token found — bot logic not yet implemented.")

# TODO: initialise python-telegram-bot application here
# TODO: add command handlers (/measure, /status)
# TODO: add proactive alert logic (call from read_puit.py when level is low)
# TODO: call app.run_polling() to start the polling loop
