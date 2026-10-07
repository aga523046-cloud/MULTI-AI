import os
import sqlite3
import logging
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
MODEL = "gpt-5-5"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "multi_ai.db")

MAX_HISTORY = 20
MAX_REQUESTS_PER_MINUTE = 4
MAX_REQUESTS_PER_HOUR = 25

LOCK_DURATION = timedelta(hours=2)
RESET_PRICE_STARS = 10
TIMEZONE = timezone(timedelta(hours=3))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не найден в Environment Variables.")

if not AI_API_KEY:
    raise RuntimeError("AI_API_KEY не найден в Environment Variables.")

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("multi_ai")

# =========================================================
# AI
# =========================================================

client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=AI_API_KEY,
    timeout=120.0,
    max_retries=2,
)

SYSTEM_PROMPT = """
Ты — MULTI AI, универсальный ИИ-помощник в Telegram.

Ты создан компанией AllI из поколения MULTI MEDIA (MMAI),
в сотрудничестве со STRAUSENOK11.

Отвечай естественно, понятно и полезно.

Если пользователь пишет по-русски, отвечай по-русски.
Если пишет по-английски, отвечай по-английски.

Используй Telegram Markdown:
*жирный*
_курсив_
`код`

Для больших фрагментов кода используй тройные обратные кавычки.

Используй эмодзи умеренно.

Длинные ответы структурируй списками и короткими абзацами.

Если пользователь просит короткий ответ, отвечай коротко.

Не выдумывай факты. Если не уверен, скажи об этом.

Поддерживай контекст текущего чата.

Не утверждай, что ты Claude, ChatGPT или другая модель.
Ты — MULTI AI.

Если пользователь спрашивает, кто ты, объясняй,
что ты MULTI AI, созданный компанией AllI из поколения
MULTI MEDIA (MMAI), в сотрудничестве со STRAUSENOK11,
для общения в Telegram.
"""

# =========================================================
# DATABASE
# =========================================================

def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row

    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")

    return con


def init_db():
    con = db()

    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                used_tokens INTEGER DEFAULT 0,
                bonus_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                stars_spent INTEGER DEFAULT 0,
                last_date TEXT,
                joined_at TEXT,
                lock_until TEXT DEFAULT NULL,
                rate_reset_used INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_request_log_user_time
            ON request_log(user_id, created_at);

            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                tokens INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)

        # Миграция старых БД
        columns = {
            row["name"]
            for row in con.execute("PRAGMA table_info(users)").fetchall()
        }

        migrations = {
            "username": "TEXT DEFAULT ''",
            "first_name": "TEXT DEFAULT ''",
            "used_tokens": "INTEGER DEFAULT 0",
            "bonus_tokens": "INTEGER DEFAULT 0",
            "total_tokens": "INTEGER DEFAULT 0",
            "stars_spent": "INTEGER DEFAULT 0",
            "last_date": "TEXT",
            "joined_at": "TEXT",
            "lock_until": "TEXT DEFAULT NULL",
            "rate_reset_used": "INTEGER DEFAULT 0",
        }

        for column, definition in migrations.items():
            if column not in columns:
                con.execute(
                    f"ALTER TABLE users ADD COLUMN {column} {definition}"
                )

        con.commit()
        logger.info("База данных готова: %s", DB_FILE)

    except Exception:
        con.rollback()
        logger.exception("Ошибка инициализации БД")
        raise

    finally:
        con.close()


# =========================================================
# TIME
# =========================================================

def now():
    return datetime.now(TIMEZONE)


def iso_now():
    return now().isoformat()


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def format_duration(seconds):
    seconds = max(0, int(seconds))

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} сек"
    return f"{s} сек"


# =========================================================
# USERS
# =========================================================

def get_user(user_id, username=None, first_name=None):
    con = db()

    try:
        user = con.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if user is None:
            con.execute("""
                INSERT INTO users (
                    user_id,
                    username,
                    first_name,
                    used_tokens,
                    bonus_tokens,
                    total_tokens,
                    stars_spent,
                    last_date,
                    joined_at,
                    lock_until,
                    rate_reset_used
                )
                VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?, NULL, 0)
            """, (
                user_id,
                username or "",
                first_name or "",
                now().strftime("%Y-%m-%d"),
                iso_now(),
            ))

            con.commit()

        else:
            # ВАЖНО:
            # старый код мог стереть username/first_name,
            # когда get_user(user_id) вызывался без данных.
            if username is not None or first_name is not None:
                con.execute("""
                    UPDATE users
                    SET username = COALESCE(?, username),
                        first_name = COALESCE(?, first_name)
                    WHERE user_id = ?
                """, (
                    username,
                    first_name,
                    user_id,
                ))
                con.commit()

        return con.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    finally:
        con.close()


# =========================================================
# RATE LIMIT
# =========================================================

def cleanup_requests(con, user_id):
    cutoff = (
        now() - timedelta(hours=2)
    ).isoformat()

    con.execute("""
        DELETE FROM request_log
        WHERE user_id = ?
        AND created_at < ?
    """, (user_id, cutoff))


def rate_counts(con, user_id):
    current = now()

    minute = (
        current - timedelta(minutes=1)
    ).isoformat()

    hour = (
        current - timedelta(hours=1)
    ).isoformat()

    minute_count = con.execute("""
        SELECT COUNT(*)
        FROM request_log
        WHERE user_id = ?
        AND created_at >= ?
    """, (user_id, minute)).fetchone()[0]

    hour_count = con.execute("""
        SELECT COUNT(*)
        FROM request_log
        WHERE user_id = ?
        AND created_at >= ?
    """, (user_id, hour)).fetchone()[0]

    return minute_count, hour_count


def get_rate_status(user_id):
    con = db()

    try:
        cleanup_requests(con, user_id)

        user = con.execute("""
            SELECT lock_until, rate_reset_used
            FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        if user is None:
            con.commit()
            return {
                "allowed": True,
                "locked": False,
                "seconds": 0,
                "minute_count": 0,
                "hour_count": 0,
                "reset_used": False,
            }

        current = now()
        lock_until = parse_time(user["lock_until"])

        if lock_until and current < lock_until:
            minute_count, hour_count = rate_counts(con, user_id)
            con.commit()

            return {
                "allowed": False,
                "locked": True,
                "seconds": int(
                    (lock_until - current).total_seconds()
                ),
                "minute_count": minute_count,
                "hour_count": hour_count,
                "reset_used": bool(user["rate_reset_used"]),
            }

        if lock_until:
            con.execute("""
                UPDATE users
                SET lock_until = NULL
                WHERE user_id = ?
            """, (user_id,))

        minute_count, hour_count = rate_counts(con, user_id)
        con.commit()

        return {
            "allowed": (
                minute_count < MAX_REQUESTS_PER_MINUTE
                and hour_count < MAX_REQUESTS_PER_HOUR
            ),
            "locked": False,
            "seconds": 0,
            "minute_count": minute_count,
            "hour_count": hour_count,
            "reset_used": bool(user["rate_reset_used"]),
        }

    finally:
        con.close()


def register_request(user_id):
    con = db()

    try:
        con.execute("BEGIN IMMEDIATE")
        cleanup_requests(con, user_id)

        user = con.execute("""
            SELECT lock_until, rate_reset_used
            FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        if user is None:
            con.rollback()
            return False, {"reason": "user_missing"}

        current = now()
        lock_until = parse_time(user["lock_until"])

        if lock_until and current < lock_until:
            con.rollback()

            return False, {
                "reason": "locked",
                "seconds": int(
                    (lock_until - current).total_seconds()
                ),
                "reset_used": bool(user["rate_reset_used"]),
            }

        if lock_until:
            con.execute("""
                UPDATE users
                SET lock_until = NULL
                WHERE user_id = ?
            """, (user_id,))

        minute_count, hour_count = rate_counts(con, user_id)

        if minute_count >= MAX_REQUESTS_PER_MINUTE:
            con.commit()

            return False, {
                "reason": "minute",
                "seconds": 60,
            }

        if hour_count >= MAX_REQUESTS_PER_HOUR:
            lock_until = (
                current + LOCK_DURATION
            ).isoformat()

            con.execute("""
                UPDATE users
                SET lock_until = ?
                WHERE user_id = ?
            """, (lock_until, user_id))

            con.commit()

            return False, {
                "reason": "locked",
                "seconds": int(
                    LOCK_DURATION.total_seconds()
                ),
                "reset_used": bool(user["rate_reset_used"]),
            }

        con.execute("""
            INSERT INTO request_log(user_id, created_at)
            VALUES (?, ?)
        """, (user_id, current.isoformat()))

        con.commit()

        return True, {
            "minute_count": minute_count + 1,
            "hour_count": hour_count + 1,
        }

    except Exception:
        con.rollback()
        logger.exception("REGISTER REQUEST ERROR")
        raise

    finally:
        con.close()


def can_reset_lock(user_id):
    user = get_user(user_id)
    return user is not None and not bool(user["rate_reset_used"])


# =========================================================
# CHATS
# =========================================================

def create_chat(user_id, title="Новый чат"):
    timestamp = iso_now()
    con = db()

    try:
        cur = con.execute("""
            INSERT INTO chats(
                user_id, title, created_at, updated_at
            )
            VALUES (?, ?, ?, ?)
        """, (user_id, title, timestamp, timestamp))

        con.commit()
        return cur.lastrowid

    finally:
        con.close()


def get_chats(user_id):
    con = db()

    try:
        return con.execute("""
            SELECT *
            FROM chats
            WHERE user_id = ?
            ORDER BY updated_at DESC
        """, (user_id,)).fetchall()

    finally:
        con.close()


def get_chat(chat_id, user_id):
    con = db()

    try:
        return con.execute("""
            SELECT *
            FROM chats
            WHERE id = ?
            AND user_id = ?
        """, (chat_id, user_id)).fetchone()

    finally:
        con.close()


def update_chat_title(chat_id, user_id, title):
    con = db()

    try:
        con.execute("""
            UPDATE chats
            SET title = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
        """, (title, iso_now(), chat_id, user_id))

        con.commit()

    finally:
        con.close()


def clear_chat(chat_id, user_id):
    con = db()

    try:
        con.execute("""
            DELETE FROM messages
            WHERE chat_id = ?
            AND EXISTS (
                SELECT 1 FROM chats
                WHERE chats.id = ?
                AND chats.user_id = ?
            )
        """, (chat_id, chat_id, user_id))

        con.commit()

    finally:
        con.close()


def save_message(chat_id, role, content):
    con = db()

    try:
        timestamp = iso_now()

        con.execute("""
            INSERT INTO messages(
                chat_id, role, content, created_at
            )
            VALUES (?, ?, ?, ?)
        """, (chat_id, role, content, timestamp))

        con.execute("""
            UPDATE chats
            SET updated_at = ?
            WHERE id = ?
        """, (timestamp, chat_id))

        con.commit()

    finally:
        con.close()


def get_history(chat_id):
    con = db()

    try:
        rows = con.execute("""
            SELECT role, content
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (chat_id, MAX_HISTORY)).fetchall()

    finally:
        con.close()

    rows.reverse()

    return [
        {
            "role": row["role"],
            "content": row["content"],
        }
        for row in rows
    ]


def generate_local_title(text):
    title = " ".join(text.split())
    return title[:40].rstrip() or "Новый чат"


# =========================================================
# KEYBOARDS
# =========================================================

def menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 МОИ ЧАТЫ", callback_data="menu_chats")],
        [InlineKeyboardButton("👤 МОЙ ПРОФИЛЬ", callback_data="menu_profile")],
        [
            InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="menu_stats"),
            InlineKeyboardButton("ℹ️ ПОМОЩЬ", callback_data="menu_help"),
        ],
    ])


def only_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☰ МЕНЮ", callback_data="menu")]
    ])


def chats_keyboard(user_id):
    buttons = []

    for chat in get_chats(user_id)[:10]:
        title = str(chat["title"] or "Новый чат")[:35]

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


def locked_keyboard(user_id):
    buttons = []

    if can_reset_lock(user_id):
        buttons.append([
            InlineKeyboardButton(
                "⭐ СБРОСИТЬ ОГРАНИЧЕНИЕ • 10 ⭐",
                callback_data="rate_reset",
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
# COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user

    get_user(
        user.id,
        user.username or "",
        user.first_name or "",
    )

    chats = get_chats(user.id)
    chat_id = chats[0]["id"] if chats else create_chat(user.id)

    context.user_data["chat_id"] = chat_id

    await update.message.reply_text(
        "🧠 *Добро пожаловать в MULTI AI!*\n\n"
        "Модель: *GPT-5.5*\n\n"
        "♾️ Безлимитный доступ\n"
        "🛡️ 4 запроса/мин и 25 запросов/час\n"
        "💬 История сохраняется в твоих чатах.\n\n"
        "Просто напиши сообщение!",
        parse_mode="Markdown",
        reply_markup=menu_keyboard(),
    )


async def menu_command(update, context):
    if update.message:
        await update.message.reply_text(
            "☰ *MULTI AI — МЕНЮ*",
            parse_mode="Markdown",
            reply_markup=menu_keyboard(),
        )


async def profile_command(update, context):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    get_user(user.id, user.username or "", user.first_name or "")
    status = get_rate_status(user.id)

    lock_text = (
        f"🔒 Заблокирован ещё на: *{format_duration(status['seconds'])}*"
        if status["locked"]
        else "🟢 Доступ активен"
    )

    await update.message.reply_text(
        "👤 *МОЙ ПРОФИЛЬ*\n\n"
        "🧠 Модель: `GPT-5.5`\n"
        "♾️ Токены не ограничены\n\n"
        f"📨 За минуту: *{status['minute_count']}/{MAX_REQUESTS_PER_MINUTE}*\n"
        f"📨 За час: *{status['hour_count']}/{MAX_REQUESTS_PER_HOUR}*\n\n"
        f"{lock_text}",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )


async def stats_command(update, context):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    get_user(user.id, user.username or "", user.first_name or "")
    status = get_rate_status(user.id)

    await update.message.reply_text(
        "📊 *СТАТИСТИКА*\n\n"
        f"📨 За минуту: *{status['minute_count']}/{MAX_REQUESTS_PER_MINUTE}*\n"
        f"📨 За час: *{status['hour_count']}/{MAX_REQUESTS_PER_HOUR}*\n\n"
        "♾️ Токены: без лимита\n"
        "🧠 Модель: *GPT-5.5*",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )


async def help_command(update, context):
    if not update.message:
        return

    await update.message.reply_text(
        "ℹ️ *ПОМОЩЬ*\n\n"
        "🧠 MULTI AI работает на GPT-5.5.\n"
        "♾️ Лимита токенов нет.\n"
        "🛡️ 4 запроса за минуту.\n"
        "🛡️ 25 запросов за час.\n"
        "⏳ После превышения часового лимита блокировка на 2 часа.\n"
        "⭐ Одноразовый сброс блокировки: 10 Stars.\n\n"
        "Команды:\n"
        "`/start` — запуск\n"
        "`/menu` — меню\n"
        "`/profile` — профиль\n"
        "`/stats` — статистика\n"
        "`/chats` — мои чаты\n"
        "`/clear` — очистить чат\n"
        "`/help` — помощь",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )


async def chats_command(update, context):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    get_user(user.id, user.username or "", user.first_name or "")

    chats = get_chats(user.id)

    if not chats:
        context.user_data["chat_id"] = create_chat(user.id)

    await update.message.reply_text(
        "💬 *МОИ ЧАТЫ*\n\n"
        "Выбери существующий чат или создай новый:",
        parse_mode="Markdown",
        reply_markup=chats_keyboard(user.id),
    )


async def clear_command(update, context):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    get_user(user.id, user.username or "", user.first_name or "")

    chat_id = context.user_data.get("chat_id")

    if not chat_id or not get_chat(chat_id, user.id):
        chats = get_chats(user.id)
        chat_id = chats[0]["id"] if chats else create_chat(user.id)
        context.user_data["chat_id"] = chat_id

    clear_chat(chat_id, user.id)

    await update.message.reply_text(
        "🧹 *История текущего чата очищена.*",
        parse_mode="Markdown",
        reply_markup=only_menu_keyboard(),
    )


# =========================================================
# AI
# =========================================================

def describe_ai_error(error):
    status = getattr(error, "status_code", None)
    message = str(error)

    logger.error(
        "AI API ERROR | type=%s | status=%s | message=%s",
        type(error).__name__,
        status,
        message,
    )

    if status in (401, 403):
        return "🔑 Ошибка авторизации AI API. Проверь AI_API_KEY."

    if status == 404:
        return (
            "🔎 AI API не нашёл endpoint или модель "
            f"`{MODEL}`. Проверь BASE_URL и название модели."
        )

    if status == 429:
        return "⏳ AI API временно ограничил запросы. Попробуй чуть позже."

    if status and status >= 500:
        return "☁️ Сервер AI API временно недоступен."

    if "timeout" in message.lower():
        return "⏱️ AI API слишком долго отвечает."

    return "❌ AI API вернул ошибку. Подробности записаны в лог."


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

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
        )

    except Exception as error:
        logger.exception(
            "REQUEST TO AI FAILED | model=%s | base_url=%s",
            MODEL,
            BASE_URL,
        )
        raise RuntimeError(describe_ai_error(error)) from error

    if not response.choices:
        raise RuntimeError("AI не вернул вариантов ответа.")

    answer = response.choices[0].message.content

    if not answer:
        raise RuntimeError("AI вернул пустой ответ.")

    return str(answer)


# =========================================================
# THINKING
# =========================================================

async def animated_thinking(message, stop_event):
    phrases = [
        "думаю…",
        "обрабатываю…",
        "анализирую…",
        "формирую ответ…",
        "уточняю детали…",
    ]

    index = 0

    try:
        while not stop_event.is_set():
            try:
                await message.edit_text(
                    f"🤔 *{phrases[index % len(phrases)]}*",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

            index += 1

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=1.2,
                )
            except asyncio.TimeoutError:
                pass

    except asyncio.CancelledError:
        # Это НОРМАЛЬНАЯ отмена задачи.
        return

    except Exception:
        logger.exception("THINKING ANIMATION ERROR")


async def stop_animation(task, event):
    if not task:
        return

    event.set()
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("ANIMATION STOP ERROR")


async def send_ai_chunks(message, answer):
    for i in range(0, len(answer), 4000):
        chunk = answer[i:i + 4000]

        try:
            await message.reply_text(
                chunk,
                parse_mode="Markdown",
            )
        except Exception:
            await message.reply_text(chunk)


# =========================================================
# RATE LIMIT MESSAGE
# =========================================================

async def send_rate_limit_message(message, status, user_id):
    if status.get("reason") == "minute":
        await message.reply_text(
            "🛡️ *Слишком быстро!*\n\n"
            f"Максимум *{MAX_REQUESTS_PER_MINUTE} запроса в минуту*.\n"
            "Подожди немного.",
            parse_mode="Markdown",
            reply_markup=only_menu_keyboard(),
        )
        return

    if status.get("reason") == "locked":
        seconds = status.get("seconds", 0)

        text = (
            "🚨 *ДОСТУП ВРЕМЕННО ЗАБЛОКИРОВАН*\n\n"
            f"Превышен лимит *{MAX_REQUESTS_PER_HOUR} запросов в час*.\n\n"
            f"⏳ До сброса: *{format_duration(seconds)}*\n\n"
        )

        if status.get("reset_used"):
            text += "⭐ Одноразовый сброс уже использован."
        else:
            text += "⭐ Можно один раз снять блокировку за *10 Telegram Stars*."

        await message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=locked_keyboard(user_id),
        )


# =========================================================
# MESSAGE HANDLER
# =========================================================

async def message_handler(update, context):
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
    thinking_msg = None
    animation_task = None
    stop_event = asyncio.Event()

    try:
        get_user(
            user.id,
            user.username or "",
            user.first_name or "",
        )

        allowed, status = register_request(user.id)

        if not allowed:
            await send_rate_limit_message(
                update.message,
                status,
                user.id,
            )
            return

        chat_id = context.user_data.get("chat_id")

        if not chat_id or not get_chat(chat_id, user.id):
            chats = get_chats(user.id)

            if chats:
                chat_id = chats[0]["id"]
            else:
                chat_id = create_chat(user.id)

            context.user_data["chat_id"] = chat_id

        try:
            await update.message.chat.send_action(
                action=ChatAction.TYPING
            )
        except Exception:
            pass

        try:
            thinking_msg = await update.message.reply_text(
                "🤔 *думаю…*",
                parse_mode="Markdown",
            )

            animation_task = asyncio.create_task(
                animated_thinking(
                    thinking_msg,
                    stop_event,
                )
            )

        except Exception:
            thinking_msg = None

        history = get_history(chat_id)

        try:
            answer = await ask_ai(text, history)

        except Exception as error:
            logger.exception(
                "AI ERROR | user=%s | chat=%s",
                user.id,
                chat_id,
            )

            await stop_animation(
                animation_task,
                stop_event,
            )

            if thinking_msg:
                try:
                    await thinking_msg.delete()
                except Exception:
                    pass

            error_text = str(error)

            await update.message.reply_text(
                error_text
                if error_text
                else "❌ Не удалось получить ответ от AI."
            )

            return

        # Сохраняем только успешный запрос.
        save_message(chat_id, "user", text)
        save_message(chat_id, "assistant", answer)

        chat = get_chat(chat_id, user.id)

        if chat and chat["title"] == "Новый чат":
            update_chat_title(
                chat_id,
                user.id,
                generate_local_title(text),
            )

        await stop_animation(
            animation_task,
            stop_event,
        )

        if thinking_msg:
            try:
                await thinking_msg.delete()
            except Exception:
                pass

        await send_ai_chunks(
            update.message,
            answer,
        )

    except asyncio.CancelledError:
        await stop_animation(
            animation_task,
            stop_event,
        )
        raise

    except Exception:
        logger.exception(
            "MESSAGE HANDLER ERROR | user=%s",
            user.id,
        )

        await stop_animation(
            animation_task,
            stop_event,
        )

        if thinking_msg:
            try:
                await thinking_msg.delete()
            except Exception:
                pass

        try:
            await update.message.reply_text(
                "❌ Произошла внутренняя ошибка. "
                "Подробности есть в логах."
            )
        except Exception:
            pass


# =========================================================
# PAYMENTS
# =========================================================

async def send_reset_payment(query, context):
    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title="MULTI AI • Сброс ограничения",
        description="Одноразовый сброс двухчасовой блокировки MULTI AI",
        payload=f"multi_ai:rate_reset:{query.from_user.id}",
        currency="XTR",
        prices=[
            LabeledPrice(
                "Сброс ограничения",
                RESET_PRICE_STARS,
            )
        ],
        provider_token="",
    )


async def precheckout_handler(update, context):
    query = update.pre_checkout_query

    try:
        parts = query.invoice_payload.split(":")

        if (
            len(parts) != 3
            or parts[0] != "multi_ai"
            or parts[1] != "rate_reset"
        ):
            await query.answer(
                ok=False,
                error_message="Неизвестный товар.",
            )
            return

        try:
            payload_user = int(parts[2])
        except ValueError:
            await query.answer(
                ok=False,
                error_message="Некорректный заказ.",
            )
            return

        if payload_user != query.from_user.id:
            await query.answer(
                ok=False,
                error_message="Заказ принадлежит другому пользователю.",
            )
            return

        if query.total_amount != RESET_PRICE_STARS:
            await query.answer(
                ok=False,
                error_message="Неверная сумма.",
            )
            return

        user = get_user(query.from_user.id)

        if user is None:
            await query.answer(
                ok=False,
                error_message="Пользователь не найден.",
            )
            return

        lock_until = parse_time(user["lock_until"])

        if not lock_until or now() >= lock_until:
            await query.answer(
                ok=False,
                error_message="Сейчас нет активной блокировки.",
            )
            return

        if user["rate_reset_used"]:
            await query.answer(
                ok=False,
                error_message="Одноразовый сброс уже использован.",
            )
            return

        await query.answer(ok=True)

    except Exception:
        logger.exception("PRECHECKOUT ERROR")

        try:
            await query.answer(
                ok=False,
                error_message="Не удалось проверить оплату.",
            )
        except Exception:
            pass


async def payment_handler(update, context):
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
            len(parts) != 3
            or parts[0] != "multi_ai"
            or parts[1] != "rate_reset"
            or int(parts[2]) != user_id
            or payment.total_amount != RESET_PRICE_STARS
        ):
            return

        charge_id = payment.telegram_payment_charge_id
        con = db()

        try:
            con.execute("BEGIN IMMEDIATE")

            if con.execute(
                "SELECT 1 FROM payments WHERE charge_id = ?",
                (charge_id,),
            ).fetchone():
                con.rollback()
                return

            user = con.execute("""
                SELECT rate_reset_used
                FROM users
                WHERE user_id = ?
            """, (user_id,)).fetchone()

            if user is None or user["rate_reset_used"]:
                con.rollback()
                return

            con.execute("""
                INSERT INTO payments(
                    charge_id, user_id, stars, tokens, created_at
                )
                VALUES (?, ?, ?, 0, ?)
            """, (
                charge_id,
                user_id,
                RESET_PRICE_STARS,
                iso_now(),
            ))

            changed = con.execute("""
                UPDATE users
                SET lock_until = NULL,
                    rate_reset_used = 1,
                    stars_spent = stars_spent + ?
                WHERE user_id = ?
                AND rate_reset_used = 0
            """, (
                RESET_PRICE_STARS,
                user_id,
            )).rowcount

            if changed != 1:
                con.rollback()
                return

            con.execute(
                "DELETE FROM request_log WHERE user_id = ?",
                (user_id,),
            )

            con.commit()

        except Exception:
            con.rollback()
            raise

        finally:
            con.close()

        await update.message.reply_text(
            "🎉 *ОГРАНИЧЕНИЕ СНЯТО!*\n\n"
            "🔓 Доступ к MULTI AI восстановлен.\n"
            "⭐ Одноразовый сброс использован.",
            parse_mode="Markdown",
            reply_markup=menu_keyboard(),
        )

    except Exception:
        logger.exception("PAYMENT ERROR")

        try:
            await update.message.reply_text(
                "⚠️ Платёж получен, но возникла ошибка "
                "при снятии ограничения. Не покупай повторно."
            )
        except Exception:
            pass


# =========================================================
# BUTTONS
# =========================================================

async def button_handler(update, context):
    query = update.callback_query

    if not query:
        return

    try:
        user_id = query.from_user.id
        data = query.data or ""

        if data == "menu":
            await query.answer()
            await query.edit_message_text(
                "☰ *MULTI AI — МЕНЮ*",
                parse_mode="Markdown",
                reply_markup=menu_keyboard(),
            )
            return

        if data == "menu_profile":
            await query.answer()

            user = get_user(user_id)
            status = get_rate_status(user_id)

            lock_text = (
                f"🔒 Заблокирован на *{format_duration(status['seconds'])}*"
                if status["locked"]
                else "🟢 Доступ активен"
            )

            await query.edit_message_text(
                "👤 *МОЙ ПРОФИЛЬ*\n\n"
                "🧠 Модель: `GPT-5.5`\n"
                "♾️ Лимита токенов нет\n\n"
                f"📨 Минута: *{status['minute_count']}/{MAX_REQUESTS_PER_MINUTE}*\n"
                f"📨 Час: *{status['hour_count']}/{MAX_REQUESTS_PER_HOUR}*\n\n"
                f"{lock_text}",
                parse_mode="Markdown",
                reply_markup=only_menu_keyboard(),
            )
            return

        if data == "menu_stats":
            await query.answer()

            get_user(user_id)
            status = get_rate_status(user_id)

            await query.edit_message_text(
                "📊 *СТАТИСТИКА*\n\n"
                f"📨 За минуту: *{status['minute_count']}/{MAX_REQUESTS_PER_MINUTE}*\n"
                f"📨 За час: *{status['hour_count']}/{MAX_REQUESTS_PER_HOUR}*\n\n"
                "♾️ Токены: без лимита\n"
                "🧠 Модель: GPT-5.5",
                parse_mode="Markdown",
                reply_markup=only_menu_keyboard(),
            )
            return

        if data == "menu_help":
            await query.answer()

            await query.edit_message_text(
                "ℹ️ *ПОМОЩЬ*\n\n"
                "🧠 MULTI AI работает на GPT-5.5.\n"
                "♾️ Лимита токенов нет.\n"
                "🛡️ 4 запроса за минуту.\n"
                "🛡️ 25 запросов за час.\n"
                "⏳ После превышения часового лимита блокировка на 2 часа.\n"
                "⭐ Одноразовый сброс: 10 Stars.",
                parse_mode="Markdown",
                reply_markup=only_menu_keyboard(),
            )
            return

        if data == "menu_chats":
            await query.answer()

            get_user(user_id)

            await query.edit_message_text(
                "💬 *МОИ ЧАТЫ*\n\n"
                "Выбери чат или создай новый:",
                parse_mode="Markdown",
                reply_markup=chats_keyboard(user_id),
            )
            return

        if data == "new_chat":
            await query.answer()

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
                chat_id = int(data.split(":", 1)[1])
            except (ValueError, IndexError):
                await query.answer(
                    "Некорректный чат.",
                    show_alert=True,
                )
                return

            chat = get_chat(chat_id, user_id)

            if chat is None:
                await query.answer(
                    "Чат не найден.",
                    show_alert=True,
                )
                return

            await query.answer()
            context.user_data["chat_id"] = chat_id

            history = get_history(chat_id)

            if history:
                preview = []

                for item in history[-4:]:
                    prefix = (
                        "👤 "
                        if item["role"] == "user"
                        else "🧠 "
                    )
                    preview.append(
                        prefix + item["content"][:300]
                    )

                text = (
                    f"💬 *{chat['title']}*\n\n"
                    + "\n\n".join(preview)
                    + "\n\nПродолжай диалог."
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

        if data == "rate_reset":
            if not can_reset_lock(user_id):
                await query.answer(
                    "Одноразовый сброс уже использован.",
                    show_alert=True,
                )
                return

            user = get_user(user_id)
            lock_until = parse_time(user["lock_until"])

            if not lock_until or now() >= lock_until:
                await query.answer(
                    "Блокировки уже нет.",
                    show_alert=True,
                )
                return

            await query.answer()
            await send_reset_payment(query, context)
            return

    except Exception:
        logger.exception(
            "BUTTON HANDLER ERROR user=%s data=%s",
            query.from_user.id,
            query.data,
        )

        try:
            await query.answer(
                "Не удалось выполнить действие.",
                show_alert=True,
            )
        except Exception:
            pass


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    logger.error(
        "UNHANDLED TELEGRAM ERROR: %r",
        context.error,
        exc_info=context.error,
    )


# =========================================================
# STARTUP
# =========================================================

async def post_init(application: Application):
    logger.info("Подготовка Telegram...")

    try:
        await application.bot.delete_webhook(
            drop_pending_updates=False
        )
        logger.info("Webhook удалён.")
    except Exception:
        logger.exception("Не удалось удалить webhook.")

    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Запуск"),
            BotCommand("menu", "Меню"),
            BotCommand("chats", "Мои чаты"),
            BotCommand("profile", "Профиль"),
            BotCommand("stats", "Статистика"),
            BotCommand("clear", "Очистить чат"),
            BotCommand("help", "Помощь"),
        ])
        logger.info("Команды установлены.")
    except Exception:
        logger.exception("Не удалось установить команды.")


# =========================================================
# APPLICATION
# =========================================================

def create_application():
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(False)
        .post_init(post_init)
        .build()
    )

    application.add_error_handler(error_handler)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("chats", chats_command))
    application.add_handler(CommandHandler("clear", clear_command))

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
    print("========================================")
    print("🚀 MULTI AI")
    print("========================================")
    print(f"Model: {MODEL}")
    print(f"API: {BASE_URL}")
    print(f"Database: {DB_FILE}")
    print(
        f"Rate limit: "
        f"{MAX_REQUESTS_PER_MINUTE}/min | "
        f"{MAX_REQUESTS_PER_HOUR}/hour"
    )
    print("========================================")

    init_db()

    application = create_application()

    logger.info("MULTI AI запускается...")

    application.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 MULTI AI остановлен.")
    except Exception:
        logger.exception("КРИТИЧЕСКАЯ ОШИБКА ЗАПУСКА")
        raise
