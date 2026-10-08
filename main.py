import os
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from openai import OpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    BotCommand,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    filters,
    ContextTypes,
)

# =========================================================
# CONFIG
# =========================================================

# DarkAPI
AI_API_KEY = os.getenv("DARK_API_KEY")

# Telegram
TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
)

# DarkAPI
BASE_URL = "https://darkapi.shop/v1"
MODEL = "gpt-6-luna"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "multi_ai.db")

REQUESTS_PER_MINUTE = 4
REQUESTS_PER_HOUR = 25

LOCK_HOURS = 2
RESET_PRICE_STARS = 10

MAX_HISTORY = 20
MAX_MESSAGE_LENGTH = 12000

if not AI_API_KEY:
    raise RuntimeError(
        "Не найден секрет DARK_API_KEY"
    )

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "Не найден TELEGRAM_TOKEN или TELEGRAM_BOT_TOKEN"
    )

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("MULTI_AI")

# =========================================================
# DARKAPI / OPENAI
# =========================================================

client = OpenAI(
    base_url=BASE_URL,
    api_key=AI_API_KEY,
    timeout=120,
)

# =========================================================
# DATABASE
# =========================================================

db_lock = asyncio.Lock()


def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            lock_until TEXT DEFAULT NULL,
            reset_used INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT DEFAULT 'Новый чат',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def parse_dt(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def get_user(user_id, username="", first_name=""):
    conn = db_connect()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO users
            (user_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                username or "",
                first_name or "",
                iso(now()),
            ),
        )
        conn.commit()
    else:
        if username or first_name:
            conn.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?
                WHERE user_id = ?
                """,
                (
                    username or row["username"] or "",
                    first_name or row["first_name"] or "",
                    user_id,
                ),
            )
            conn.commit()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    conn.close()
    return row


def create_chat(user_id, title="Новый чат"):
    conn = db_connect()
    current = iso(now())

    cursor = conn.execute(
        """
        INSERT INTO chats
        (user_id, title, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, title, current, current),
    )

    chat_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return chat_id


def get_last_chat(user_id):
    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM chats
        WHERE user_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()

    conn.close()

    if row:
        return row

    chat_id = create_chat(user_id)

    conn = db_connect()

    row = conn.execute(
        "SELECT * FROM chats WHERE id = ?",
        (chat_id,),
    ).fetchone()

    conn.close()

    return row


def add_message(chat_id, role, content):
    conn = db_connect()

    conn.execute(
        """
        INSERT INTO messages
        (chat_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            chat_id,
            role,
            content,
            iso(now()),
        ),
    )

    conn.execute(
        """
        UPDATE chats
        SET updated_at = ?
        WHERE id = ?
        """,
        (
            iso(now()),
            chat_id,
        ),
    )

    conn.commit()
    conn.close()


def get_history(chat_id):
    conn = db_connect()

    rows = conn.execute(
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
    ).fetchall()

    conn.close()

    rows = list(reversed(rows))

    return [
        {
            "role": row["role"],
            "content": row["content"],
        }
        for row in rows
    ]


def clear_chat(chat_id):
    conn = db_connect()

    conn.execute(
        "DELETE FROM messages WHERE chat_id = ?",
        (chat_id,),
    )

    conn.execute(
        """
        UPDATE chats
        SET updated_at = ?
        WHERE id = ?
        """,
        (
            iso(now()),
            chat_id,
        ),
    )

    conn.commit()
    conn.close()


def get_user_chats(user_id):
    conn = db_connect()

    rows = conn.execute(
        """
        SELECT id, title, created_at, updated_at
        FROM chats
        WHERE user_id = ?
        ORDER BY updated_at DESC
        """,
        (user_id,),
    ).fetchall()

    conn.close()

    return rows


def delete_old_requests(user_id):
    cutoff = now() - timedelta(hours=1)

    conn = db_connect()

    conn.execute(
        """
        DELETE FROM requests
        WHERE user_id = ?
        AND created_at < ?
        """,
        (
            user_id,
            iso(cutoff),
        ),
    )

    conn.commit()
    conn.close()


def register_request(user_id):
    conn = db_connect()

    conn.execute(
        """
        INSERT INTO requests
        (user_id, created_at)
        VALUES (?, ?)
        """,
        (
            user_id,
            iso(now()),
        ),
    )

    conn.commit()
    conn.close()


def get_request_counts(user_id):
    delete_old_requests(user_id)

    conn = db_connect()

    minute_cutoff = now() - timedelta(minutes=1)
    hour_cutoff = now() - timedelta(hours=1)

    minute_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM requests
        WHERE user_id = ?
        AND created_at >= ?
        """,
        (
            user_id,
            iso(minute_cutoff),
        ),
    ).fetchone()[0]

    hour_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM requests
        WHERE user_id = ?
        AND created_at >= ?
        """,
        (
            user_id,
            iso(hour_cutoff),
        ),
    ).fetchone()[0]

    conn.close()

    return minute_count, hour_count


def get_lock(user_id):
    conn = db_connect()

    row = conn.execute(
        """
        SELECT lock_until, reset_used
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    conn.close()

    if not row:
        return None, False

    return parse_dt(row["lock_until"]), bool(row["reset_used"])


def set_lock(user_id):
    lock_until = now() + timedelta(hours=LOCK_HOURS)

    conn = db_connect()

    conn.execute(
        """
        UPDATE users
        SET lock_until = ?
        WHERE user_id = ?
        """,
        (
            iso(lock_until),
            user_id,
        ),
    )

    conn.commit()
    conn.close()

    return lock_until


def reset_lock(user_id):
    conn = db_connect()

    conn.execute(
        """
        UPDATE users
        SET lock_until = NULL,
            reset_used = 1
        WHERE user_id = ?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()


# =========================================================
# RATE LIMIT
# =========================================================

def check_limits(user_id):
    lock_until, reset_used = get_lock(user_id)

    if lock_until and lock_until > now():
        return False, (
            "🔒 Ты временно заблокирован за превышение лимитов.\n\n"
            f"Блокировка закончится примерно:\n"
            f"{lock_until.astimezone().strftime('%d.%m.%Y %H:%M')}"
        )

    if lock_until and lock_until <= now():
        conn = db_connect()

        conn.execute(
            """
            UPDATE users
            SET lock_until = NULL
            WHERE user_id = ?
            """,
            (user_id,),
        )

        conn.commit()
        conn.close()

    minute_count, hour_count = get_request_counts(user_id)

    if minute_count >= REQUESTS_PER_MINUTE:
        lock_until = set_lock(user_id)

        return False, (
            "⛔ Слишком много запросов.\n\n"
            f"Лимит: {REQUESTS_PER_MINUTE} запросов в минуту.\n"
            f"Ты получил блокировку на {LOCK_HOURS} часа.\n\n"
            f"До снятия: {lock_until.astimezone().strftime('%H:%M:%S')}"
        )

    if hour_count >= REQUESTS_PER_HOUR:
        lock_until = set_lock(user_id)

        return False, (
            "⛔ Ты достиг часового лимита.\n\n"
            f"Лимит: {REQUESTS_PER_HOUR} запросов в час.\n"
            f"Блокировка: {LOCK_HOURS} часа."
        )

    return True, None


# =========================================================
# AI
# =========================================================

def ask_ai(messages):
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
    )

    if not response.choices:
        raise RuntimeError("AI не вернул ответ.")

    content = response.choices[0].message.content

    if not content:
        raise RuntimeError("AI вернул пустой ответ.")

    return content


def format_ai_error(error):
    text = str(error)
    status = getattr(error, "status_code", None)

    logger.error(
        "DARKAPI ERROR | type=%s | status=%s | model=%s | url=%s | message=%s",
        type(error).__name__,
        status,
        MODEL,
        BASE_URL,
        text[:1500],
    )

    if status == 401:
        return "❌ Ошибка DarkAPI: неверный API-ключ."

    if status == 403:
        return "❌ Ошибка DarkAPI: доступ к API запрещён."

    if status == 404:
        return (
            "❌ DarkAPI вернул 404.\n\n"
            f"Модель: `{MODEL}`\n"
            f"Endpoint: `{BASE_URL}/chat/completions`\n\n"
            "Проверь API-совместимость DarkAPI."
        )

    if status == 429:
        return "⏳ DarkAPI временно перегружен или достигнут лимит."

    if status and status >= 500:
        return (
            "🔧 DarkAPI временно недоступен. "
            "Попробуй ещё раз через несколько секунд."
        )

    if "timeout" in text.lower():
        return "⏱ AI слишком долго не отвечает. Попробуй ещё раз."

    return "❌ Не удалось получить ответ от AI. Попробуй ещё раз."


# =========================================================
# TELEGRAM HELPERS
# =========================================================

async def thinking_animation(message, stop_event):
    frames = [
        "🤔 Думаю.",
        "🤔 Думаю..",
        "🤔 Думаю...",
    ]

    index = 0

    try:
        while not stop_event.is_set():
            try:
                await message.edit_text(frames[index])
            except Exception:
                pass

            index = (index + 1) % len(frames)

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=0.8,
                )
            except asyncio.TimeoutError:
                pass

    except asyncio.CancelledError:
        return


async def stop_animation(task, event):
    event.set()

    if task:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


async def send_long_message(message, text):
    if not text:
        return

    max_length = 4000

    chunks = [
        text[i:i + max_length]
        for i in range(0, len(text), max_length)
    ]

    for chunk in chunks:
        try:
            await message.reply_text(
                chunk,
                parse_mode="Markdown",
            )
        except Exception:
            await message.reply_text(chunk)


# =========================================================
# COMMANDS
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    get_user(
        user.id,
        user.username,
        user.first_name,
    )

    get_last_chat(user.id)

    keyboard = [
        [
            InlineKeyboardButton(
                "🤖 Новый запрос",
                callback_data="new_chat",
            ),
        ],
        [
            InlineKeyboardButton(
                "👤 Профиль",
                callback_data="profile",
            ),
            InlineKeyboardButton(
                "📊 Статистика",
                callback_data="stats",
            ),
        ],
        [
            InlineKeyboardButton(
                "💬 Чаты",
                callback_data="chats",
            ),
        ],
        [
            InlineKeyboardButton(
                "🧹 Очистить чат",
                callback_data="clear",
            ),
        ],
    ]

    await update.message.reply_text(
        "🤖 *MULTI AI*\n\n"
        "Я готов отвечать на твои сообщения.\n\n"
        "Просто напиши мне что-нибудь.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "🤖 Новый чат",
                callback_data="new_chat",
            ),
            InlineKeyboardButton(
                "👤 Профиль",
                callback_data="profile",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 Статистика",
                callback_data="stats",
            ),
            InlineKeyboardButton(
                "💬 Чаты",
                callback_data="chats",
            ),
        ],
        [
            InlineKeyboardButton(
                "🧹 Очистить",
                callback_data="clear",
            ),
        ],
    ]

    await update.message.reply_text(
        "📋 *Меню MULTI AI*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    get_user(
        user.id,
        user.username,
        user.first_name,
    )

    lock_until, reset_used = get_lock(user.id)
    minute_count, hour_count = get_request_counts(user.id)

    lock_text = "нет"

    if lock_until and lock_until > now():
        lock_text = lock_until.astimezone().strftime(
            "%d.%m.%Y %H:%M"
        )

    reset_text = "использован" if reset_used else "доступен"

    text = (
        "👤 *Профиль*\n\n"
        f"ID: `{user.id}`\n"
        f"Имя: {user.first_name or 'не указано'}\n"
        f"Username: @{user.username if user.username else 'нет'}\n\n"
        f"Запросов за минуту: {minute_count}/{REQUESTS_PER_MINUTE}\n"
        f"Запросов за час: {hour_count}/{REQUESTS_PER_HOUR}\n"
        f"Блокировка: {lock_text}\n"
        f"Сброс за {RESET_PRICE_STARS} ⭐: {reset_text}"
    )

    keyboard = []

    if lock_until and lock_until > now() and not reset_used:
        keyboard.append([
            InlineKeyboardButton(
                f"🔓 Снять блокировку • {RESET_PRICE_STARS} ⭐",
                callback_data="buy_reset",
            )
        ])

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
        if keyboard else None,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    get_user(
        user.id,
        user.username,
        user.first_name,
    )

    conn = db_connect()

    total_messages = conn.execute(
        """
        SELECT COUNT(*)
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE c.user_id = ?
        """,
        (user.id,),
    ).fetchone()[0]

    total_chats = conn.execute(
        """
        SELECT COUNT(*)
        FROM chats
        WHERE user_id = ?
        """,
        (user.id,),
    ).fetchone()[0]

    total_requests = conn.execute(
        """
        SELECT COUNT(*)
        FROM requests
        WHERE user_id = ?
        """,
        (user.id,),
    ).fetchone()[0]

    conn.close()

    await update.message.reply_text(
        "📊 *Статистика*\n\n"
        f"💬 Чатов: {total_chats}\n"
        f"📝 Сообщений: {total_messages}\n"
        f"🤖 AI-запросов: {total_requests}",
        parse_mode="Markdown",
    )


async def chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    chats = get_user_chats(user.id)

    if not chats:
        await update.message.reply_text(
            "💬 У тебя пока нет чатов."
        )
        return

    lines = ["💬 *Твои чаты:*\n"]

    for chat in chats[:15]:
        lines.append(
            f"• `{chat['id']}` — {chat['title']}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    chat = get_last_chat(user.id)

    clear_chat(chat["id"])

    await update.message.reply_text(
        "🧹 История текущего чата очищена."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *MULTI AI*\n\n"
        "/start — запустить бота\n"
        "/menu — меню\n"
        "/profile — профиль\n"
        "/stats — статистика\n"
        "/chats — чаты\n"
        "/clear — очистить текущий чат\n"
        "/help — помощь\n\n"
        f"Лимиты: {REQUESTS_PER_MINUTE}/мин и "
        f"{REQUESTS_PER_HOUR}/час.",
        parse_mode="Markdown",
    )


# =========================================================
# MESSAGE HANDLER
# =========================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    text = update.message.text.strip()

    if not text:
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        await update.message.reply_text(
            f"❌ Сообщение слишком длинное. Максимум {MAX_MESSAGE_LENGTH} символов."
        )
        return

    get_user(
        user.id,
        user.username,
        user.first_name,
    )

    allowed, reason = check_limits(user.id)

    if not allowed:
        await update.message.reply_text(reason)

        lock_until, reset_used = get_lock(user.id)

        if (
            lock_until
            and lock_until > now()
            and not reset_used
        ):
            await update.message.reply_text(
                f"Можно снять блокировку за {RESET_PRICE_STARS} ⭐.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            f"🔓 Снять за {RESET_PRICE_STARS} ⭐",
                            callback_data="buy_reset",
                        )
                    ]
                ]),
            )

        return

    chat = get_last_chat(user.id)
    chat_id = chat["id"]

    history = get_history(chat_id)

    messages = list(history)

    messages.append({
        "role": "user",
        "content": text,
    })

    thinking_message = await update.message.reply_text(
        "🤔 Думаю..."
    )

    stop_event = asyncio.Event()

    animation_task = asyncio.create_task(
        thinking_animation(
            thinking_message,
            stop_event,
        )
    )

    try:
        response = await asyncio.to_thread(
            ask_ai,
            messages,
        )

        register_request(user.id)

        add_message(
            chat_id,
            "user",
            text,
        )

        add_message(
            chat_id,
            "assistant",
            response,
        )

    except Exception as error:
        logger.exception(
            "MESSAGE ERROR | user_id=%s",
            user.id,
        )

        await stop_animation(
            animation_task,
            stop_event,
        )

        try:
            await thinking_message.edit_text(
                format_ai_error(error)
            )
        except Exception:
            await update.message.reply_text(
                format_ai_error(error)
            )

        return

    await stop_animation(
        animation_task,
        stop_event,
    )

    try:
        await thinking_message.delete()
    except Exception:
        pass

    await send_long_message(
        update.message,
        response,
    )


# =========================================================
# CALLBACKS
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    user = query.from_user
    data = query.data

    get_user(
        user.id,
        user.username,
        user.first_name,
    )

    if data == "new_chat":
        create_chat(user.id)

        await query.message.reply_text(
            "🆕 Создан новый чат."
        )

    elif data == "profile":
        row = get_user(user.id)

        lock_until, reset_used = get_lock(user.id)
        minute_count, hour_count = get_request_counts(user.id)

        lock_text = "нет"

        if lock_until and lock_until > now():
            lock_text = lock_until.astimezone().strftime(
                "%d.%m.%Y %H:%M"
            )

        text = (
            "👤 *Профиль*\n\n"
            f"ID: `{user.id}`\n"
            f"Имя: {row['first_name'] or 'не указано'}\n"
            f"Username: @{row['username'] if row['username'] else 'нет'}\n\n"
            f"Минута: {minute_count}/{REQUESTS_PER_MINUTE}\n"
            f"Час: {hour_count}/{REQUESTS_PER_HOUR}\n"
            f"Блокировка: {lock_text}\n"
            f"Сброс: {'использован' if reset_used else 'доступен'}"
        )

        keyboard = []

        if (
            lock_until
            and lock_until > now()
            and not reset_used
        ):
            keyboard.append([
                InlineKeyboardButton(
                    f"🔓 Снять блокировку • {RESET_PRICE_STARS} ⭐",
                    callback_data="buy_reset",
                )
            ])

        await query.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
            if keyboard else None,
        )

    elif data == "stats":
        conn = db_connect()

        total_messages = conn.execute(
            """
            SELECT COUNT(*)
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE c.user_id = ?
            """,
            (user.id,),
        ).fetchone()[0]

        total_chats = conn.execute(
            """
            SELECT COUNT(*)
            FROM chats
            WHERE user_id = ?
            """,
            (user.id,),
        ).fetchone()[0]

        conn.close()

        await query.message.reply_text(
            "📊 *Статистика*\n\n"
            f"💬 Чатов: {total_chats}\n"
            f"📝 Сообщений: {total_messages}",
            parse_mode="Markdown",
        )

    elif data == "chats":
        chats = get_user_chats(user.id)

        if not chats:
            await query.message.reply_text(
                "💬 У тебя пока нет чатов."
            )
            return

        lines = ["💬 *Чаты:*\n"]

        for chat in chats[:15]:
            lines.append(
                f"• `{chat['id']}` — {chat['title']}"
            )

        await query.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )

    elif data == "clear":
        chat = get_last_chat(user.id)

        clear_chat(chat["id"])

        await query.message.reply_text(
            "🧹 История текущего чата очищена."
        )

    elif data == "buy_reset":
        lock_until, reset_used = get_lock(user.id)

        if reset_used:
            await query.message.reply_text(
                "❌ Ты уже использовал бесплатный сброс."
            )
            return

        if not lock_until or lock_until <= now():
            await query.message.reply_text(
                "ℹ️ Сейчас блокировки нет."
            )
            return

        await context.bot.send_invoice(
            chat_id=user.id,
            title="Сброс блокировки MULTI AI",
            description="Снятие двухчасовой блокировки.",
            payload=f"reset_lock:{user.id}",
            currency="XTR",
            prices=[
                LabeledPrice(
                    "Сброс блокировки",
                    RESET_PRICE_STARS,
                )
            ],
        )


# =========================================================
# PAYMENT
# =========================================================

async def pre_checkout_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.pre_checkout_query

    try:
        if not query.invoice_payload.startswith("reset_lock:"):
            await query.answer(
                ok=False,
                error_message="Неизвестный платёж.",
            )
            return

        user_id = int(
            query.invoice_payload.split(":", 1)[1]
        )

        if user_id != query.from_user.id:
            await query.answer(
                ok=False,
                error_message="Платёж привязан к другому пользователю.",
            )
            return

        if query.total_amount != RESET_PRICE_STARS:
            await query.answer(
                ok=False,
                error_message="Неверная сумма платежа.",
            )
            return

        await query.answer(ok=True)

    except Exception:
        logger.exception("PRECHECKOUT ERROR")

        try:
            await query.answer(
                ok=False,
                error_message="Ошибка проверки платежа.",
            )
        except Exception:
            pass


async def successful_payment_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.message.successful_payment:
        return

    payment = update.message.successful_payment
    user_id = update.effective_user.id

    if not payment.invoice_payload.startswith("reset_lock:"):
        return

    try:
        paid_user_id = int(
            payment.invoice_payload.split(":", 1)[1]
        )
    except Exception:
        return

    if paid_user_id != user_id:
        return

    reset_lock(user_id)

    conn = db_connect()

    conn.execute(
        """
        INSERT INTO payments
        (user_id, amount, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            payment.total_amount,
            payment.invoice_payload,
            iso(now()),
        ),
    )

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ Оплата получена.\n\n"
        "🔓 Блокировка снята.\n"
        "Можешь снова пользоваться MULTI AI."
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error

    if "Conflict" in str(error):
        logger.error(
            "TELEGRAM CONFLICT: другой экземпляр этого бота "
            "уже получает getUpdates. Останови второй экземпляр."
        )
        return

    logger.error(
        "UNHANDLED ERROR: %s",
        error,
    )


# =========================================================
# STARTUP
# =========================================================

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Запустить бота"),
        BotCommand("menu", "Меню"),
        BotCommand("profile", "Профиль"),
        BotCommand("stats", "Статистика"),
        BotCommand("chats", "Мои чаты"),
        BotCommand("clear", "Очистить чат"),
        BotCommand("help", "Помощь"),
    ])

    me = await application.bot.get_me()

    logger.info(
        "MULTI AI запущен: @%s | ID=%s | model=%s",
        me.username,
        me.id,
        MODEL,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    init_db()

    logger.info("Database: %s", DB_FILE)
    logger.info("AI API: %s", BASE_URL)
    logger.info("AI Model: %s", MODEL)

    if os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_BOT_TOKEN"):
        if os.getenv("TELEGRAM_TOKEN") != os.getenv("TELEGRAM_BOT_TOKEN"):
            logger.warning(
                "TELEGRAM_TOKEN и TELEGRAM_BOT_TOKEN отличаются. "
                "Используется TELEGRAM_TOKEN."
            )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
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
        CommandHandler("chats", chats_command)
    )

    application.add_handler(
        CommandHandler("clear", clear_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        PreCheckoutQueryHandler(pre_checkout_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Starting polling...")

    application.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
