import os
import re
import json
import time
import random
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = 7783222972
COOLDOWN = 2 * 60 * 60
REP_COOLDOWN = 5 * 60
DATABASE = "gardenview.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

db = sqlite3.connect(DATABASE, check_same_thread=False)
db.row_factory = sqlite3.Row


# =========================================================
# DATABASE
# =========================================================

def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        tapes INTEGER DEFAULT 0,
        role TEXT DEFAULT 'Ð£ÑÐ°ÑÑÐ½Ð¸Ðº',
        last_tapes INTEGER DEFAULT 0,
        reputation INTEGER DEFAULT 10,
        nickname TEXT DEFAULT '',
        pronouns TEXT DEFAULT '',
        bio TEXT DEFAULT '',
        photos TEXT DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS settings (
        name TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS rep_cooldowns (
        user_id INTEGER PRIMARY KEY,
        last_used INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS relationships (
        user_id INTEGER PRIMARY KEY,
        partner_id INTEGER NOT NULL,
        started_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS relationship_requests (
        target_id INTEGER PRIMARY KEY,
        proposer_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER,
        reason TEXT DEFAULT '',
        admin_id INTEGER
    );
    """)
    db.commit()

    # ÐÐ¸Ð³ÑÐ°ÑÐ¸Ñ ÑÑÐ°ÑÐ¾Ð¹ Ð±Ð°Ð·Ñ Ð´Ð°Ð½Ð½ÑÑ
    columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    additions = {
        "reputation": "INTEGER DEFAULT 10",
        "nickname": "TEXT DEFAULT ''",
        "pronouns": "TEXT DEFAULT ''",
        "bio": "TEXT DEFAULT ''",
        "photos": "TEXT DEFAULT '[]'"
    }
    for name, definition in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
    db.commit()


def register_user(user):
    if not user:
        return
    db.execute("""
        INSERT INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
    """, (user.id, user.username, user.first_name))
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


def display_name(row):
    if not row:
        return "ÐÐµÐ¸Ð·Ð²ÐµÑÑÐ½ÑÐ¹ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ"
    if row["nickname"]:
        return row["nickname"]
    if row["username"]:
        return "@" + row["username"]
    return row["first_name"] or "Ð£ÑÐ°ÑÑÐ½Ð¸Ðº"


def mention(row):
    if row and row["username"]:
        return "@" + row["username"]
    if row:
        return f'<a href="tg://user?id={row["user_id"]}">{row["first_name"] or "Ð£ÑÐ°ÑÑÐ½Ð¸Ðº"}</a>'
    return "Ð£ÑÐ°ÑÑÐ½Ð¸Ðº"


# =========================================================
# GENERAL HELPERS
# =========================================================

def parse_duration(value):
    """3Ð¼ = Ð¼Ð¸Ð½ÑÑÑ, 3Ñ = ÑÐ°ÑÑ, 3Ð´ = Ð´Ð½Ð¸."""
    match = re.fullmatch(r"(\d+)([Ð¼ÑÐ´])", value.lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if amount < 1:
        return None
    multiplier = {"Ð¼": 60, "Ñ": 3600, "Ð´": 86400}[unit]
    return amount * multiplier


def format_relationship_time(started_at):
    total_hours = max(0, int((time.time() - started_at) // 3600))
    total_days = total_hours // 24
    hours = total_hours % 24
    months = total_days // 30
    days = total_days % 30

    parts = []
    if months:
        parts.append(f"{months} Ð¼ÐµÑ")
    if days:
        parts.append(f"{days}Ð´Ð½")
    if hours or not parts:
        parts.append(f"{hours}Ñ")
    return " ".join(parts)


async def is_admin(update, context):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if user.id == OWNER_ID:
        return True
    if chat.type == "private":
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def target_is_admin(chat_id, target_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id, target_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def get_replied_user(update):
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    return None


# =========================================================
# START / WELCOME
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)
    await update.effective_message.reply_text(
        f"ð¼ ÐÑÐ¸Ð²ÐµÑ, {user.first_name}!\n\n"
        "Ð¯ ÐÑÐ½Ð´Ð¸, ÑÐ°Ð´ Ð²Ð¸Ð´ÐµÑÑ ÑÐµÐ±Ñ Ð² Gardenview Roleplay!\n\n"
        "ðï¸ Â«ÑÐµÑÐ¼Ð°Â» â Ð¿Ð¾Ð»ÑÑÐ¸ÑÑ ÐºÐ°ÑÑÐµÑÑ\n"
        "ð¤ Â«Ð¼Ð¾Ð¹ Ð¿ÑÐ¾ÑÐ¸Ð»ÑÂ» â Ð¾ÑÐºÑÑÑÑ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ\n"
        "ð Â«Ð¿ÑÐ°Ð²Ð¸Ð»Ð°Â» â Ð¿ÑÐ°Ð²Ð¸Ð»Ð° Ð³ÑÑÐ¿Ð¿Ñ\n"
        "â Â«Ð¿Ð¾Ð¼Ð¾ÑÑÂ» â ÑÐ¿Ð¸ÑÐ¾Ðº Ð²Ð¾Ð·Ð¼Ð¾Ð¶Ð½Ð¾ÑÑÐµÐ¹\n\n"
        "Ð£Ð´Ð°ÑÐ½Ð¾Ð³Ð¾ Ð¿ÑÐµÐ±ÑÐ²Ð°Ð½Ð¸Ñ Ð² Gardenview!"
    )


async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.effective_message.new_chat_members:
        if member.is_bot:
            continue
        register_user(member)
        await update.effective_message.reply_text(
            f"ð¼ ÐÐ¾Ð²ÑÐ¹ Ð¿Ð¾ÑÐµÑÐ¸ÑÐµÐ»Ñ Gardenview!\n\n"
            f"ÐÑÐ¸Ð²ÐµÑ, {member.first_name}!\n"
            "Ð¯ ÐÑÐ½Ð´Ð¸, Ð´Ð¾Ð±ÑÐ¾ Ð¿Ð¾Ð¶Ð°Ð»Ð¾Ð²Ð°ÑÑ Ð² Ð¼Ð¾Ð¹ ÑÐ°Ð´!\n\n"
            "ÐÑÐ¼Ð°ÑÑÐ¸Ð²Ð°Ð¹ÑÑ, Ð·Ð½Ð°ÐºÐ¾Ð¼ÑÑÑ Ñ ÑÑÐ½Ð°Ð¼Ð¸ "
            "Ð¸ Ð½Ðµ Ð·Ð°Ð±ÑÐ´Ñ Ð¾Ð·Ð½Ð°ÐºÐ¾Ð¼Ð¸ÑÑÑÑ Ñ Ð¿ÑÐ°Ð²Ð¸Ð»Ð°Ð¼Ð¸.\n\n"
            "ÐÐ°Ð¿Ð¸ÑÐ¸ Â«Ð¿ÑÐ°Ð²Ð¸Ð»Ð°Â», ÑÑÐ¾Ð±Ñ Ð¿ÑÐ¾ÑÐ¸ÑÐ°ÑÑ Ð¸Ñ."
        )


# =========================================================
# RULES
# =========================================================

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = db.execute(
        "SELECT value FROM settings WHERE name = 'rules'"
    ).fetchone()
    if not result:
        await update.effective_message.reply_text(
            "ð ÐÑÐ°Ð²Ð¸Ð»Ð° Gardenview Ð¿Ð¾ÐºÐ° Ð½Ðµ ÑÑÑÐ°Ð½Ð¾Ð²Ð»ÐµÐ½Ñ."
        )
        return
    await update.effective_message.reply_text(
        f"ð¼ GARDENVIEW ROLEPLAY\nð ÐÑÐ°Ð²Ð¸Ð»Ð°\n\n{result['value']}"
    )


async def save_rules(update, text):
    db.execute("""
        INSERT INTO settings (name, value) VALUES ('rules', ?)
        ON CONFLICT(name) DO UPDATE SET value = excluded.value
    """, (text,))
    db.commit()
    await update.effective_message.reply_text(
        "ð¼ ÐÑÐ½Ð´Ð¸ ÑÑÐ¿ÐµÑÐ½Ð¾ Ð¾Ð±Ð½Ð¾Ð²Ð¸Ð» Ð¿ÑÐ°Ð²Ð¸Ð»Ð° Gardenview!"
    )


async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "â ÐÐ·Ð¼ÐµÐ½ÑÑÑ Ð¿ÑÐ°Ð²Ð¸Ð»Ð° Ð¼Ð¾Ð¶Ð½Ð¾ ÑÐ¾Ð»ÑÐºÐ¾ Ð² Ð»Ð¸ÑÐ½ÑÑ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸ÑÑ Ñ Ð±Ð¾ÑÐ¾Ð¼."
        )
        return
    text = update.effective_message.text.partition(" ")[2].strip()
    if not text:
        await update.effective_message.reply_text(
            "ÐÑÐ¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°Ð½Ð¸Ðµ: /setrules Ð¢ÐµÐºÑÑ Ð¿ÑÐ°Ð²Ð¸Ð»"
        )
        return
    await save_rules(update, text)


# =========================================================
# TAPES
# =========================================================

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
            "ðï¸ Ð¢Ñ ÑÐ¶Ðµ Ð·Ð°Ð±Ð¸ÑÐ°Ð» ÐºÐ°ÑÑÐµÑÑ!\n\n"
            f"â³ Ð¡Ð»ÐµÐ´ÑÑÑÐ°Ñ Ð½Ð°Ð³ÑÐ°Ð´Ð° ÑÐµÑÐµÐ·: {hours} Ñ. "
            f"{minutes} Ð¼Ð¸Ð½. {seconds} ÑÐµÐº."
        )
        return

    reward = random.randint(30, 45)
    db.execute("""
        UPDATE users SET tapes = tapes + ?, last_tapes = ?
        WHERE user_id = ?
    """, (reward, now, user.id))
    db.commit()
    await update.effective_message.reply_text(
        "ð¼ ÐÑÐ½Ð´Ð¸ Ð¿ÑÐ¸Ð³Ð¾ÑÐ¾Ð²Ð¸Ð» Ð´Ð»Ñ ÑÐµÐ±Ñ Ð½Ð°Ð³ÑÐ°Ð´Ñ!\n\n"
        f"ðï¸ ÐÐ¾Ð»ÑÑÐµÐ½Ð¾ ÐºÐ°ÑÑÐµÑ: +{reward}\n\n"
        "ÐÐ¾Ð·Ð²ÑÐ°ÑÐ°Ð¹ÑÑ ÑÐµÑÐµÐ· 2 ÑÐ°ÑÐ°!"
    )


# =========================================================
# PROFILE
# =========================================================

def profile_text(row):
    username = mention(row)
    lines = [
        "ð¼ GARDENVIEW | ÐÐ¸ÑÐ½ÑÐ¹ ÐºÐ°Ð±Ð¸Ð½ÐµÑ",
        "",
        f"ð¤ {username}"
    ]
    if row["nickname"]:
        lines.append(f"ð·ï¸ ÐÑÐ¾Ð·Ð²Ð¸ÑÐµ: {row['nickname']}")
    if row["pronouns"]:
        lines.append(f"ð£ï¸ ÐÐµÑÑÐ¾Ð¸Ð¼ÐµÐ½Ð¸Ñ: {row['pronouns']}")
    lines.extend([
        f"ðï¸ ÐÐ°ÑÑÐµÑÑ: {row['tapes']}",
        f"ðº Ð Ð¾Ð»Ñ: {row['role']}",
        f"ð Ð ÐµÐ¿ÑÑÐ°ÑÐ¸Ñ: {row['reputation']}/100"
    ])

    relation = db.execute(
        "SELECT partner_id, started_at FROM relationships WHERE user_id = ?",
        (row["user_id"],)
    ).fetchone()
    if relation:
        partner = get_user_by_id(relation["partner_id"])
        if partner:
            lines.append(
                f"ð Ð Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸ÑÑ Ñ {mention(partner)} ÑÐ¶Ðµ "
                f"{format_relationship_time(relation['started_at'])}"
            )

    if row["bio"]:
        lines.extend(["", f"ð {row['bio']}"])
    return "\n".join(lines)


def profile_keyboard(owner_id, viewer_id):
    if owner_id != viewer_id:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "âï¸ Ð ÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°ÑÑ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ",
            callback_data=f"editprofile:{owner_id}"
        )
    ]])


async def show_profile(update, target_user=None):
    viewer = update.effective_user
    target_user = target_user or viewer
    row = get_user(target_user)
    photos = json.loads(row["photos"] or "[]")

    text = profile_text(row)
    keyboard = profile_keyboard(target_user.id, viewer.id)

    if photos:
        await update.effective_message.reply_photo(
            photo=photos[0], caption=text,
            reply_markup=keyboard
        )
        for photo_id in photos[1:]:
            await update.effective_message.reply_photo(photo=photo_id)
    else:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode="HTML"
        )


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_profile(update)


async def edit_profile_start(update, context):
    user = update.effective_user
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("ð·ï¸ ÐÑÐ¾Ð·Ð²Ð¸ÑÐµ", callback_data="field:nickname")],
        [InlineKeyboardButton("ð£ï¸ ÐÐµÑÑÐ¾Ð¸Ð¼ÐµÐ½Ð¸Ñ", callback_data="field:pronouns")],
        [InlineKeyboardButton("ð ÐÐ¿Ð¸ÑÐ°Ð½Ð¸Ðµ", callback_data="field:bio")],
        [InlineKeyboardButton("ð¼ï¸ ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ ÑÐ¾ÑÐ¾", callback_data="field:photo")],
        [InlineKeyboardButton("ðï¸ Ð£Ð´Ð°Ð»Ð¸ÑÑ Ð²ÑÐµ ÑÐ¾ÑÐ¾", callback_data="field:clearphotos")],
        [InlineKeyboardButton("ÐÐ¾ÑÐ¾Ð²Ð¾", callback_data="field:done")]
    ])
    await update.effective_message.reply_text(
        "âï¸ Ð ÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°Ð½Ð¸Ðµ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ\n\nÐÑÐ±ÐµÑÐ¸, ÑÑÐ¾ ÑÐ¾ÑÐµÑÑ Ð¸Ð·Ð¼ÐµÐ½Ð¸ÑÑ:",
        reply_markup=keyboard
    )


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data

    if data.startswith("editprofile:"):
        owner_id = int(data.split(":")[1])
        if user.id != owner_id:
            await query.answer("Ð­ÑÐ¾ Ð½Ðµ ÑÐ²Ð¾Ð¹ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ!", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("ð·ï¸ ÐÑÐ¾Ð·Ð²Ð¸ÑÐµ", callback_data="field:nickname")],
            [InlineKeyboardButton("ð£ï¸ ÐÐµÑÑÐ¾Ð¸Ð¼ÐµÐ½Ð¸Ñ", callback_data="field:pronouns")],
            [InlineKeyboardButton("ð ÐÐ¿Ð¸ÑÐ°Ð½Ð¸Ðµ", callback_data="field:bio")],
            [InlineKeyboardButton("ð¼ï¸ ÐÐ¾Ð±Ð°Ð²Ð¸ÑÑ ÑÐ¾ÑÐ¾", callback_data="field:photo")],
            [InlineKeyboardButton("ðï¸ Ð£Ð´Ð°Ð»Ð¸ÑÑ Ð²ÑÐµ ÑÐ¾ÑÐ¾", callback_data="field:clearphotos")],
            [InlineKeyboardButton("ÐÐ¾ÑÐ¾Ð²Ð¾", callback_data="field:done")]
        ])
        await query.message.reply_text(
            "âï¸ Ð§ÑÐ¾ ÑÐµÐ´Ð°ÐºÑÐ¸ÑÑÐµÐ¼?", reply_markup=keyboard
        )
        return

    if not data.startswith("field:"):
        return
    field = data.split(":", 1)[1]
    if field == "done":
        context.user_data.pop("profile_edit", None)
        await query.message.reply_text("â Ð ÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°Ð½Ð¸Ðµ Ð·Ð°Ð²ÐµÑÑÐµÐ½Ð¾.")
        return

    if field == "clearphotos":
        db.execute("UPDATE users SET photos = '[]' WHERE user_id = ?", (user.id,))
        db.commit()
        await query.message.reply_text("ðï¸ Ð¤Ð¾ÑÐ¾Ð³ÑÐ°ÑÐ¸Ð¸ ÑÐ´Ð°Ð»ÐµÐ½Ñ.")
        return

    context.user_data["profile_edit"] = field
    prompts = {
        "nickname": "ÐÑÐ¿ÑÐ°Ð²Ñ Ð½Ð¾Ð²Ð¾Ðµ Ð¿ÑÐ¾Ð·Ð²Ð¸ÑÐµ. Ð§ÑÐ¾Ð±Ñ ÑÐ±ÑÐ°ÑÑ ÐµÐ³Ð¾, Ð¾ÑÐ¿ÑÐ°Ð²Ñ Â«-Â».",
        "pronouns": "ÐÑÐ¿ÑÐ°Ð²Ñ Ð¼ÐµÑÑÐ¾Ð¸Ð¼ÐµÐ½Ð¸Ñ. Ð§ÑÐ¾Ð±Ñ ÑÐ±ÑÐ°ÑÑ Ð¸Ñ, Ð¾ÑÐ¿ÑÐ°Ð²Ñ Â«-Â».",
        "bio": "ÐÑÐ¿ÑÐ°Ð²Ñ Ð½Ð¾Ð²Ð¾Ðµ Ð¾Ð¿Ð¸ÑÐ°Ð½Ð¸Ðµ. Ð§ÑÐ¾Ð±Ñ ÑÐ±ÑÐ°ÑÑ ÐµÐ³Ð¾, Ð¾ÑÐ¿ÑÐ°Ð²Ñ Â«-Â».",
        "photo": "ÐÑÐ¿ÑÐ°Ð²Ñ ÑÐ¾ÑÐ¾Ð³ÑÐ°ÑÐ¸Ñ. ÐÐ¾Ð¶Ð½Ð¾ Ð´Ð¾Ð±Ð°Ð²Ð¸ÑÑ Ð´Ð¾ ÑÑÑÑ Ð¸Ð·Ð¾Ð±ÑÐ°Ð¶ÐµÐ½Ð¸Ð¹."
    }
    await query.message.reply_text(prompts[field])


async def handle_profile_edit(update, context):
    field = context.user_data.get("profile_edit")
    if not field:
        return False

    user = update.effective_user
    if field == "photo":
        if not update.effective_message.photo:
            await update.effective_message.reply_text(
                "ÐÑÐ¸ÑÐ»Ð¸ Ð¸Ð¼ÐµÐ½Ð½Ð¾ ÑÐ¾ÑÐ¾Ð³ÑÐ°ÑÐ¸Ñ."
            )
            return True
        row = get_user(user)
        photos = json.loads(row["photos"] or "[]")
        if len(photos) >= 3:
            await update.effective_message.reply_text(
                "Ð Ð¿ÑÐ¾ÑÐ¸Ð»Ðµ ÑÐ¶Ðµ Ð¼Ð°ÐºÑÐ¸Ð¼ÑÐ¼ 3 ÑÐ¾ÑÐ¾Ð³ÑÐ°ÑÐ¸Ð¸. Ð£Ð´Ð°Ð»Ð¸ ÑÑÐ°ÑÑÐµ, ÑÑÐ¾Ð±Ñ Ð´Ð¾Ð±Ð°Ð²Ð¸ÑÑ Ð½Ð¾Ð²ÑÐµ."
            )
        else:
            photos.append(update.effective_message.photo[-1].file_id)
            db.execute(
                "UPDATE users SET photos = ? WHERE user_id = ?",
                (json.dumps(photos), user.id)
            )
            db.commit()
            await update.effective_message.reply_text(
                f"ð¼ï¸ Ð¤Ð¾ÑÐ¾ Ð´Ð¾Ð±Ð°Ð²Ð»ÐµÐ½Ð¾! ({len(photos)}/3)"
            )
        return True

    if not update.effective_message.text:
        await update.effective_message.reply_text("ÐÑÐ¿ÑÐ°Ð²Ñ ÑÐµÐºÑÑÐ¾Ð²Ð¾Ðµ Ð·Ð½Ð°ÑÐµÐ½Ð¸Ðµ.")
        return True

    value = update.effective_message.text.strip()
    if value == "-":
        value = ""
    if len(value) > 500:
        await update.effective_message.reply_text(
            "Ð¡Ð»Ð¸ÑÐºÐ¾Ð¼ Ð´Ð»Ð¸Ð½Ð½Ð¾. ÐÐ°ÐºÑÐ¸Ð¼ÑÐ¼ 500 ÑÐ¸Ð¼Ð²Ð¾Ð»Ð¾Ð²."
        )
        return True

    db.execute(
        f"UPDATE users SET {field} = ? WHERE user_id = ?",
        (value, user.id)
    )
    db.commit()
    context.user_data.pop("profile_edit", None)
    await update.effective_message.reply_text("â ÐÑÐ¾ÑÐ¸Ð»Ñ Ð¾Ð±Ð½Ð¾Ð²Ð»ÑÐ½!")
    return True


# =========================================================
# ROLES
# =========================================================

async def change_role(update, target, role):
    register_user(target)
    db.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, target.id))
    db.commit()
    await update.effective_message.reply_text(
        f"ðº Ð Ð¾Ð»Ñ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ {mention(get_user(target))} Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ð°!\n"
        f"ÐÐ¾Ð²Ð°Ñ ÑÐ¾Ð»Ñ: {role}",
        parse_mode="HTML"
    )


async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        role = msg.text.partition(" ")[2].strip()
        if role:
            await change_role(update, msg.reply_to_message.from_user, role)
        else:
            await msg.reply_text("ÐÐ°Ð¿Ð¸ÑÐ¸: Ð Ð¾Ð»Ñ ÐÐÐÐÐÐÐÐ")
        return

    if len(context.args) < 2:
        await msg.reply_text("ÐÑÐ¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°Ð½Ð¸Ðµ: /setrole @username ÑÐ¾Ð»Ñ")
        return
    username = context.args[0].lstrip("@").lower()
    role = " ".join(context.args[1:])
    target = db.execute(
        "SELECT user_id FROM users WHERE LOWER(username) = ?", (username,)
    ).fetchone()
    if not target:
        await msg.reply_text("â ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½. ÐÐ½ Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð½Ð°Ð¿Ð¸ÑÐ°ÑÑ Ð±Ð¾ÑÑ Ð¸Ð»Ð¸ Ð³ÑÑÐ¿Ð¿Ðµ.")
        return
    db.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, target["user_id"]))
    db.commit()
    await msg.reply_text(f"ðº Ð Ð¾Ð»Ñ @{username} Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ð°!\nÐÐ¾Ð²Ð°Ñ ÑÐ¾Ð»Ñ: {role}")


# =========================================================
# REPUTATION
# =========================================================

async def give_rep(update, context):
    msg = update.effective_message
    target = get_replied_user(update)
    giver = update.effective_user

    if not target:
        await msg.reply_text("ÐÑÐ²ÐµÑÑ Ð½Ð° ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ° ÐºÐ¾Ð¼Ð°Ð½Ð´Ð¾Ð¹ Â«+ÑÐµÐ¿Â».")
        return
    if target.id == giver.id:
        await msg.reply_text("ÐÐµÐ»ÑÐ·Ñ Ð¿Ð¾Ð²ÑÑÐ°ÑÑ ÑÐ¾Ð±ÑÑÐ²ÐµÐ½Ð½ÑÑ ÑÐµÐ¿ÑÑÐ°ÑÐ¸Ñ.")
        return

    register_user(target)
    now = int(time.time())
    row = db.execute(
        "SELECT last_used FROM rep_cooldowns WHERE user_id = ?", (giver.id,)
    ).fetchone()
    if row and now - row["last_used"] < REP_COOLDOWN:
        left = REP_COOLDOWN - (now - row["last_used"])
        await msg.reply_text(
            f"â³ Ð ÐµÐ¿ÑÑÐ°ÑÐ¸Ñ Ð¼Ð¾Ð¶Ð½Ð¾ Ð²ÑÐ´Ð°Ð²Ð°ÑÑ ÑÐ°Ð· Ð² 5 Ð¼Ð¸Ð½ÑÑ. ÐÐ¾Ð´Ð¾Ð¶Ð´Ð¸ ÐµÑÑ {left // 60} Ð¼Ð¸Ð½."
        )
        return

    db.execute("""
        UPDATE users SET reputation = MIN(100, reputation + 1)
        WHERE user_id = ?
    """, (target.id,))
    db.execute("""
        INSERT INTO rep_cooldowns (user_id, last_used) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_used = excluded.last_used
    """, (giver.id, now))
    db.commit()
    row = get_user(target)
    await msg.reply_text(
        f"ð {mention(row)} Ð¿Ð¾Ð»ÑÑÐ¸Ð» +1 Ðº ÑÐµÐ¿ÑÑÐ°ÑÐ¸Ð¸! "
        f"Ð¢ÐµÐ¿ÐµÑÑ {row['reputation']}/100.",
        parse_mode="HTML"
    )


# =========================================================
# RELATIONSHIPS
# =========================================================

async def relationship_request(update, context):
    msg = update.effective_message
    proposer = update.effective_user
    target = get_replied_user(update)

    if not target:
        await msg.reply_text("ÐÑÐ²ÐµÑÑ Ð½Ð° ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ° ÑÐ»Ð¾Ð²Ð¾Ð¼ Â«Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸ÑÂ».")
        return
    if target.id == proposer.id:
        await msg.reply_text("ÐÐµÐ»ÑÐ·Ñ Ð¿ÑÐµÐ´Ð»Ð¾Ð¶Ð¸ÑÑ Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸Ñ ÑÐ°Ð¼Ð¾Ð¼Ñ ÑÐµÐ±Ðµ.")
        return

    register_user(target)
    register_user(proposer)

    busy = db.execute(
        "SELECT user_id FROM relationships WHERE user_id IN (?, ?)",
        (proposer.id, target.id)
    ).fetchone()
    if busy:
        await msg.reply_text("â ÐÐ´Ð¸Ð½ Ð¸Ð· ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ¾Ð² ÑÐ¶Ðµ ÑÐ¾ÑÑÐ¾Ð¸Ñ Ð² Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸ÑÑ.")
        return

    pending = db.execute(
        "SELECT target_id FROM relationship_requests WHERE target_id = ?",
        (target.id,)
    ).fetchone()
    if pending:
        await msg.reply_text("Ð£ ÑÑÐ¾Ð³Ð¾ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ° ÑÐ¶Ðµ ÐµÑÑÑ Ð¾Ð¶Ð¸Ð´Ð°ÑÑÐµÐµ Ð¿ÑÐµÐ´Ð»Ð¾Ð¶ÐµÐ½Ð¸Ðµ.")
        return

    db.execute("""
        INSERT OR REPLACE INTO relationship_requests
        (target_id, proposer_id, created_at) VALUES (?, ?, ?)
    """, (target.id, proposer.id, int(time.time())))
    db.commit()

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("ð ÐÐ°", callback_data=f"rel:yes:{proposer.id}"),
        InlineKeyboardButton("ÐÐµÑ", callback_data=f"rel:no:{proposer.id}")
    ]])
    await context.bot.send_message(
        chat_id=target.id,
        text=f"ð {mention(get_user(proposer))} Ð¿ÑÐµÐ´Ð»Ð°Ð³Ð°ÐµÑ ÑÐµÐ±Ðµ Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸Ñ Ð² Gardenview.\n\nÐÑÐ¸Ð½Ð¸Ð¼Ð°ÐµÑÑ?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await msg.reply_text("ð ÐÑÐµÐ´Ð»Ð¾Ð¶ÐµÐ½Ð¸Ðµ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½Ð¾ Ð² Ð»Ð¸ÑÐ½ÑÐµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ñ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ°.")


async def relationship_callback(update, context):
    query = update.callback_query
    await query.answer()
    target_id = query.from_user.id
    action, proposer_id = query.data.split(":")[1:]
    proposer_id = int(proposer_id)

    request = db.execute(
        "SELECT proposer_id FROM relationship_requests WHERE target_id = ?",
        (target_id,)
    ).fetchone()
    if not request or request["proposer_id"] != proposer_id:
        await query.edit_message_text("Ð­ÑÐ¾ Ð¿ÑÐµÐ´Ð»Ð¾Ð¶ÐµÐ½Ð¸Ðµ ÑÐ¶Ðµ Ð½ÐµÐ´ÐµÐ¹ÑÑÐ²Ð¸ÑÐµÐ»ÑÐ½Ð¾.")
        return

    db.execute("DELETE FROM relationship_requests WHERE target_id = ?", (target_id,))
    if action == "no":
        db.commit()
        await query.edit_message_text("â ÐÑÐµÐ´Ð»Ð¾Ð¶ÐµÐ½Ð¸Ðµ Ð¾ÑÐºÐ»Ð¾Ð½ÐµÐ½Ð¾.")
        try:
            await context.bot.send_message(proposer_id, "Ð¢Ð²Ð¾Ñ Ð¿ÑÐµÐ´Ð»Ð¾Ð¶ÐµÐ½Ð¸Ðµ Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸Ð¹ Ð¾ÑÐºÐ»Ð¾Ð½Ð¸Ð»Ð¸.")
        except Exception:
            pass
        return

    now = int(time.time())
    busy = db.execute(
        "SELECT user_id FROM relationships WHERE user_id IN (?, ?)",
        (target_id, proposer_id)
    ).fetchone()
    if busy:
        db.commit()
        await query.edit_message_text("ÐÐ´Ð¸Ð½ Ð¸Ð· ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ¾Ð² ÑÐ¶Ðµ ÑÐ¾ÑÑÐ¾Ð¸Ñ Ð² Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸ÑÑ.")
        return

    db.execute(
        "INSERT INTO relationships (user_id, partner_id, started_at) VALUES (?, ?, ?)",
        (target_id, proposer_id, now)
    )
    db.execute(
        "INSERT INTO relationships (user_id, partner_id, started_at) VALUES (?, ?, ?)",
        (proposer_id, target_id, now)
    )
    db.commit()
    await query.edit_message_text("ð ÐÑ ÑÐµÐ¿ÐµÑÑ Ð² Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸ÑÑ! ÐÐ¾Ð·Ð´ÑÐ°Ð²Ð»ÑÐµÐ¼!")
    try:
        await context.bot.send_message(proposer_id, "ð Ð¢Ð²Ð¾Ñ Ð¿ÑÐµÐ´Ð»Ð¾Ð¶ÐµÐ½Ð¸Ðµ Ð¿ÑÐ¸Ð½ÑÐ»Ð¸!")
    except Exception:
        pass


async def breakup(update, context):
    user_id = update.effective_user.id
    row = db.execute(
        "SELECT partner_id FROM relationships WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        await update.effective_message.reply_text("Ð¢Ñ ÑÐµÐ¹ÑÐ°Ñ Ð½Ðµ ÑÐ¾ÑÑÐ¾Ð¸ÑÑ Ð² Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸ÑÑ.")
        return
    partner_id = row["partner_id"]
    db.execute("DELETE FROM relationships WHERE user_id IN (?, ?)", (user_id, partner_id))
    db.commit()
    await update.effective_message.reply_text("ð ÐÑÐ½Ð¾ÑÐµÐ½Ð¸Ñ Ð·Ð°Ð²ÐµÑÑÐµÐ½Ñ.")
    try:
        await context.bot.send_message(partner_id, "ð ÐÐ°ÑÐ¸ Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸Ñ Ð±ÑÐ»Ð¸ Ð·Ð°Ð²ÐµÑÑÐµÐ½Ñ.")
    except Exception:
        pass


# =========================================================
# MODERATION
# =========================================================

async def moderation(update, context):
    msg = update.effective_message
    actor = update.effective_user
    if msg.chat.type == "private":
        return False

    match = re.match(
        r"^(Ð±Ð°Ð½|ÐºÐ¸Ðº|Ð¼ÑÑ|Ð²Ð°ÑÐ½)(?:\s+(\d+[Ð¼ÑÐ´]))?(?:\s+(.+))?$",
        (msg.text or "").strip(),
        re.IGNORECASE
    )
    if not match:
        return False

    action = match.group(1).lower()
    duration_text = match.group(2)
    reason = (match.group(3) or "").strip()
    target = get_replied_user(update)

    if not await is_admin(update, context):
        await msg.reply_text("â Ð­ÑÐ° ÐºÐ¾Ð¼Ð°Ð½Ð´Ð° Ð´Ð¾ÑÑÑÐ¿Ð½Ð° ÑÐ¾Ð»ÑÐºÐ¾ Ð°Ð´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾ÑÐ°Ð¼.")
        return True
    if not target:
        await msg.reply_text("ÐÑÐ²ÐµÑÑ Ð½Ð° ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ° ÐºÐ¾Ð¼Ð°Ð½Ð´Ð¾Ð¹ Ð½Ð°ÐºÐ°Ð·Ð°Ð½Ð¸Ñ.")
        return True
    if target.id == actor.id:
        await msg.reply_text("ÐÐµÐ»ÑÐ·Ñ Ð¿ÑÐ¸Ð¼ÐµÐ½Ð¸ÑÑ Ð½Ð°ÐºÐ°Ð·Ð°Ð½Ð¸Ðµ Ðº ÑÐ°Ð¼Ð¾Ð¼Ñ ÑÐµÐ±Ðµ.")
        return True
    if target.id == OWNER_ID or await target_is_admin(msg.chat_id, target.id, context):
        await msg.reply_text("â ÐÐµÐ»ÑÐ·Ñ Ð½Ð°ÐºÐ°Ð·Ð°ÑÑ Ð°Ð´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¾ÑÐ°.")
        return True

    duration = parse_duration(duration_text) if duration_text else None
    if action in ("Ð±Ð°Ð½", "Ð¼ÑÑ") and duration is None:
        await msg.reply_text(
            f"Ð£ÐºÐ°Ð¶Ð¸ ÑÑÐ¾Ðº: {action} 3Ð¼/Ñ/Ð´ Ð¿ÑÐ¸ÑÐ¸Ð½Ð°\n"
            "ÐÐ°Ð¿ÑÐ¸Ð¼ÐµÑ: Ð¼ÑÑ 2Ñ ÑÐ»ÑÐ´"
        )
        return True
    if duration_text and duration is None:
        await msg.reply_text("â ÐÐµÐ²ÐµÑÐ½ÑÐ¹ ÑÑÐ¾Ðº. ÐÑÐ¿Ð¾Ð»ÑÐ·ÑÐ¹ 3Ð¼, 2Ñ Ð¸Ð»Ð¸ 5Ð´.")
        return True

    try:
        if action == "Ð¼ÑÑ":
            until = datetime.fromtimestamp(time.time() + duration, tz=timezone.utc)
            await context.bot.restrict_chat_member(
                msg.chat_id, target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until
            )
            result = "ð ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð¿Ð¾Ð»ÑÑÐ¸Ð» Ð¼ÑÑ"
        elif action == "Ð±Ð°Ð½":
            until = datetime.fromtimestamp(time.time() + duration, tz=timezone.utc)
            await context.bot.ban_chat_member(
                msg.chat_id, target.id, until_date=until
            )
            result = "â ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð·Ð°Ð±Ð°Ð½ÐµÐ½"
        elif action == "ÐºÐ¸Ðº":
            await context.bot.ban_chat_member(msg.chat_id, target.id)
            await context.bot.unban_chat_member(msg.chat_id, target.id)
            result = "ð¢ ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð¸ÑÐºÐ»ÑÑÑÐ½ Ð¸Ð· ÑÐ°ÑÐ°"
        else:
            now = int(time.time())
            expires = now + duration if duration else None
            db.execute("""
                INSERT INTO warnings (user_id, created_at, expires_at, reason, admin_id)
                VALUES (?, ?, ?, ?, ?)
            """, (target.id, now, expires, reason, actor.id))

            active = db.execute("""
                SELECT id FROM warnings
                WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at ASC
            """, (target.id, now)).fetchall()

            penalty = False
            if len(active) >= 3:
                ids = [row["id"] for row in active[:3]]
                db.executemany("DELETE FROM warnings WHERE id = ?", [(i,) for i in ids])
                db.execute("""
                    UPDATE users SET reputation = MAX(0, reputation - 25)
                    WHERE user_id = ?
                """, (target.id,))
                penalty = True
            db.commit()

            result = "â ï¸ ÐÐ¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ Ð¿Ð¾Ð»ÑÑÐ¸Ð» Ð¿ÑÐµÐ´ÑÐ¿ÑÐµÐ¶Ð´ÐµÐ½Ð¸Ðµ"
            if penalty:
                result += "\nð ÐÐ° ÑÑÐ¸ Ð²Ð°ÑÐ½Ð° ÑÐ½ÑÑÐ¾ 25 ÑÐµÐ¿ÑÑÐ°ÑÐ¸Ð¸."

        await msg.reply_text(
            f"{result}\n"
            f"ð¤ {mention(get_user(target))}\n"
            f"ð ÐÑÐ¸ÑÐ¸Ð½Ð°: {reason or 'Ð½Ðµ ÑÐºÐ°Ð·Ð°Ð½Ð°'}",
            parse_mode="HTML"
        )
        try:
            await context.bot.send_message(
                target.id,
                f"{result}\nð ÐÑÐ¸ÑÐ¸Ð½Ð°: {reason or 'Ð½Ðµ ÑÐºÐ°Ð·Ð°Ð½Ð°'}"
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Moderation failed")
        await msg.reply_text(
            "â ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¿ÑÐ¸Ð¼ÐµÐ½Ð¸ÑÑ Ð½Ð°ÐºÐ°Ð·Ð°Ð½Ð¸Ðµ. ÐÑÐ¾Ð²ÐµÑÑ Ð¿ÑÐ°Ð²Ð° Ð±Ð¾ÑÐ° "
            "Ð½Ð° Ð¾Ð³ÑÐ°Ð½Ð¸ÑÐµÐ½Ð¸Ðµ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ¾Ð² Ð¸ Ð±Ð»Ð¾ÐºÐ¸ÑÐ¾Ð²ÐºÑ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»ÐµÐ¹."
        )
    return True


# =========================================================
# HELP / TOP
# =========================================================

async def help_command(update, context):
    await update.effective_message.reply_text(
        "ð¼ GARDENVIEW | ÐÐ¾Ð·Ð¼Ð¾Ð¶Ð½Ð¾ÑÑÐ¸ Ð±Ð¾ÑÐ°\n\n"
        "ðï¸ ÐÐ°ÑÑÐµÑÑ: Â«ÑÐµÑÐ¼Ð°Â», Â«ÑÐ°ÑÐ¼Â», /tapes\n"
        "ð¤ ÐÑÐ¾ÑÐ¸Ð»Ñ: Â«Ð¿ÑÐ¾ÑÐ¸Ð»ÑÂ», Â«Ð¼Ð¾Ð¹ Ð¿ÑÐ¾ÑÐ¸Ð»ÑÂ», /me\n"
        "ð ÐÑÐ°Ð²Ð¸Ð»Ð°: Â«Ð¿ÑÐ°Ð²Ð¸Ð»Ð°Â», /rules\n"
        "ð Ð ÐµÐ¿ÑÑÐ°ÑÐ¸Ñ: Ð¾ÑÐ²ÐµÑÑ Ð½Ð° ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ Ñ Â«+ÑÐµÐ¿Â»\n"
        "ð ÐÑÐ½Ð¾ÑÐµÐ½Ð¸Ñ: Ð¾ÑÐ²ÐµÑÑ Ð½Ð° ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ ÑÐ»Ð¾Ð²Ð¾Ð¼ Â«Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸ÑÂ»\n"
        "âï¸ Ð ÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°Ð½Ð¸Ðµ: Â«ÑÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°ÑÑ Ð¿ÑÐ¾ÑÐ¸Ð»ÑÂ»\n"
        "ð ÐÐ°Ð²ÐµÑÑÐ¸ÑÑ Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸Ñ: Â«ÑÐ°ÑÑÑÐ°ÑÑÑÑÂ»\n\n"
        "ÐÐ´Ð¼Ð¸Ð½Ð¸ÑÑÑÐ°ÑÐ¸Ñ Ð¼Ð¾Ð¶ÐµÑ Ð²ÑÐ´Ð°Ð²Ð°ÑÑ ÑÐ¾Ð»Ð¸ Ð¸ Ð¿ÑÐ¸Ð¼ÐµÐ½ÑÑÑ Ð½Ð°ÐºÐ°Ð·Ð°Ð½Ð¸Ñ."
    )


async def top_command(update, context):
    rows = db.execute("""
        SELECT * FROM users ORDER BY reputation DESC, tapes DESC LIMIT 10
    """).fetchall()
    if not rows:
        await update.effective_message.reply_text("ÐÐ¾ÐºÐ° Ð½ÐµÑ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ¾Ð² Ð² ÑÐµÐ¹ÑÐ¸Ð½Ð³Ðµ.")
        return
    lines = ["ð GARDENVIEW | Ð¢ÐÐ ÑÐµÐ¿ÑÑÐ°ÑÐ¸Ð¸", ""]
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. {mention(row)} Â· ð {row['reputation']}/100")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML"
    )


# =========================================================
# TEXT ROUTER
# =========================================================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    register_user(user)

    if await handle_profile_edit(update, context):
        return

    text = (msg.text or "").strip()
    normalized = text.lower()

    # ÐÐ¾Ð¼Ð°Ð½Ð´Ñ-ÑÐ¸Ð½Ð¾Ð½Ð¸Ð¼Ñ Ð±ÐµÐ· /
    if normalized in ("ÑÐµÑÐ¼Ð°", "ÑÐ°ÑÐ¼", "ÐºÐ°ÑÑÐµÑÑ", "Ð¿Ð¾Ð»ÑÑÐ¸ÑÑ ÐºÐ°ÑÑÐµÑÑ"):
        await tapes(update, context)
        return

    if normalized in ("Ð¿ÑÐ¾ÑÐ¸Ð»Ñ", "Ð¼Ð¾Ð¹ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ", "Ð»Ð¸ÑÐ½ÑÐ¹ ÐºÐ°Ð±Ð¸Ð½ÐµÑ"):
        target = get_replied_user(update)
        await show_profile(update, target)
        return

    if normalized in ("Ð¿ÑÐ°Ð²Ð¸Ð»Ð°", "Ð¿ÑÐ°Ð²Ð¸Ð»Ð° Ð³ÑÑÐ¿Ð¿Ñ"):
        await rules(update, context)
        return

    if normalized in ("Ð¿Ð¾Ð¼Ð¾ÑÑ", "ÐºÐ¾Ð¼Ð°Ð½Ð´Ñ", "help"):
        await help_command(update, context)
        return

    if normalized in ("ÑÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°ÑÑ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ", "Ð¸Ð·Ð¼ÐµÐ½Ð¸ÑÑ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ"):
        await edit_profile_start(update, context)
        return

    if normalized == "ÑÐ°ÑÑÑÐ°ÑÑÑÑ":
        await breakup(update, context)
        return

    if normalized == "Ð¾ÑÐ½Ð¾ÑÐµÐ½Ð¸Ñ":
        await relationship_request(update, context)
        return

    if normalized == "+ÑÐµÐ¿":
        await give_rep(update, context)
        return

    if normalized.startswith("ÑÐ¾Ð»Ñ "):
        if user.id != OWNER_ID:
            return
        target = get_replied_user(update)
        role = text.partition(" ")[2].strip()
        if not target:
            await msg.reply_text("ÐÑÐ²ÐµÑÑ Ð½Ð° ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ° ÐºÐ¾Ð¼Ð°Ð½Ð´Ð¾Ð¹ Â«Ð Ð¾Ð»Ñ ÐÐÐÐÐÐÐÐÂ».")
        elif not role:
            await msg.reply_text("Ð£ÐºÐ°Ð¶Ð¸ Ð½Ð°Ð·Ð²Ð°Ð½Ð¸Ðµ ÑÐ¾Ð»Ð¸.")
        else:
            await change_role(update, target, role)
        return

    if normalized.startswith("Ð¸Ð·Ð¼ÐµÐ½Ð¸ÑÑ Ð¿ÑÐ°Ð²Ð¸Ð»Ð° "):
        if user.id == OWNER_ID and update.effective_chat.type == "private":
            await save_rules(update, text.partition(" ")[2].partition(" ")[2].strip())
        return

    if await moderation(update, context):
        return

    # ÐÐ±ÑÑÐ½Ð¾Ðµ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ: Ð·Ð°Ð¿Ð¾Ð¼Ð¸Ð½Ð°ÐµÐ¼ ÑÑÐ°ÑÑÐ½Ð¸ÐºÐ° Ð´Ð»Ñ Ð¿Ð¾Ð¸ÑÐºÐ° Ð¿Ð¾ username
    register_user(user)


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Handler error:", exc_info=context.error)


# =========================================================
# MAIN
# =========================================================

def main():
    if not TOKEN:
        raise RuntimeError(
            "Environment variable TELEGRAM_BOT_TOKEN is missing."
        )

    init_db()
    app = Application.builder().token(TOKEN).build()

    # ÐÐ¾Ð¼Ð°Ð½Ð´Ñ ÑÐ¾ ÑÐ»ÐµÑÐµÐ¼ ÑÐ¾ÑÑÐ°Ð½ÑÑÑÑÑ Ð´Ð»Ñ ÑÐ¾Ð²Ð¼ÐµÑÑÐ¸Ð¼Ð¾ÑÑÐ¸
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("tapes", tapes))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("setrules", setrules))
    app.add_handler(CommandHandler("setrole", setrole))

    app.add_handler(CallbackQueryHandler(profile_callback, pattern=r"^(editprofile:|field:)"))
    app.add_handler(CallbackQueryHandler(relationship_callback, pattern=r"^rel:"))

    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome)
    )

    # Ð¢ÐµÐºÑÑÐ¾Ð²ÑÐµ Ð°Ð»Ð¸Ð°ÑÑ Ð¸ ÑÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°Ð½Ð¸Ðµ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_router),
        group=1
    )
    app.add_handler(
        MessageHandler(filters.PHOTO, text_router),
        group=1
    )

    app.add_error_handler(error_handler)

    logger.info("Gardenview Roleplay Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
