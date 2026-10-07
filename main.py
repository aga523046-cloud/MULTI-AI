import os
import sqlite3
import logging
import time
from datetime import datetime, timedelta, timezone

from openai import AsyncOpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")

BASE_URL = "https://www.ai-funpay.ru/v1"
MODEL = "claude-opus-5-5"

DB_FILE = "multi_ai.db"

DAILY_LIMIT = 50_000
MAX_HISTORY = 20

TIMEZONE = timezone(timedelta(hours=3))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не найден в Replit Secrets.")

if not AI_API_KEY:
    raise RuntimeError("AI_API_KEY не найден в Replit Secrets.")

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

# =========================================================
# AI
# =========================================================

client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=AI_API_KEY,
)

SYSTEM_PROMPT = """
Ты MULTI AI — универсальный ИИ-помощник в Telegram.

Отвечай естественно, понятно и полезно.

Если пользователь пишет по-русски, отвечай по-русски.
Если пользователь пишет по-английски, отвечай по-английски.

Используй Telegram Markdown для красивого оформления.

Можно использовать:
*жирный текст*
_курсив_
`код`

Для больших фрагментов кода используй тройные обратные кавычки.

Используй подходящие эмодзи, но не спамь ими.

Длинные ответы структурируй заголовками, списками и короткими абзацами.

Если пользователь просит короткий ответ, отвечай коротко.

Не выдумывай факты. Если не уверен, честно скажи об этом.

Поддерживай контекст текущего чата.
"""

# =========================================================
# PACKAGES
# =========================================================

PACKAGES = {
    "5": {
        "stars": 5,
        "tokens": 5_000,
        "name": "5 000 токенов",
    },
    "25": {
        "stars": 25,
        "tokens": 50_000,
        "name": "50 000 токенов",
    },
    "50": {
        "stars": 50,
        "tokens": 125_000,
        "name": "125 000 токенов",
    },
}

# =========================================================
# DATABASE
# =========================================================

def db():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    connection = db()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            used_tokens INTEGER DEFAULT 0,
            bonus_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            stars_spent INTEGER DEFAULT 0,
            last_date TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            stars INTEGER NOT NULL,
            tokens INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    connection.commit()
    connection.close()

# =========================================================
# TIME
# =========================================================

def current_time():
    return datetime.now(TIMEZONE)


def current_date():
    return current_time().strftime("%Y-%m-%d")


def reset_countdown():
    now = current_time()

    tomorrow = (
        now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        + timedelta(days=1)
    )

    seconds = int((tomorrow - now).total_seconds())

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    return f"{hours} ч {minutes} мин"

# =========================================================
# USERS
# =========================================================

def get_user(user_id, username="", first_name=""):
    connection = db()
    cursor = connection.cursor()
    date = current_date()

    cursor.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    )

    user = cursor.fetchone()

    if user is None:
        cursor.execute(
            """
            INSERT INTO users (
                user_id,
                username,
                first_name,
                used_tokens,
                bonus_tokens,
                total_tokens,
                stars_spent,
                last_date
            )
            VALUES (?, ?, ?, 0, 0, 0, 0, ?)
            """,
            (
                user_id,
                username,
                first_name,
                date,
            ),
        )
        connection.commit()

    elif user["last_date"] != date:
        cursor.execute(
            """
            UPDATE users
            SET used_tokens = 0,
                last_date = ?
            WHERE user_id = ?
            """,
            (date, user_id),
        )
        connection.commit()

    cursor.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    )

    user = cursor.fetchone()
    connection.close()

    return user


def get_balance(user_id):
    user = get_user(user_id)

    free_left = max(
        0,
        DAILY_LIMIT - user["used_tokens"],
    )

    return {
        "used": user["used_tokens"],
        "free": free_left,
        "bonus": user["bonus_tokens"],
        "total": free_left + user["bonus_tokens"],
        "spent": user["total_tokens"],
        "stars": user["stars_spent"],
    }


def spend_tokens(user_id, amount):
    user = get_user(user_id)

    free_left = max(
        0,
        DAILY_LIMIT - user["used_tokens"],
    )

    free_used = min(free_left, amount)
    bonus_used = amount - free_used

    connection = db()
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE users
        SET used_tokens = used_tokens + ?,
            bonus_tokens = MAX(0, bonus_tokens - ?),
            total_tokens = total_tokens + ?
        WHERE user_id = ?
        """,
        (
            free_used,
            bonus_used,
            amount,
            user_id,
        ),
    )

    connection.commit()
    connection.close()


def add_bonus(user_id, tokens, stars):
    connection = db()
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE users
        SET bonus_tokens = bonus_tokens + ?,
            stars_spent = stars_spent + ?
        WHERE user_id = ?
        """,
        (
            tokens,
            stars,
            user_id,
        ),
    )

    connection.commit()
    connection.close()

# =========================================================
# CHATS
# =========================================================

def create_chat(user_id, title="Новый чат"):
    timestamp = current_time().isoformat()

    connection = db()
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO chats (
            user_id,
            title,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            title,
            timestamp,
            timestamp,
        ),
    )

    chat_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return chat_id


def get_chats(user_id):
    connection = db()
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT *
        FROM chats
        WHERE user_id = ?
        ORDER BY updated_at DESC
        """,
        (user_id,),
    )

    chats = cursor.fetchall()
    connection.close()

    return chats


def get_chat(chat_id, user_id):
    connection = db()
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT *
        FROM chats
        WHERE id = ?
        AND user_id = ?
        """,
        (chat_id, user_id),
    )

    chat = cursor.fetchone()
    connection.close()

    return chat


def update_chat_title(chat_id, user_id, title):
    connection = db()
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE chats
        SET title = ?,
            updated_at = ?
        WHERE id = ?
        AND user_id = ?
        """,
        (
            title,
            current_time().isoformat(),
            chat_id,
            user_id,
        ),
    )

    connection.commit()
    connection.close()


def clear_chat(chat_id):
    connection = db()
    cursor = connection.cursor()

    cursor.execute(
        "DELETE FROM messages WHERE chat_id = ?",
        (chat_id,),
    )

    connection.commit()
    connection.close()


def save_message(chat_id, role, content):
    connection = db()
    cursor = connection.cursor()
    timestamp = current_time().isoformat()

    cursor.execute(
        """
        INSERT INTO messages (
            chat_id,
            role,
            content,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            chat_id,
            role,
            content,
            timestamp,
        ),
    )

    cursor.execute(
        """
        UPDATE chats
        SET updated_at = ?
        WHERE id = ?
        """,
        (
            timestamp,
            chat_id,
        ),
    )

    connection.commit()
    connection.close()


def get_history(chat_id):
    connection = db()
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT role, content
        FROM messages
        WHERE chat_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            chat_id,
            MAX_HISTORY,
        ),
    )

    rows = cursor.fetchall()
    connection.close()

    rows.reverse()

    return [
        {
            "role": row["role"],
            "content": row["content"],
        }
        for row in rows
    ]

# =========================================================
# TOKEN ESTIMATION
# =========================================================

def estimate_tokens(text):
    if not text:
        return 0

    return max(1, len(text) // 4)


def response_tokens(response, fallback):
    try:
        usage = getattr(response, "usage", None)

        if usage:
            total = getattr(
                usage,
                "total_tokens",
                None,
            )

            if total is not None:
                return int(total)

    except Exception:
        pass

    return estimate_tokens(fallback)

# =========================================================
# KEYBOARDS
# =========================================================

def menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💬 МОИ ЧАТЫ",
                callback_data="menu_chats",
            ),
        ],
        [
            InlineKeyboardButton(
                "👤 МОЙ ПРОФИЛЬ",
                callback_data="menu_profile",
            ),
            InlineKeyboardButton(
                "⭐ КУПИТЬ ТОКЕНЫ",
                callback_data="menu_topup",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 СТАТИСТИКА",
                callback_data="menu_stats",
            ),
            InlineKeyboardButton(
                "ℹ️ ПОМОЩЬ",
                callback_data="menu_help",
            ),
        ],
    ])


def only_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "☰ МЕНЮ",
                callback_data="menu",
            ),
        ],
    ])


def chats_keyboard(user_id):
    chats = get_chats(user_id)
    buttons = []

    for chat in chats[:10]:
        buttons.append([
            InlineKeyboardButton(
                f"💬 {chat['title'][:35]}",
                callback_data=f"open_chat:{chat['id']}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "➕ НОВЫЙ ЧАТ",
            callback_data="new_chat",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "☰ МЕНЮ",
            callback_data="menu",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def topup_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⭐ 5 → 5 000",
                callback_data="buy:5",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔥 25 → 50 000 • +100%",
                callback_data="buy:25",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚀 50 → 125 000 • +150%",
                callback_data="buy:50",
            ),
        ],
        [
            InlineKeyboardButton(
                "☰ МЕНЮ",
                callback_data="menu",
            ),
        ],
    ])

# =========================================================
# COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    get_user(
        user.id,
        user.username or "",
        user.first_name or "",
    )

    chats = get_chats(user.id)

    if not chats:
        chat_id = create_chat(user.id)
    else:
        chat_id = chats[0]["id"]

    context.user_data["chat_id"] = chat_id

    await update.message.reply_text(
        "🧠 *Добро пожаловать в MULTI AI!*\n\n"
        "Модель: *Claude Opus 5.5*\n\n"
        "🎁 Каждый день: *50 000 токенов*\n"
        "⭐ Дополнительные токены: за Telegram Stars\n"
        "💬 История сохраняется в твоих чатах.\n\n"
        "Просто напиши сообщение!",
        parse_mode="Markdown",
        reply_markup=menu_keyboard(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "☰ *MULTI AI — МЕНЮ*",
        parse_mode="Markdown",
        reply_markup=menu_keyboard(),
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    balance = get_balance(user.id)

    await update.message.reply_text(
        "👤 *МОЙ ПРОФИЛЬ*\n\n"
        "🧠 Модель: `Claude Opus 5.5`\n\n"
        f"🎁 Бесплатно осталось: *{balance['free']:,}*\n"
        f"⭐ Бонусов осталось: *{balance['bonus']:,}*\n\n"
        f"💎 Всего доступно: *{balance['total']:,}*\n\n"
        f"⏳ До сброса: *{reset_countdown()}*",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    balance = get_balance(user.id)

    await update.message.reply_text(
        "📊 *СТАТИСТИКА*\n\n"
        f"🔥 Использовано сегодня: *{balance['used']:,}*\n"
        f"🎁 Бесплатно осталось: *{balance['free']:,}*\n"
        f"⭐ Бонусов осталось: *{balance['bonus']:,}*\n\n"
        f"💎 Доступно сейчас: *{balance['total']:,}*\n"
        f"⭐ Потрачено Stars: *{balance['stars']}*\n\n"
        f"⏳ До сброса: *{reset_countdown()}*",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *ПОМОЩЬ*\n\n"
        "🧠 MULTI AI использует Claude Opus 5.5.\n"
        "🎁 Каждый день доступно 50 000 токенов.\n"
        "⭐ Купленные токены не сгорают при ежедневном сбросе.\n"
        "💬 Каждый чат имеет собственную историю.\n\n"
        "Команды:\n"
        "`/start` — запуск\n"
        "`/menu` — меню\n"
        "`/profile` — профиль\n"
        "`/stats` — статистика\n"
        "`/topup` — купить токены\n"
        "`/clear` — очистить текущий чат",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )


async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⭐ *ТОКЕНЫ MULTI AI*\n\n"
        "⭐ 5 Stars → *5 000 токенов*\n\n"
        "🔥 25 Stars → *50 000 токенов*\n"
        "*НА 100% БОЛЬШЕ!*\n\n"
        "🚀 50 Stars → *125 000 токенов*\n"
        "*НА 150% БОЛЬШЕ!*",
        parse_mode="Markdown",
        reply_markup=topup_keyboard(),
    )


async def chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chats = get_chats(user.id)

    if not chats:
        chat_id = create_chat(user.id)
        context.user_data["chat_id"] = chat_id

    await update.message.reply_text(
        "💬 *МОИ ЧАТЫ*\n\n"
        "Выбери существующий чат или создай новый:",
        parse_mode="Markdown",
        reply_markup=chats_keyboard(user.id),
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = context.user_data.get("chat_id")

    if not chat_id:
        chats = get_chats(user.id)

        if chats:
            chat_id = chats[0]["id"]
        else:
            chat_id = create_chat(user.id)

        context.user_data["chat_id"] = chat_id

    clear_chat(chat_id)

    await update.message.reply_text(
        "🧹 *История текущего чата очищена.*",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )

# =========================================================
# AI
# =========================================================

async def ask_ai(text, history):
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        }
    ]

    messages.extend(history)

    messages.append({
        "role": "user",
        "content": text,
    })

    response = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
    )

    answer = (
        response.choices[0].message.content
        or "Не удалось получить ответ."
    )

    used = response_tokens(
        response,
        text + answer,
    )

    return answer, used


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    if not text:
        return

    user = update.effective_user

    get_user(
        user.id,
        user.username or "",
        user.first_name or "",
    )

    chat_id = context.user_data.get("chat_id")

    if chat_id is None:
        chats = get_chats(user.id)

        if chats:
            chat_id = chats[0]["id"]
        else:
            chat_id = create_chat(user.id)

        context.user_data["chat_id"] = chat_id

    if get_chat(chat_id, user.id) is None:
        chat_id = create_chat(user.id)
        context.user_data["chat_id"] = chat_id

    balance = get_balance(user.id)

    if balance["total"] <= 0:
        await update.message.reply_text(
            "🚫 *Токены закончились.*\n\n"
            "Открой магазин и пополни баланс ⭐",
            parse_mode="Markdown",
            reply_markup=topup_keyboard(),
        )
        return

    await update.message.chat.send_action(
        action=ChatAction.TYPING,
    )

    history = get_history(chat_id)

    try:
        answer, used = await ask_ai(
            text,
            history,
        )

        if used > balance["total"]:
            await update.message.reply_text(
                "⚠️ *Для этого ответа не хватает токенов.*\n\n"
                "Открой магазин и пополни баланс ⭐",
                parse_mode="Markdown",
                reply_markup=topup_keyboard(),
            )
            return

        spend_tokens(user.id, used)

        save_message(
            chat_id,
            "user",
            text,
        )

        save_message(
            chat_id,
            "assistant",
            answer,
        )

        chat = get_chat(chat_id, user.id)

        if chat and chat["title"] == "Новый чат":
            title = text.replace("\n", " ")[:40]

            update_chat_title(
                chat_id,
                user.id,
                title,
            )

        chunks = [
            answer[i:i + 4000]
            for i in range(0, len(answer), 4000)
        ]

        for chunk in chunks:
            try:
                await update.message.reply_text(
                    chunk,
                    parse_mode="Markdown",
                )
            except Exception:
                await update.message.reply_text(chunk)

        await update.message.reply_text(
            "☰",
            reply_markup=only_menu_keyboard(),
        )

    except Exception as error:
        logger.exception("AI REQUEST ERROR")

        await update.message.reply_text(
            "❌ *Ошибка MULTI AI*\n\n"
            f"`{str(error)[:700]}`",
            parse_mode="Markdown",
            reply_markup=only_menu_keyboard(),
        )

# =========================================================
# PAYMENTS
# =========================================================

async def send_payment(query, context, stars):
    package = PACKAGES[str(stars)]

    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title=f"MULTI AI • {package['name']}",
        description=package["name"],
        payload=f"multi_ai:{stars}:{query.from_user.id}",
        currency="XTR",
        prices=[
            LabeledPrice(
                package["name"],
                package["stars"],
            )
        ],
        provider_token="",
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    try:
        parts = query.invoice_payload.split(":")

        if len(parts) != 3:
            await query.answer(
                ok=False,
                error_message="Некорректный заказ.",
            )
            return

        if parts[0] != "multi_ai":
            await query.answer(
                ok=False,
                error_message="Неизвестный товар.",
            )
            return

        stars = int(parts[1])
        payload_user_id = int(parts[2])

        if payload_user_id != query.from_user.id:
            await query.answer(
                ok=False,
                error_message="Пользователь заказа не совпадает.",
            )
            return

        if str(stars) not in PACKAGES:
            await query.answer(
                ok=False,
                error_message="Такого пакета нет.",
            )
            return

        package = PACKAGES[str(stars)]

        if query.currency != "XTR":
            await query.answer(
                ok=False,
                error_message="Неверная валюта.",
            )
            return

        if query.total_amount != package["stars"]:
            await query.answer(
                ok=False,
                error_message="Неверная сумма.",
            )
            return

        await query.answer(ok=True)

    except Exception:
        logger.exception("PRECHECKOUT ERROR")

        await query.answer(
            ok=False,
            error_message="Не удалось проверить оплату.",
        )


async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment

    try:
        parts = payment.invoice_payload.split(":")

        if len(parts) != 3:
            return

        if parts[0] != "multi_ai":
            return

        stars = int(parts[1])
        user_id = int(parts[2])

        if user_id != update.effective_user.id:
            return

        if str(stars) not in PACKAGES:
            return

        package = PACKAGES[str(stars)]

        if payment.currency != "XTR":
            return

        if payment.total_amount != package["stars"]:
            return

        charge_id = payment.telegram_payment_charge_id

        connection = db()
        cursor = connection.cursor()

        cursor.execute(
            "SELECT charge_id FROM payments WHERE charge_id = ?",
            (charge_id,),
        )

        if cursor.fetchone():
            connection.close()
            return

        cursor.execute(
            """
            INSERT INTO payments (
                charge_id,
                user_id,
                stars,
                tokens,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                charge_id,
                user_id,
                package["stars"],
                package["tokens"],
                current_time().isoformat(),
            ),
        )

        connection.commit()
        connection.close()

        add_bonus(
            user_id,
            package["tokens"],
            package["stars"],
        )

        balance = get_balance(user_id)

        await update.message.reply_text(
            "🎉 *ОПЛАТА ПРОШЛА УСПЕШНО!*\n\n"
            f"⭐ Получено: *+{package['tokens']:,} токенов*\n\n"
            f"💎 Доступно сейчас: *{balance['total']:,}*",
            parse_mode="Markdown",
            reply_markup=menu_keyboard(),
        )

    except Exception:
        logger.exception("PAYMENT ERROR")

# =========================================================
# BUTTONS
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "menu":
        await query.edit_message_text(
            "☰ *MULTI AI — МЕНЮ*",
            parse_mode="Markdown",
            reply_markup=menu_keyboard(),
        )
        return

    if data == "menu_profile":
        balance = get_balance(user_id)

        await query.edit_message_text(
            "👤 *МОЙ ПРОФИЛЬ*\n\n"
            "🧠 Модель: `Claude Opus 5.5`\n\n"
            f"🎁 Бесплатно осталось: *{balance['free']:,}*\n"
            f"⭐ Бонусов осталось: *{balance['bonus']:,}*\n\n"
            f"💎 Всего доступно: *{balance['total']:,}*\n\n"
            f"⏳ До сброса: *{reset_countdown()}*",
            parse_mode="Markdown",
            reply_markup=only_menu_keyboard(),
        )
        return

    if data == "menu_stats":
        balance = get_balance(user_id)

        await query.edit_message_text(
            "📊 *СТАТИСТИКА*\n\n"
            f"🔥 Использовано сегодня: *{balance['used']:,}*\n"
            f"🎁 Бесплатно осталось: *{balance['free']:,}*\n"
            f"⭐ Бонусов осталось: *{balance['bonus']:,}*\n\n"
            f"💎 Доступно сейчас: *{balance['total']:,}*\n"
            f"⭐ Потрачено Stars: *{balance['stars']}*\n\n"
            f"⏳ До сброса: *{reset_countdown()}*",
            parse_mode="Markdown",
            reply_markup=only_menu_keyboard(),
        )
        return

    if data == "menu_help":
        await query.edit_message_text(
            "ℹ️ *ПОМОЩЬ*\n\n"
            "🧠 Claude Opus 5.5\n"
            "🎁 50 000 бесплатных токенов каждый день\n"
            "⭐ Дополнительные токены за Stars\n"
            "💬 Отдельная история для каждого чата\n\n"
            "Используй меню для управления MULTI AI.",
            parse_mode="Markdown",
            reply_markup=only_menu_keyboard(),
        )
        return

    if data == "menu_topup":
        await query.edit_message_text(
            "⭐ *ТОКЕНЫ MULTI AI*\n\n"
            "⭐ 5 Stars → *5 000 токенов*\n\n"
            "🔥 25 Stars → *50 000 токенов*\n"
            "*НА 100% БОЛЬШЕ!*\n\n"
            "🚀 50 Stars → *125 000 токенов*\n"
            "*НА 150% БОЛЬШЕ!*",
            parse_mode="Markdown",
            reply_markup=topup_keyboard(),
        )
        return

    if data == "menu_chats":
        await query.edit_message_text(
            "💬 *МОИ ЧАТЫ*\n\n"
            "Выбери чат или создай новый:",
            parse_mode="Markdown",
            reply_markup=chats_keyboard(user_id),
        )
        return

    if data == "new_chat":
        chat_id = create_chat(user_id)
        context.user_data["chat_id"] = chat_id

        await query.edit_message_text(
            "✨ *Новый чат создан!*\n\n"
            "Напиши первое сообщение.",
            parse_mode="Markdown",
            reply_markup=only_menu_keyboard(),
        )
        return

    if data.startswith("open_chat:"):
        chat_id = int(data.split(":")[1])

        chat = get_chat(chat_id, user_id)

        if chat is None:
            return

        context.user_data["chat_id"] = chat_id
        history = get_history(chat_id)

        if history:
            preview_parts = []

            for item in history[-4:]:
                prefix = "👤 " if item["role"] == "user" else "🧠 "
                preview_parts.append(
                    prefix + item["content"][:300]
                )

            preview = "\n\n".join(preview_parts)

            text = (
                f"💬 *{chat['title']}*\n\n"
                f"{preview}\n\n"
                "Продолжай диалог."
            )
        else:
            text = (
                f"💬 *{chat['title']}*\n\n"
                "Чат пока пуст."
            )

        try:
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=only_menu_keyboard(),
            )
        except Exception:
            await query.edit_message_text(
                text,
                reply_markup=only_menu_keyboard(),
            )

        return

    if data.startswith("buy:"):
        stars = int(data.split(":")[1])

        if str(stars) not in PACKAGES:
            return

        await send_payment(
            query,
            context,
            stars,
        )

# =========================================================
# APPLICATION
# =========================================================

def create_application():
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("menu", menu_command)
    )

    application.add_handler(
        CommandHandler("profile", profile_command)
    )

    application.add_handler(
        CommandHandler("stats", stats_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("topup", topup_command)
    )

    application.add_handler(
        CommandHandler("chats", chats_command)
    )

    application.add_handler(
        CommandHandler("clear", clear_command)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_handler(
        PreCheckoutQueryHandler(precheckout_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            payment_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    return application

# =========================================================
# MAIN
# =========================================================

def main():
    init_db()

    print("================================")
    print("🚀 MULTI AI")
    print("================================")
    print(f"Model: {MODEL}")
    print(f"Daily limit: {DAILY_LIMIT}")
    print("5 ⭐  = 5 000")
    print("25 ⭐ = 50 000")
    print("50 ⭐ = 125 000")
    print("================================")
    print("▶️ Запуск Telegram polling...")

    application = create_application()

    print("✅ MULTI AI запущен!")

    application.run_polling(
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    while True:
        try:
            main()

            print("🛑 Polling завершён.")
            break

        except KeyboardInterrupt:
            print("🛑 MULTI AI остановлен вручную.")
            break

        except Exception:
            logger.exception(
                "❌ Критическая ошибка. "
                "Перезапуск через 10 секунд..."
            )

            time.sleep(10)
