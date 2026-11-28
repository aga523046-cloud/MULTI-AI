import os
import re
import time
import random
import sqlite3
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
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

REP_COOLDOWN = 5 * 60
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

# Миграции для старых баз
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
    if isinstance(row_or_user, sqlite3.Row):
        uname = row_or_user["username"]
        fname = row_or_user["first_name"]
    else:
        uname = row_or_user.username
        fname = row_or_user.first_name
    return f"@{uname}" if uname else (fname or "тун")


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID


# ==========================================
# HELPERS — время
# ==========================================

DURATION_RE = re.compile(r"^(\d+)([мчдmhd])$", re.IGNORECASE)
DURATION_MULT = {"м": 60, "m": 60, "ч": 3600, "h": 3600, "д": 86400, "d": 86400}


def parse_duration(text: str):
    m = DURATION_RE.match(text.strip())
    if not m:
        return None
    return int(m.group(1)) * DURATION_MULT[m.group(2).lower()]


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

    if months:
        tail = []
        if days:
            tail.append(f"{days}дн")
        if hours:
            tail.append(f"{hours}ч")
        return f"{months} мес, " + " ".join(tail) if tail else f"{months} мес"

    parts = []
    if days:
        parts.append(f"{days}дн")
    if hours:
        parts.append(f"{hours}ч")
    return " ".join(parts) or "0ч"


# ==========================================
# START
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)

    # Deep-link: /start edit / /start edit_profile
    args = context.args or []
    if args and args[0].lower() in ("edit", "edit_profile", "профиль"):
        await update.effective_message.reply_text(
            "✏️ Редактирование профиля\n\n"
            "Выбери, что хочешь изменить.\n"
            "Для фото — просто отправь изображение следующим сообщением "
            "(можно до 3 штук, старые заменяются).",
            reply_markup=edit_menu_kb(),
        )
        return

    await update.effective_message.reply_text(
        f"🌼 Привет, {user.first_name}!\n\n"
        "Я Дэнди, рад видеть тебя в Gardenview Roleplay!\n\n"
        "Здесь ты можешь знакомиться с другими тунами, "
        "участвовать в ролевой и собирать кассеты.\n\n"
        "📜 правила — правила\n"
        "🎞️ ферма / фарм — кассеты\n"
        "👤 мой профиль — личный кабинет\n"
        "👤 профиль — посмотреть чужой профиль (ответом на сообщение)\n"
        "👤 профиль @username — посмотреть чужой профиль по юзу\n"
        "✏️ редактировать профиль — изменить профиль (только в ЛС с ботом)\n"
        "💐 отношения — предложить (ответом)\n"
        "⭐ +реп — репутация (ответом)\n\n"
        "Удачного пребывания в Gardenview!"
    )


# ==========================================
# WELCOME / RULES
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
    parts = message.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text(
            "Использование:\n\nsetrules Текст правил"
        )
        return

    db.execute(
        """
        INSERT INTO settings (name, value)
        VALUES ('rules', ?)
        ON CONFLICT(name) DO UPDATE SET value = excluded.value
        """,
        (parts[1].strip(),),
    )
    db.commit()

    await update.effective_message.reply_text(
        "🌼 Дэнди успешно обновил правила Gardenview!"
    )


# ==========================================
# TAPES
# ==========================================

async def tapes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user)

    now = int(time.time())
    remaining = COOLDOWN - (now - (data["last_tapes"] or 0))

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


def profile_keyboard(row: sqlite3.Row, requester_id: int, in_group: bool):
    """
    Кнопка «Редактировать профиль»:
    - только владельцу профиля
    - в группе ведёт в deep-link в ЛС
    - в ЛС открывает меню
    """
    if row["user_id"] != requester_id:
        return None

    if in_group:
        bot_username = get_bot_username()
        if bot_username:
            url = f"https://t.me/{bot_username}?start=edit"
            return InlineKeyboardMarkup(
                [[InlineKeyboardButton("✏️ Редактировать профиль", url=url)]]
            )
        # fallback, если username бота ещё не получен
        return None

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✏️ Редактировать профиль", callback_data="edit_menu")]]
    )


async def send_profile(update: Update, target_row: sqlite3.Row):
    text = build_profile_text(target_row)
    in_group = update.effective_chat.type != "private"
    kb = profile_keyboard(target_row, update.effective_user.id, in_group)

    photos = [
        p for p in (target_row["photo1"], target_row["photo2"], target_row["photo3"]) if p
    ]

    if photos:
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


async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user)
    await send_profile(update, data)


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    register_user(user)

    text = (msg.text or "").strip()

    parts = text.split(maxsplit=1)
    if len(parts) >= 2 and parts[1].strip().startswith("@"):
        username = parts[1].strip().lstrip("@").lower()
        target = db.execute(
            "SELECT * FROM users WHERE LOWER(username) = ?",
            (username,),
        ).fetchone()
        if target is None:
            await msg.reply_text("❌ Пользователь не найден.")
            return
        await send_profile(update, target)
        return

    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
        if target_user.is_bot:
            await msg.reply_text("❌ Нельзя смотреть профиль бота.")
            return
        if target_user.id == user.id:
            await send_profile(update, get_user(user))
            return
        await send_profile(update, get_user(target_user))
        return

    await msg.reply_text(
        "👤 Использование:\n"
        "• «мой профиль» — свой профиль\n"
        "• «профиль» ответом на сообщение — чужой профиль\n"
        "• «профиль @username» — чужой профиль по юзу"
    )


# ==========================================
# SET ROLE
# ==========================================

async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    msg = update.effective_message
    text = (msg.text or msg.caption or "").strip()
    lower = text.lower()

    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
        if target_user.is_bot:
            return

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

    if lower.startswith("/setrole"):
        args = text.split()[1:]
        if len(args) >= 2:
            username = args[0].lstrip("@").lower()
            role = " ".join(args[1:])
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
        "• По юзу: /setrole @username РОЛЬ"
    )


# ==========================================
# REPUTATION
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
# RELATIONSHIPS
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
        await msg.reply_text("❌ Ты уже в отношениях. Сначала нужно их завершить.")
        return
    if target_row["partner_id"]:
        await msg.reply_text(f"❌ {display_name(target_user)} уже в отношениях.")
        return

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Да",
                    callback_data=f"rel_yes:{sender.id}:{target_user.id}",
                ),
                InlineKeyboardButton(
                    "❌ Нет",
                    callback_data=f"rel_no:{sender.id}:{target_user.id}",
                ),
            ]
        ]
    )

    await msg.reply_text(
        f"💐 {display_name(sender)} предлагает отношения {display_name(target_user)}!\n\n"
        f"{display_name(target_user)}, ты согласен(на)?",
        reply_markup=kb,
    )


async def relationship_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    try:
        parts = data.split(":")
        if len(parts) != 3:
            await q.answer("Некорректные данные.", show_alert=True)
            return
        kind, pid_raw, tid_raw = parts
        pid, tid = int(pid_raw), int(tid_raw)
    except (ValueError, IndexError):
        await q.answer("Некорректные данные.", show_alert=True)
        return

    if q.from_user.id != tid:
        await q.answer("Это предложение не для тебя.", show_alert=True)
        return

    proposer = get_user_by_id(pid)
    target = get_user_by_id(tid)
    if not proposer or not target:
        await q.answer("Пользователь не найден.", show_alert=True)
        return

    if kind == "rel_yes":
        if proposer["partner_id"] or target["partner_id"]:
            await q.edit_message_text("❌ Кто-то из вас уже в отношениях.")
            await q.answer()
            return

        now = int(time.time())
        db.execute(
            "UPDATE users SET partner_id = ?, relationship_since = ? WHERE user_id = ?",
            (tid, now, pid),
        )
        db.execute(
            "UPDATE users SET partner_id = ?, relationship_since = ? WHERE user_id = ?",
            (pid, now, tid),
        )
        db.commit()

        await q.edit_message_text(
            f"💐 {display_name(proposer)} и {display_name(target)} теперь в отношениях! 🎉"
        )
        await q.answer("Отношения приняты!")

    elif kind == "rel_no":
        await q.edit_message_text("💔 Предложение отношений отклонено.")
        await q.answer("Отклонено.")


# ==========================================
# EDIT PROFILE
# ==========================================

EDIT_FIELDS = {
    "nickname": "прозвище",
    "pronouns": "местоимения",
    "bio": "описание",
}

# Кэш username бота (нужен для deep-link в группах)
BOT_USERNAME_CACHE: dict[str, str | None] = {"username": None}


def get_bot_username():
    return BOT_USERNAME_CACHE["username"]


async def cache_bot_username(app):
    me = await app.bot.get_me()
    BOT_USERNAME_CACHE["username"] = me.username
    logger.info(f"Bot username cached: @{me.username}")


def edit_menu_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏷️ Прозвище", callback_data="edit:nickname")],
            [InlineKeyboardButton("🏳️ Местоимения", callback_data="edit:pronouns")],
            [InlineKeyboardButton("📝 Описание", callback_data="edit:bio")],
            [InlineKeyboardButton("🖼️ Добавить фото (до 3)", callback_data="edit:photo")],
            [InlineKeyboardButton("❌ Закрыть", callback_data="edit:close")],
        ]
    )


async def edit_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    'редактировать профиль':
    - в ЛС → открываем меню
    - в группе → говорим идти в ЛС + даём deep-link
    """
    user = update.effective_user
    register_user(user)

    msg = update.effective_message

    if update.effective_chat.type != "private":
        bot_username = get_bot_username()
        if bot_username:
            url = f"https://t.me/{bot_username}?start=edit"
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✏️ Перейти в ЛС", url=url)]]
            )
            await msg.reply_text(
                "✏️ Начни редактировать профиль в личных сообщениях!",
                reply_markup=kb,
            )
        else:
            await msg.reply_text(
                "✏️ Начни редактировать профиль в личных сообщениях с ботом!"
            )
        return

    await msg.reply_text(
        "✏️ Редактирование профиля\n\n"
        "Выбери, что хочешь изменить.\n"
        "Для фото — просто отправь изображение следующим сообщением "
        "(можно до 3 штук, старые заменяются).",
        reply_markup=edit_menu_kb(),
    )


async def edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    # Кнопка под профилем в ЛС
    if data == "edit_menu":
        await q.message.reply_text(
            "✏️ Редактирование профиля\n\n"
            "Выбери, что хочешь изменить.\n"
            "Для фото — просто отправь изображение следующим сообщением.",
            reply_markup=edit_menu_kb(),
        )
        await q.answer()
        return

    if not data.startswith("edit:"):
        await q.answer()
        return

    # Редактирование только в ЛС
    if q.message.chat.type != "private":
        bot_username = get_bot_username()
        if bot_username:
            url = f"https://t.me/{bot_username}?start=edit"
            await q.answer(
                "Начни редактировать профиль в личных сообщениях!",
                show_alert=True,
            )
            try:
                kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✏️ Перейти в ЛС", url=url)]]
                )
                await q.message.reply_text(
                    "✏️ Начни редактировать профиль в личных сообщениях!",
                    reply_markup=kb,
                )
            except Exception:
                pass
        else:
            await q.answer(
                "Начни редактировать профиль в личных сообщениях!",
                show_alert=True,
            )
        return

    action = data.split(":", 1)[1]

    if action == "close":
        try:
            await q.message.delete()
        except Exception:
            pass
        await q.answer()
        return

    if action == "photo":
        context.user_data["awaiting_photo"] = True
        context.user_data["editing_field"] = None
        await q.answer()
        await q.message.reply_text(
            "🖼️ Отправь изображение (до 3 штук). "
            "Старые фото будут заменены по порядку."
        )
        return

    if action in EDIT_FIELDS:
        context.user_data["editing_field"] = action
        context.user_data["awaiting_photo"] = False
        await q.answer()
        await q.message.reply_text(
            f"✏️ Отправь новое значение для поля «{EDIT_FIELDS[action]}».\n"
            "Чтобы очистить — отправь «-»."
        )
        return

    await q.answer()


async def edit_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("editing_field")
    if not field:
        return False

    # Ввод полей — только в ЛС
    if update.effective_chat.type != "private":
        context.user_data["editing_field"] = None
        return False

    text = (update.effective_message.text or "").strip()
    if not text:
        return False

    value = None if text == "-" else text[:200]

    db.execute(
        f"UPDATE users SET {field} = ? WHERE user_id = ?",
        (value, update.effective_user.id),
    )
    db.commit()

    context.user_data["editing_field"] = None
    label = EDIT_FIELDS.get(field, field)

    await update.effective_message.reply_text(
        f"✅ Поле «{label}» обновлено!\n\nПосмотреть профиль: мой профиль"
    )
    return True


async def edit_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_photo"):
        return

    # Фото профиля — только в ЛС
    if update.effective_chat.type != "private":
        context.user_data["awaiting_photo"] = False
        return

    msg = update.effective_message
    if not msg.photo:
        return

    file_id = msg.photo[-1].file_id

    row = get_user_by_id(update.effective_user.id)
    if row is None:
        register_user(update.effective_user)
        row = get_user_by_id(update.effective_user.id)

    photos = [p for p in (row["photo1"], row["photo2"], row["photo3"]) if p]
    if len(photos) >= 3:
        photos = photos[1:]

    photos.append(file_id)
    while len(photos) < 3:
        photos.append(None)

    db.execute(
        "UPDATE users SET photo1 = ?, photo2 = ?, photo3 = ? WHERE user_id = ?",
        (photos[0], photos[1], photos[2], update.effective_user.id),
    )
    db.commit()

    context.user_data["awaiting_photo"] = False
    await msg.reply_text("✅ Фото добавлено в профиль!")
    await msg.reply_text("Посмотреть профиль: мой профиль")


# ==========================================
# PUNISHMENTS
# ==========================================

PUNISHMENT_KEYWORDS = {"бан", "мут", "кик", "варн"}
PUNISHMENT_MAP = {"бан": "ban", "мут": "mute", "кик": "kick", "варн": "warn"}


async def punishment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        if len(parts) >= 2 and parts[1].strip():
            reason = " ".join(parts[1:]).strip()

    now = int(time.time())
    expires_at = now + duration if duration else 0

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

        db.execute(
            """
            INSERT INTO punishments (user_id, admin_id, type, duration, reason, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (target_user.id, sender.id, action, duration, reason, now, expires_at),
        )
        db.commit()
        return

    db.execute(
        """
        INSERT INTO punishments (user_id, admin_id, type, duration, reason, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (target_user.id, sender.id, action, duration, reason, now, expires_at),
    )
    db.commit()

    label = {"ban": "🔨 Бан", "mute": "🔇 Мут", "kick": "👢 Кик"}[action]
    reason_text = f"\n📝 Причина: {reason}" if reason else ""
    duration_text = f"\n⏳ Срок: {fmt_duration(duration)}" if duration else ""

    try:
        if action == "kick":
            await update.effective_chat.ban_member(target_user.id)
            await update.effective_chat.unban_member(target_user.id)
        elif action == "ban":
            await update.effective_chat.ban_member(target_user.id)
        elif action == "mute":
            await update.effective_chat.restrict_member(
                target_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=expires_at,
            )
    except Exception as e:
        logger.warning(f"Не удалось применить наказание: {e}")
        await msg.reply_text(
            "⚠️ Не удалось применить наказание (нет прав у бота?). Запись сохранена."
        )
        return

    await msg.reply_text(
        f"{label} для {display_name(target_user)}{duration_text}{reason_text}"
    )


# ==========================================
# TEXT ROUTER
# ==========================================

TEXT_COMMANDS = {
    "ферма": "tapes",
    "фарм": "tapes",
    "кассеты": "tapes",

    "мой профиль": "my_profile",
    "профиль": "profile",

    "правила": "rules",
    "старт": "start",
    "начать": "start",
    "редактировать профиль": "edit_profile",
    "изменить профиль": "edit_profile",
    "отношения": "relationships",
    "+реп": "reputation",
    "реп": "reputation",
}

HANDLERS = {
    "tapes": tapes,
    "my_profile": my_profile,
    "profile": profile_cmd,
    "rules": rules,
    "start": start,
    "edit_profile": edit_profile_command,
    "relationships": relationships,
    "reputation": reputation,
}


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    user = update.effective_user
    if user:
        register_user(user)

    # 0) Ввод поля редактирования (только в ЛС — внутри проверим)
    if context.user_data.get("editing_field"):
        handled = await edit_text_input(update, context)
        if handled:
            return

    raw = msg.text.strip()
    lower = raw.lower()
    first_word = lower.split()[0] if lower.split() else ""

    # 1) Наказания (админ)
    if is_admin(user.id) and first_word in PUNISHMENT_KEYWORDS:
        await punishment_handler(update, context)
        return

    # 2) Роль РОЛЬ (ответом) / /setrole
    if is_admin(user.id) and (lower.startswith("роль") or lower.startswith("/setrole")):
        await setrole(update, context)
        return

    # 3) «профиль @username»
    if lower.startswith("профиль ") and len(raw.split()) >= 2:
        second = raw.split(maxsplit=1)[1].strip()
        if second.startswith("@"):
            await profile_cmd(update, context)
            return

    # 4) Точные текстовые команды
    handler_name = TEXT_COMMANDS.get(lower)
    if handler_name:
        await HANDLERS[handler_name](update, context)


# ==========================================
# ERROR HANDLER
# ==========================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Handler error:", exc_info=context.error)


# ==========================================
# POST INIT (кэшируем username бота для deep-link)
# ==========================================

async def post_init(app: Application):
    await cache_bot_username(app)


# ==========================================
# MAIN
# ==========================================

def main():
    if not TOKEN:
        raise RuntimeError("TOKEN environment variable is missing!")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # Команды со слэшем
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("tapes", tapes))
    app.add_handler(CommandHandler("me", my_profile))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("setrules", setrules))
    app.add_handler(CommandHandler("setrole", setrole))

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(edit_callback, pattern=r"^edit"))
    app.add_handler(CallbackQueryHandler(relationship_callback, pattern=r"^rel_"))

    # Новые участники
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))

    # Фото (для редактирования профиля)
    app.add_handler(MessageHandler(filters.PHOTO, edit_photo_input))

    # Текстовые команды без слэша
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    app.add_error_handler(error_handler)

    logger.info("Gardenview Roleplay Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
