import os
import sqlite3
import logging
import time
import random
import json
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

TOKEN_MULTIPLIER = 2  # Токены расходуются в 2 раза больше

TIMEZONE = timezone(timedelta(hours=3))

ADMIN_CODE = "SCR-CDE-1026-2026-MAI-v07"

# =========================================================
# LOGGING (с буфером для админ-панели)
# =========================================================

class BufferHandler(logging.Handler):
    def __init__(self, capacity=500):
        super().__init__()
        self.capacity = capacity
        self.buffer = []

    def emit(self, record):
        try:
            entry = {
                "time": datetime.now(TIMEZONE).isoformat(),
                "level": record.levelname,
                "message": self.format(record),
            }
            self.buffer.append(entry)
            if len(self.buffer) > self.capacity:
                self.buffer = self.buffer[-self.capacity:]
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

error_buffer = BufferHandler()
error_buffer.setLevel(logging.WARNING)
error_buffer.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
logger.addHandler(error_buffer)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не найден в Replit Secrets.")

if not AI_API_KEY:
    raise RuntimeError("AI_API_KEY не найден в Replit Secrets.")

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

TITLE_PROMPT = """
Сгенерируй короткое название (максимум 40 символов) для чата на основе первого сообщения пользователя.
Отвечай ТОЛЬКО названием, без кавычек, без точки в конце, без лишних слов.
Примеры:
- "Как приготовить борщ" → Борщ: рецепт
- "Помоги написать код на Python" → Python-помощь
- "Расскажи про космос" → Космос и вселенная
"""

# =========================================================
# PACKAGES (базовые)
# =========================================================

PACKAGES = {
    "5": {
        "stars": 5,
        "tokens": 5_000,
        "name": "5 000 токенов",
        "active": True,
    },
    "25": {
        "stars": 25,
        "tokens": 50_000,
        "name": "50 000 токенов",
        "active": True,
    },
    "50": {
        "stars": 50,
        "tokens": 125_000,
        "name": "125 000 токенов",
        "active": True,
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
            last_date TEXT,
            joined_at TEXT,
            daily_offer_seen TEXT DEFAULT '',
            is_admin INTEGER DEFAULT 0
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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS custom_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stars INTEGER NOT NULL,
            tokens INTEGER NOT NULL,
            title TEXT NOT NULL,
            active_until TEXT,
            created_at TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
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
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
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
    now_iso = current_time().isoformat()

    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()

    if user is None:
        cursor.execute(
            """
            INSERT INTO users (
                user_id, username, first_name, used_tokens, bonus_tokens,
                total_tokens, stars_spent, last_date, joined_at,
                daily_offer_seen, is_admin
            )
            VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?, '', 0)
            """,
            (user_id, username, first_name, date, now_iso),
        )
        connection.commit()
    elif user["last_date"] != date:
        cursor.execute(
            "UPDATE users SET used_tokens = 0, last_date = ? WHERE user_id = ?",
            (date, user_id),
        )
        connection.commit()

    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    connection.close()
    return user

def get_balance(user_id):
    user = get_user(user_id)
    free_left = max(0, DAILY_LIMIT - user["used_tokens"])
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
    free_left = max(0, DAILY_LIMIT - user["used_tokens"])
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
        (free_used, bonus_used, amount, user_id),
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
        (tokens, stars, user_id),
    )
    connection.commit()
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
    return (current_time() - joined) < timedelta(hours=24)

def get_all_user_ids():
    connection = db()
    cursor = connection.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    connection.close()
    return [row["user_id"] for row in rows]

def set_admin(user_id, value=1):
    connection = db()
    cursor = connection.cursor()
    cursor.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (value, user_id))
    connection.commit()
    connection.close()

def check_admin(user_id):
    user = get_user(user_id)
    return bool(user["is_admin"])

# =========================================================
# CHATS
# =========================================================

def create_chat(user_id, title="Новый чат"):
    timestamp = current_time().isoformat()
    connection = db()
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO chats (user_id, title, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, title, timestamp, timestamp),
    )
    chat_id = cursor.lastrowid
    connection.commit()
    connection.close()
    return chat_id

def get_chats(user_id):
    connection = db()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT * FROM chats WHERE user_id = ? ORDER BY updated_at DESC",
        (user_id,),
    )
    chats = cursor.fetchall()
    connection.close()
    return chats

def get_chat(chat_id, user_id):
    connection = db()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT * FROM chats WHERE id = ? AND user_id = ?",
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
        UPDATE chats SET title = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (title, current_time().isoformat(), chat_id, user_id),
    )
    connection.commit()
    connection.close()

def clear_chat(chat_id):
    connection = db()
    cursor = connection.cursor()
    cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    connection.commit()
    connection.close()

def save_message(chat_id, role, content):
    connection = db()
    cursor = connection.cursor()
    timestamp = current_time().isoformat()
    cursor.execute(
        """
        INSERT INTO messages (chat_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (chat_id, role, content, timestamp),
    )
    cursor.execute(
        "UPDATE chats SET updated_at = ? WHERE id = ?",
        (timestamp, chat_id),
    )
    connection.commit()
    connection.close()

def get_history(chat_id):
    connection = db()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT role, content FROM messages
        WHERE chat_id = ? ORDER BY id DESC LIMIT ?
        """,
        (chat_id, MAX_HISTORY),
    )
    rows = cursor.fetchall()
    connection.close()
    rows.reverse()
    return [{"role": row["role"], "content": row["content"]} for row in rows]

# =========================================================
# CUSTOM OFFERS
# =========================================================

def get_active_custom_offers():
    now = current_time().isoformat()
    connection = db()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT * FROM custom_offers
        WHERE active = 1
        AND (active_until IS NULL OR active_until > ?)
        ORDER BY id DESC
        """,
        (now,),
    )
    rows = cursor.fetchall()
    connection.close()
    return rows

def add_custom_offer(stars, tokens, title, active_until=None):
    connection = db()
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO custom_offers (stars, tokens, title, active_until, created_at, active)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (stars, tokens, title, active_until, current_time().isoformat()),
    )
    offer_id = cursor.lastrowid
    connection.commit()
    connection.close()
    return offer_id

def deactivate_offer(offer_id):
    connection = db()
    cursor = connection.cursor()
    cursor.execute("UPDATE custom_offers SET active = 0 WHERE id = ?", (offer_id,))
    connection.commit()
    connection.close()

def get_offer_by_id(offer_id):
    connection = db()
    cursor = connection.cursor()
    cursor.execute("SELECT * FROM custom_offers WHERE id = ?", (offer_id,))
    row = cursor.fetchone()
    connection.close()
    return row

# =========================================================
# DAILY 23:00 MSK DISCOUNT
# =========================================================

def get_daily_discount():
    """
    Определяет, есть ли сейчас ежедневная скидка 25% (активируется с 23:00 МСК).
    Возвращает: stars_key (str) или None.
    """
    now = current_time()
    # Скидка активна с 23:00 до 23:59
    if now.hour == 23:
        keys = list(PACKAGES.keys())
        if not keys:
            return None
        seed = int(now.strftime("%Y%m%d"))
        random.seed(seed)
        return random.choice(keys)
    return None

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
            total = getattr(usage, "total_tokens", None)
            if total is not None:
                return int(total)
    except Exception:
        pass
    return estimate_tokens(fallback)

# =========================================================
# KEYBOARDS
# =========================================================

def menu_keyboard():
    rows = [
        [InlineKeyboardButton("💬 МОИ ЧАТЫ", callback_data="menu_chats")],
        [
            InlineKeyboardButton("👤 МОЙ ПРОФИЛЬ", callback_data="menu_profile"),
            InlineKeyboardButton("⭐ КУПИТЬ ТОКЕНЫ", callback_data="menu_topup"),
        ],
        [
            InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="menu_stats"),
            InlineKeyboardButton("ℹ️ ПОМОЩЬ", callback_data="menu_help"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

def only_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☰ МЕНЮ", callback_data="menu")],
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
    buttons.append([InlineKeyboardButton("➕ НОВЫЙ ЧАТ", callback_data="new_chat")])
    buttons.append([InlineKeyboardButton("☰ МЕНЮ", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)

def topup_keyboard(user_id=None):
    buttons = []

    # Новый пользователь: 15,000 за 10 звезд (+50%)
    if user_id is not None and is_new_user(user_id):
        buttons.append([
            InlineKeyboardButton(
                "🎁 НОВИЧОК: 10 ⭐ → 15 000 токенов (+50%)",
                callback_data="buy_starter",
            )
        ])

    # Активные кастомные офферы
    for offer in get_active_custom_offers():
        buttons.append([
            InlineKeyboardButton(
                f"🔥 {offer['stars']} ⭐ → {offer['tokens']:,} токенов",
                callback_data=f"buy_offer:{offer['id']}",
            )
        ])

    # Ежедневная скидка 23:00 MSK
    daily_key = get_daily_discount()
    if daily_key and daily_key in PACKAGES:
        package = PACKAGES[daily_key]
        discounted = int(package["stars"] * 0.75)
        if discounted < 1:
            discounted = 1
        buttons.append([
            InlineKeyboardButton(
                f"🌙 23:00 СКИДКА -25%: {discounted} ⭐ → {package['tokens']:,}",
                callback_data=f"buy_daily:{daily_key}",
            )
        ])

    # Базовые пакеты
    buttons.append([InlineKeyboardButton("⭐ 5 → 5 000", callback_data="buy:5")])
    buttons.append([InlineKeyboardButton("🔥 25 → 50 000 • +100%", callback_data="buy:25")])
    buttons.append([InlineKeyboardButton("🚀 50 → 125 000 • +150%", callback_data="buy:50")])

    # Новый пакет: 250 000 за 100 звезд (активен до следующего понедельника)
    buttons.append([
        InlineKeyboardButton(
            "👑 100 ⭐ → 250 000 токенов",
            callback_data="buy:100",
        )
    ])

    # Для новых юзеров: 75 000 за 40 звезд (активен 24 часа)
    if user_id is not None and is_new_user(user_id):
        buttons.append([
            InlineKeyboardButton(
                "⚡ НОВИЧОК: 40 ⭐ → 75 000 токенов",
                callback_data="buy:40",
            )
        ])

    buttons.append([InlineKeyboardButton("☰ МЕНЮ", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)

# =========================================================
# ADMIN KEYBOARD
# =========================================================

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ СОЗДАТЬ ПРЕДЛОЖЕНИЕ", callback_data="adm_create_offer")],
        [InlineKeyboardButton("📋 МОИ ПРЕДЛОЖЕНИЯ", callback_data="adm_list_offers")],
        [InlineKeyboardButton("📢 РАССЫЛКА", callback_data="adm_broadcast")],
        [InlineKeyboardButton("🐞 ОШИБКИ 24Ч", callback_data="adm_errors")],
        [InlineKeyboardButton("📊 СТАТИСТИКА БОТА", callback_data="adm_stats")],
        [InlineKeyboardButton("♻️ ПЕРЕЗАПУСК", callback_data="adm_restart")],
        [InlineKeyboardButton("☰ МЕНЮ", callback_data="menu")],
    ])

# =========================================================
# COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    get_user(user.id, user.username or "", user.first_name or "")

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
        "Выбери подходящий пакет:",
        parse_mode="Markdown",
        reply_markup=topup_keyboard(update.effective_user.id),
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
# ADMIN PANEL
# =========================================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Проверка кода из аргументов команды
    args = context.args
    if not args:
        await update.message.reply_text("🚫 Доступ запрещён.")
        return

    if args[0] != ADMIN_CODE:
        await update.message.reply_text("🚫 Доступ запрещён.")
        return

    get_user(user.id, user.username or "", user.first_name or "")
    set_admin(user.id, 1)

    await update.message.reply_text(
        "🔐 *АДМИН-ПАНЕЛЬ MULTI AI*\n\n"
        "Выбери действие:",
        parse_mode="Markdown",
        reply_markup=admin_keyboard(),
    )

async def admin_stats_text():
    connection = db()
    cursor = connection.cursor()

    cursor.execute("SELECT COUNT(*) AS c FROM users")
    users = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) AS c FROM chats")
    chats = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) AS c FROM messages")
    messages = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) AS c FROM payments")
    payments = cursor.fetchone()["c"]

    cursor.execute("SELECT COALESCE(SUM(stars), 0) AS s FROM payments")
    stars = cursor.fetchone()["s"]

    cursor.execute("SELECT COALESCE(SUM(tokens), 0) AS s FROM payments")
    tokens = cursor.fetchone()["s"]

    cursor.execute("SELECT COUNT(*) AS c FROM users WHERE last_date = ?", (current_date(),))
    active_today = cursor.fetchone()["c"]

    connection.close()

    return (
        "📊 *СТАТИСТИКА БОТА*\n\n"
        f"👥 Пользователей: *{users:,}*\n"
        f"🟢 Активных сегодня: *{active_today:,}*\n"
        f"💬 Чатов: *{chats:,}*\n"
        f"✉️ Сообщений: *{messages:,}*\n\n"
        f"💳 Платежей: *{payments:,}*\n"
        f"⭐ Звёзд получено: *{stars:,}*\n"
        f"🎁 Токенов продано: *{tokens:,}*"
    )

# =========================================================
# AI
# =========================================================

async def ask_ai(text, history):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    response = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
    )

    answer = response.choices[0].message.content or "Не удалось получить ответ."
    used = response_tokens(response, text + answer)
    return answer, used

async def generate_chat_title(text):
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": TITLE_PROMPT},
                {"role": "user", "content": text[:500]},
            ],
        )
        title = response.choices[0].message.content or ""
        title = title.strip().strip('"').strip("'").strip("«»").strip()
        title = title.split("\n")[0]
        title = title[:40]
        return title or "Новый чат"
    except Exception:
        logger.exception("TITLE GENERATION ERROR")
        return text.replace("\n", " ")[:40]

# =========================================================
# ANIMATED "THINKING" MESSAGE
# =========================================================

async def animated_thinking(message, stop_event: asyncio.Event):
    phrases = [
        "думаю…",
        "обрабатываю…",
        "анализирую…",
        "формирую ответ…",
        "уточняю детали…",
    ]
    percent = 0
    idx = 0
    try:
        while not stop_event.is_set():
            phrase = phrases[idx % len(phrases)]
            try:
                await message.edit_text(f"🤔 *{phrase}* ({percent}%)", parse_mode="Markdown")
            except Exception:
                pass
            idx += 1
            percent = min(95, percent + random.randint(3, 12))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.2)
            except asyncio.TimeoutError:
                pass
    except Exception:
        pass

# =========================================================
# MESSAGE HANDLER
# =========================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
        return

    user = update.effective_user

    get_user(user.id, user.username or "", user.first_name or "")

    # ВАЖНО: если чат не выбран — создаём НОВЫЙ
    chat_id = context.user_data.get("chat_id")

    if chat_id is None or get_chat(chat_id, user.id) is None:
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

    await update.message.chat.send_action(action=ChatAction.TYPING)

    # Отправляем анимированное сообщение "думаю…"
    thinking_msg = await update.message.reply_text("🤔 *думаю…* (0%)", parse_mode="Markdown")
    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(animated_thinking(thinking_msg, stop_event))

    history = get_history(chat_id)

    try:
        answer, used = await ask_ai(text, history)

        # Множитель токенов
        used = used * TOKEN_MULTIPLIER

        stop_event.set()
        try:
            await animation_task
        except Exception:
            pass

        # Проверка лимита
        balance = get_balance(user.id)
        if used > balance["total"]:
            try:
                await thinking_msg.edit_text(
                    "⚠️ *Для этого ответа не хватает токенов.*\n\n"
                    "Открой магазин и пополни баланс ⭐",
                    parse_mode="Markdown",
                    reply_markup=topup_keyboard(user.id),
                )
            except Exception:
                pass
            return

        spend_tokens(user.id, used)

        save_message(chat_id, "user", text)
        save_message(chat_id, "assistant", answer)

        # Генерация названия чата через ИИ
        chat = get_chat(chat_id, user.id)
        if chat and chat["title"] == "Новый чат":
            title = await generate_chat_title(text)
            update_chat_title(chat_id, user.id, title)

        # Удаляем сообщение "думаю…"
        try:
            await thinking_msg.delete()
        except Exception:
            try:
                await thinking_msg.edit_text("✅ Готово!")
            except Exception:
                pass

        # Отправляем ответ частями
        chunks = [answer[i:i + 4000] for i in range(0, len(answer), 4000)]
        for chunk in chunks:
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(chunk)

        # Финальная подпись вместо надоедливой полоски меню
        await update.message.reply_text(
            "Рад был помочь! Выход в меню — /menu"
        )

    except Exception as error:
        logger.exception("AI REQUEST ERROR")

        stop_event.set()
        try:
            await animation_task
        except Exception:
            pass

        try:
            await thinking_msg.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ *Ошибка MULTI AI*\n\n"
            f"`{str(error)[:700]}`",
            parse_mode="Markdown",
        )

# =========================================================
# PAYMENTS
# =========================================================

async def send_payment(query, context, title, description, payload, stars):
    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(description, stars)],
        provider_token="",
    )

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    try:
        parts = query.invoice_payload.split(":")

        if len(parts) < 3 or parts[0] != "multi_ai":
            await query.answer(ok=False, error_message="Неизвестный товар.")
            return

        kind = parts[1]

        if kind == "base":
            if len(parts) != 4:
                await query.answer(ok=False, error_message="Некорректный заказ.")
                return
            stars = int(parts[2])
            user_id = int(parts[3])

            all_packages = dict(PACKAGES)
            all_packages["100"] = {"stars": 100, "tokens": 250_000, "name": "250 000 токенов"}
            all_packages["40"] = {"stars": 40, "tokens": 75_000, "name": "75 000 токенов"}

            if str(stars) not in all_packages:
                await query.answer(ok=False, error_message="Пакет не найден.")
                return

            if stars == 100:
                # Активен до следующего понедельника
                if not is_before_next_monday():
                    await query.answer(ok=False, error_message="Акция завершена.")
                    return

            if stars == 40:
                if not is_new_user(user_id):
                    await query.answer(ok=False, error_message="Акция только для новичков.")
                    return

            package = all_packages[str(stars)]
            if query.total_amount != package["stars"]:
                await query.answer(ok=False, error_message="Неверная сумма.")
                return

        elif kind == "starter":
            if len(parts) != 3:
                await query.answer(ok=False, error_message="Некорректный заказ.")
                return
            user_id = int(parts[2])
            if not is_new_user(user_id):
                await query.answer(ok=False, error_message="Акция только для новичков.")
                return
            if query.total_amount != 10:
                await query.answer(ok=False, error_message="Неверная сумма.")
                return

        elif kind == "offer":
            if len(parts) != 3:
                await query.answer(ok=False, error_message="Некорректный заказ.")
                return
            offer_id = int(parts[2])
            offer = get_offer_by_id(offer_id)
            if not offer or not offer["active"]:
                await query.answer(ok=False, error_message="Предложение недоступно.")
                return
            if offer["active_until"] and offer["active_until"] < current_time().isoformat():
                await query.answer(ok=False, error_message="Акция завершена.")
                return
            if query.total_amount != offer["stars"]:
                await query.answer(ok=False, error_message="Неверная сумма.")
                return

        elif kind == "daily":
            if len(parts) != 3:
                await query.answer(ok=False, error_message="Некорректный заказ.")
                return
            key = parts[2]
            daily_key = get_daily_discount()
            if key != daily_key:
                await query.answer(ok=False, error_message="Скидка неактивна.")
                return
            package = PACKAGES[key]
            discounted = max(1, int(package["stars"] * 0.75))
            if query.total_amount != discounted:
                await query.answer(ok=False, error_message="Неверная сумма.")
                return

        else:
            await query.answer(ok=False, error_message="Неизвестный тип заказа.")
            return

        await query.answer(ok=True)

    except Exception:
        logger.exception("PRECHECKOUT ERROR")
        await query.answer(ok=False, error_message="Не удалось проверить оплату.")

async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user_id = update.effective_user.id

    try:
        parts = payment.invoice_payload.split(":")
        if len(parts) < 3 or parts[0] != "multi_ai":
            return

        kind = parts[1]
        tokens_to_add = 0
        stars_amount = payment.total_amount
        label = ""

        if kind == "base":
            stars = int(parts[2])
            payload_user = int(parts[3])
            if payload_user != user_id:
                return
            all_packages = dict(PACKAGES)
            all_packages["100"] = {"stars": 100, "tokens": 250_000, "name": "250 000 токенов"}
            all_packages["40"] = {"stars": 40, "tokens": 75_000, "name": "75 000 токенов"}
            if str(stars) not in all_packages:
                return
            package = all_packages[str(stars)]
            if payment.total_amount != package["stars"]:
                return
            tokens_to_add = package["tokens"]
            label = package["name"]

        elif kind == "starter":
            payload_user = int(parts[2])
            if payload_user != user_id:
                return
            tokens_to_add = 15_000
            label = "Стартовый пакет 15 000"

        elif kind == "offer":
            offer_id = int(parts[2])
            offer = get_offer_by_id(offer_id)
            if not offer:
                return
            tokens_to_add = offer["tokens"]
            label = offer["title"]

        elif kind == "daily":
            key = parts[2]
            package = PACKAGES.get(key)
            if not package:
                return
            tokens_to_add = package["tokens"]
            label = f"Скидка 25%: {package['name']}"

        else:
            return

        if tokens_to_add <= 0:
            return

        charge_id = payment.telegram_payment_charge_id

        connection = db()
        cursor = connection.cursor()
        cursor.execute("SELECT charge_id FROM payments WHERE charge_id = ?", (charge_id,))
        if cursor.fetchone():
            connection.close()
            return

        cursor.execute(
            """
            INSERT INTO payments (charge_id, user_id, stars, tokens, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (charge_id, user_id, stars_amount, tokens_to_add, current_time().isoformat()),
        )
        connection.commit()
        connection.close()

        add_bonus(user_id, tokens_to_add, stars_amount)

        balance = get_balance(user_id)

        await update.message.reply_text(
            "🎉 *ОПЛАТА ПРОШЛА УСПЕШНО!*\n\n"
            f"🎁 {label}\n"
            f"⭐ Получено: *+{tokens_to_add:,} токенов*\n\n"
            f"💎 Доступно сейчас: *{balance['total']:,}*",
            parse_mode="Markdown",
            reply_markup=menu_keyboard(),
        )

    except Exception:
        logger.exception("PAYMENT ERROR")

def is_before_next_monday():
    """Проверяет, что сейчас до следующего понедельника 00:00 МСК"""
    now = current_time()
    days_ahead = (0 - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_monday = (now + timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return now < next_monday

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
                preview_parts.append(prefix + item["content"][:300])
            preview = "\n\n".join(preview_parts)
            text = f"💬 *{chat['title']}*\n\n{preview}\n\nПродолжай диалог."
        else:
            text = f"💬 *{chat['title']}*\n\nЧат пока пуст."

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

    # ==================== ПОКУПКИ ====================

    if data == "buy_starter":
        if not is_new_user(user_id):
            await query.answer("Акция только для новичков", show_alert=True)
            return
        await send_payment(
            query, context,
            title="MULTI AI • Стартовый пакет",
            description="15 000 токенов (+50% бонус)",
            payload=f"multi_ai:starter:{user_id}",
            stars=10,
        )
        return

    if data.startswith("buy_offer:"):
        offer_id = int(data.split(":")[1])
        offer = get_offer_by_id(offer_id)
        if not offer or not offer["active"]:
            await query.answer("Предложение недоступно", show_alert=True)
            return
        if offer["active_until"] and offer["active_until"] < current_time().isoformat():
            await query.answer("Акция завершена", show_alert=True)
            return
        await send_payment(
            query, context,
            title=f"MULTI AI • {offer['title']}",
            description=f"{offer['tokens']:,} токенов",
            payload=f"multi_ai:offer:{offer_id}",
            stars=offer["stars"],
        )
        return

    if data.startswith("buy_daily:"):
        key = data.split(":")[1]
        daily_key = get_daily_discount()
        if key != daily_key:
            await query.answer("Скидка сейчас неактивна", show_alert=True)
            return
        package = PACKAGES.get(key)
        if not package:
            return
        discounted = max(1, int(package["stars"] * 0.75))
        await send_payment(
            query, context,
            title=f"MULTI AI • Скидка 25% ({package['name']})",
            description=package["name"],
            payload=f"multi_ai:daily:{key}",
            stars=discounted,
        )
        return

    if data.startswith("buy:"):
        stars = int(data.split(":")[1])

        all_packages = dict(PACKAGES)
        all_packages[100] = {"stars": 100, "tokens": 250_000, "name": "250 000 токенов"}
        all_packages[40] = {"stars": 40, "tokens": 75_000, "name": "75 000 токенов"}

        if stars not in all_packages:
            return

        if stars == 100 and not is_before_next_monday():
            await query.answer("Акция завершена", show_alert=True)
            return

        if stars == 40 and not is_new_user(user_id):
            await query.answer("Акция только для новичков", show_alert=True)
            return

        package = all_packages[stars]
        await send_payment(
            query, context,
            title=f"MULTI AI • {package['name']}",
            description=package["name"],
            payload=f"multi_ai:base:{stars}:{user_id}",
            stars=package["stars"],
        )
        return

    # ==================== АДМИН ====================

    if data.startswith("adm_"):
        if not check_admin(user_id):
            await query.answer("Доступ запрещён", show_alert=True)
            return

        if data == "adm_stats":
            text = await admin_stats_text()
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=admin_keyboard(),
            )
            return

        if data == "adm_errors":
            entries = error_buffer.buffer[-30:]
            if not entries:
                await query.edit_message_text(
                    "🐞 *ОШИБКИ ЗА 24Ч*\n\nНет записей.",
                    parse_mode="Markdown",
                    reply_markup=admin_keyboard(),
                )
                return
            lines = []
            for e in entries:
                lines.append(f"`{e['time'][11:19]}` {e['level']}: {e['message'][:200]}")
            text = "🐞 *ПОСЛЕДНИЕ ОШИБКИ*\n\n" + "\n".join(lines)[-3500:]
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=admin_keyboard(),
            )
            return

        if data == "adm_list_offers":
            offers = get_active_custom_offers()
            if not offers:
                text = "📋 *АКТИВНЫЕ ПРЕДЛОЖЕНИЯ*\n\nНет активных предложений."
                buttons = [[InlineKeyboardButton("☰ НАЗАД", callback_data="adm_back")]]
                await query.edit_message_text(
                    text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
                return

            buttons = []
            text = "📋 *АКТИВНЫЕ ПРЕДЛОЖЕНИЯ*\n\n"
            for o in offers:
                until = o["active_until"] or "без срока"
                text += f"• ID {o['id']}: {o['stars']}⭐ → {o['tokens']:,} (до {until[:16]})\n"
                buttons.append([
                    InlineKeyboardButton(
                        f"❌ Удалить #{o['id']}",
                        callback_data=f"adm_del_offer:{o['id']}",
                    )
                ])
            buttons.append([InlineKeyboardButton("☰ НАЗАД", callback_data="adm_back")])

            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        if data == "adm_back":
            await query.edit_message_text(
                "🔐 *АДМИН-ПАНЕЛЬ MULTI AI*\n\nВыбери действие:",
                parse_mode="Markdown",
                reply_markup=admin_keyboard(),
            )
            return

        if data == "adm_restart":
            await query.edit_message_text(
                "♻️ *ПЕРЕЗАПУСК*\n\nСервис будет перезапущен через 3 секунды…",
                parse_mode="Markdown",
            )
            await asyncio.sleep(3)
            os._exit(0)
            return

        if data == "adm_create_offer":
            context.user_data["adm_state"] = "offer_stars"
            context.user_data["adm_offer"] = {}
            await query.edit_message_text(
                "➕ *СОЗДАНИЕ ПРЕДЛОЖЕНИЯ*\n\n"
                "Шаг 1/4: введи количество звёзд (целое число).\n\n"
                "Отмена: /cancel",
                parse_mode="Markdown",
            )
            return

        if data == "adm_broadcast":
            context.user_data["adm_state"] = "broadcast_text"
            await query.edit_message_text(
                "📢 *РАССЫЛКА*\n\n"
                "Отправь текст для рассылки.\n\n"
                "Отмена: /cancel",
                parse_mode="Markdown",
            )
            return

        if data.startswith("adm_del_offer:"):
            offer_id = int(data.split(":")[1])
            deactivate_offer(offer_id)
            await query.edit_message_text(
                f"✅ Предложение #{offer_id} деактивировано.",
                reply_markup=admin_keyboard(),
            )
            return

# =========================================================
# ADMIN DIALOG HANDLER
# =========================================================

async def admin_dialog_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("adm_state")
    if not state:
        return False

    user = update.effective_user
    if not check_admin(user.id):
        context.user_data["adm_state"] = None
        return False

    text = (update.message.text or "").strip()

    if text == "/cancel":
        context.user_data["adm_state"] = None
        context.user_data["adm_offer"] = None
        await update.message.reply_text(
            "❌ Отменено.",
            reply_markup=admin_keyboard(),
        )
        return True

    if state == "offer_stars":
        try:
            stars = int(text)
            if stars <= 0 or stars > 100000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Введи корректное целое число > 0.")
            return True
        context.user_data["adm_offer"] = {"stars": stars}
        context.user_data["adm_state"] = "offer_tokens"
        await update.message.reply_text("Шаг 2/4: введи количество токенов.")
        return True

    if state == "offer_tokens":
        try:
            tokens = int(text)
            if tokens <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Введи корректное целое число > 0.")
            return True
        context.user_data["adm_offer"]["tokens"] = tokens
        context.user_data["adm_state"] = "offer_title"
        await update.message.reply_text("Шаг 3/4: введи название предложения.")
        return True

    if state == "offer_title":
        context.user_data["adm_offer"]["title"] = text[:100]
        context.user_data["adm_state"] = "offer_until"
        await update.message.reply_text(
            "Шаг 4/4: введи срок действия в часах (целое число, 0 = без срока)."
        )
        return True

    if state == "offer_until":
        try:
            hours = int(text)
            if hours < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Введи корректное целое число >= 0.")
            return True

        offer = context.user_data["adm_offer"]
        active_until = None
        if hours > 0:
            active_until = (current_time() + timedelta(hours=hours)).isoformat()

        offer_id = add_custom_offer(
            offer["stars"],
            offer["tokens"],
            offer["title"],
            active_until,
        )

        context.user_data["adm_state"] = None
        context.user_data["adm_offer"] = None

        await update.message.reply_text(
            f"✅ Предложение #{offer_id} создано!\n\n"
            f"⭐ {offer['stars']} → 🎁 {offer['tokens']:,}\n"
            f"🏷 {offer['title']}\n"
            f"⏳ {active_until or 'без срока'}",
            reply_markup=admin_keyboard(),
        )
        return True

    if state == "broadcast_text":
        broadcast_text = text
        user_ids = get_all_user_ids()

        status_msg = await update.message.reply_text(
            f"📢 Начинаю рассылку на {len(user_ids)} пользователей…"
        )

        sent = 0
        failed = 0
        for uid in user_ids:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=broadcast_text,
                )
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)

        connection = db()
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO broadcasts (text, sent, failed, created_at) VALUES (?, ?, ?, ?)",
            (broadcast_text, sent, failed, current_time().isoformat()),
        )
        connection.commit()
        connection.close()

        context.user_data["adm_state"] = None

        await status_msg.edit_text(
            f"✅ Рассылка завершена.\n\n"
            f"📨 Отправлено: {sent}\n"
            f"❌ Ошибок: {failed}"
        )
        return True

    return False

# =========================================================
# APPLICATION
# =========================================================

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Запуск"),
        BotCommand("menu", "Меню"),
        BotCommand("chats", "Мои чаты"),
        BotCommand("profile", "Профиль"),
        BotCommand("stats", "Статистика"),
        BotCommand("topup", "Купить токены"),
        BotCommand("clear", "Очистить чат"),
        BotCommand("help", "Помощь"),
    ])

def create_application():
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("topup", topup_command))
    application.add_handler(CommandHandler("chats", chats_command))
    application.add_handler(CommandHandler("clear", clear_command))

    # Админ-панель
    application.add_handler(CommandHandler("admpnl", admin_panel))

    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_handler))

    # Админ-диалог должен идти ДО обычного текстового обработчика
    async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
        handled = await admin_dialog_handler(update, context)
        if handled:
            return
        await message_handler(update, context)

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_router)
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
    print(f"Token multiplier: x{TOKEN_MULTIPLIER}")
    print("5 ⭐  = 5 000")
    print("25 ⭐ = 50 000")
    print("50 ⭐ = 125 000")
    print("100 ⭐ = 250 000 (до след. пн)")
    print("40 ⭐ = 75 000 (для новых)")
    print("================================")
    print("▶️ Запуск Telegram polling...")

    application = create_application()

    print("✅ MULTI AI запущен!")

    application.run_polling(drop_pending_updates=True)

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
                "❌ Критическая ошибка. Перезапуск через 10 секунд..."
            )
            time.sleep(10)
