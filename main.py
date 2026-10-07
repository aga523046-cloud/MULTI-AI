import os
import sqlite3
import logging
import random
import asyncio
from datetime import datetime, timedelta, timezone

from openai import AsyncOpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    BotCommand,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
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

TOKEN_MULTIPLIER = 2

TIMEZONE = timezone(timedelta(hours=3))


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("multi_ai")


# =========================================================
# ENV CHECK
# =========================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN не найден в Environment Variables."
    )

if not AI_API_KEY:
    raise RuntimeError(
        "AI_API_KEY не найден в Environment Variables."
    )


# =========================================================
# AI CLIENT
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


TITLE_PROMPT = """
Сгенерируй короткое название (максимум 40 символов) для чата
на основе первого сообщения пользователя.

Отвечай ТОЛЬКО названием.

Без кавычек.
Без точки в конце.
Без лишних слов.
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
    "100": {
        "stars": 100,
        "tokens": 250_000,
        "name": "250 000 токенов",
    },
    "40": {
        "stars": 40,
        "tokens": 75_000,
        "name": "75 000 токенов",
    },
}


# =========================================================
# DATABASE
# =========================================================

def db():
    connection = sqlite3.connect(
        DB_FILE,
        timeout=30,
    )

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
            last_date TEXT,
            joined_at TEXT
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

    seconds = max(
        0,
        int((tomorrow - now).total_seconds()),
    )

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    return f"{hours} ч {minutes} мин"


def is_before_next_monday():
    now = current_time()

    days_ahead = (-now.weekday()) % 7

    if days_ahead == 0:
        days_ahead = 7

    next_monday = (
        now + timedelta(days=days_ahead)
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    return now < next_monday


# =========================================================
# USERS
# =========================================================

def get_user(
    user_id,
    username="",
    first_name="",
):
    connection = db()

    try:
        cursor = connection.cursor()

        date = current_date()
        now_iso = current_time().isoformat()

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
                    last_date,
                    joined_at
                )
                VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?)
                """,
                (
                    user_id,
                    username,
                    first_name,
                    date,
                    now_iso,
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
                (
                    date,
                    user_id,
                ),
            )

            connection.commit()

        cursor.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        )

        return cursor.fetchone()

    finally:
        connection.close()


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
    if amount <= 0:
        return

    user = get_user(user_id)

    free_left = max(
        0,
        DAILY_LIMIT - user["used_tokens"],
    )

    free_used = min(
        free_left,
        amount,
    )

    bonus_used = amount - free_used

    connection = db()

    try:
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

    finally:
        connection.close()


def add_bonus(
    user_id,
    tokens,
    stars,
):
    connection = db()

    try:
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

    finally:
        connection.close()


def is_new_user(user_id):
    user = get_user(user_id)

    joined_at = user["joined_at"]

    if not joined_at:
        return False

    try:
        joined = datetime.fromisoformat(joined_at)

    except Exception:
        return False

    return (
        current_time() - joined
    ) < timedelta(hours=24)


# =========================================================
# CHATS
# =========================================================

def create_chat(
    user_id,
    title="Новый чат",
):
    timestamp = current_time().isoformat()

    connection = db()

    try:
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

        return chat_id

    finally:
        connection.close()


def get_chats(user_id):
    connection = db()

    try:
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

        return cursor.fetchall()

    finally:
        connection.close()


def get_chat(
    chat_id,
    user_id,
):
    connection = db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT *
            FROM chats
            WHERE id = ?
            AND user_id = ?
            """,
            (
                chat_id,
                user_id,
            ),
        )

        return cursor.fetchone()

    finally:
        connection.close()


def update_chat_title(
    chat_id,
    user_id,
    title,
):
    connection = db()

    try:
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

    finally:
        connection.close()


def clear_chat(
    chat_id,
    user_id,
):
    connection = db()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            DELETE FROM messages
            WHERE chat_id = ?
            AND EXISTS (
                SELECT 1
                FROM chats
                WHERE chats.id = ?
                AND chats.user_id = ?
            )
            """,
            (
                chat_id,
                chat_id,
                user_id,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def save_message(
    chat_id,
    role,
    content,
):
    connection = db()

    try:
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

    finally:
        connection.close()


def get_history(chat_id):
    connection = db()

    try:
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

    finally:
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
# DAILY DISCOUNT
# =========================================================

def get_daily_discount():
    now = current_time()

    if now.hour != 23:
        return None

    keys = ["5", "25", "50"]

    seed = int(
        now.strftime("%Y%m%d")
    )

    random.seed(seed)

    return random.choice(keys)


# =========================================================
# TOKEN ESTIMATION
# =========================================================

def estimate_tokens(text):
    if not text:
        return 0

    return max(
        1,
        len(text) // 4,
    )


def response_tokens(
    response,
    fallback,
):
    try:
        usage = getattr(
            response,
            "usage",
            None,
        )

        if usage:
            total = getattr(
                usage,
                "total_tokens",
                None,
            )

            if total is not None:
                return max(
                    1,
                    int(total),
                )

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
            )
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
            )
        ]
    ])


def chats_keyboard(user_id):
    chats = get_chats(user_id)

    buttons = []

    for chat in chats[:10]:
        title = str(
            chat["title"] or "Новый чат"
        )[:35]

        buttons.append([
            InlineKeyboardButton(
                f"💬 {title}",
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


def topup_keyboard(user_id=None):
    buttons = []

    if (
        user_id is not None
        and is_new_user(user_id)
    ):
        buttons.append([
            InlineKeyboardButton(
                "🎁 НОВИЧОК: 10 ⭐ → 15 000",
                callback_data="buy_starter",
            )
        ])

    daily_key = get_daily_discount()

    if daily_key:
        package = PACKAGES.get(daily_key)

        if package:
            discounted = max(
                1,
                int(package["stars"] * 0.75),
            )

            buttons.append([
                InlineKeyboardButton(
                    f"🌙 СКИДКА -25%: "
                    f"{discounted} ⭐ → "
                    f"{package['tokens']:,}",
                    callback_data=f"buy_daily:{daily_key}",
                )
            ])

    buttons.append([
        InlineKeyboardButton(
            "⭐ 5 → 5 000",
            callback_data="buy:5",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "🔥 25 → 50 000 • +100%",
            callback_data="buy:25",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "🚀 50 → 125 000 • +150%",
            callback_data="buy:50",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "👑 100 ⭐ → 250 000",
            callback_data="buy:100",
        )
    ])

    if (
        user_id is not None
        and is_new_user(user_id)
    ):
        buttons.append([
            InlineKeyboardButton(
                "⚡ НОВИЧОК: 40 ⭐ → 75 000",
                callback_data="buy:40",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "☰ МЕНЮ",
            callback_data="menu",
        )
    ])

    return InlineKeyboardMarkup(buttons)


# =========================================================
# START / COMMANDS
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.message:
        return

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


async def menu_command(
    update,
    context,
):
    if not update.message:
        return

    await update.message.reply_text(
        "☰ *MULTI AI — МЕНЮ*",
        parse_mode="Markdown",
        reply_markup=menu_keyboard(),
    )


async def profile_command(
    update,
    context,
):
    if not update.message or not update.effective_user:
        return

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


async def stats_command(
    update,
    context,
):
    if not update.message or not update.effective_user:
        return

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


async def help_command(
    update,
    context,
):
    if not update.message:
        return

    await update.message.reply_text(
        "ℹ️ *ПОМОЩЬ*\n\n"
        "🧠 MULTI AI использует Claude Opus 5.5.\n"
        "🎁 Каждый день доступно 50 000 токенов.\n"
        "⭐ Купленные токены не сгорают.\n"
        "💬 Каждый чат имеет собственную историю.\n\n"
        "Команды:\n"
        "`/start` — запуск\n"
        "`/menu` — меню\n"
        "`/profile` — профиль\n"
        "`/stats` — статистика\n"
        "`/topup` — купить токены\n"
        "`/chats` — мои чаты\n"
        "`/clear` — очистить текущий чат\n"
        "`/help` — помощь",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )


async def topup_command(
    update,
    context,
):
    if not update.message or not update.effective_user:
        return

    await update.message.reply_text(
        "⭐ *ТОКЕНЫ MULTI AI*\n\n"
        "Выбери подходящий пакет:",
        parse_mode="Markdown",
        reply_markup=topup_keyboard(
            update.effective_user.id
        ),
    )


async def chats_command(
    update,
    context,
):
    if not update.message or not update.effective_user:
        return

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


async def clear_command(
    update,
    context,
):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user

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

    clear_chat(
        chat_id,
        user.id,
    )

    await update.message.reply_text(
        "🧹 *История текущего чата очищена.*",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )


# =========================================================
# AI
# =========================================================

async def ask_ai(
    text,
    history,
):
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

    if not response.choices:
        raise RuntimeError(
            "AI не вернул вариантов ответа."
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


async def generate_chat_title(text):
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": TITLE_PROMPT,
                },
                {
                    "role": "user",
                    "content": text[:500],
                },
            ],
        )

        if not response.choices:
            return text.replace("\n", " ")[:40] or "Новый чат"

        title = (
            response.choices[0]
            .message
            .content
            or ""
        )

        title = (
            title
            .strip()
            .strip('"')
            .strip("'")
            .strip("«»")
            .strip()
        )

        title = title.split("\n")[0][:40]

        return title or "Новый чат"

    except Exception:
        logger.exception(
            "TITLE GENERATION ERROR"
        )

        return (
            text
            .replace("\n", " ")
            .strip()
            [:40]
            or "Новый чат"
        )


# =========================================================
# THINKING ANIMATION
# =========================================================

async def animated_thinking(
    message,
    stop_event,
):
    phrases = [
        "думаю…",
        "обрабатываю…",
        "анализирую…",
        "формирую ответ…",
        "уточняю детали…",
    ]

    percent = 0
    idx = 0

    while not stop_event.is_set():
        try:
            phrase = phrases[
                idx % len(phrases)
            ]

            try:
                await message.edit_text(
                    f"🤔 *{phrase}* ({percent}%)",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

            idx += 1

            percent = min(
                95,
                percent + random.randint(3, 12),
            )

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=1.2,
                )
            except asyncio.TimeoutError:
                pass

        except asyncio.CancelledError:
            break

        except Exception:
            break


# =========================================================
# SAFE TELEGRAM TEXT
# =========================================================

async def send_ai_chunks(
    message,
    answer,
):
    chunks = [
        answer[i:i + 4000]
        for i in range(
            0,
            len(answer),
            4000,
        )
    ]

    if not chunks:
        chunks = ["Не удалось получить ответ."]

    for chunk in chunks:
        try:
            await message.reply_text(
                chunk,
                parse_mode="Markdown",
            )

        except Exception:
            try:
                await message.reply_text(
                    chunk,
                )

            except Exception:
                logger.exception(
                    "FAILED TO SEND AI CHUNK"
                )


# =========================================================
# MESSAGE HANDLER
# =========================================================

async def message_handler(
    update,
    context,
):
    if (
        not update.message
        or not update.message.text
        or not update.effective_user
    ):
        return

    text = update.message.text.strip()

    if not text:
        return

    user = update.effective_user

    try:
        get_user(
            user.id,
            user.username or "",
            user.first_name or "",
        )

        chat_id = context.user_data.get(
            "chat_id"
        )

        if (
            chat_id is None
            or get_chat(
                chat_id,
                user.id,
            ) is None
        ):
            chat_id = create_chat(user.id)
            context.user_data["chat_id"] = chat_id

        balance = get_balance(user.id)

        if balance["total"] <= 0:
            await update.message.reply_text(
                "🚫 *Токены закончились.*\n\n"
                "Открой магазин и пополни баланс ⭐",
                parse_mode="Markdown",
                reply_markup=topup_keyboard(user.id),
            )
            return

        try:
            await update.message.chat.send_action(
                action=ChatAction.TYPING
            )
        except Exception:
            pass

        try:
            thinking_msg = await update.message.reply_text(
                "🤔 *думаю…* (0%)",
                parse_mode="Markdown",
            )
        except Exception:
            thinking_msg = None

        stop_event = asyncio.Event()

        animation_task = None

        if thinking_msg:
            animation_task = asyncio.create_task(
                animated_thinking(
                    thinking_msg,
                    stop_event,
                )
            )

        try:
            history = get_history(chat_id)

            answer, used = await ask_ai(
                text,
                history,
            )

            used = max(
                1,
                used * TOKEN_MULTIPLIER,
            )

            balance = get_balance(user.id)

            if used > balance["total"]:
                stop_event.set()

                if animation_task:
                    try:
                        await animation_task
                    except Exception:
                        pass

                if thinking_msg:
                    try:
                        await thinking_msg.edit_text(
                            "⚠️ *Для этого ответа "
                            "не хватает токенов.*\n\n"
                            "Открой магазин и пополни баланс ⭐",
                            parse_mode="Markdown",
                            reply_markup=topup_keyboard(user.id),
                        )
                    except Exception:
                        pass

                return

            spend_tokens(
                user.id,
                used,
            )

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

            chat = get_chat(
                chat_id,
                user.id,
            )

            if (
                chat
                and chat["title"] == "Новый чат"
            ):
                title = await generate_chat_title(
                    text
                )

                update_chat_title(
                    chat_id,
                    user.id,
                    title,
                )

            stop_event.set()

            if animation_task:
                try:
                    await animation_task
                except Exception:
                    pass

            if thinking_msg:
                try:
                    await thinking_msg.delete()
                except Exception:
                    pass

            await send_ai_chunks(
                update.message,
                answer,
            )

            try:
                await update.message.reply_text(
                    "Рад был помочь! Выход в меню — /menu"
                )
            except Exception:
                pass

        except asyncio.CancelledError:
            stop_event.set()

            if animation_task:
                animation_task.cancel()

            raise

        except Exception:
            logger.exception(
                "AI REQUEST ERROR"
            )

            stop_event.set()

            if animation_task:
                try:
                    await animation_task
                except Exception:
                    pass

            if thinking_msg:
                try:
                    await thinking_msg.delete()
                except Exception:
                    pass

            try:
                await update.message.reply_text(
                    "❌ Не удалось получить ответ от AI.\n\n"
                    "Попробуй ещё раз через несколько секунд."
                )
            except Exception:
                pass

    except Exception:
        logger.exception(
            "MESSAGE HANDLER ERROR"
        )

        try:
            await update.message.reply_text(
                "❌ Произошла внутренняя ошибка.\n"
                "Попробуй ещё раз."
            )
        except Exception:
            pass


# =========================================================
# PAYMENTS
# =========================================================

async def send_payment(
    query,
    context,
    title,
    description,
    payload,
    stars,
):
    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title=title[:32],
        description=description[:255],
        payload=payload,
        currency="XTR",
        prices=[
            LabeledPrice(
                description[:32],
                stars,
            )
        ],
        provider_token="",
    )


async def precheckout_handler(
    update,
    context,
):
    query = update.pre_checkout_query

    try:
        parts = query.invoice_payload.split(":")

        if (
            len(parts) < 3
            or parts[0] != "multi_ai"
        ):
            await query.answer(
                ok=False,
                error_message="Неизвестный товар.",
            )
            return

        kind = parts[1]

        if kind == "base":
            if len(parts) != 4:
                await query.answer(
                    ok=False,
                    error_message="Некорректный заказ.",
                )
                return

            stars = int(parts[2])
            user_id = int(parts[3])

            if user_id != query.from_user.id:
                await query.answer(
                    ok=False,
                    error_message="Заказ принадлежит другому пользователю.",
                )
                return

            package = PACKAGES.get(
                str(stars)
            )

            if not package:
                await query.answer(
                    ok=False,
                    error_message="Пакет не найден.",
                )
                return

            if (
                stars == 100
                and not is_before_next_monday()
            ):
                await query.answer(
                    ok=False,
                    error_message="Акция завершена.",
                )
                return

            if (
                stars == 40
                and not is_new_user(user_id)
            ):
                await query.answer(
                    ok=False,
                    error_message="Акция только для новичков.",
                )
                return

            if query.total_amount != package["stars"]:
                await query.answer(
                    ok=False,
                    error_message="Неверная сумма.",
                )
                return

        elif kind == "starter":
            if len(parts) != 3:
                await query.answer(
                    ok=False,
                    error_message="Некорректный заказ.",
                )
                return

            user_id = int(parts[2])

            if user_id != query.from_user.id:
                await query.answer(
                    ok=False,
                    error_message="Заказ принадлежит другому пользователю.",
                )
                return

            if not is_new_user(user_id):
                await query.answer(
                    ok=False,
                    error_message="Акция только для новичков.",
                )
                return

            if query.total_amount != 10:
                await query.answer(
                    ok=False,
                    error_message="Неверная сумма.",
                )
                return

        elif kind == "daily":
            if len(parts) != 3:
                await query.answer(
                    ok=False,
                    error_message="Некорректный заказ.",
                )
                return

            key = parts[2]

            daily_key = get_daily_discount()

            if key != daily_key:
                await query.answer(
                    ok=False,
                    error_message="Скидка неактивна.",
                )
                return

            package = PACKAGES.get(key)

            if not package:
                await query.answer(
                    ok=False,
                    error_message="Пакет не найден.",
                )
                return

            discounted = max(
                1,
                int(package["stars"] * 0.75),
            )

            if query.total_amount != discounted:
                await query.answer(
                    ok=False,
                    error_message="Неверная сумма.",
                )
                return

        else:
            await query.answer(
                ok=False,
                error_message="Неизвестный тип заказа.",
            )
            return

        await query.answer(ok=True)

    except asyncio.CancelledError:
        raise

    except Exception:
        logger.exception(
            "PRECHECKOUT ERROR"
        )

        try:
            await query.answer(
                ok=False,
                error_message="Не удалось проверить оплату.",
            )
        except Exception:
            pass


async def payment_handler(
    update,
    context,
):
    if (
        not update.message
        or not update.message.successful_payment
        or not update.effective_user
    ):
        return

    payment = update.message.successful_payment
    user_id = update.effective_user.id

    try:
        parts = payment.invoice_payload.split(":")

        if (
            len(parts) < 3
            or parts[0] != "multi_ai"
        ):
            return

        kind = parts[1]

        tokens_to_add = 0
        stars_amount = payment.total_amount
        label = ""

        if kind == "base":
            if len(parts) != 4:
                return

            stars = int(parts[2])
            payload_user = int(parts[3])

            if payload_user != user_id:
                return

            package = PACKAGES.get(
                str(stars)
            )

            if not package:
                return

            if (
                stars == 100
                and not is_before_next_monday()
            ):
                return

            if (
                stars == 40
                and not is_new_user(user_id)
            ):
                return

            if payment.total_amount != package["stars"]:
                return

            tokens_to_add = package["tokens"]
            label = package["name"]

        elif kind == "starter":
            if len(parts) != 3:
                return

            payload_user = int(parts[2])

            if payload_user != user_id:
                return

            if not is_new_user(user_id):
                return

            if payment.total_amount != 10:
                return

            tokens_to_add = 15_000
            label = "Стартовый пакет 15 000"

        elif kind == "daily":
            if len(parts) != 3:
                return

            key = parts[2]
            package = PACKAGES.get(key)

            if not package:
                return

            if get_daily_discount() != key:
                return

            discounted = max(
                1,
                int(package["stars"] * 0.75),
            )

            if payment.total_amount != discounted:
                return

            tokens_to_add = package["tokens"]
            label = f"Скидка 25%: {package['name']}"

        else:
            return

        if tokens_to_add <= 0:
            return

        charge_id = (
            payment.telegram_payment_charge_id
        )

        connection = db()

        try:
            cursor = connection.cursor()

            cursor.execute(
                """
                SELECT charge_id
                FROM payments
                WHERE charge_id = ?
                """,
                (charge_id,),
            )

            if cursor.fetchone():
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
                    stars_amount,
                    tokens_to_add,
                    current_time().isoformat(),
                ),
            )

            connection.commit()

        finally:
            connection.close()

        add_bonus(
            user_id,
            tokens_to_add,
            stars_amount,
        )

        balance = get_balance(user_id)

        await update.message.reply_text(
            "🎉 *ОПЛАТА ПРОШЛА УСПЕШНО!*\n\n"
            f"🎁 {label}\n"
            f"⭐ Получено: *+{tokens_to_add:,} токенов*\n\n"
            f"💎 Доступно сейчас: "
            f"*{balance['total']:,}*",
            parse_mode="Markdown",
            reply_markup=menu_keyboard(),
        )

    except Exception:
        logger.exception(
            "PAYMENT ERROR"
        )

        try:
            await update.message.reply_text(
                "⚠️ Платёж получен, но произошла "
                "ошибка при зачислении.\n"
                "Не покупай пакет повторно. "
                "Попробуй открыть меню ещё раз."
            )
        except Exception:
            pass


# =========================================================
# BUTTONS
# =========================================================

async def button_handler(
    update,
    context,
):
    query = update.callback_query

    if not query:
        return

    try:
        await query.answer()
    except Exception:
        pass

    try:
        user_id = query.from_user.id
        data = query.data or ""

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
                f"🎁 Бесплатно осталось: "
                f"*{balance['free']:,}*\n"
                f"⭐ Бонусов осталось: "
                f"*{balance['bonus']:,}*\n\n"
                f"💎 Всего доступно: "
                f"*{balance['total']:,}*\n\n"
                f"⏳ До сброса: "
                f"*{reset_countdown()}*",
                parse_mode="Markdown",
                reply_markup=only_menu_keyboard(),
            )
            return

        if data == "menu_stats":
            balance = get_balance(user_id)

            await query.edit_message_text(
                "📊 *СТАТИСТИКА*\n\n"
                f"🔥 Использовано сегодня: "
                f"*{balance['used']:,}*\n"
                f"🎁 Бесплатно осталось: "
                f"*{balance['free']:,}*\n"
                f"⭐ Бонусов осталось: "
                f"*{balance['bonus']:,}*\n\n"
                f"💎 Доступно сейчас: "
                f"*{balance['total']:,}*\n"
                f"⭐ Потрачено Stars: "
                f"*{balance['stars']}*\n\n"
                f"⏳ До сброса: "
                f"*{reset_countdown()}*",
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
                "Выбери подходящий пакет:",
                parse_mode="Markdown",
                reply_markup=topup_keyboard(user_id),
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
            try:
                chat_id = int(
                    data.split(":", 1)[1]
                )
            except (ValueError, IndexError):
                return

            chat = get_chat(
                chat_id,
                user_id,
            )

            if chat is None:
                return

            context.user_data["chat_id"] = chat_id

            history = get_history(chat_id)

            if history:
                preview_parts = []

                for item in history[-4:]:
                    prefix = (
                        "👤 "
                        if item["role"] == "user"
                        else "🧠 "
                    )

                    preview_parts.append(
                        prefix
                        + item["content"][:300]
                    )

                preview = "\n\n".join(
                    preview_parts
                )

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
                try:
                    await query.edit_message_text(
                        text,
                        reply_markup=only_menu_keyboard(),
                    )
                except Exception:
                    pass

            return

        # =================================================
        # PURCHASES
        # =================================================

        if data == "buy_starter":
            if not is_new_user(user_id):
                try:
                    await query.answer(
                        "Акция только для новичков",
                        show_alert=True,
                    )
                except Exception:
                    pass
                return

            try:
                await send_payment(
                    query,
                    context,
                    "MULTI AI • Стартовый пакет",
                    "15 000 токенов (+50% бонус)",
                    f"multi_ai:starter:{user_id}",
                    10,
                )
            except Exception:
                logger.exception(
                    "STARTER PAYMENT ERROR"
                )

                try:
                    await query.message.reply_text(
                        "❌ Не удалось открыть оплату. "
                        "Попробуй ещё раз."
                    )
                except Exception:
                    pass

            return

        if data.startswith("buy_daily:"):
            key = data.split(":", 1)[1]

            if get_daily_discount() != key:
                try:
                    await query.answer(
                        "Скидка сейчас неактивна",
                        show_alert=True,
                    )
                except Exception:
                    pass
                return

            package = PACKAGES.get(key)

            if not package:
                return

            discounted = max(
                1,
                int(package["stars"] * 0.75),
            )

            try:
                await send_payment(
                    query,
                    context,
                    f"MULTI AI • Скидка 25% "
                    f"({package['name']})",
                    package["name"],
                    f"multi_ai:daily:{key}",
                    discounted,
                )
            except Exception:
                logger.exception(
                    "DAILY PAYMENT ERROR"
                )

                try:
                    await query.message.reply_text(
                        "❌ Не удалось открыть оплату. "
                        "Попробуй ещё раз."
                    )
                except Exception:
                    pass

            return

        if data.startswith("buy:"):
            try:
                stars = int(
                    data.split(":", 1)[1]
                )
            except (ValueError, IndexError):
                return

            package = PACKAGES.get(
                str(stars)
            )

            if not package:
                return

            if (
                stars == 100
                and not is_before_next_monday()
            ):
                try:
                    await query.answer(
                        "Акция завершена",
                        show_alert=True,
                    )
                except Exception:
                    pass
                return

            if (
                stars == 40
                and not is_new_user(user_id)
            ):
                try:
                    await query.answer(
                        "Акция только для новичков",
                        show_alert=True,
                    )
                except Exception:
                    pass
                return

            try:
                await send_payment(
                    query,
                    context,
                    f"MULTI AI • {package['name']}",
                    package["name"],
                    f"multi_ai:base:{stars}:{user_id}",
                    package["stars"],
                )
            except Exception:
                logger.exception(
                    "PACKAGE PAYMENT ERROR"
                )

                try:
                    await query.message.reply_text(
                        "❌ Не удалось открыть оплату. "
                        "Попробуй ещё раз."
                    )
                except Exception:
                    pass

            return

    except Exception:
        logger.exception(
            "BUTTON HANDLER ERROR"
        )

        try:
            await query.message.reply_text(
                "❌ Не удалось выполнить действие. "
                "Попробуй ещё раз."
            )
        except Exception:
            pass


# =========================================================
# GLOBAL ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context,
):
    error = context.error

    logger.error(
        "UNHANDLED TELEGRAM ERROR: %r",
        error,
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        )
        if error
        else None,
    )


# =========================================================
# TELEGRAM STARTUP
# =========================================================

async def post_init(
    application: Application,
):
    logger.info(
        "Проверка подключения к Telegram..."
    )

    try:
        me = await application.bot.get_me()

        logger.info(
            "Telegram OK: @%s | id=%s",
            me.username,
            me.id,
        )

    except Exception:
        logger.exception(
            "Не удалось подключиться к Telegram."
        )

        # Если Telegram недоступен при запуске,
        # приложение действительно не сможет работать.
        raise

    try:
        await application.bot.delete_webhook(
            drop_pending_updates=True
        )

        logger.info(
            "Webhook удалён. Polling готов."
        )

    except Exception:
        logger.exception(
            "Не удалось удалить webhook."
        )

        # Не даём второстепенной операции
        # сломать запуск приложения.
        logger.warning(
            "Продолжаю запуск без удаления webhook."
        )

    try:
        await application.bot.set_my_commands([
            BotCommand(
                "start",
                "Запуск",
            ),
            BotCommand(
                "menu",
                "Меню",
            ),
            BotCommand(
                "chats",
                "Мои чаты",
            ),
            BotCommand(
                "profile",
                "Профиль",
            ),
            BotCommand(
                "stats",
                "Статистика",
            ),
            BotCommand(
                "topup",
                "Купить токены",
            ),
            BotCommand(
                "clear",
                "Очистить чат",
            ),
            BotCommand(
                "help",
                "Помощь",
            ),
        ])

        logger.info(
            "Команды Telegram установлены."
        )

    except Exception:
        logger.exception(
            "Не удалось установить команды."
        )

        logger.warning(
            "Продолжаю работу без обновления списка команд."
        )


# =========================================================
# APPLICATION
# =========================================================

def create_application():
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_error_handler(
        error_handler
    )

    # -----------------------------------------------------
    # COMMANDS
    # -----------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "menu",
            menu_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "profile",
            profile_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "topup",
            topup_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "chats",
            chats_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "clear",
            clear_command,
        )
    )

    # -----------------------------------------------------
    # CALLBACKS
    # -----------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # -----------------------------------------------------
    # PAYMENTS
    # -----------------------------------------------------

    application.add_handler(
        PreCheckoutQueryHandler(
            precheckout_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            payment_handler,
        )
    )

    # -----------------------------------------------------
    # TEXT
    # -----------------------------------------------------

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
    print()
    print("========================================")
    print("🚀 MULTI AI")
    print("========================================")
    print(f"Model: {MODEL}")
    print(f"Daily limit: {DAILY_LIMIT:,}")
    print(f"Token multiplier: x{TOKEN_MULTIPLIER}")
    print("========================================")
    print()

    logger.info(
        "Инициализация базы данных..."
    )

    init_db()

    logger.info(
        "База данных готова."
    )

    logger.info(
        "Создание Telegram Application..."
    )

    application = create_application()

    logger.info(
        "Application создан."
    )

    print("▶️ Подключение к Telegram...")
    print("▶️ Polling будет запущен автоматически.")
    print()

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print("🛑 MULTI AI остановлен.")

    except Exception:
        print()
        print("========================================")
        print("❌ MULTI AI НЕ ЗАПУСТИЛСЯ")
        print("========================================")
        print("Подробности ошибки находятся выше в логах.")
        print("========================================")

        logger.exception(
            "КРИТИЧЕСКАЯ ОШИБКА ЗАПУСКА"
        )

        raise
