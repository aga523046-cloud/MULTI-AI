
import sqlite3
import random
import time
import re

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# =========================
# CONFIG
# =========================

TOKEN = "9093073841:AAE_VaXfN3zKvOwwb1-X2cPFQR4amchGlAk"
OWNER_ID = 7783222972

COOLDOWN = 2 * 60 * 60  # 2 часа

DB = sqlite3.connect("gardenview.db", check_same_thread=False)
DB.row_factory = sqlite3.Row

# =========================
# DATABASE
# =========================

DB.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    tapes INTEGER DEFAULT 0,
    role TEXT DEFAULT 'Участник',
    last_tapes INTEGER DEFAULT 0
)
""")

DB.execute("""
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT
)
""")

DB.commit()


def get_user(user):
    username = user.username or user.first_name

    DB.execute("""
    INSERT INTO users (user_id, username)
    VALUES (?, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET username = excluded.username
    """, (user.id, username))

    DB.commit()

    return DB.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user.id,)
    ).fetchone()


def get_rules():
    result = DB.execute(
        "SELECT value FROM settings WHERE name = 'rules'"
    ).fetchone()

    return result["value"] if result else None


# =========================
# WELCOME
# =========================

async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        get_user(member)

        await update.message.reply_text(
            f"🌼 О, новый посетитель Gardenview!\n\n"
            f"Привет, {member.first_name}!\n"
            f"Я Дэнди, рад видеть тебя в нашем саду!\n\n"
            f"Осматривайся, знакомься с другими тунами "
            f"и не забывай заглядывать ко мне в магазин!\n\n"
            f"🎞️ Используй /rules, чтобы ознакомиться с правилами."
        )


# =========================
# RULES
# =========================

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_rules()

    if not text:
        await update.message.reply_text(
            "🌼 Правила Gardenview пока не установлены!"
        )
        return

    await update.message.reply_text(
        f"📜 Правила Gardenview Roleplay\n\n{text}"
    )


async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id != OWNER_ID:
        return

    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Настраивать правила можно только в личных сообщениях с ботом."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Отправь правила после команды:\n\n"
            "/setrules 1. Правило первое\n2. Правило второе"
        )
        return

    text = " ".join(context.args)

    DB.execute("""
    INSERT INTO settings (name, value)
    VALUES ('rules', ?)
    ON CONFLICT(name) DO UPDATE SET value = excluded.value
    """, (text,))

    DB.commit()

    await update.message.reply_text(
        "🌼 Дэнди сохранил новые правила Gardenview!"
    )


# =========================
# TAPES
# =========================

async def tapes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user)

    now = int(time.time())
    remaining = COOLDOWN - (now - data["last_tapes"])

    if remaining > 0:
        minutes = remaining // 60
        seconds = remaining % 60

        await update.message.reply_text(
            f"🎞️ Ты уже забирал кассеты!\n"
            f"Возвращайся через {minutes} мин. {seconds} сек."
        )
        return

    reward = random.randint(30, 45)

    DB.execute("""
    UPDATE users
    SET tapes = tapes + ?, last_tapes = ?
    WHERE user_id = ?
    """, (reward, now, user.id))

    DB.commit()

    await update.message.reply_text(
        f"📼 Отличная работа!\n\n"
        f"Дэнди выдал тебе {reward} 🎞️ кассет!\n"
        f"Возвращайся через 2 часа за следующей наградой."
    )


# =========================
# PROFILE
# =========================

async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user)

    username = (
        f"@{user.username}"
        if user.username
        else user.first_name
    )

    await update.message.reply_text(
        f"🌼 Gardenview | Личный кабинет\n\n"
        f"👤 {username}\n"
        f"🎞️ Кассеты: {data['tapes']}\n"
        f"📺 Роль: {data['role']}"
    )


# =========================
# SET ROLE
# =========================

async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id != OWNER_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование:\n/setrole @username роль"
        )
        return

    username = context.args[0].lstrip("@").lower()
    role = " ".join(context.args[1:])

    target = DB.execute(
        "SELECT user_id FROM users WHERE LOWER(username) = ?",
        (username,)
    ).fetchone()

    if not target:
        await update.message.reply_text(
            "❌ Пользователь не найден. "
            "Сначала он должен написать что-нибудь в группе."
        )
        return

    DB.execute(
        "UPDATE users SET role = ? WHERE user_id = ?",
        (role, target["user_id"])
    )

    DB.commit()

    await update.message.reply_text(
        f"📺 Роль @{username} изменена на: {role}"
    )


# =========================
# TRACK USERS
# =========================

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        get_user(update.effective_user)


# =========================
# START
# =========================

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            welcome
        )
    )

    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("setrules", setrules))
    app.add_handler(CommandHandler("tapes", tapes))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CommandHandler("setrole", setrole))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            track_user
        )
    )

    print("🌼 Gardenview Roleplay bot is running!")

    app.run_polling()


if __name__ == "__main__":
    main()
