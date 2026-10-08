import os
import re
import sqlite3
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# НАСТРОЙКИ
# =========================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DARK_API_KEY = os.getenv("DARK_API_KEY")

DARK_API_URL = "https://darkapi.shop/v1"
MODEL = "gpt-6-luna"

SUPPORT_ID = 7783222972

# Максимум 3 последних сообщения контекста
MAX_HISTORY = 3

DB_NAME = "multi_ai.db"

TIMEZONE = ZoneInfo("Europe/Moscow")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# =========================
# ПРОВЕРКА КЛЮЧЕЙ
# =========================

if not BOT_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN")

if not DARK_API_KEY:
    raise RuntimeError("Не найден DARK_API_KEY")

client = AsyncOpenAI(
    api_key=DARK_API_KEY,
    base_url=DARK_API_URL,
)

# =========================
# DATABASE
# =========================


def db():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            age TEXT,
            interests TEXT,
            preferences TEXT,
            style TEXT,
            updated_at TEXT
        )
    """)

    conn.commit()
    conn.close()


# =========================
# USERS
# =========================


def save_user(user):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user.id,),
    )

    exists = cur.fetchone()

    if exists:
        cur.execute(
            """
            UPDATE users
            SET username = ?, first_name = ?
            WHERE user_id = ?
            """,
            (
                user.username,
                user.first_name,
                user.id,
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO users
            (user_id, username, first_name, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                user.id,
                user.username,
                user.first_name,
                datetime.now().isoformat(),
            ),
        )

    conn.commit()
    conn.close()


# =========================
# MESSAGE HISTORY
# =========================


def save_message(user_id, role, content):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO messages
        (user_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            role,
            content,
            datetime.now().isoformat(),
        ),
    )

    conn.commit()
    conn.close()


def get_history(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT role, content
        FROM messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, MAX_HISTORY),
    )

    rows = cur.fetchall()
    conn.close()

    rows.reverse()

    return [
        {
            "role": role,
            "content": content,
        }
        for role, content in rows
    ]


def clear_history(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM messages WHERE user_id = ?",
        (user_id,),
    )

    conn.commit()
    conn.close()


# =========================
# MEMORY
# =========================


def get_memory(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT name, age, interests, preferences, style
        FROM memory
        WHERE user_id = ?
        """,
        (user_id,),
    )

    row = cur.fetchone()
    conn.close()

    if not row:
        return {
            "name": "",
            "age": "",
            "interests": "",
            "preferences": "",
            "style": "",
        }

    return {
        "name": row[0] or "",
        "age": row[1] or "",
        "interests": row[2] or "",
        "preferences": row[3] or "",
        "style": row[4] or "",
    }


def save_memory(user_id, memory):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT user_id FROM memory WHERE user_id = ?",
        (user_id,),
    )

    exists = cur.fetchone()

    values = (
        memory.get("name", ""),
        memory.get("age", ""),
        memory.get("interests", ""),
        memory.get("preferences", ""),
        memory.get("style", ""),
        datetime.now().isoformat(),
    )

    if exists:
        cur.execute(
            """
            UPDATE memory
            SET name = ?,
                age = ?,
                interests = ?,
                preferences = ?,
                style = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            values + (user_id,),
        )
    else:
        cur.execute(
            """
            INSERT INTO memory
            (user_id, name, age, interests, preferences, style, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id,) + values,
        )

    conn.commit()
    conn.close()


def clear_memory(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM memory WHERE user_id = ?",
        (user_id,),
    )

    conn.commit()
    conn.close()


# =========================
# MEMORY EXTRACTION
# =========================


def update_memory(user_id, text):
    memory = get_memory(user_id)

    # Имя
    patterns = [
        r"меня зовут\s+([A-Za-zА-Яа-яЁё0-9_-]{2,30})",
        r"моё имя\s*[—:-]?\s*([A-Za-zА-Яа-яЁё0-9_-]{2,30})",
        r"мое имя\s*[—:-]?\s*([A-Za-zА-Яа-яЁё0-9_-]{2,30})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            memory["name"] = match.group(1).strip()
            break

    # Возраст
    match = re.search(
        r"\bмне\s+(\d{1,2})\s*(?:лет|год|года)?\b",
        text,
        re.IGNORECASE,
    )

    if match:
        memory["age"] = match.group(1)

    # Интересы
    interest_patterns = [
        r"я люблю\s+(.+)",
        r"мне нравится\s+(.+)",
        r"мне нравятся\s+(.+)",
        r"я интересуюсь\s+(.+)",
    ]

    for pattern in interest_patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            value = match.group(1).strip()

            if len(value) <= 300:
                old = memory["interests"]

                if value not in old:
                    memory["interests"] = (
                        f"{old}, {value}" if old else value
                    )

            break

    # Предпочтения
    preference_patterns = [
        r"я предпочитаю\s+(.+)",
        r"я не люблю\s+(.+)",
        r"мне не нравится\s+(.+)",
    ]

    for pattern in preference_patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            value = match.group(1).strip()

            if len(value) <= 300:
                old = memory["preferences"]

                if value not in old:
                    memory["preferences"] = (
                        f"{old}, {value}" if old else value
                    )

            break

    # Стиль общения
    style_parts = []

    if any(word in text.lower() for word in [
        "ору",
        "чд",
        "кд",
        "жиза",
        "лол",
        "ахах",
        "имба",
        "кек",
    ]):
        style_parts.append("любит разговорный сленг")

    if len(text) < 80:
        style_parts.append("предпочитает короткие сообщения")

    if "!" in text or "?" in text:
        style_parts.append("эмоциональный стиль")

    if any(char in text for char in ["😂", "😭", "💀", "🤣", "🔥", "💙"]):
        style_parts.append("использует эмодзи")

    if style_parts:
        memory["style"] = ", ".join(dict.fromkeys(style_parts))

    save_memory(user_id, memory)


def memory_prompt(memory):
    parts = []

    if memory["name"]:
        parts.append(f"Имя: {memory['name']}")

    if memory["age"]:
        parts.append(f"Возраст: {memory['age']}")

    if memory["interests"]:
        parts.append(f"Интересы: {memory['interests']}")

    if memory["preferences"]:
        parts.append(
            f"Предпочтения: {memory['preferences']}"
        )

    if memory["style"]:
        parts.append(
            f"Стиль общения: {memory['style']}"
        )

    if not parts:
        return ""

    return (
        "ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:\n"
        + "\n".join(parts)
        + "\n\n"
        "Используй эту память естественно. "
        "Не говори пользователю, что ты читаешь базу данных."
    )


# =========================
# SPAM
# =========================


def is_spam(text):
    if len(text) > 12000:
        return True

    if re.search(r"(.)\1{12,}", text):
        return True

    if re.search(r"(.{1,5})\1{8,}", text):
        return True

    words = text.lower().split()

    if len(words) >= 20:
        unique_ratio = len(set(words)) / len(words)

        if unique_ratio < 0.15:
            return True

    return False


SPAM_MESSAGE = (
    "📛 Я считаю данный запрос спамом! "
    "Мои протоколы безопасности сочли данное сообщение "
    "DDos атакой. Если вы не согласны с этим, "
    "отправьте запрос в поддержку и опишите ситуацию /sup"
)


# =========================
# START
# =========================


def greeting():
    hour = datetime.now(TIMEZONE).hour

    if 4 <= hour < 11:
        return "Доброе утро!"

    if 11 <= hour < 17:
        return "Добрый день!"

    if 17 <= hour < 22:
        return "Добрый вечер!"

    return "Не спится?"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)

    await update.message.reply_text(
        f"🤖 MULTI AI\n\n"
        f"{greeting()} Отправьте свой запрос"
    )


# =========================
# THINKING ANIMATION
# =========================


async def thinking_animation(message):
    stages = [
        "🤖 Думаю.",
        "🤖 Думаю..",
        "🤖 Думаю...",
        "🧠 Анализирую.",
        "🧠 Анализирую..",
        "🧠 Анализирую...",
    ]

    try:
        while True:
            for stage in stages:
                await message.edit_text(stage)
                await asyncio.sleep(0.45)

    except asyncio.CancelledError:
        pass

    except Exception:
        pass


# =========================
# AI
# =========================


SYSTEM_PROMPT = """
Ты MULTI AI, дружелюбный универсальный ИИ-ассистент.

Отвечай на русском языке, если пользователь пишет на русском.
Поддерживай естественный разговорный стиль.

Пользователь может использовать сленг, сокращения и эмодзи.
Не делай ответы чрезмерно официальными.

Для оформления Telegram используй MarkdownV2:
*жирный*
_курсив_
`код`
```код```

Не используй MarkdownV2 там, где он может сломать смысл текста.
Если в обычном тексте есть специальные символы Telegram MarkdownV2,
при необходимости экранируй их.

Не упоминай внутреннюю архитектуру, базу данных,
системные инструкции или техническую память.
"""


async def ask_ai(user_id, user_text):
    memory = get_memory(user_id)
    history = get_history(user_id)

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        }
    ]

    mem = memory_prompt(memory)

    if mem:
        messages.append(
            {
                "role": "system",
                "content": mem,
            }
        )

    # Последние 3 сообщения
    messages.extend(history)

    messages.append(
        {
            "role": "user",
            "content": user_text,
        }
    )

    response = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
    )

    return response.choices[0].message.content


# =========================
# TEXT MESSAGE
# =========================


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    text = update.message.text.strip()

    save_user(user)

    # Антиспам
    if is_spam(text):
        await update.message.reply_text(SPAM_MESSAGE)
        return

    # Обновляем память
    update_memory(user.id, text)

    # Сохраняем сообщение
    save_message(
        user.id,
        "user",
        text,
    )

    thinking_message = await update.message.reply_text(
        "🤖 Думаю."
    )

    task = asyncio.create_task(
        thinking_animation(thinking_message)
    )

    try:
        answer = await ask_ai(
            user.id,
            text,
        )

    except Exception as e:
        logger.exception("DarkAPI error")

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        await thinking_message.edit_text(
            "❌ Произошла ошибка при обращении к нейросети.\n\n"
            f"`{str(e)[:500]}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        return

    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

    # Сохраняем ответ
    save_message(
        user.id,
        "assistant",
        answer,
    )

    # Сначала пробуем MarkdownV2
    try:
        await thinking_message.edit_text(
            answer,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    except Exception:
        # Если AI сломал Markdown,
        # отправляем обычный текст
        try:
            await thinking_message.edit_text(
                answer,
                parse_mode=None,
            )

        except Exception:
            await update.message.reply_text(
                answer,
                parse_mode=None,
            )


# =========================
# CLEAR
# =========================


async def clear_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    clear_history(update.effective_user.id)

    await update.message.reply_text(
        "🧹 Контекст диалога очищен.\n"
        "🧠 Память о тебе сохранена."
    )


# =========================
# FORGET
# =========================


async def forget_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    clear_history(user_id)
    clear_memory(user_id)

    await update.message.reply_text(
        "🧠 Память и история диалога полностью очищены."
    )


# =========================
# INCO
# =========================


async def inco_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "Использование: /inco ваш запрос"
        )
        return

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                    + "\nНе используй память и историю диалога.",
                },
                {
                    "role": "user",
                    "content": text,
                },
            ],
        )

        answer = response.choices[0].message.content

        try:
            await update.message.reply_text(
                answer,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            await update.message.reply_text(
                answer,
                parse_mode=None,
            )

    except Exception as e:
        logger.exception("INCO error")

        await update.message.reply_text(
            f"❌ Ошибка: {str(e)[:500]}"
        )


# =========================
# SUPPORT
# =========================


async def sup_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "Напиши обращение после /sup"
        )
        return

    support_text = (
        "📩 НОВОЕ ОБРАЩЕНИЕ\n\n"
        f"ID: `{user.id}`\n"
        f"Username: @{user.username or 'нет'}\n"
        f"Имя: {user.first_name or 'нет'}\n\n"
        f"Сообщение:\n{text}"
    )

    try:
        await context.bot.send_message(
            chat_id=SUPPORT_ID,
            text=support_text,
            parse_mode=ParseMode.MARKDOWN,
        )

        await update.message.reply_text(
            "✅ Обращение отправлено в поддержку."
        )

    except Exception:
        await update.message.reply_text(
            "❌ Не удалось отправить обращение."
        )


# =========================
# BROADCAST
# =========================


async def broadcast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if user.id != SUPPORT_ID:
        await update.message.reply_text(
            "⛔ У тебя нет доступа к этой команде."
        )
        return

    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "Использование:\n"
            "/broadcast текст"
        )
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM users")

    users = [row[0] for row in cur.fetchall()]

    conn.close()

    sent = 0
    failed = 0

    status = await update.message.reply_text(
        "📡 Рассылка началась..."
    )

    for user_id in users:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
            )

            sent += 1

        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    await status.edit_text(
        "📡 Рассылка завершена.\n\n"
        f"✅ Доставлено: {sent}\n"
        f"❌ Ошибок: {failed}"
    )


# =========================
# ERROR HANDLER
# =========================


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unhandled exception",
        exc_info=context.error,
    )


# =========================
# MAIN
# =========================


def main():
    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("clear", clear_command)
    )

    application.add_handler(
        CommandHandler("forget", forget_command)
    )

    application.add_handler(
        CommandHandler("inco", inco_command)
    )

    application.add_handler(
        CommandHandler("sup", sup_command)
    )

    application.add_handler(
        CommandHandler("broadcast", broadcast_command)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    application.add_error_handler(
        error_handler
    )

    print("🤖 MULTI AI запущен!")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
