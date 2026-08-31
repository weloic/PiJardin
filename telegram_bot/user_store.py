###################################################################################################
# -------------------------------------------------------------------------------------------------
# IMPORTS
import os
import json
import logging

# -------------------------------------------------------------------------------------------------
# CONFIGURATION

log = logging.getLogger(__name__)

## Registered Telegram users (shared by bot.py and alerts.py)
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.telegram_users.json')

# -------------------------------------------------------------------------------------------------
# USER REGISTRY

def load_users():
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not load {USERS_FILE} ({e}); no registered users.")
        return {}

def save_users(users):
    """Write the registry, atomically.

    The file is small and always rewritten whole, so a crash or a power cut partway through a
    plain write would leave truncated JSON behind — load_users would then find no registered
    users at all, and every alert would silently go nowhere. Writing a temp file and renaming it
    means a reader sees either the old registry or the new one, never half of one. The Pi runs
    off an SD card, and the fsync is what extends that guarantee from a crash to a power cut.
    """
    tmp = f"{USERS_FILE}.{os.getpid()}.tmp"   # pid-suffixed: two writers cannot share a temp file
    try:
        with open(tmp, 'w') as f:
            json.dump(users, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, USERS_FILE)
    except Exception as e:
        log.warning(f"Could not save {USERS_FILE} ({e}).")
        try:
            os.remove(tmp)
        except OSError:
            pass  # Never written, or already renamed; nothing to clean up.

def forget_user(chat_id):
    """Drop one chat from the registry, for a chat Telegram reports as permanently unreachable.

    Called from alerts.py, i.e. possibly from pump.service or deploy.sh rather than the bot
    process. It reloads the file rather than taking a dict from the caller so a setting changed
    in the bot meanwhile survives; two processes writing at the same instant would still lose
    one of the two changes, which is accepted here — the write happens at most once per dead chat.
    """
    users = load_users()
    if users.pop(str(chat_id), None) is None:
        return False
    save_users(users)
    return True

def rekey_user(old_chat_id, new_chat_id):
    """Move one chat's entry to a new id, keeping its role and opt-ins.

    Telegram assigns a group a brand new id when it is upgraded to a supergroup, and the old id
    stops accepting messages — without this, such a group would be dropped as if it were dead.
    """
    users = load_users()
    entry = users.pop(str(old_chat_id), None)
    if entry is None:
        return False
    users[str(new_chat_id)] = entry
    save_users(users)
    return True
