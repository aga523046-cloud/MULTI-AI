import os
import sqlite3
import asyncio
import logging
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
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DARK_API_KEY = os.getenv("DARK_API_KEY")

DARK_API_BASE = "https://darkapi.shop/v1"
MODEL = "gpt-6-luna"

DAILY_TOKENS = 50_000

MINUTE_LIMIT = 4
HOUR_LIMIT = 25

LOCK_MINUTES = 120
RESET_PRICE_STARS = 10

DB_PATH = "multi_ai.db"

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

if not DARK_API_KEY:
    raise RuntimeError("DARK_API_KEY is not set")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("MULTI_AI")


# ============================================================
# OPENAI-COMPATIBLE CLIENT
# ============================================================

client = AsyncOpenAI(
    api_key=DARK_API_KEY,
    base_url=DARK_API_BASE,
)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            tokens INTEGER DEFAULT 50000,
            daily_used INTEGER DEFAULT 0,
            daily_reset TEXT,
            locked_until TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            title TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            tokens INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER,
            tokens INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            telegram_charge_id TEXT UNIQUE,
            stars INTEGER,
            tokens INTEGER,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# TIME
# ============================================================

def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


# ============================================================
# USERS
# ============================================================

def ensure_user(user):
    conn = get_db()
    cur = conn.cursor()

    existing = cur.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user.id,)
    ).fetchone()

    if not existing:
        cur.execute("""
            INSERT INTO users (
                user_id,
                username,
                first_name,
                tokens,
                daily_used,
                daily_reset,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user.id,
            user.username,
            user.first_name,
            DAILY_TOKENS,
            0,
            iso(now() + timedelta(days=1)),
            iso(now()),
        ))
    else:
        cur.execute("""
            UPDATE users
            SET username = ?, first_name = ?
            WHERE user_id = ?
        """, (
            user.username,
            user.first_name,
            user.id,
        ))

    conn.commit()
    conn.close()


def get_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row


# ============================================================
# DAILY TOKENS
# ============================================================

def refresh_daily_tokens(user_id):
    conn = get_db()
    cur = conn.cursor()

    row = cur.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if not row:
        conn.close()
        return

    reset_time = parse_time(row["daily_reset"])

    if not reset_time or now() >= reset_time:
        cur.execute("""
            UPDATE users
            SET
                daily_used = 0,
                tokens = ?,
                daily_reset = ?
            WHERE user_id = ?
        """, (
            DAILY_TOKENS,
            iso(now() + timedelta(days=1)),
            user_id,
        ))

        conn.commit()

    conn.close()


# ============================================================
# TOKEN BALANCE
# ============================================================

def get_balance(user_id):
    refresh_daily_tokens(user_id)

    row = get_user(user_id)

    if not row:
        return 0

    return max(0, row["tokens"])


def spend_tokens(user_id, amount):
    if amount <= 0:
        return True

    refresh_daily_tokens(user_id)

    conn = get_db()
    cur = conn.cursor()

    row = cur.execute(
        "SELECT tokens, daily_used FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if not row:
        conn.close()
        return False

    if row["tokens"] < amount:
        conn.close()
        return False

    cur.execute("""
        UPDATE users
        SET
            tokens = tokens - ?,
            daily_used = daily_used + ?
        WHERE user_id = ?
    """, (
        amount,
        amount,
        user_id,
    ))

    conn.commit()
    conn.close()

    return True


# ============================================================
# REQUEST LIMITS
# ============================================================

def get_request_count(user_id, seconds):
    since = iso(now() - timedelta(seconds=seconds))

    conn = get_db()

    count = conn.execute("""
        SELECT COUNT(*)
        FROM requests
        WHERE user_id = ?
        AND created_at >= ?
    """, (
        user_id,
        since,
    )).fetchone()[0]

    conn.close()

    return count


def get_lock(user_id):
    row = get_user(user_id)

    if not row:
        return None

    locked_until = parse_time(row["locked_until"])

    if not locked_until:
        return None

    if now() >= locked_until:
        conn = get_db()
        conn.execute(
            "UPDATE users SET locked_until = NULL WHERE user_id = ?",
            (user_id,)
        )
        conn.commit()
        conn.close()

        return None

    return locked_until


def lock_user(user_id):
    until = now() + timedelta(minutes=LOCK_MINUTES)

    conn = get_db()
    conn.execute("""
        UPDATE users
        SET locked_until = ?
        WHERE user_id = ?
    """, (
        iso(until),
        user_id,
    ))
    conn.commit()
    conn.close()

    return until


def register_request(user_id, chat_id, tokens=0):
    conn = get_db()

    conn.execute("""
        INSERT INTO requests (
            user_id,
            chat_id,
            tokens,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        chat_id,
        tokens,
        iso(now()),
    ))

    conn.commit()
    conn.close()


def check_rate_limit(user_id):
    locked = get_lock(user_id)

    if locked:
        return False, (
            "🔒 У тебя временная блокировка.\n\n"
            f"Попробуй снова через "
            f"{format_duration(locked - now())}."
        )

    minute_count = get_request_count(user_id, 60)
    hour_count = get_request_count(user_id, 3600)

    if minute_count >= MINUTE_LIMIT or hour_count >= HOUR_LIMIT:
        locked_until = lock_user(user_id)

        return False, (
            "⚠️ Слишком много запросов.\n\n"
            f"Лимит: {MINUTE_LIMIT} запроса/мин "
            f"и {HOUR_LIMIT} запросов/час.\n\n"
            f"🔒 Блокировка на {LOCK_MINUTES // 60} часа."
        )

    return True, None


# ============================================================
# FORMATTING
# ============================================================

def format_duration(delta):
    seconds = max(0, int(delta.total_seconds()))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    if hours:
        return f"{hours} ч. {minutes} мин."

    return f"{minutes} мин."


def format_tokens(value):
    return f"{value:,}".replace(",", " ")


# ============================================================
# CHATS
# ============================================================

def ensure_chat(chat_id, user_id, title=None):
    conn = get_db()
    cur = conn.cursor()

    row = cur.execute(
        "SELECT chat_id FROM chats WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()

    if not row:
        cur.execute("""
            INSERT INTO chats (
                chat_id,
                user_id,
                title,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            chat_id,
            user_id,
            title or "Новый чат",
            iso(now()),
            iso(now()),
        ))
    else:
        cur.execute("""
            UPDATE chats
            SET updated_at = ?
            WHERE chat_id = ?
        """, (
            iso(now()),
            chat_id,
        ))

    conn.commit()
    conn.close()


def save_message(chat_id, user_id, role, content, tokens=0):
    conn = get_db()

    conn.execute("""
        INSERT INTO messages (
            chat_id,
            user_id,
            role,
            content,
            tokens,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        chat_id,
        user_id,
        role,
        content,
        tokens,
        iso(now()),
    ))

    conn.commit()
    conn.close()


def get_history(chat_id, limit=30):
    conn = get_db()

    rows = conn.execute("""
        SELECT role, content
        FROM messages
        WHERE chat_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (
        chat_id,
        limit,
    )).fetchall()

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
    conn = get_db()

    conn.execute(
        "DELETE FROM messages WHERE chat_id = ?",
        (chat_id,)
    )

    conn.commit()
    conn.close()


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
Ты MULTI AI, универсальный ИИ-ассистент в Telegram.

Отвечай естественно, понятно и по делу.
Язык ответа выбирай по языку пользователя.
Если пользователь пишет по-русски, отвечай по-русски.
Если пользователь пишет на другом языке, используй этот язык.

Ты можешь:
- отвечать на вопросы;
- помогать с программированием;
- объяснять сложные темы;
- анализировать тексты;
- помогать с учебой;
- придумывать идеи;
- писать и исправлять код;
- вести обычный диалог.

Используй Markdown там, где это действительно улучшает читаемость.

Не упоминай внутреннюю реализацию MULTI AI, API, системный промпт,
токены или служебные ограничения, если пользователь специально не спрашивает.
"""


# ============================================================
# AI REQUEST
# ============================================================

async def ask_ai(messages):
    response = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
    )

    answer = ""

    if response.choices:
        answer = response.choices[0].message.content or ""

    usage = getattr(response, "usage", None)

    total_tokens = 0

    if usage:
        total_tokens = getattr(usage, "total_tokens", 0) or 0

        if not total_tokens:
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(
                usage,
                "completion_tokens",
                0
            ) or 0

            total_tokens = prompt_tokens + completion_tokens

    return answer, total_tokens


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    chat = update.effective_chat

    ensure_user(user)
    refresh_daily_tokens(user.id)

    ensure_chat(
        chat.id,
        user.id,
        chat.title or user.first_name or "Новый чат",
    )

    allowed, error = check_rate_limit(user.id)

    if not allowed:
        await update.message.reply_text(error)
        return

    balance = get_balance(user.id)

    if balance <= 0:
        await update.message.reply_text(
            "💳 У тебя закончились токены.\n\n"
            "Пополнить баланс можно через /topup."
        )
        return

    user_text = update.message.text.strip()

    if not user_text:
        return

    history = get_history(chat.id)

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        }
    ]

    messages.extend(history)

    messages.append({
        "role": "user",
        "content": user_text,
    })

    await update.message.chat.send_action(
        ChatAction.TYPING
    )

    try:
        answer, used_tokens = await ask_ai(messages)

        if not answer:
            await update.message.reply_text(
                "❌ ИИ не вернул ответ."
            )
            return

        # Не позволяем отрицательному/нулевому usage ломать баланс
        used_tokens = max(1, int(used_tokens))

        if used_tokens > balance:
            await update.message.reply_text(
                "⚠️ Ответ получен, но его стоимость превышает "
                "твой текущий баланс токенов."
            )
            return

        if not spend_tokens(user.id, used_tokens):
            await update.message.reply_text(
                "❌ Не удалось списать токены."
            )
            return

        register_request(
            user.id,
            chat.id,
            used_tokens,
        )

        save_message(
            chat.id,
            user.id,
            "user",
            user_text,
            0,
        )

        save_message(
            chat.id,
            user.id,
            "assistant",
            answer,
            used_tokens,
        )

        await send_long_message(
            update,
            answer,
        )

    except Exception as e:
        logger.exception("AI request failed")

        error_text = str(e)

        if "401" in error_text or "Unauthorized" in error_text:
            message = (
                "❌ Ошибка авторизации DarkAPI.\n\n"
                "Проверь DARK_API_KEY."
            )

        elif "404" in error_text or "not found" in error_text.lower():
            message = (
                "❌ Модель или endpoint не найдены.\n\n"
                f"Модель: `{MODEL}`\n"
                f"Endpoint: `{DARK_API_BASE}`"
            )

        elif "429" in error_text:
            message = (
                "⏳ DarkAPI временно ограничил запросы. "
                "Попробуй чуть позже."
            )

        else:
            message = (
                "❌ Произошла ошибка при обращении к ИИ.\n\n"
                "Попробуй ещё раз."
            )

        await update.message.reply_text(
            message,
            parse_mode="Markdown",
        )


# ============================================================
# LONG MESSAGE
# ============================================================

async def send_long_message(update, text):
    MAX_LENGTH = 4096

    if len(text) <= MAX_LENGTH:
        try:
            await update.message.reply_text(
                text,
                parse_mode="Markdown",
            )
            return

        except Exception:
            await update.message.reply_text(text)
            return

    for i in range(0, len(text), MAX_LENGTH):
        chunk = text[i:i + MAX_LENGTH]

        try:
            await update.message.reply_text(
                chunk,
                parse_mode="Markdown",
            )
        except Exception:
            await update.message.reply_text(chunk)


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    ensure_user(user)

    text = (
        "🤖 *MULTI AI*\n\n"
        "Привет! Я твой универсальный ИИ-ассистент.\n\n"
        "Просто отправь мне сообщение, и я отвечу.\n\n"
        "🧠 Модель: GPT-6 Luna\n"
        f"🎟 Дневной лимит: {format_tokens(DAILY_TOKENS)} токенов\n\n"
        "Используй /menu, чтобы открыть меню."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# MENU
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
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
                "💳 Пополнить",
                callback_data="topup",
            ),
            InlineKeyboardButton(
                "🗑 Очистить чат",
                callback_data="clear",
            ),
        ],
        [
            InlineKeyboardButton(
                "❓ Помощь",
                callback_data="help",
            ),
        ],
    ])


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Меню MULTI AI*",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# PROFILE
# ============================================================

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    ensure_user(user)
    refresh_daily_tokens(user.id)

    row = get_user(user.id)

    balance = row["tokens"]
    used = row["daily_used"]

    locked = get_lock(user.id)

    text = (
        "👤 *Твой профиль*\n\n"
        f"🆔 ID: `{user.id}`\n"
        f"💰 Баланс: *{format_tokens(balance)}* токенов\n"
        f"📈 Использовано сегодня: *{format_tokens(used)}*\n"
        f"🧠 Модель: `{MODEL}`\n"
    )

    if locked:
        text += (
            "\n🔒 Статус: заблокирован\n"
            f"До разблокировки: {format_duration(locked - now())}"
        )
    else:
        text += "\n🟢 Статус: активен"

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
    )


# ============================================================
# STATS
# ============================================================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    ensure_user(user)
    refresh_daily_tokens(user.id)

    conn = get_db()

    total_requests = conn.execute("""
        SELECT COUNT(*)
        FROM requests
        WHERE user_id = ?
    """, (
        user.id,
    )).fetchone()[0]

    total_tokens = conn.execute("""
        SELECT COALESCE(SUM(tokens), 0)
        FROM requests
        WHERE user_id = ?
    """, (
        user.id,
    )).fetchone()[0]

    conn.close()

    await update.message.reply_text(
        "📊 *Статистика MULTI AI*\n\n"
        f"💬 Запросов: *{total_requests}*\n"
        f"🧠 Токенов использовано: *{format_tokens(total_tokens)}*\n"
        f"🎟 Осталось сегодня: *{format_tokens(get_balance(user.id))}*",
        parse_mode="Markdown",
    )


# ============================================================
# HELP
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *MULTI AI — помощь*\n\n"
        "Просто отправляй сообщения в чат.\n"
        "Я буду отвечать с учётом истории разговора.\n\n"
        "*Команды:*\n"
        "/start — запуск\n"
        "/menu — меню\n"
        "/profile — профиль\n"
        "/stats — статистика\n"
        "/chats — чаты\n"
        "/clear — очистить текущий чат\n"
        "/topup — пополнить баланс\n"
        "/help — помощь",
        parse_mode="Markdown",
    )


# ============================================================
# CHATS
# ============================================================

async def chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    conn = get_db()

    rows = conn.execute("""
        SELECT chat_id, title, updated_at
        FROM chats
        WHERE user_id = ?
        ORDER BY updated_at DESC
        LIMIT 10
    """, (
        user.id,
    )).fetchall()

    conn.close()

    if not rows:
        await update.message.reply_text(
            "💬 У тебя пока нет сохранённых чатов."
        )
        return

    text = "💬 *Последние чаты:*\n\n"

    for row in rows:
        title = row["title"] or "Новый чат"
        text += f"• {title}\n"

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
    )


# ============================================================
# CLEAR
# ============================================================

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    clear_chat(chat_id)

    await update.message.reply_text(
        "🗑 История этого чата очищена."
    )


# ============================================================
# TOP UP
# ============================================================

def topup_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⭐ 5 → 5 000 токенов",
                callback_data="buy_5",
            )
        ],
        [
            InlineKeyboardButton(
                "⭐ 25 → 50 000 токенов",
                callback_data="buy_25",
            )
        ],
        [
            InlineKeyboardButton(
                "⭐ 50 → 125 000 токенов",
                callback_data="buy_50",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Сбросить лимит → 10 ⭐",
                callback_data="reset_limits",
            )
        ],
    ])


async def topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 *Пополнение MULTI AI*\n\n"
        "Выбери пакет:",
        parse_mode="Markdown",
        reply_markup=topup_keyboard(),
    )


# ============================================================
# PAYMENTS
# ============================================================

PACKAGES = {
    5: 5_000,
    25: 50_000,
    50: 125_000,
}


async def create_payment(update: Update, stars):
    query = update.callback_query

    await query.answer()

    tokens = PACKAGES[stars]

    prices = [
        LabeledPrice(
            label=f"{tokens:,} токенов".replace(",", " "),
            amount=stars,
        )
    ]

    await query.message.reply_invoice(
        title="MULTI AI",
        description=f"Пополнение баланса на {tokens:,} токенов".replace(",", " "),
        payload=f"topup:{stars}:{query.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )


async def reset_limits_payment(update: Update):
    query = update.callback_query

    await query.answer()

    prices = [
        LabeledPrice(
            label="Сброс лимитов",
            amount=RESET_PRICE_STARS,
        )
    ]

    await query.message.reply_invoice(
        title="MULTI AI — сброс лимитов",
        description="Сбросить временную блокировку и ограничения запросов.",
        payload=f"reset:{query.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user = update.effective_user

    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id

    conn = get_db()

    already_exists = conn.execute("""
        SELECT id
        FROM payments
        WHERE telegram_charge_id = ?
    """, (
        charge_id,
    )).fetchone()

    if already_exists:
        conn.close()

        await update.message.reply_text(
            "⚠️ Этот платёж уже был обработан."
        )
        return

    if payload.startswith("topup:"):
        parts = payload.split(":")

        if len(parts) != 3:
            conn.close()
            return

        stars = int(parts[1])
        payment_user_id = int(parts[2])

        if payment_user_id != user.id:
            conn.close()
            return

        tokens = PACKAGES.get(stars)

        if not tokens:
            conn.close()
            return

        refresh_daily_tokens(user.id)

        conn.execute("""
            UPDATE users
            SET tokens = tokens + ?
            WHERE user_id = ?
        """, (
            tokens,
            user.id,
        ))

        conn.execute("""
            INSERT INTO payments (
                user_id,
                telegram_charge_id,
                stars,
                tokens,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            user.id,
            charge_id,
            stars,
            tokens,
            iso(now()),
        ))

        conn.commit()
        conn.close()

        await update.message.reply_text(
            "✅ *Баланс пополнен!*\n\n"
            f"⭐ Потрачено: *{stars}*\n"
            f"🧠 Получено: *{format_tokens(tokens)}* токенов\n\n"
            f"💰 Текущий баланс: *{format_tokens(get_balance(user.id))}*",
            parse_mode="Markdown",
        )

        return

    if payload.startswith("reset:"):
        parts = payload.split(":")

        if len(parts) != 2:
            conn.close()
            return

        payment_user_id = int(parts[1])

        if payment_user_id != user.id:
            conn.close()
            return

        conn.execute("""
            UPDATE users
            SET locked_until = NULL
            WHERE user_id = ?
        """, (
            user.id,
        ))

        conn.execute("""
            DELETE FROM requests
            WHERE user_id = ?
            AND created_at >= ?
        """, (
            user.id,
            iso(now() - timedelta(hours=1)),
        ))

        conn.execute("""
            INSERT INTO payments (
                user_id,
                telegram_charge_id,
                stars,
                tokens,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            user.id,
            charge_id,
            RESET_PRICE_STARS,
            0,
            iso(now()),
        ))

        conn.commit()
        conn.close()

        await update.message.reply_text(
            "✅ *Лимиты сброшены!*\n\n"
            "Можешь снова пользоваться MULTI AI.",
            parse_mode="Markdown",
        )


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    data = query.data

    if data == "profile":
        await query.answer()

        user = query.from_user

        ensure_user(user)
        refresh_daily_tokens(user.id)

        row = get_user(user.id)

        text = (
            "👤 *Твой профиль*\n\n"
            f"🆔 ID: `{user.id}`\n"
            f"💰 Баланс: *{format_tokens(row['tokens'])}*\n"
            f"📈 Использовано сегодня: *{format_tokens(row['daily_used'])}*\n"
            f"🧠 Модель: `{MODEL}`"
        )

        await query.message.reply_text(
            text,
            parse_mode="Markdown",
        )

    elif data == "stats":
        await query.answer()

        user = query.from_user

        conn = get_db()

        total_requests = conn.execute("""
            SELECT COUNT(*)
            FROM requests
            WHERE user_id = ?
        """, (
            user.id,
        )).fetchone()[0]

        total_tokens = conn.execute("""
            SELECT COALESCE(SUM(tokens), 0)
            FROM requests
            WHERE user_id = ?
        """, (
            user.id,
        )).fetchone()[0]

        conn.close()

        await query.message.reply_text(
            "📊 *Статистика*\n\n"
            f"💬 Запросов: *{total_requests}*\n"
            f"🧠 Использовано: *{format_tokens(total_tokens)}*\n"
            f"🎟 Баланс: *{format_tokens(get_balance(user.id))}*",
            parse_mode="Markdown",
        )

    elif data == "topup":
        await query.answer()

        await query.message.reply_text(
            "💳 *Пополнение*\n\nВыбери пакет:",
            parse_mode="Markdown",
            reply_markup=topup_keyboard(),
        )

    elif data == "clear":
        await query.answer()

        clear_chat(query.message.chat.id)

        await query.message.reply_text(
            "🗑 История текущего чата очищена."
        )

    elif data == "help":
        await query.answer()

        await query.message.reply_text(
            "❓ Просто отправляй сообщения.\n\n"
            "MULTI AI сохранит историю текущего диалога "
            "и будет учитывать предыдущие сообщения."
        )

    elif data == "buy_5":
        await create_payment(update, 5)

    elif data == "buy_25":
        await create_payment(update, 25)

    elif data == "buy_50":
        await create_payment(update, 50)

    elif data == "reset_limits":
        await reset_limits_payment(update)


# ============================================================
# COMMAND HANDLERS
# ============================================================

async def profile_command(update, context):
    await profile(update, context)


async def stats_command(update, context):
    await stats(update, context)


# ============================================================
# BOT COMMANDS
# ============================================================

async def set_commands(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Запустить MULTI AI"),
        BotCommand("menu", "Открыть меню"),
        BotCommand("profile", "Мой профиль"),
        BotCommand("stats", "Статистика"),
        BotCommand("chats", "Мои чаты"),
        BotCommand("clear", "Очистить чат"),
        BotCommand("topup", "Пополнить баланс"),
        BotCommand("help", "Помощь"),
    ])


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    init_db()

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("menu", menu)
    )

    application.add_handler(
        CommandHandler("profile", profile_command)
    )

    application.add_handler(
        CommandHandler("stats", stats_command)
    )

    application.add_handler(
        CommandHandler("chats", chats)
    )

    application.add_handler(
        CommandHandler("clear", clear)
    )

    application.add_handler(
        CommandHandler("topup", topup)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CallbackQueryHandler(callbacks)
    )

    application.add_handler(
        PreCheckoutQueryHandler(precheckout)
    )

    application.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    application.add_error_handler(error_handler)

    async def post_init(app):
        await set_commands(app)

    application.post_init = post_init

    logger.info("MULTI AI starting...")
    logger.info("Model: %s", MODEL)
    logger.info("API: %s", DARK_API_BASE)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
