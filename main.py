import os
import re
import time
import random
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==========================================
# CONFIG
# ==========================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = 7783222972

COOLDOWN = 2 * 60 * 60
DATABASE = "gardenview.db"

REP_COOLDOWN = 5 * 60  # 5 минут
REP_MAX = 100
REP_START = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ==========================================
# DATABASE
# ==========================================

db = sqlite3.connect(DATABASE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    tapes INTEGER DEFAULT 0,
    role TEXT DEFAULT 'Участник',
    last_tapes INTEGER DEFAULT 0,
    reputation INTEGER DEFAULT 10,
    last_rep INTEGER DEFAULT 0,
    warns INTEGER DEFAULT 0,
    nickname TEXT,
    pronouns TEXT,
    bio TEXT,
    partner_id INTEGER DEFAULT 0,
    relationship_since INTEGER DEFAULT 0,
    photo1 TEXT,
    photo2 TEXT,
    photo3 TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS punishments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    admin_id INTEGER,
    type TEXT,
    duration INTEGER,
    reason TEXT,
    created_at INTEGER,
    expires_at INTEGER
)
""")

db.commit()

# Миграции: добавляем колонки, если их нет (для старых баз)
existing_cols = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
migrations = {
    "reputation": "INTEGER DEFAULT 10",
    "last_rep": "INTEGER DEFAULT 0",
    "warns": "INTEGER DEFAULT 0",
    "nickname": "TEXT",
    "pronouns": "TEXT",
    "bio": "TEXT",
    "partner_id": "INTEGER DEFAULT 0",
    "relationship_since": "INTEGER DEFAULT 0",
    "photo1": "TEXT",
    "photo2": "TEXT",
    "photo3": "TEXT",
}
for col, decl in migrations.items():
    if col not in existing_cols:
        db.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
db.commit()


def register_user(user):
    db.execute(
        """
        INSERT INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
        """,
        (user.id, user.username, user.first_name),
    )
    db.commit()


def get_user(user):
    register_user(user)
    return db.execute(
        "SELECT * FROM users WHERE user_id = ?", (user.id,)
    ).fetchone()


def get_user_by_id(user_id):
    return db.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()


def display_name(row_or_user):
    """Возвращает @username или first_name."""
    if isinstance(row_or_user, sqlite3.Row):
        uname = row_or_user["username"]
        fname = row_or_user["first_name"]
    else:
        uname = row_or_user.username
        fname = row_or_user.first_name
    if uname:
        return f"@{uname}"
    return fname or "тун"


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID


# ==========================================
# HELPERS — время
# ==========================================

DURATION_RE = re.compile(r"^(\d+)([мчдmhd])$", re.IGNORECASE)

DURATION_MULT = {
    "м": 60, "m": 60,
    "ч": 3600, "h": 3600,
    "д": 86400, "d": 86400,
}


def parse_duration(text: str):
    """'1м'/'2ч'/'3д' -> секунды или None."""
    m = DURATION_RE.match(text.strip())
    if not m:
        return None
    amount = int(m.group(1))
    unit = m.group(2).lower()
    return amount * DURATION_MULT[unit]


def fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0м"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes and not days:
        parts.append(f"{minutes}м")
    return " ".join(parts) or "0м"


def fmt_relationship(seconds: int) -> str:
    """
    Форматирует таймер отношений:
    - 1 мес, 28дн 3ч
    - 5дн 3ч
    - 3ч
    - 1ч
    - 28дн
    """
    if seconds <= 0:
        return "0ч"

    MONTH = 30 * 86400
    DAY = 86400
    HOUR = 3600

    months = seconds // MONTH
    rem = seconds % MONTH
    days = rem // DAY
    rem %= DAY
    hours = rem // HOUR

    parts = []

    if months:
        # месяц + оставшиеся дни/часы (без слова "мес" в днях)
        head = f"{months} мес"
        tail = []
        if days:
            tail.append(f"{days}дн")
        if hours:
            tail.append(f"{hours}ч")
        if tail:
            return head + ", " + " ".join(tail)
        return head

    if days:
        parts.append(f"{days}дн")
    if hours:
        parts.append(f"{hours}ч")
    if not parts:
        parts.append("0ч")
    return " ".join(parts)


# ==========================================
# START
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)

    await update.effective_message.reply_text(
        f"🌼 Привет, {user.first_name}!\n\n"
        "Я Дэнди, рад видеть тебя в Gardenview Roleplay!\n\n"
        "Здесь ты можешь знакомиться с другими тунами, "
        "участвовать в ролевой и собирать кассеты.\n\n"
        "📜 правила — правила\n"
        "🎞️ ферма / фарм — получить кассеты\n"
        "👤 профиль / мой профиль — личный кабинет\n"
        "✏️ редактировать профиль — изменить профиль\n"
        "💐 отношения — предложить отношения (ответом на сообщение)\n"
        "⭐ +реп — повысить репутацию (ответом на сообщение)\n\n"
        "Удачного пребывания в Gardenview!"
    )


# ==========================================
# WELCOME
# ==========================================

async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        register_user(member)
        await update.message.reply_text(
            f"🌼 Новый посетитель Gardenview!\n\n"
            f"Привет, {member.first_name}!\n"
            "Я Дэнди, добро пожаловать в мой сад!\n\n"
            "Осматривайся, знакомься с тунами "
            "и не забудь ознакомиться с правилами.\n\n"
            "📜 правила"
        )


# ==========================================
# RULES
# ==========================================

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = db.execute(
        "SELECT value FROM settings WHERE name = 'rules'"
    ).fetchone()

    if result is None:
        await update.effective_message.reply_text(
            "📜 Правила Gardenview пока не установлены."
        )
        return

    await update.effective_message.reply_text(
        f"🌼 GARDENVIEW ROLEPLAY\n📜 Правила\n\n{result['value']}"
    )


async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "❌ Настраивать правила можно только в личных сообщениях с ботом."
        )
        return

    message = update.effective_message.text or ""
    # убираем команду/ключевое слово
    parts = message.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text(
            "Использование:\n\nsetrules Текст правил"
        )
        return

    text = parts[1].strip()

    db.execute(
        """
        INSERT INTO settings (name, value)
        VALUES ('rules', ?)
        ON CONFLICT(name) DO UPDATE SET value = excluded.value
        """,
        (text,),
    )
    db.commit()

    await update.effective_message.reply_text(
        "🌼 Дэнди успешно обновил правила Gardenview!"
    )


# ==========================================
# TAPES (ферма / фарм / кассеты)
# ==========================================

async def tapes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user)

    now = int(time.time())
    remaining = COOLDOWN - (now - data["last_tapes"])

    if remaining > 0:
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        seconds = remaining % 60
        await update.effective_message.reply_text(
            "🎞️ Ты уже забирал кассеты!\n\n"
            f"⏳ Следующая награда через:\n"
            f"{hours} ч. {minutes} мин. {seconds} сек."
        )
        return

    reward = random.randint(30, 45)

    db.execute(
        """
        UPDATE users
        SET tapes = tapes + ?, last_tapes = ?
        WHERE user_id = ?
        """,
        (reward, now, user.id),
    )
    db.commit()

    await update.effective_message.reply_text(
        "📼 Дэнди приготовил для тебя награду!\n\n"
        f"🎞️ Получено кассет: +{reward}\n\n"
        "Возвращайся через 2 часа!"
    )


# ==========================================
# PROFILE
# ==========================================

def build_profile_text(row: sqlite3.Row) -> str:
    uname = display_name(row)
    nickname = row["nickname"]
    pronouns = row["pronouns"]
    bio = row["bio"]

    lines = ["🌼 GARDENVIEW | Личный кабинет", ""]

    if nickname:
        lines.append(f"👤 {nickname} ({uname})")
    else:
        lines.append(f"👤 {uname}")

    if pronouns:
        lines.append(f"🏳️ Местоимения: {pronouns}")

    if bio:
        lines.append(f"📝 {bio}")

    lines.append("")
    lines.append(f"🎞️ Кассеты: {row['tapes']}")
    lines.append(f"⭐ Репутация: {row['reputation']}/{REP_MAX}")
    lines.append(f"⚠️ Варны: {row['warns']}/3")
    lines.append(f"📺 Роль: {row['role']}")

    # Отношения
    if row["partner_id"]:
        partner = get_user_by_id(row["partner_id"])
        if partner:
            since = row["relationship_since"] or 0
            delta = max(0, int(time.time()) - since) if since else 0
            timer = fmt_relationship(delta) if since else "0ч"
            lines.append(
                f"💐 в отношениях с {display_name(partner)} уже {timer}"
            )

    return "\n".join(lines)


def profile_keyboard(row: sqlite3.Row, requester_id: int):
    """Кнопка редактирования доступна только владельцу профиля."""
    if row["user_id"] != requester_id:
        return None
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✏️ Редактировать профиль", callback_data="edit_profile")]]
    )
    return kb


async def send_profile(update: Update, target_row: sqlite3.Row, reply: bool = True):
    text = build_profile_text(target_row)
    kb = profile_keyboard(target_row, update.effective_user.id)

    photos = [p for p in (target_row["photo1"], target_row["photo2"], target_row["photo3"]) if p]

    if photos:
        # отправляем первое фото с подписью, остальные — отдельно
        try:
            await update.effective_message.reply_photo(
                photo=photos[0], caption=text, reply_markup=kb
            )
            for extra in photos[1:]:
                await update.effective_message.reply_photo(photo=extra)
            return
        except Exception as e:
            logger.warning(f"Не удалось отправить фото профиля: {e}")

    await update.effective_message.reply_text(text, reply_markup=kb)


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user)

    # Если ответом на чужое сообщение — показываем профиль того юзера
    if update.effective_message.reply_to_message:
        target_user = update.effective_message.reply_to_message.from_user
        if target_user and not target_user.is_bot:
            target_data = get_user(target_user)
            await send_profile(update, target_data)
            return

    await send_profile(update, data)


# ==========================================
# SET ROLE (ответом на сообщение)
# ==========================================

async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    msg = update.effective_message

    # Вариант 1: ответом на сообщение
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
        if target_user.is_bot:
            return
        # роль — всё после "роль"
        text = msg.text or msg.caption or ""
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await msg.reply_text(
                "Использование (ответом):\nРоль РОЛЬ\n\n"
                "Использование (по юзу):\nроль @username РОЛЬ"
            )
            return
        role = parts[1].strip()
        register_user(target_user)
        db.execute(
            "UPDATE users SET role = ? WHERE user_id = ?",
            (role, target_user.id),
        )
        db.commit()
        await msg.reply_text(
            f"📺 Роль пользователя {display_name(target_user)} изменена!\n\n"
            f"Новая роль: {role}"
        )
        return

    # Вариант 2: роль @username РОЛЬ
    if len(context.args) >= 2:
        username = context.args[0].lstrip("@").lower()
        role = " ".join(context.args[1:])
        target = db.execute(
            "SELECT user_id FROM users WHERE LOWER(username) = ?",
            (username,),
        ).fetchone()
        if target is None:
            await msg.reply_text(
                "❌ Пользователь не найден.\n\n"
                "Пусть он сначала напишет боту start "
                "или отправит сообщение в группе."
            )
            return
        db.execute(
            "UPDATE users SET role = ? WHERE user_id = ?",
            (role, target["user_id"]),
        )
        db.commit()
        await msg.reply_text(
            f"📺 Роль пользователя @{username} изменена!\n\n"
            f"Новая роль: {role}"
        )
        return

    await msg.reply_text(
        "Использование:\n"
        "• Ответом на сообщение: Роль РОЛЬ\n"
        "• По юзу: роль @username РОЛЬ"
    )


# ==========================================
# REPUTATION (+реп)
# ==========================================

async def reputation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    sender = update.effective_user
    register_user(sender)

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text(
            "⭐ Чтобы повысить репутацию, ответь командой «+реп» на сообщение другого участника."
        )
        return

    target_user = msg.reply_to_message.from_user
    if target_user.is_bot:
        return
    if target_user.id == sender.id:
        await msg.reply_text("❌ Нельзя повышать репутацию самому себе.")
        return

    target_row = get_user(target_user)
    sender_row = get_user(sender)

    now = int(time.time())
    remaining = REP_COOLDOWN - (now - (sender_row["last_rep"] or 0))
    if remaining > 0:
        await msg.reply_text(
            f"⏳ Ты уже повышал репутацию недавно!\n"
            f"Следующий +реп через {fmt_duration(remaining)}."
        )
        return

    new_rep = min(REP_MAX, (target_row["reputation"] or 0) + 1)
    db.execute(
        "UPDATE users SET reputation = ? WHERE user_id = ?",
        (new_rep, target_user.id),
    )
    db.execute(
        "UPDATE users SET last_rep = ? WHERE user_id = ?",
        (now, sender.id),
    )
    db.commit()

    await msg.reply_text(
        f"⭐ {display_name(target_user)} получил +1 репутацию!\n"
        f"Теперь у него {new_rep}/{REP_MAX} 🌟"
    )


# ==========================================
# RELATIONSHIPS (отношения)
# ==========================================

async def relationships(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    sender = update.effective_user
    sender_row = get_user(sender)

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text(
            "💐 Чтобы предложить отношения, ответь командой «отношения» "
            "на сообщение другого участника."
        )
        return

    target_user = msg.reply_to_message.from_user
    if target_user.is_bot:
        return
    if target_user.id == sender.id:
        await msg.reply_text("❌ Нельзя предложить отношения самому себе.")
        return

    target_row = get_user(target_user)

    if sender_row["partner_id"]:
        await msg.reply_text(
            "❌ Ты уже в отношениях. Сначала нужно их завершить."
        )
        return
    if target_row["partner_id"]:
        await msg.reply_text(
            f"❌ {display_name(target_user)} уже в отношениях."
        )
        return

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Да", callback_data=f"rel_yes:{sender.id}:{target_user.id}"),
                InlineKeyboardButton("❌ Нет", callback_data=f"rel_no:{sender.id}:{target_user.id}"),
            ]
        ]
    )

    await msg.reply_text(
        f"💐 {display_name(sender)} предлагает отношения {display_name(target_user)}!\n\n"
        f"{display_name(target_user)}, ты согласен(на)?",
        reply_markup=kb,
    )


async def relationship_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if data.startswith("rel_yes:"):
        _, proposer_id, target_id = data.split(":")
        proposer_id = int(proposer_id)
        target_id = int(target_id)

        if query.from_user.id != target_id:
            await query.answer("Это предложение не для тебя.", show_alert=True)
            return

        proposer = get_user_by_id(proposer_id)
        target = get_user_by_id(target_id)

        if not proposer or not target:
            await query.answer("Ошибка: пользователь не найден.", show_alert=True)
            return

        if proposer["partner_id"] or target["partner_id"]:
            await query.edit_message_text("❌ Кто-то из вас уже в отношениях.")
            return

        now = int(time.time())
        db.execute(
            "UPDATE users SET partner_id = ?, relationship_since = ? WHERE user_id = ?",
            (target_id, now, proposer_id),
        )
        db.execute(
            "UPDATE users SET partner_id = ?, relationship_since = ? WHERE user_id = ?",
            (proposer_id, now, target_id),
        )
        db.commit()

        await query.edit_message_text(
            f"💐 {display_name(proposer)} и {display_name(target)} теперь в отношениях! 🎉"
        )
        await query.answer("Отношения приняты!")

    elif data.startswith("rel_no:"):
        _, proposer_id, target_id = data.split(":")
        target_id = int(target_id)

        if query.from_user.id != target_id:
            await query.answer("Это предложение не для тебя.", show_alert=True)
            return

        await query.edit_message_text("💔 Предложение отношений отклонено.")
        await query.answer("Отклонено.")

    elif data == "edit_profile":
        # Открываем меню редактирования
        if query.from_user.id:
            await show_edit_menu(query)


# ==========================================
# EDIT PROFILE
# ==========================================

EDIT_FIELDS = {
    "nickname": "прозвище",
    "pronouns": "местоимения",
    "bio": "описание",
}

async def show_edit_menu(query):
    user_id = query.from_user.id
    row = get_user_by_id(user_id)
    if not row:
        await query.answer("Ошибка профиля.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏷️ Прозвище", callback_data="edit:nickname")],
            [InlineKeyboardButton("🏳️ Местоимения", callback_data="edit:pronouns")],
            [InlineKeyboardButton("📝 Описание", callback_data="edit:bio")],
            [InlineKeyboardButton("🖼️ Добавить фото (до 3)", callback_data="edit:photo")],
            [InlineKeyboardButton("❌ Закрыть", callback_data="edit:close")],
        ]
    )

    text = (
        "✏️ Редактирование профиля\n\n"
        "Выбери, что хочешь изменить.\n"
        "Для фото — просто отправь изображение следующим сообщением "
        "(можно до 3 штук, старые заменяются)."
    )

    try:
        await query.edit_message_reply_markup(reply_markup=kb)
        await query.message.reply_text(text, reply_markup=kb)
    except Exception:
        await query.message.reply_text(text, reply_markup=kb)
    await query.answer()


async def edit_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда 'редактировать профиль' — открывает меню."""
    user = update.effective_user
    register_user(user)
    row = get_user_by_id(user.id)
    if not row:
        return

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏷️ Прозвище", callback_data="edit:nickname")],
            [InlineKeyboardButton("🏳️ Местоимения", callback_data="edit:pronouns")],
            [InlineKeyboardButton("📝 Описание", callback_data="edit:bio")],
            [InlineKeyboardButton("🖼️ Добавить фото (до 3)", callback_data="edit:photo")],
            [InlineKeyboardButton("❌ Закрыть", callback_data="edit:close")],
        ]
    )

    await update.effective_message.reply_text(
        "✏️ Редактирование профиля\n\n"
        "Выбери, что хочешь изменить.\n"
        "Для фото — просто отправь изображение следующим сообщением "
        "(можно до 3 штук, старые заменяются).",
        reply_markup=kb,
    )


async def edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if not data.startswith("edit:"):
        return

    action = data.split(":", 1)[1]
    user_id = query.from_user.id

    if action == "close":
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer()
        return

    if action == "photo":
        context.user_data["awaiting_photo"] = True
        await query.answer()
        await query.message.reply_text(
            "🖼️ Отправь изображение (до 3 штук). "
            "Старые фото будут заменены по порядку."
        )
        return

    if action in EDIT_FIELDS:
        context.user_data["editing_field"] = action
        await query.answer()
        await query.message.reply_text(
            f"✏️ Отправь новое значение для поля «{EDIT_FIELDS[action]}».\n"
            "Чтобы очистить — отправь «-»."
        )
        return

    await query.answer()


async def edit_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит текст после выбора поля редактирования."""
    user = update.effective_user
    field = context.user_data.get("editing_field")
    if not field:
        return

    text = (update.effective_message.text or "").strip()
    if not text:
        return

    if text == "-":
        value = None
    else:
        value = text[:200]

    db.execute(
        f"UPDATE users SET {field} = ? WHERE user_id = ?",
        (value, user.id),
    )
    db.commit()

    context.user_data["editing_field"] = None
    label = EDIT_FIELDS.get(field, field)

    await update.effective_message.reply_text(
        f"✅ Поле «{label}» обновлено!\n\nПосмотреть профиль: профиль"
    )


async def edit_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит фото после нажатия 'Добавить фото'."""
    user = update.effective_user
    if not context.user_data.get("awaiting_photo"):
        return

    msg = update.effective_message
    if not msg.photo:
        return

    file_id = msg.photo[-1].file_id

    row = get_user_by_id(user.id)
    photos = [p for p in (row["photo1"], row["photo2"], row["photo3"]) if p]
    if len(photos) >= 3:
        await msg.reply_text("🖼️ У тебя уже 3 фото. Новое заменит самое старое.")
        photos = photos[1:]

    photos.append(file_id)
    # гарантируем 3 слота
    while len(photos) < 3:
        photos.append(None)

    db.execute(
        "UPDATE users SET photo1 = ?, photo2 = ?, photo3 = ? WHERE user_id = ?",
        (photos[0], photos[1], photos[2], user.id),
    )
    db.commit()

    context.user_data["awaiting_photo"] = False
    await msg.reply_text("✅ Фото добавлено в профиль!")


# ==========================================
# PUNISHMENTS (бан / мут / кик / варн)
# ==========================================

PUNISHMENT_KEYWORDS = {"бан", "мут", "кик", "варн"}
PUNISHMENT_MAP = {
    "бан": "ban",
    "мут": "mute",
    "кик": "kick",
    "варн": "warn",
}


async def punishment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Формат:
      бан/мут/кик/варн 3м/ч/д
      причина (необязательно)
    Ответом на сообщение нарушителя.
    """
    msg = update.effective_message
    sender = update.effective_user

    if not is_admin(sender.id):
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    first = text.split()[0].lower()
    if first not in PUNISHMENT_KEYWORDS:
        return

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text(
            "❌ Наказание нужно выдавать ответом на сообщение участника.\n\n"
            "Формат: бан/мут/кик/варн 3м/ч/д [причина]"
        )
        return

    target_user = msg.reply_to_message.from_user
    if target_user.is_bot:
        return

    register_user(target_user)
    target_row = get_user(target_user)

    parts = text.split(maxsplit=2)
    action_word = parts[0].lower()
    action = PUNISHMENT_MAP[action_word]

    duration = 0
    reason = None

    if action != "kick":
        if len(parts) < 2:
            await msg.reply_text(
                f"❌ Укажи срок: {action_word} 3м/ч/д [причина]"
            )
            return
        duration = parse_duration(parts[1])
        if duration is None:
            await msg.reply_text(
                "❌ Неверный формат срока. Пример: 3м, 2ч, 1д"
            )
            return
        if len(parts) >= 3 and parts[2].strip():
            reason = parts[2].strip()
    else:
        # кик — причина может быть в parts[1]
        if len(parts) >= 2 and parts[1].strip():
            reason = " ".join(parts[1:]).strip()

    now = int(time.time())
    expires_at = now + duration if duration else 0

    # Варны
    if action == "warn":
        new_warns = (target_row["warns"] or 0) + 1
        if new_warns >= 3:
            new_warns = 0
            new_rep = max(0, (target_row["reputation"] or 0) - 25)
            db.execute(
                "UPDATE users SET warns = ?, reputation = ? WHERE user_id = ?",
                (new_warns, new_rep, target_user.id),
            )
            db.commit()
            await msg.reply_text(
                f"⚠️ {display_name(target_user)} получил 3-й варн!\n"
                f"⭐ Репутация снижена на 25 (теперь {new_rep}/{REP_MAX})."
            )
        else:
            db.execute(
                "UPDATE users SET warns = ? WHERE user_id = ?",
                (new_warns, target_user.id),
            )
            db.commit()
            await msg.reply_text(
                f"⚠️ {display_name(target_user)} получил варн ({new_warns}/3)."
            )
        # сохраняем запись
        db.execute(
            """
            INSERT INTO punishments (user_id, admin_id, type, duration, reason, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (target_user.id, sender.id, action, duration, reason, now, expires_at),
        )
        db.commit()
        return

    # Баны/муты/кики
    db.execute(
        """
        INSERT INTO punishments (user_id, admin_id, type, duration, reason, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (target_user.id, sender.id, action, duration, reason, now, expires_at),
    )
    db.commit()

    label = {
        "ban": "🔨 Бан",
        "mute": "🔇 Мут",
        "kick": "👢 Кик",
    }[action]

    reason_text = f"\n📝 Причина: {reason}" if reason else ""
    duration_text = f"\n⏳ Срок: {fmt_duration(duration)}" if duration else ""

    try:
        if action == "kick":
            await update.effective_chat.ban_member(target_user.id)
            await update.effective_chat.unban_member(target_user.id)
        elif action == "ban":
            await update.effective_chat.ban_member(target_user.id)
        elif action == "mute":
            from telegram import ChatPermissions
            await update.effective_chat.restrict_member(
                target_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=expires_at,
            )
    except Exception as e:
        logger.warning(f"Не удалось применить наказание: {e}")
        await msg.reply_text(
            f"⚠️ Не удалось применить наказание (нет прав?). Запись сохранена."
        )
        return

    await msg.reply_text(
        f"{label} для {display_name(target_user)}"
        f"{duration_text}{reason_text}"
    )


# ==========================================
# TEXT ROUTER (все команды без /)
# ==========================================

TEXT_COMMANDS = {
    # tapes
    "ферма": "tapes",
    "фарм": "tapes",
    "кассеты": "tapes",
    # profile
    "профиль": "me",
    "мой профиль": "me",
    "я": "me",
    # rules
    "правила": "rules",
    # start
    "старт": "start",
    "начать": "start",
    # edit profile
    "редактировать профиль": "edit_profile",
    "изменить профиль": "edit_profile",
    # relationships
    "отношения": "relationships",
    # reputation
    "+реп": "reputation",
    "реп": "reputation",
}


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Роутер текстовых команд без слэша + спец-команды:
    - роль РОЛЬ (ответом)
    - бан/мут/кик/варн ...
    - +реп (ответом)
    - отношения (ответом)
    """
    msg = update.effective_message
    if not msg or not msg.text:
        return

    user = update.effective_user
