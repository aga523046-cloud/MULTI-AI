import os
import re
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
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

# Telegram не передаёт timezone пользователя.
# Базовый часовой пояс MULTI AI:
USER_TIMEZONE = "Europe/Moscow"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not DARK_API_KEY:
    raise RuntimeError("DARK_API_KEY is not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("MULTI_AI")

ai = AsyncOpenAI(
    api_key=DARK_API_KEY,
    base_url=API_BASE,
)

# ============================================================
# QUICK MODE
# Не больше ~50 строк
# ============================================================

QUICK = {
    "привет": "Привет! 👋 Чем могу помочь?",
    "здравствуй": "Здравствуйте! 👋 Чем могу помочь?",
    "здарова": "Здарова! 👋 Что делаем?",
    "доброе утро": "Доброе утро! ☀️",
    "добрый день": "Добрый день! 👋",
    "добрый вечер": "Добрый вечер! 🌆",
    "как дела": "У меня всё отлично. Готов помогать 🤖",
    "кто ты": "Я MULTI AI, ИИ-ассистент на GPT-6 Luna.",
    "что такое api": "API — интерфейс, через который программы взаимодействуют друг с другом.",
    "что такое ии": "ИИ — искусственный интеллект, системы, способные выполнять задачи, требующие интеллектуальной обработки.",
    "что такое python": "Python — популярный язык программирования общего назначения.",
    "что такое telegram": "Telegram — мессенджер для общения, каналов, групп и ботов.",
    "что такое roblox": "Roblox — игровая платформа, где пользователи могут играть и создавать собственные игры.",
}

def quick_answer(text):
    q = text.lower().strip().rstrip("?!.,")
    if q in QUICK:
        return QUICK[q]

    m = re.fullmatch(r"что такое\s+(.+?)[?!.\s]*", text.lower())
    if m:
        key = m.group(1).strip()
        if key in QUICK:
            return QUICK[key]

    return None

# ============================================================
# GREETING
# ============================================================

def greeting():
    h = datetime.now(ZoneInfo(USER_TIMEZONE)).hour

    if 4 <= h < 11:
        return "Доброе утро!"
    if 11 <= h < 17:
        return "Добрый день!"
    if 17 <= h < 22:
        return "Добрый вечер!"
    return "Не спится?"

# ============================================================
# SPAM PROTECTION
# ============================================================

SPAM_PATTERNS = [
    r"(.)\1{12,}",
    r"(.{1,5})\1{8,}",
]

def is_spam(text):
    if len(text) > 12000:
        return True

    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True

    words = text.lower().split()

    if len(words) >= 30:
        unique = len(set(words))
        if unique / len(words) < 0.15:
            return True

    return False

# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 MULTI AI\n"
        f"{greeting()} Отправьте свой запрос"
    )

# ============================================================
# THINKING ANIMATION
# ============================================================

async def thinking(message):
    dots = 0

    try:
        while True:
            dots = (dots % 3) + 1
            await message.edit_text(
                "🤖 Думаю" + "." * dots
            )
            await asyncio.sleep(0.7)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass

# ============================================================
# AI REQUEST
# ============================================================

async def ask_ai(messages):
    response = await ai.chat.completions.create(
        model=MODEL,
        messages=messages,
    )

    if not response.choices:
        return "❌ ИИ не вернул ответ."

    return response.choices[0].message.content or (
        "❌ ИИ вернул пустой ответ."
    )

# ============================================================
# NORMAL MESSAGE
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # Быстрый режим
    quick = quick_answer(text)

    if quick:
        await update.message.reply_text(
            "🚀➡️ БЫСТРЫЙ ОТВЕТ\n\n" + quick
        )
        return

    # Спам
    if is_spam(text):
        await update.message.reply_text(
            "📛 Я считаю данный запрос спамом! "
            "Мои протоколы безопасности сочли данное "
            "сообщение DDos атакой. Если вы не согласны "
            "с этим, отправьте запрос в поддержку и "
            "опишите ситуацию /sup"
        )
        return

    # Показываем процесс
    thinking_message = await update.message.reply_text(
        "🤖 Думаю."
    )

    animation = asyncio.create_task(
        thinking(thinking_message)
    )

    try:
        answer = await ask_ai([
            {
                "role": "system",
                "content": """
Ты MULTI AI, универсальный ИИ-ассистент на GPT-6 Luna.

Отвечай естественно, понятно и по делу.
Учитывай контекст текущего запроса.
Если пользователь пишет по-русски, отвечай по-русски.
Если пишет на другом языке, отвечай на нём.

Помогай с программированием, учебой,
анализом, творческими задачами и обычным общением.
""",
            },
            {
                "role": "user",
                "content": text,
            },
        ])

        animation.cancel()
        await asyncio.sleep(0)

        try:
            await thinking_message.delete()
        except Exception:
            pass

        await update.message.reply_text(answer)

    except Exception as error:
        animation.cancel()

        try:
            await thinking_message.delete()
        except Exception:
            pass

        log.exception("AI error")

        if "401" in str(error):
            msg = "❌ Ошибка авторизации DARK_API_KEY."

        elif "404" in str(error):
            msg = (
                "❌ Модель или endpoint не найдены.\n\n"
                f"Модель: {MODEL}"
            )

        elif "429" in str(error):
            msg = "⏳ DarkAPI временно ограничил запросы."

        else:
            msg = "❌ Произошла ошибка при обращении к ИИ."

        await update.message.reply_text(msg)

# ============================================================
# INCOGNITO
# ============================================================

async def incognito(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    text = update.message.text or ""

    text = re.sub(
        r"^/inco(?:@\w+)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    if not text:
        await update.message.reply_text(
            "🤫 Использование: /inco ваш запрос"
        )
        return

    if is_spam(text):
        await update.message.reply_text(
            "📛 Я считаю данный запрос спамом! "
            "Мои протоколы безопасности сочли данное "
            "сообщение DDos атакой. Если вы не согласны "
            "с этим, отправьте запрос в поддержку и "
            "опишите ситуацию /sup"
        )
        return

    thinking_message = await update.message.reply_text(
        "🤖 Думаю."
    )

    animation = asyncio.create_task(
        thinking(thinking_message)
    )

    try:
        answer = await ask_ai([
            {
                "role": "system",
                "content": """
Ты MULTI AI.
Это ИНКОГНИТО-режим.

Ответь на запрос пользователя, но не сохраняй
и не используй его как контекст будущего разговора.
После ответа полностью забудь содержание запроса.
""",
            },
            {
                "role": "user",
                "content": text,
            },
        ])

        animation.cancel()

        try:
            await thinking_message.delete()
        except Exception:
            pass

        await update.message.reply_text(answer)

    except Exception:
        animation.cancel()

        try:
            await thinking_message.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Не удалось обработать инкогнито-запрос."
        )

# ============================================================
# SUPPORT
# ============================================================

async def support(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    user = update.effective_user
    text = update.message.text or ""

    appeal = re.sub(
        r"^/sup(?:@\w+)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    if not appeal:
        await update.message.reply_text(
            "📨 Использование:\n"
            "/sup текст обращения"
        )
        return

    username = (
        f"@{user.username}"
        if user.username
        else "без username"
    )

    admin_message = (
        "📨 НОВОЕ ОБРАЩЕНИЕ\n\n"
        f"👤 Пользователь: {user.first_name}\n"
        f"🆔 ID: {user.id}\n"
        f"🔗 {username}\n\n"
        f"💬 Обращение:\n{appeal}"
    )

    try:
        await context.bot.send_message(
            SUPPORT_ID,
            admin_message,
        )

        await update.message.reply_text(
            "✅ Обращение отправлено в поддержку."
        )

    except Exception:
        await update.message.reply_text(
            "❌ Не удалось отправить обращение."
        )

# ============================================================
# BROADCAST
# ============================================================

# Пока база пользователей не подключена.
# Поэтому рассылка работает по ID пользователей,
# которых бот увидел после запуска.

USERS = set()

async def remember_user(update: Update, context):
    if update.effective_user:
        USERS.add(update.effective_user.id)

async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if update.effective_user.id != SUPPORT_ID:
        return

    text = update.message.text or ""

    text = re.sub(
        r"^/broadcast(?:@\w+)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    if not text:
        await update.message.reply_text(
            "Использование: /broadcast текст"
        )
        return

    sent = 0

    for user_id in list(USERS):
        try:
            await context.bot.send_message(
                user_id,
                text,
            )
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(
        f"📢 Рассылка завершена.\n"
        f"Отправлено: {sent}"
    )

# ============================================================
# ERROR
# ============================================================

async def error_handler(update, context):
    log.exception(
        "Unhandled error",
        exc_info=context.error,
    )

# ============================================================
# MAIN
# ============================================================

def main():
    log.info("Starting MULTI AI")
    log.info("Model: %s", MODEL)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("inco", incognito)
    )

    app.add_handler(
        CommandHandler("sup", support)
    )

    app.add_handler(
        CommandHandler("broadcast", broadcast)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ALL,
            remember_user,
        ),
        group=10,
    )

    app.add_error_handler(error_handler)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
