import os
import logging
import telebot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

API_TOKEN       = "8702703868:AAFepRxGuNvp3JjUw8P4AzOvdfGbM4ha15E"
ALLOWED_CHAT_ID = 8398874714
LOCK_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aegis.lock')

bot = telebot.TeleBot(API_TOKEN, parse_mode=None)


def _is_authorized(message) -> bool:
    """
    Returns True if the message originates from ALLOWED_CHAT_ID.
    All other chat IDs are logged and rejected.
    """
    if message.chat.id != ALLOWED_CHAT_ID:
        logging.warning(
            f"Rejected unauthorized command '{message.text}' "
            f"from chat_id={message.chat.id} (user: {message.from_user.username})"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# /start — always responds; this is how the owner discovers their chat ID.
# ---------------------------------------------------------------------------
@bot.message_handler(commands=['start'])
def handle_start(message):
    bot.reply_to(
        message,
        f"Aegis C&C Online.\n\n"
        f"Your Chat ID is: {message.chat.id}\n\n"
        f"To lock down this bot, add the following line to your .env file "
        f"and restart telegram_cnc.py:\n\n"
        f"TELEGRAM_ALLOWED_CHAT_ID={message.chat.id}"
    )
    logging.info(f"/start received from chat_id={message.chat.id} (user: {message.from_user.username})")


# ---------------------------------------------------------------------------
# /status — reports whether aegis.lock is present.
# ---------------------------------------------------------------------------
@bot.message_handler(commands=['status'])
def handle_status(message):
    if not _is_authorized(message):
        return
    if os.path.exists(LOCK_FILE):
        bot.reply_to(message, "Aegis is PAUSED")
    else:
        bot.reply_to(message, "Aegis is RUNNING")
    logging.info(f"/status queried by chat_id={message.chat.id}")


# ---------------------------------------------------------------------------
# /pause — creates aegis.lock; main.py will detect it within 5 minutes.
# ---------------------------------------------------------------------------
@bot.message_handler(commands=['pause'])
def handle_pause(message):
    if not _is_authorized(message):
        return
    try:
        if os.path.exists(LOCK_FILE):
            bot.reply_to(message, "Aegis is already PAUSED. Lock file already exists.")
            return
        open(LOCK_FILE, 'w').close()
        bot.reply_to(
            message,
            "Aegis PAUSED. The trading loop will suspend within 5 minutes.\n"
            "Send /resume to reactivate."
        )
        logging.info(f"Lock file created by chat_id={message.chat.id}")
    except Exception as e:
        bot.reply_to(message, f"ERROR: Failed to create lock file — {e}")
        logging.error(f"Failed to create lock file: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# /resume — removes aegis.lock; main.py resumes on its next cycle.
# ---------------------------------------------------------------------------
@bot.message_handler(commands=['resume'])
def handle_resume(message):
    if not _is_authorized(message):
        return
    try:
        if not os.path.exists(LOCK_FILE):
            bot.reply_to(message, "Aegis is already RUNNING. No lock file found.")
            return
        os.remove(LOCK_FILE)
        bot.reply_to(
            message,
            "Aegis RESUMED. The trading loop will reactivate on its next cycle."
        )
        logging.info(f"Lock file removed by chat_id={message.chat.id}")
    except Exception as e:
        bot.reply_to(message, f"ERROR: Failed to remove lock file — {e}")
        logging.error(f"Failed to remove lock file: {type(e).__name__}: {e}")


if __name__ == "__main__":
    logging.info(f"Aegis Telegram C&C listener starting. Authorized chat ID: {ALLOWED_CHAT_ID}")
    bot.infinity_polling(timeout=30, long_polling_timeout=25)
