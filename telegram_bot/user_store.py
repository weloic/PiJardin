###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import json

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

## Registered Telegram users (shared by bot.py and alerts.py)
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.telegram_users.json')

# -------------------------------------------------------------------------------------------------
# USER REGISTRY

def load_users():
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: could not load {USERS_FILE} ({e}); no registered users.")
        return {}

def save_users(users):
    try:
        with open(USERS_FILE, 'w') as f:
            json.dump(users, f)
    except Exception as e:
        print(f"Warning: could not save {USERS_FILE} ({e}).")
