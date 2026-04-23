"""
Telegram bot: commands + interactive inline-keyboard menus.
All handlers are async and guarded by ALLOWED_USER_IDS / TELEGRAM_CHAT_ID.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALLOWED_USER_IDS

logger = logging.getLogger(__name__)

# Conversation states
CHOOSE_SYMBOL, CHOOSE_AMOUNT, CHOOSE_RISK = range(3)

# Popular pairs for quick-select
POPULAR_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
RISK_LEVELS = ["low", "medium", "high"]

# Will be injected by main.py
_engine = None
_client = None


def set_engine(engine, client) -> None:
    global _engine, _client
    _engine = engine
    _client = client


# ── Auth guard ─────────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
        return False
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return False
    return True


async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text("⛔ Unauthorized.")
    elif update.callback_query:
        await update.callback_query.answer("⛔ Unauthorized.", show_alert=True)






