
import os
import time
import random
import sqlite3
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ==========================================
# CONFIG
# ==========================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = 7783222972

COOLDOWN = 2 * 60 * 60
DATABASE = "gardenview.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)

# ==========================================
# DATABASE
# ==========================================

db = sqlite3.connect(DATABASE, check_same_thread=False)
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


# ==========================================
# START
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)

    await update.effective_message.reply_text(
        f"🌼 Привет, {user.first_name}!\n\n"
        "Я Дэнди, рад видеть тебя в Gardenview Roleplay!\n\n"
        "Здесь ты можешь знакомиться с другими тунами, "
        "участвовать в ролевой и собирать кассеты.\n\n"
        "📜 /rules — правила\n"
        "🎞️ /tapes — получить кассеты\n"
        "👤 /me — личный кабинет\n\n"
        "Удачного пребывания в Gardenview!"
    )


# ==========================================
# WELCOME
# ==========================================

async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:

        if member.is_bot:
            continue

        register_user(member)

        await update.message.reply_text(
            f"🌼 Новый посетитель Gardenview!\n\n"
            f"Привет, {member.first_name}!\n"
            "Я Дэнди, добро пожаловать в мой сад!\n\n"
            "Осматривайся, знакомься с тунами "
            "и не забудь ознакомиться с правилами.\n\n"
            "📜 /rules"
        )


# ==========================================
# RULES
# ==========================================

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = db.execute(
        "SELECT value FROM settings WHERE name = 'rules'"
    ).fetchone()

    if result is None:
        await update.effective_message.reply_text(
            "📜 Правила Gardenview пока не установлены."
        )
        return

    await update.effective_message.reply_text(
        f"🌼 GARDENVIEW ROLEPLAY\n"
        f"📜 Правила\n\n{result['value']}"
    )


async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "❌ Настраивать правила можно только в личных сообщениях с ботом."
        )
        return

    message = update.effective_message.text or ""
    parts = message.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text(
            "Использование:\n\n"
            "/setrules Текст правил"
        )
        return

    text = parts[1].strip()

    db.execute("""
        INSERT INTO settings (name, value)
        VALUES ('rules', ?)
        ON CONFLICT(name) DO UPDATE SET value = excluded.value
    """, (text,))

    db.commit()

    await update.effective_message.reply_text(
        "🌼 Дэнди успешно обновил правила Gardenview!"
    )


# ==========================================
# TAPES
# ==========================================

async def tapes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user)

    now = int(time.time())
    remaining = COOLDOWN - (now - data["last_tapes"])

    if remaining > 0:
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        seconds = remaining % 60

        await update.effective_message.reply_text(
            "🎞️ Ты уже забирал кассеты!\n\n"
            f"⏳ Следующая награда через:\n"
            f"{hours} ч. {minutes} мин. {seconds} сек."
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

    await update.effective_message.reply_text(
        "📼 Дэнди приготовил для тебя награду!\n\n"
        f"🎞️ Получено кассет: +{reward}\n\n"
        "Возвращайся через 2 часа!"
    )


# ==========================================
# PROFILE
# ==========================================

async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user)

    username = (
        f"@{user.username}"
        if user.username
        else user.first_name
    )

    await update.effective_message.reply_text(
        "🌼 GARDENVIEW | Личный кабинет\n\n"
        f"👤 {username}\n"
        f"🎞️ Кассеты: {data['tapes']}\n"
        f"📺 Роль: {data['role']}"
    )


# ==========================================
# SET ROLE
# ==========================================

async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Использование:\n"
            "/setrole @username роль"
        )
        return

    username = context.args[0].lstrip("@").lower()
    role = " ".join(context.args[1:])

    target = db.execute("""
        SELECT user_id FROM users
        WHERE LOWER(username) = ?
    """, (username,)).fetchone()

    if target is None:
        await update.effective_message.reply_text(
            "❌ Пользователь не найден.\n\n"
            "Пусть он сначала напишет боту /start "
            "или отправит сообщение в группе."
        )
        return

    db.execute("""
        UPDATE users SET role = ?
        WHERE user_id = ?
    """, (role, target["user_id"]))

    db.commit()

    await update.effective_message.reply_text(
        f"📺 Роль пользователя @{username} изменена!\n\n"
        f"Новая роль: {role}"
    )


# ==========================================
# TRACK USERS
# ==========================================

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        register_user(update.effective_user)


# ==========================================
# ERROR HANDLER
# ==========================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Handler error:",
        exc_info=context.error
    )


# ==========================================
# MAIN
# ==========================================

def main():
    if not TOKEN:
        raise RuntimeError(
            "TOKEN environment variable is missing!"
        )

    app = Application.builder().token(TOKEN).build()

    # Public commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("tapes", tapes))
    app.add_handler(CommandHandler("me", me))

    # Owner commands
    app.add_handler(CommandHandler("setrules", setrules))
    app.add_handler(CommandHandler("setrole", setrole))

    # Welcome new members
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            welcome
        )
    )

    # Register group users
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            track_user
        )
    )

    app.add_error_handler(error_handler)

    logger.info("Gardenview Roleplay Bot is starting...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
