
import logging
import sqlite3
import random
import time
import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# =====================================
# CONFIGURATION
# =====================================

TOKEN = "9093073841:AAE_VaXfN3zKvOwwb1-X2cPFQR4amchGlAk"
OWNER_ID = 7783222972

COOLDOWN = 7200  # 2 hours

DB_NAME = "gardenview.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# =====================================
# DATABASE
# =====================================

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    tapes INTEGER DEFAULT 0,
    role TEXT DEFAULT 'Участник',
    last_tapes INTEGER DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT
)
""")

db.commit()


def register_user(user):
    db.execute("""
        INSERT INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
    """, (
        user.id,
        user.username,
        user.first_name
    ))
    db.commit()


def get_user(user):
    register_user(user)

    return db.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user.id,)
    ).fetchone()


def get_rules():
    result = db.execute(
        "SELECT value FROM settings WHERE name = 'rules'"
    ).fetchone()

    return result["value"] if result else None


# =====================================
# START
# =====================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    register_user(user)

    await update.effective_message.reply_text(
        f"🌼 Привет, {user.first_name}!\n\n"
        "Добро пожаловать в Gardenview Roleplay!\n"
        "Я Дэнди, хозяин этого замечательного сада.\n\n"
        "Вот что ты можешь делать:\n\n"
        "📜 /rules — правила ролевой\n"
        "🎞️ /tapes — получить кассеты\n"
        "👤 /me — твой личный кабинет\n\n"
        "Надеюсь, тебе здесь понравится!"
    )


# =====================================
# WELCOME
# =====================================

async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):

    for member in update.message.new_chat_members:

        if member.is_bot:
            continue

        register_user(member)

        await update.message.reply_text(
            f"🌼 О! У нас новый посетитель!\n\n"
            f"Привет, {member.first_name}!\n"
            "Я Дэнди, рад видеть тебя в Gardenview!\n\n"
            "Осматривайся, знакомься с другими тунами "
            "и обязательно заглядывай ко мне!\n\n"
            "📜 Не забудь прочитать /rules."
        )


# =====================================
# RULES
# =====================================

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = get_rules()

    if not text:
        await update.effective_message.reply_text(
            "📜 Правила Gardenview пока не установлены."
        )
        return

    await update.effective_message.reply_text(
        "🌼 GARDENVIEW ROLEPLAY\n"
        "📜 Правила сервера\n\n"
        f"{text}"
    )


async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_ID:
        return

    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Настройка правил доступна только в личных сообщениях с ботом."
        )
        return

    text = update.effective_message.text.partition(" ")[2].strip()

    if not text:
        await update.message.reply_text(
            "Использование:\n\n"
            "/setrules Правила твоей ролевой"
        )
        return

    db.execute("""
        INSERT INTO settings (name, value)
        VALUES ('rules', ?)
        ON CONFLICT(name) DO UPDATE SET value = excluded.value
    """, (text,))

    db.commit()

    await update.message.reply_text(
        "🌼 Дэнди обновил правила Gardenview!"
    )


# =====================================
# TAPES
# =====================================

async def tapes(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    data = get_user(user)

    now = int(time.time())
    remaining = COOLDOWN - (now - data["last_tapes"])

    if remaining > 0:

        hours = remaining // 3600
        minutes = (remaining % 3600) // 60

        await update.message.reply_text(
            "🎞️ Ты уже забирал свои кассеты!\n\n"
            f"⏳ Следующая награда через: {hours} ч. {minutes} мин."
        )
        return

    reward = random.randint(30, 45)

    db.execute("""
        UPDATE users
        SET tapes = tapes + ?,
            last_tapes = ?
        WHERE user_id = ?
    """, (reward, now, user.id))

    db.commit()

    await update.message.reply_text(
        "📼 Отличные новости!\n\n"
        f"Дэнди выдал тебе {reward} 🎞️ кассет!\n\n"
        "Возвращайся через 2 часа за следующей наградой."
    )


# =====================================
# PROFILE
# =====================================

async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    data = get_user(user)

    username = (
        f"@{user.username}"
        if user.username
        else user.first_name
    )

    await update.message.reply_text(
        "🌼 GARDENVIEW | Личный кабинет\n\n"
        f"👤 {username}\n"
        f"🎞️ Кассеты: {data['tapes']}\n"
        f"📺 Роль: {data['role']}"
    )


# =====================================
# SET ROLE
# =====================================

async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "/setrole @username роль"
        )
        return

    username = context.args[0].lstrip("@")
    role = " ".join(context.args[1:])

    target = db.execute("""
        SELECT user_id FROM users
        WHERE LOWER(username) = LOWER(?)
    """, (username,)).fetchone()

    if not target:
        await update.message.reply_text(
            "❌ Пользователь не найден.\n"
            "Пусть он сначала напишет /start боту "
            "или отправит сообщение в группе."
        )
        return

    db.execute(
        "UPDATE users SET role = ? WHERE user_id = ?",
        (role, target["user_id"])
    )
    db.commit()

    await update.message.reply_text(
        f"📺 Роль @{username} изменена!\n\n"
        f"Новая роль: {role}"
    )


# =====================================
# TRACK GROUP USERS
# =====================================

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user:
        register_user(update.effective_user)


# =====================================
# ERROR HANDLER
# =====================================

async def error_handler(update, context):

    logger.error(
        "An error occurred:",
        exc_info=context.error
    )


# =====================================
# MAIN
# =====================================

def main():

    if TOKEN == "PASTE_YOUR_NEW_TOKEN_HERE":
        print("ERROR: Insert your bot token first!")
        return

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("setrules", setrules))
    app.add_handler(CommandHandler("tapes", tapes))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CommandHandler("setrole", setrole))

    # New members
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            welcome
        )
    )

    # Remember group users
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            track_user
        )
    )

    app.add_error_handler(error_handler)

    print("Connecting to Telegram...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
