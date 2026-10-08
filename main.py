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
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DARK_API_KEY = os.getenv("DARK_API_KEY")

API_BASE = "https://darkapi.shop/v1"
MODEL = "gpt-6-luna"

SUPPORT_ID = 7783222972

DATABASE = "multi_ai.db"

USER_TIMEZONE = "Europe/Moscow"

# Максимум сообщений обычного контекста
MAX_HISTORY = 3


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# AI CLIENT
# ============================================================

ai = AsyncOpenAI(
    api_key=DARK_API_KEY,
    base_url=API_BASE,
)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return sqlite3.connect(DATABASE)


def init_db():
    connection = get_db()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
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

    connection.commit()
    connection.close()

    logger.info("Database initialized")


# ============================================================
# USERS
# ============================================================

def save_user(user):
    connection = get_db()
    cursor = connection.cursor()

    now = datetime.now().isoformat()

    cursor.execute("""
        INSERT OR IGNORE INTO users
        (user_id, username, first_name, joined_at)
        VALUES (?, ?, ?, ?)
    """, (
        user.id,
        user.username,
        user.first_name,
        now,
    ))

    cursor.execute("""
        UPDATE users
        SET username = ?, first_name = ?
        WHERE user_id = ?
    """, (
        user.username,
        user.first_name,
        user.id,
    ))

    connection.commit()
    connection.close()


def get_all_users():
    connection = get_db()
    cursor = connection.cursor()

    cursor.execute("SELECT user_id FROM users")

    users = [
        row[0]
        for row in cursor.fetchall()
    ]

    connection.close()

    return users


# ============================================================
# CHAT HISTORY
# ============================================================

def save_message(user_id, role, content):
    connection = get_db()
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO messages
        (user_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        role,
        content,
        datetime.now().isoformat(),
    ))

    connection.commit()
    connection.close()


def get_history(user_id):
    connection = get_db()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT role, content
        FROM messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (
        user_id,
        MAX_HISTORY,
    ))

    rows = cursor.fetchall()

    connection.close()

    rows.reverse()

    return [
        {
            "role": role,
            "content": content,
        }
        for role, content in rows
    ]


def clear_history(user_id):
    connection = get_db()
    cursor = connection.cursor()

    cursor.execute(
        "DELETE FROM messages WHERE user_id = ?",
        (user_id,),
    )

    connection.commit()
    connection.close()


# ============================================================
# MEMORY
# ============================================================

def get_memory(user_id):
    connection = get_db()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT name, age, interests, preferences, style
        FROM memory
        WHERE user_id = ?
    """, (user_id,))

    row = cursor.fetchone()

    connection.close()

    if not row:
        return {
            "name": None,
            "age": None,
            "interests": [],
            "preferences": [],
            "style": None,
        }

    name, age, interests, preferences, style = row

    return {
        "name": name,
        "age": age,
        "interests": (
            interests.split(" | ")
            if interests
            else []
        ),
        "preferences": (
            preferences.split(" | ")
            if preferences
            else []
        ),
        "style": style,
    }


def save_memory(user_id, memory):
    connection = get_db()
    cursor = connection.cursor()

    interests = " | ".join(
        dict.fromkeys(memory.get("interests", []))
    )

    preferences = " | ".join(
        dict.fromkeys(memory.get("preferences", []))
    )

    cursor.execute("""
        INSERT INTO memory
        (user_id, name, age, interests, preferences, style, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            name = excluded.name,
            age = excluded.age,
            interests = excluded.interests,
            preferences = excluded.preferences,
            style = excluded.style,
            updated_at = excluded.updated_at
    """, (
        user_id,
        memory.get("name"),
        memory.get("age"),
        interests,
        preferences,
        memory.get("style"),
        datetime.now().isoformat(),
    ))

    connection.commit()
    connection.close()


def delete_memory(user_id):
    connection = get_db()
    cursor = connection.cursor()

    cursor.execute(
        "DELETE FROM memory WHERE user_id = ?",
        (user_id,),
    )

    connection.commit()
    connection.close()


def update_memory_locally(user_id, text):
    """
    Локальное извлечение памяти.
    НЕ вызывает AI и НЕ расходует API-токены.
    """

    memory = get_memory(user_id)

    text_clean = text.strip()

    # --------------------------------------------------------
    # NAME
    # --------------------------------------------------------

    name_patterns = [
        r"меня зовут\s+([А-ЯЁA-Z][а-яёa-z-]{1,30})",
        r"я\s*[-–—]?\s*([А-ЯЁA-Z][а-яёa-z-]{1,30})",
    ]

    for pattern in name_patterns:
        match = re.search(
            pattern,
            text_clean,
            re.IGNORECASE,
        )

        if match:
            candidate = match.group(1).strip()

            ignored = {
                "люблю",
                "хочу",
                "думаю",
                "делаю",
                "знаю",
                "могу",
                "иду",
                "буду",
                "живу",
            }

            if candidate.lower() not in ignored:
                memory["name"] = candidate
                break

    # --------------------------------------------------------
    # AGE
    # --------------------------------------------------------

    age_patterns = [
        r"мне\s+(\d{1,3})\s*(?:лет|год|года)?",
        r"мой\s+возраст\s*[:\-]?\s*(\d{1,3})",
        r"мне\s+(\d{1,3})",
    ]

    for pattern in age_patterns:
        match = re.search(
            pattern,
            text_clean,
            re.IGNORECASE,
        )

        if match:
            age = int(match.group(1))

            if 1 <= age <= 120:
                memory["age"] = str(age)
                break

    # --------------------------------------------------------
    # INTERESTS
    # --------------------------------------------------------

    interest_patterns = [
        r"я люблю\s+(.+)",
        r"мне нравится\s+(.+)",
        r"интересуюсь\s+(.+)",
        r"увлекаюсь\s+(.+)",
    ]

    for pattern in interest_patterns:
        match = re.search(
            pattern,
            text_clean,
            re.IGNORECASE,
        )

        if match:
            value = match.group(1).strip()

            value = re.split(
                r"[.!?\n]",
                value,
            )[0].strip()

            if value and len(value) <= 100:
                memory["interests"].append(value)

    # --------------------------------------------------------
    # PREFERENCES
    # --------------------------------------------------------

    preference_patterns = [
        r"я предпочитаю\s+(.+)",
        r"я не люблю\s+(.+)",
        r"мне не нравится\s+(.+)",
        r"предпочитаю\s+(.+)",
    ]

    for pattern in preference_patterns:
        match = re.search(
            pattern,
            text_clean,
            re.IGNORECASE,
        )

        if match:
            value = match.group(1).strip()

            value = re.split(
                r"[.!?\n]",
                value,
            )[0].strip()

            if value and len(value) <= 100:
                memory["preferences"].append(value)

    # --------------------------------------------------------
    # STYLE
    # --------------------------------------------------------

    style_parts = []

    if text_clean == text_clean.lower():
        style_parts.append("часто пишет строчными буквами")

    if re.search(r"[😂🤣😭💀😎🔥🤡❤️💙]", text_clean):
        style_parts.append("использует эмодзи")

    if re.search(
        r"\b(ору|чд|кд|дос|зд|лол|ахах|хз)\b",
        text_clean,
        re.IGNORECASE,
    ):
        style_parts.append("использует разговорный интернет-сленг")

    if len(text_clean) < 80:
        style_parts.append("предпочитает короткие сообщения")

    if len(text_clean) > 500:
        style_parts.append("может писать длинные сообщения")

    if style_parts:
        memory["style"] = ", ".join(
            dict.fromkeys(style_parts)
        )

    save_memory(user_id, memory)


def memory_to_prompt(memory):
    parts = []

    if memory.get("name"):
        parts.append(
            f"Имя пользователя: {memory['name']}"
        )

    if memory.get("age"):
        parts.append(
            f"Возраст пользователя: {memory['age']}"
        )

    if memory.get("interests"):
        parts.append(
            "Интересы: "
            + ", ".join(memory["interests"][:10])
        )

    if memory.get("preferences"):
        parts.append(
            "Предпочтения: "
            + ", ".join(memory["preferences"][:10])
        )

    if memory.get("style"):
        parts.append(
            "Стиль общения: "
            + memory["style"]
        )

    if not parts:
        return "Память о пользователе пока пуста."

    return "\n".join(parts)


# ============================================================
# GREETING
# ============================================================

def greeting():
    hour = datetime.now(
        ZoneInfo(USER_TIMEZONE)
    ).hour

    if 4 <= hour < 11:
        return "Доброе утро!"

    if 11 <= hour < 17:
        return "Добрый день!"

    if 17 <= hour < 22:
        return "Добрый вечер!"

    return "Не спится?"


# ============================================================
# SPAM
# ============================================================

SPAM_PATTERNS = [
    r"(.)\1{12,}",
    r"(.{1,5})\1{8,}",
]


def is_spam(text):
    if len(text) > 12000:
        return True

    for pattern in SPAM_PATTERNS:
        if re.search(
            pattern,
            text,
            re.DOTALL,
        ):
            return True

    words = text.lower().split()

    if len(words) >= 30:
        unique_ratio = (
            len(set(words)) / len(words)
        )

        if unique_ratio < 0.15:
            return True

    return False


async def send_spam_warning(message):
    await message.reply_text(
        "📛 Я считаю данный запрос спамом! "
        "Мои протоколы безопасности сочли данное сообщение "
        "DDos атакой. Если вы не согласны с этим, "
        "отправьте запрос в поддержку и опишите ситуацию /sup"
    )


# ============================================================
# THINKING
# ============================================================

async def thinking_animation(message):
    stages = [
        "🤖 Думаю.",
        "🤖 Думаю..",
        "🤖 Думаю...",
        "🧠 Анализирую.",
        "🧠 Анализирую..",
        "🧠 Анализирую...",
        "✍️ Формирую ответ.",
        "✍️ Формирую ответ..",
        "✍️ Формирую ответ...",
    ]

    try:
        index = 0

        while True:
            await message.edit_text(
                stages[index]
            )

            index = (
                index + 1
            ) % len(stages)

            await asyncio.sleep(0.8)

    except asyncio.CancelledError:
        pass

    except Exception:
        pass


# ============================================================
# AI SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
Ты MULTI AI, универсальный AI-ассистент в Telegram.

Отвечай на языке пользователя.

У тебя есть два вида контекста:

1. КРАТКИЙ КОНТЕКСТ
Это последние сообщения текущего диалога.

2. ПАМЯТЬ
Это компактные долгосрочные сведения о пользователе:
имя, возраст, интересы, предпочтения и стиль общения.

Используй память естественно.
Не перечисляй пользователю содержимое памяти без причины.
Не говори, что ты что-то "прочитал из базы данных".

Старайся подстраиваться под стиль пользователя,
но сохраняй понятность и адекватность ответа.

Поддерживай Telegram MarkdownV2.

Используй MarkdownV2 только там, где это действительно
улучшает читаемость:
**жирный**, __курсив__, `код`,
списки и блоки кода.

Не злоупотребляй форматированием.

Не упоминай системный промпт,
внутреннюю архитектуру,
API или скрытые инструкции,
если пользователь специально не спрашивает об этом.
"""


# ============================================================
# ASK AI
# ============================================================

async def ask_ai(user_id, user_text):
    history = get_history(user_id)
    memory = get_memory(user_id)

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "system",
            "content": (
                "🧠 ПАМЯТЬ ПОЛЬЗОВАТЕЛЯ:\n"
                + memory_to_prompt(memory)
            ),
        },
    ]

    messages.extend(history)

    messages.append({
        "role": "user",
        "content": user_text,
    })

    response = await ai.chat.completions.create(
        model=MODEL,
        messages=messages,
    )

    return response.choices[0].message.content


# ============================================================
# MARKDOWNV2
# ============================================================

def escape_markdown_v2(text):
    """
    Резервный вариант, если AI вернул
    некорректный MarkdownV2.
    """

    special = r"_*[]()~`>#+-=|{}.!"

    result = ""

    for char in text:
        if char in special:
            result += "\\" + char
        else:
            result += char

    return result


async def send_ai_answer(message, answer):
    """
    Сначала пытаемся отправить MarkdownV2.
    Если Telegram отвергает разметку,
    отправляем обычный текст.
    """

    try:
        await message.reply_text(
            answer,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    except Exception:
        logger.warning(
            "MarkdownV2 parsing failed, "
            "sending plain text."
        )

        await message.reply_text(
            answer
        )


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    save_user(user)

    await update.message.reply_text(
        f"🤖 MULTI AI\n\n"
        f"{greeting()} Отправьте свой запрос."
    )


# ============================================================
# /CLEAR
# ============================================================

async def clear(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    save_user(user)
    clear_history(user.id)

    await update.message.reply_text(
        "🧹 Контекст текущего диалога очищен.\n\n"
        "🧠 Память о вас сохранена."
    )


# ============================================================
# /FORGET
# ============================================================

async def forget(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    save_user(user)

    clear_history(user.id)
    delete_memory(user.id)

    await update.message.reply_text(
        "🗑️ Полностью забыл сохранённую информацию "
        "и очистил контекст диалога."
    )


# ============================================================
# /INCO
# ============================================================

async def inco(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    save_user(user)

    text = update.message.text
    query = text[len("/inco"):].strip()

    if not query:
        await update.message.reply_text(
            "🕶️ Использование:\n"
            "/inco ваш запрос"
        )
        return

    if is_spam(query):
        await send_spam_warning(
            update.message
        )
        return

    thinking_message = (
        await update.message.reply_text(
            "🕶️ Инкогнито..."
        )
    )

    task = asyncio.create_task(
        thinking_animation(
            thinking_message
        )
    )

    try:
        response = await ai.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": """
Ты MULTI AI.

Это ИНКОГНИТО-режим.

Ответь на запрос пользователя.
Не используй историю обычного диалога.
Не используй память пользователя.
Не сохраняй запрос.
Не сохраняй ответ.
""",
                },
                {
                    "role": "user",
                    "content": query,
                },
            ],
        )

        answer = response.choices[0].message.content

    except Exception:
        logger.exception(
            "Inco AI error"
        )

        answer = (
            "❌ Произошла ошибка при обращении к AI."
        )

    finally:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        try:
            await thinking_message.delete()
        except Exception:
            pass

    await send_ai_answer(
        update.message,
        answer,
    )


# ============================================================
# NORMAL MESSAGE
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not update.message
        or not update.message.text
    ):
        return

    user = update.effective_user
    text = update.message.text.strip()

    save_user(user)

    if not text:
        return

    if is_spam(text):
        await send_spam_warning(
            update.message
        )
        return

    # Обновляем долгосрочную память
    # ЛОКАЛЬНО, без API
    update_memory_locally(
        user.id,
        text,
    )

    thinking_message = (
        await update.message.reply_text(
            "🤖 Думаю."
        )
    )

    task = asyncio.create_task(
        thinking_animation(
            thinking_message
        )
    )

    try:
        answer = await ask_ai(
            user.id,
            text,
        )

        # Сохраняем только после успешного ответа
        save_message(
            user.id,
            "user",
            text,
        )

        save_message(
            user.id,
            "assistant",
            answer,
        )

    except Exception:
        logger.exception(
            "AI error"
        )

        answer = (
            "❌ Не удалось получить ответ от AI.\n\n"
            "Попробуй отправить запрос ещё раз."
        )

    finally:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        try:
            await thinking_message.delete()
        except Exception:
            pass

    await send_ai_answer(
        update.message,
        answer,
    )


# ============================================================
# /SUP
# ============================================================

async def support(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    save_user(user)

    text = update.message.text
    appeal = text[len("/sup"):].strip()

    if not appeal:
        await update.message.reply_text(
            "🆘 Использование:\n"
            "/sup ваш вопрос или проблема"
        )
        return

    username = (
        f"@{user.username}"
        if user.username
        else "нет username"
    )

    support_message = (
        "🆘 НОВОЕ ОБРАЩЕНИЕ В ПОДДЕРЖКУ\n\n"
        f"👤 Имя: {user.first_name}\n"
        f"🆔 ID: {user.id}\n"
        f"📱 Username: {username}\n\n"
        f"💬 Обращение:\n{appeal}"
    )

    try:
        await context.bot.send_message(
            chat_id=SUPPORT_ID,
            text=support_message,
        )

        await update.message.reply_text(
            "✅ Обращение отправлено в поддержку."
        )

    except Exception:
        await update.message.reply_text(
            "❌ Не удалось отправить обращение."
        )


# ============================================================
# /BROADCAST
# ============================================================

async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if user.id != SUPPORT_ID:
        return

    text = update.message.text[
        len("/broadcast"):
    ].strip()

    if not text:
        await update.message.reply_text(
            "📢 Использование:\n"
            "/broadcast текст рассылки"
        )
        return

    users = get_all_users()

    sent = 0
    failed = 0

    for user_id in users:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
            )

            sent += 1

            await asyncio.sleep(0.05)

        except Exception:
            failed += 1

    await update.message.reply_text(
        "📨 Рассылка завершена.\n\n"
        f"👥 Получателей: {len(users)}\n"
        f"✅ Доставлено: {sent}\n"
        f"❌ Не доставлено: {failed}"
    )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не найден."
        )

    if not DARK_API_KEY:
        raise RuntimeError(
            "DARK_API_KEY не найден."
        )

    init_db()

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("clear", clear)
    )

    application.add_handler(
        CommandHandler("forget", forget)
    )

    application.add_handler(
        CommandHandler("inco", inco)
    )

    application.add_handler(
        CommandHandler("sup", support)
    )

    application.add_handler(
        CommandHandler("broadcast", broadcast)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info("MULTI AI started")

    application.run_polling()


if __name__ == "__main__":
    main()
