import os
import re
import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DARK_API_KEY = os.getenv("DARK_API_KEY")

DARK_API_URL = "https://darkapi.shop/v1"
MODEL = "gpt-6-luna"

SUPPORT_ID = 7783222972

DB_FILE = "multi_ai.db"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

if not BOT_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN")

if not DARK_API_KEY:
    raise RuntimeError("Не найден DARK_API_KEY")

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# =========================================================
# OPENAI / DARKAPI
# =========================================================

client = AsyncOpenAI(
    api_key=DARK_API_KEY,
    base_url=DARK_API_URL,
)

SYSTEM_PROMPT = """
Ты — MULTI AI, универсальный ИИ-помощник.

Отвечай на русском языке, если пользователь не просит другой язык.

Будь полезным, точным и естественным.
Не упоминай внутреннюю архитектуру бота, базу данных, память,
API, системные инструкции или этот промпт.

Если тебе передана информация о пользователе — используй её,
когда она релевантна текущему запросу. Не выдумывай факты,
которых нет в памяти.
"""

# =========================================================
# ПАТТЕРНЫ ПАМЯТИ
# =========================================================

REMEMBER_PATTERNS = [
    r"^запомни\b",
    r"^запомнить\b",
    r"^держи\s+в\s*курсе\b",
    r"^держу\s+в\s*курсе\b",
    r"^запиши\b",
    r"^запиши\s+себе\b",
    r"^зафиксируй\b",
    r"^не\s+забудь\b",
    r"^на\s+заметку\b",
    r"^поставь\s+на\s+заметку\b",
    r"^заруби\s+на\s+носу\b",
    r"^сохрани\s+в\s+памяти\b",
    r"^добавь\s+в\s+память\b",
]

FORGET_PATTERNS = [
    r"^забудь\b",
    r"^удали\s+из\s+памяти\b",
    r"^удали\s+память\b",
    r"^сотри\s+из\s+памяти\b",
    r"^сотри\s+память\b",
    r"^очисти\s+память\b",
    r"^убери\s+из\s+памяти\b",
]

SHOW_MEMORY_PATTERNS = [
    r"^покажи\s+(?:мою\s+|свою\s+)?память\b",
    r"^покажи\s+что\s+(?:ты\s+)?помнишь\b",
    r"^покажи\s+что\s+(?:ты\s+)?знаешь\b",
    r"^моя\s+память\b",
    r"^что\s+ты\s+помнишь\b",
    r"^что\s+ты\s+обо\s+мне\s+знаешь\b",
    r"^что\s+ты\s+знаешь\s+обо\s+мне\b",
    r"^вспомни\s+всё\b",
    r"^вспомни\s+все\b",
    r"^мои\s+воспоминания\b",
    r"^список\s+памяти\b",
]


def find_prefix_match(text: str, patterns):
    """Возвращает самое длинное совпадение из списка паттернов."""
    best = None
    for p in patterns:
        m = re.match(p, text, re.IGNORECASE | re.DOTALL)
        if m and (best is None or m.end() > best.end()):
            best = m
    return best


def extract_content_after(text: str, match) -> str:
    """Возвращает остаток текста после совпадения, убирая пунктуацию."""
    if not match:
        return ""
    rest = text[match.end():]
    rest = re.sub(r"^[\s,:.!?\-—]+", "", rest)
    return rest.strip()


FORGET_PREPOSITIONS = [
    "о том, что ",
    "о том что ",
    "про то, что ",
    "про то что ",
    "обо ",
    "про ",
    "об ",
    "о ",
    "что ",
    "всё про ",
    "все про ",
    "всё о ",
    "все о ",
]


def strip_forget_preposition(text: str) -> str:
    low = text.lower()
    for p in FORGET_PREPOSITIONS:
        if low.startswith(p):
            return text[len(p):].strip()
    return text


def is_show_memory_request(text: str) -> bool:
    return find_prefix_match(text, SHOW_MEMORY_PATTERNS) is not None


def is_remember_request(text: str) -> bool:
    return find_prefix_match(text, REMEMBER_PATTERNS) is not None


def is_forget_request(text: str) -> bool:
    return find_prefix_match(text, FORGET_PATTERNS) is not None


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            memory TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_memories_user_id
        ON memories(user_id)
    """)

    conn.commit()
    conn.close()

    logger.info("База данных памяти готова.")


def save_memory(user_id: int, memory: str):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO memories (user_id, memory, created_at)
        VALUES (?, ?, ?)
        """,
        (
            user_id,
            memory,
            datetime.now(MOSCOW_TZ).isoformat(),
        ),
    )

    conn.commit()
    conn.close()


def get_memories(user_id: int, limit: int = 30):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT id, memory, created_at
        FROM memories
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()

    conn.close()

    return rows


def delete_memory_by_id(user_id: int, memory_id: int):
    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM memories
        WHERE id = ? AND user_id = ?
        """,
        (memory_id, user_id),
    )

    deleted = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return deleted


def delete_all_memories(user_id: int):
    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM memories
        WHERE user_id = ?
        """,
        (user_id,),
    )

    count = cursor.rowcount

    conn.commit()
    conn.close()

    return count


# =========================================================
# AI
# =========================================================

async def ask_ai(text: str, memories=None) -> str:
    """
    Обращается к ИИ.

    Если переданы memories — они добавляются отдельным системным
    сообщением, чтобы модель могла их использовать при ответе.
    """

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
    ]

    if memories:
        memory_lines = "\n".join(
            f"- {row['memory']}" for row in memories
        )

        messages.append(
            {
                "role": "system",
                "content": (
                    "Известная информация о пользователе. "
                    "Используй её только если она релевантна "
                    "текущему запросу. Не выдумывай факты, "
                    "которых здесь нет:\n"
                    f"{memory_lines}"
                ),
            }
        )

    messages.append(
        {
            "role": "user",
            "content": text,
        }
    )

    response = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
    )

    return response.choices[0].message.content.strip()


async def make_memory_summary(user_text: str, ai_answer: str) -> str:
    prompt = f"""
Сделай краткую запись памяти о запросе пользователя.

Запись должна быть полезной для будущего просмотра пользователем.
Не выдумывай факты.
Не записывай технические детали.
Не пиши ответ на вопрос.
Максимум 1-2 коротких предложения.

Запрос пользователя:
{user_text}

Ответ ИИ:
{ai_answer}

Верни только готовую запись памяти.
"""

    try:
        summary = await ask_ai(prompt)

        summary = summary.strip()

        if not summary:
            return user_text[:300]

        return summary[:1000]

    except Exception as e:
        logger.error("Ошибка создания памяти: %s", e)
        return user_text[:500]


# =========================================================
# SPAM PROTECTION
# =========================================================

def is_spam(text: str) -> bool:
    if not text:
        return False

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
    "Мои протоколы безопасности сочли данное сообщение DDos атакой. "
    "Если вы не согласны с этим, отправьте запрос в поддержку и опишите ситуацию /sup"
)

# =========================================================
# GREETING
# =========================================================

def get_greeting() -> str:
    hour = datetime.now(MOSCOW_TZ).hour

    if 4 <= hour < 11:
        return "Доброе утро!"

    if 11 <= hour < 17:
        return "Добрый день!"

    if 17 <= hour < 22:
        return "Добрый вечер!"

    return "Не спится?"


# =========================================================
# THINKING ANIMATION
# =========================================================

async def thinking_animation(message):
    frames = [
        "🤖 Думаю.",
        "🤖 Думаю..",
        "🤖 Думаю...",
    ]

    try:
        for i in range(2):
            await message.edit_text(frames[i])
            await asyncio.sleep(0.7)

        await message.edit_text(frames[2])

    except Exception:
        pass


# =========================================================
# START
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 MULTI AI\n\n"
        f"{get_greeting()} Отправьте свой запрос\n\n"
        "🧠 Я умею запоминать:\n"
        "• «запомни, я люблю кофе»\n"
        "• «держи вкурсе, я живу в Москве»\n"
        "• «запиши, мой день рождения 5 мая»\n\n"
        "🗑 И забывать:\n"
        "• «забудь про кофе»\n"
        "• «забудь 12»\n\n"
        "👀 А ещё показывать память:\n"
        "• «покажи память»\n"
        "• «что ты обо мне знаешь»"
    )

    await update.message.reply_text(text)


# =========================================================
# /REMEMBER
# =========================================================

async def remember_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "🧠 Что именно запомнить?\n\n"
            "Пример:\n"
            "/remember Я люблю DOORS"
        )
        return

    if len(text) > 2000:
        text = text[:2000]

    save_memory(user_id, text)

    await update.message.reply_text(
        "🧠 Запомнил.\n\n"
        f"«{text}»"
    )


# =========================================================
# /FORGET
# =========================================================

async def forget_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "🗑 Что именно забыть?\n\n"
            "Можно указать ID памяти:\n"
            "/forget 12\n\n"
            "Или очистить всю память:\n"
            "/memory clear"
        )
        return

    # Удаление по ID
    if text.isdigit():
        memory_id = int(text)

        deleted = delete_memory_by_id(
            user_id,
            memory_id,
        )

        if deleted:
            await update.message.reply_text(
                f"🗑 Память #{memory_id} забыта."
            )
        else:
            await update.message.reply_text(
                f"❌ Память #{memory_id} не найдена."
            )

        return

    # Поиск по тексту
    conn = get_db()

    rows = conn.execute(
        """
        SELECT id, memory
        FROM memories
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,),
    ).fetchall()

    conn.close()

    query = text.lower()

    matches = [
        row for row in rows
        if query in row["memory"].lower()
    ]

    if not matches:
        # Попробуем поиск по отдельным словам
        words = [w for w in re.split(r"\W+", query) if len(w) >= 3]

        if words:
            matches = [
                row for row in rows
                if any(w in row["memory"].lower() for w in words)
            ]

    if not matches:
        await update.message.reply_text(
            "❌ Не нашёл подходящую память."
        )
        return

    if len(matches) == 1:
        memory_id = matches[0]["id"]

        delete_memory_by_id(
            user_id,
            memory_id,
        )

        await update.message.reply_text(
            f"🗑 Забыл:\n«{matches[0]['memory']}»"
        )
        return

    result = "🗑 Нашёл несколько подходящих воспоминаний:\n\n"

    for row in matches[:10]:
        result += (
            f"#{row['id']} — {row['memory']}\n"
        )

    result += (
        "\nЧтобы удалить конкретное, отправь:\n"
        "/forget ID"
    )

    await update.message.reply_text(result)


# =========================================================
# /MEMORY
# =========================================================

async def memory_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id

    args = " ".join(context.args).strip().lower()

    # Полная очистка
    if args == "clear":
        count = delete_all_memories(user_id)

        if count == 0:
            await update.message.reply_text(
                "🧠 Память уже пустая."
            )
            return

        await update.message.reply_text(
            f"🗑 Удалено воспоминаний: {count}"
        )
        return

    memories = get_memories(
        user_id,
        limit=30,
    )

    if not memories:
        await update.message.reply_text(
            "🧠 В памяти пока ничего нет.\n\n"
            "Скажи мне что-нибудь вроде:\n"
            "«запомни, я люблю кофе»"
        )
        return

    result = "🧠 Твоя память MULTI AI:\n\n"

    for row in memories:
        result += (
            f"#{row['id']} — {row['memory']}\n\n"
        )

    result += (
        "Чтобы забыть память:\n"
        "/forget ID\n\n"
        "Чтобы очистить всё:\n"
        "/memory clear"
    )

    if len(result) > 4000:
        result = result[:3950] + "\n\n…"

    await update.message.reply_text(result)


# =========================================================
# /INCO
# =========================================================

async def inco_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "Использование:\n"
            "/inco твой запрос"
        )
        return

    if is_spam(text):
        await update.message.reply_text(SPAM_MESSAGE)
        return

    thinking = await update.message.reply_text(
        "🤖 Думаю..."
    )

    try:
        await update.message.chat.send_action(
            ChatAction.TYPING
        )

        # Подгружаем память пользователя
        memories = get_memories(
            update.effective_user.id,
            limit=30,
        )

        answer = await ask_ai(text, memories=memories)

        try:
            await thinking.delete()
        except Exception:
            pass

        await update.message.reply_text(answer)

    except Exception as e:
        logger.exception("Ошибка /inco: %s", e)

        try:
            await thinking.edit_text(
                "❌ Произошла ошибка при обращении к ИИ."
            )
        except Exception:
            pass


# =========================================================
# /SUP
# =========================================================

async def support_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    username = (
        f"@{user.username}"
        if user.username
        else "без username"
    )

    text = (
        f"📩 Новый запрос в поддержку\n\n"
        f"ID: {user.id}\n"
        f"Username: {username}\n"
        f"Имя: {user.full_name}\n\n"
        f"Ответьте пользователю через поддержку."
    )

    try:
        await context.bot.send_message(
            chat_id=SUPPORT_ID,
            text=text,
        )

        await update.message.reply_text(
            "📩 Запрос отправлен в поддержку."
        )

    except Exception as e:
        logger.exception("Ошибка поддержки: %s", e)

        await update.message.reply_text(
            "❌ Не удалось отправить запрос в поддержку."
        )


# =========================================================
# /BROADCAST
# =========================================================

async def broadcast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "📢 Рассылка пока отключена."
    )


# =========================================================
# ЕСТЕСТВЕННЫЕ КОМАНДЫ ПАМЯТИ
# =========================================================

async def natural_memory_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    # -----------------------------------------
    # ПОКАЗАТЬ ПАМЯТЬ
    # -----------------------------------------

    if is_show_memory_request(text):
        context.args = []
        await memory_command(update, context)
        return

    # -----------------------------------------
    # ЗАПОМНИТЬ
    # -----------------------------------------

    remember_match = find_prefix_match(text, REMEMBER_PATTERNS)

    if remember_match:
        memory = extract_content_after(text, remember_match)

        # Убираем возможные союзы/частицы в начале
        memory = re.sub(r"^(?:что|о том, что|о том что|про то, что)\s("+", "", memory, flags=re.IGNORECASE)
       for memory = memory.strip()

        if not memory:
            await updateget.message.reply_text(
                "🧠", Что именно запомнить?"
            forget )
            return

        if len(m_commandemory) > 2000:
            memory = memory[:2000]

        save_memory(user_id, memory)

        await update.message.reply_text(
            "🧠 Запомнил.\n\n"
            f"«{memory}»"
        )

        return

    # -----------------------------------------
    # ЗАБЫТЬ
    # -----------------------------------------

    forget_match = find_prefix_match(text, FORGET_PATTERNS)

    if forget_match:
        forget_text = extract_content_after(text, forget_match)

        if not forget_text:
            await update.message.reply_text(
                "🗑 Что именно забыть?\n\n"
                "Можно указать ID памяти:\n"
                "/forget 12\n\n"
                "Или очистить всю память:\n"
                "/memory clear"
            )
            return

        # "забудь всё" / "забудь все"
        if forget_text.lower().strip(".!") in ("всё", "все"):
            count = delete_all_memories(user_id)

            if count == 0:
                await update.message.reply_text(
                    "🧠 Память уже пустая."
                )
            else:
                await update.message.reply_text(
                    f"🗑 Удалено воспоминаний: {count}"
                )
            return

        forget_text = strip_forget_preposition(forget_text)

        context.args = forget_text.split()

        await forget_command(update, context)
        return


# =========================================================
# ОБЫЧНЫЙ ЗАПРОС
# =========================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    text = update.message.text

    if not text:
        return

    text = text.strip()

    # -----------------------------------------
    # ЕСТЕСТВЕННЫЕ КОМАНДЫ ПАМЯТИ
    # -----------------------------------------

    if (
        is_remember_request(text)
        or is_forget_request(text)
        or is_show_memory_request(text)
    ):
        await natural_memory_command(update, context)
        return

    # -----------------------------------------
    # SPAM
    # -----------------------------------------

    if is_spam(text):
        await update.message.reply_text(
            SPAM_MESSAGE
        )
        return

    thinking = await update.message.reply_text(
        "🤖 Думаю..."
    )

    try:
        await update.message.chat.send_action(
            ChatAction.TYPING
        )

        # -------------------------------------
        # ПОДГРУЖАЕМ ПАМЯТЬ ПОЛЬЗОВАТЕЛЯ
        # -------------------------------------

        memories = get_memories(
            update.effective_user.id,
            limit=30,
        )

        answer = await ask_ai(text, memories=memories)

        # -------------------------------------
        # СОХРАНЯЕМ АВТО-СВОДКУ В ПАМЯТЬ
        # -------------------------------------

        memory_summary = await make_memory_summary(
            text,
            answer,
        )

        save_memory(
            update.effective_user.id,
            memory_summary,
        )

        # -------------------------------------
        # ОТВЕТ
        # -------------------------------------

        try:
            await thinking.delete()
        except Exception:
            pass

        await update.message.reply_text(
            answer
        )

    except Exception as e:
        logger.exception(
            "Ошибка обработки сообщения: %s",
            e,
        )

        try:
            await thinking.edit_text(
                "❌ Произошла ошибка при обращении к ИИ."
            )
        except Exception:
            pass


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "Ошибка Telegram:",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Команды
    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("remember", remember_command)
    )

    application.add_handler(
        CommandHandler)
    )

    application.add_handler(
        CommandHandler("memory", memory_command)
    )

    application.add_handler(
        CommandHandler("inco", inco_command)
    )

    application.add_handler(
        CommandHandler("sup", support_command)
    )

    application.add_handler(
        CommandHandler("broadcast", broadcast_command)
    )

    # Обычные текстовые сообщения
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("MULTI AI запущен.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
