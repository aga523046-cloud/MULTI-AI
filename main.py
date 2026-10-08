import os
import re
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DARK_API_KEY = os.getenv("DARK_API_KEY")

DARK_API_URL = "https://darkapi.shop/v1"
MODEL = "gpt-6-luna"

SUPPORT_ID = 7783222972

TIMEZONE = ZoneInfo("Europe/Moscow")


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("MULTI_AI")


# ============================================================
# ПРОВЕРКА ПЕРЕМЕННЫХ
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "❌ Не найден TELEGRAM_BOT_TOKEN"
    )

if not DARK_API_KEY:
    raise RuntimeError(
        "❌ Не найден DARK_API_KEY"
    )


# ============================================================
# DARK API
# ============================================================

client = AsyncOpenAI(
    api_key=DARK_API_KEY,
    base_url=DARK_API_URL,
)


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
Ты MULTI AI, универсальный ИИ-ассистент.

Отвечай на языке пользователя.
Если пользователь пишет по-русски, отвечай по-русски.

Общайся естественно и дружелюбно.
Не будь чрезмерно официальным.

Используй Telegram MarkdownV2, когда это удобно.
"""


# ============================================================
# АНТИСПАМ
# ============================================================

SPAM_MESSAGE = (
    "📛 Я считаю данный запрос спамом! "
    "Мои протоколы безопасности сочли данное сообщение "
    "DDos атакой. Если вы не согласны с этим, "
    "отправьте запрос в поддержку и опишите ситуацию /sup"
)


def is_spam(text):
    # Слишком длинное сообщение
    if len(text) > 12000:
        return True

    # AAAAAAAAAAAAAAAAA
    if re.search(r"(.)\1{12,}", text):
        return True

    # ababababababab
    if re.search(r"(.{1,5})\1{8,}", text):
        return True

    # Одно и то же слово много раз
    words = text.lower().split()

    if len(words) >= 20:
        unique_ratio = len(set(words)) / len(words)

        if unique_ratio < 0.15:
            return True

    return False


# ============================================================
# ПРИВЕТСТВИЕ
# ============================================================

def get_greeting():
    hour = datetime.now(TIMEZONE).hour

    if 4 <= hour < 11:
        return "Доброе утро!"

    if 11 <= hour < 17:
        return "Добрый день!"

    if 17 <= hour < 22:
        return "Добрый вечер!"

    return "Не спится?"


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "🤖 MULTI AI\n\n"
        f"{get_greeting()} Отправьте свой запрос"
    )


# ============================================================
# АНИМАЦИЯ
# ============================================================

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
        index = 0

        while True:
            await message.edit_text(
                stages[index % len(stages)]
            )

            index += 1

            await asyncio.sleep(0.6)

    except asyncio.CancelledError:
        pass

    except Exception:
        pass


# ============================================================
# ЗАПРОС К DARK API
# ============================================================

async def ask_ai(text):
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": text,
            },
        ],
    )

    if not response.choices:
        raise RuntimeError(
            "DarkAPI не вернул ответ."
        )

    answer = response.choices[0].message.content

    if not answer:
        raise RuntimeError(
            "DarkAPI вернул пустой ответ."
        )

    return answer


# ============================================================
# ОБЫЧНЫЕ СООБЩЕНИЯ
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    if not update.message.text:
        return

    text = update.message.text.strip()

    if not text:
        return

    logger.info(
        "Получен запрос: %s",
        text[:100],
    )

    # -------------------------
    # АНТИСПАМ
    # -------------------------

    if is_spam(text):
        await update.message.reply_text(
            SPAM_MESSAGE
        )
        return

    # -------------------------
    # ОЖИДАНИЕ
    # -------------------------

    thinking = await update.message.reply_text(
        "🤖 Думаю..."
    )

    animation_task = asyncio.create_task(
        thinking_animation(thinking)
    )

    try:
        answer = await ask_ai(text)

    except Exception as error:
        logger.exception(
            "Ошибка DarkAPI"
        )

        animation_task.cancel()

        try:
            await animation_task
        except asyncio.CancelledError:
            pass

        await thinking.edit_text(
            "❌ Ошибка при обращении к нейросети:\n\n"
            + str(error)[:1500]
        )

        return

    animation_task.cancel()

    try:
        await animation_task
    except asyncio.CancelledError:
        pass

    # -------------------------
    # ОТВЕТ
    # -------------------------

    # Сначала пытаемся отправить как MarkdownV2.
    # Если AI сгенерировал некорректный Markdown,
    # отправляем обычный текст.
    try:
        from telegram.constants import ParseMode

        await thinking.edit_text(
            answer,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    except Exception:
        await thinking.edit_text(
            answer,
            parse_mode=None,
        )


# ============================================================
# INCO
# ============================================================

async def inco_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "Использование:\n"
            "/inco ваш запрос"
        )
        return

    try:
        answer = await ask_ai(text)

        try:
            from telegram.constants import ParseMode

            await update.message.reply_text(
                answer,
                parse_mode=ParseMode.MARKDOWN_V2,
            )

        except Exception:
            await update.message.reply_text(
                answer
            )

    except Exception as error:
        logger.exception(
            "Ошибка INCO"
        )

        await update.message.reply_text(
            "❌ Ошибка:\n"
            + str(error)[:1500]
        )


# ============================================================
# SUPPORT
# ============================================================

async def sup_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "Использование:\n"
            "/sup ваш текст"
        )
        return

    user = update.effective_user

    support_message = (
        "📩 НОВОЕ ОБРАЩЕНИЕ\n\n"
        f"ID: {user.id}\n"
        f"Username: @{user.username or 'нет'}\n"
        f"Имя: {user.first_name or 'нет'}\n\n"
        f"Сообщение:\n{text}"
    )

    try:
        await context.bot.send_message(
            chat_id=SUPPORT_ID,
            text=support_message,
        )

        await update.message.reply_text(
            "✅ Обращение отправлено в поддержку."
        )

    except Exception as error:
        logger.exception(
            "Ошибка поддержки"
        )

        await update.message.reply_text(
            "❌ Не удалось отправить обращение."
        )


# ============================================================
# BROADCAST
# ============================================================

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

    await update.message.reply_text(
        "⚠️ Рассылка временно отключена.\n"
        "В версии без базы данных бот не хранит список пользователей."
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unhandled error: %s",
        context.error,
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info("Запуск MULTI AI...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Команды
    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "inco",
            inco_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "sup",
            sup_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "broadcast",
            broadcast_command,
        )
    )

    # Обычные сообщения
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info("🤖 MULTI AI запущен!")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
