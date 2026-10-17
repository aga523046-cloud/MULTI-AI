import os
import random
import sqlite3
import asyncio
import json
import logging
import re
from contextlib import closing

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "7783222972"))
DB_PATH = os.getenv("DB_PATH", "wonderworld.sqlite3")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("wonderworld")

HOLLOW_BASE_HP = 100
HOLLOW_BASE_DAMAGE = 15
MAX_FLOOR = 25
LIFT_SECONDS = 10
LIFT_SHOP_SECONDS = 30

# ============ Markdown V2 escape ============
MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def md(s):
    if s is None:
        return ""
    s = str(s)
    return re.sub(r"([" + re.escape(MD_SPECIAL) + r"])", r"\\\1", s)

# ============ Шаги ConversationHandler ============
NAME, DESCRIPTION, HP, DAMAGE, STAMINA, LUCK, SUPER_DAMAGE, SUPER_CHARGES, TRAITS, PRICE, PHOTO = range(11)
H_NAME, H_HP, H_DMG, H_IDLE, H_CHASE, H_DEBUFF_TYPE, H_DEBUFF_VALUE, H_DEBUFF_GROWING, H_DEBUFF_ROOMS, H_PHOTO = range(10)
EDIT_FIELD, EDIT_VALUE = range(2)
EDIT_H_FIELD, EDIT_H_VALUE = range(2)

# ============ БД ============
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with closing(db()) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS miracles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                hp INTEGER NOT NULL,
                damage INTEGER NOT NULL,
                stamina INTEGER NOT NULL,
                luck INTEGER NOT NULL,
                super_damage INTEGER NOT NULL DEFAULT 0,
                super_charges INTEGER NOT NULL DEFAULT 1,
                traits TEXT NOT NULL DEFAULT '',
                price INTEGER NOT NULL DEFAULT 0,
                photo_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        mcols = [r[1] for r in con.execute("PRAGMA table_info(miracles)").fetchall()]
        for col, ddl in [
            ("super_damage", "ALTER TABLE miracles ADD COLUMN super_damage INTEGER NOT NULL DEFAULT 0"),
            ("super_charges", "ALTER TABLE miracles ADD COLUMN super_charges INTEGER NOT NULL DEFAULT 1"),
            ("traits", "ALTER TABLE miracles ADD COLUMN traits TEXT NOT NULL DEFAULT ''"),
            ("price", "ALTER TABLE miracles ADD COLUMN price INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in mcols:
                con.execute(ddl)

        con.execute("""
            CREATE TABLE IF NOT EXISTS hollows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                hp INTEGER NOT NULL,
                damage INTEGER NOT NULL,
                idle_damage INTEGER NOT NULL DEFAULT 1,
                chase_doors INTEGER NOT NULL DEFAULT 0,
                debuff_type TEXT NOT NULL DEFAULT 'none',
                debuff_value INTEGER NOT NULL DEFAULT 0,
                debuff_growing INTEGER NOT NULL DEFAULT 0,
                debuff_rooms INTEGER NOT NULL DEFAULT 0,
                photo_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        hcols = [r[1] for r in con.execute("PRAGMA table_info(hollows)").fetchall()]
        for col, ddl in [
            ("chase_doors", "ALTER TABLE hollows ADD COLUMN chase_doors INTEGER NOT NULL DEFAULT 0"),
            ("debuff_type", "ALTER TABLE hollows ADD COLUMN debuff_type TEXT NOT NULL DEFAULT 'none'"),
            ("debuff_value", "ALTER TABLE hollows ADD COLUMN debuff_value INTEGER NOT NULL DEFAULT 0"),
            ("debuff_growing", "ALTER TABLE hollows ADD COLUMN debuff_growing INTEGER NOT NULL DEFAULT 0"),
            ("debuff_rooms", "ALTER TABLE hollows ADD COLUMN debuff_rooms INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in hcols:
                con.execute(ddl)

        con.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                miracle_id INTEGER,
                hp INTEGER NOT NULL DEFAULT 0,
                floor INTEGER NOT NULL DEFAULT 0,
                door INTEGER NOT NULL DEFAULT 0,
                next_safe INTEGER NOT NULL DEFAULT 0,
                items TEXT NOT NULL DEFAULT '',
                battle_hp INTEGER NOT NULL DEFAULT 0,
                battle_hollow_hp INTEGER NOT NULL DEFAULT 0,
                battle_damage INTEGER NOT NULL DEFAULT 0,
                battle_active INTEGER NOT NULL DEFAULT 0,
                discs INTEGER NOT NULL DEFAULT 0,
                super_ready INTEGER NOT NULL DEFAULT 0,
                super_counter INTEGER NOT NULL DEFAULT 0,
                battle_hollow_id INTEGER NOT NULL DEFAULT 0,
                battle_idle_damage INTEGER NOT NULL DEFAULT 0,
                battle_msg_id INTEGER NOT NULL DEFAULT 0,
                battle_chat_id INTEGER NOT NULL DEFAULT 0,
                chase_remaining INTEGER NOT NULL DEFAULT 0,
                chase_hollow_id INTEGER NOT NULL DEFAULT 0,
                debuff_json TEXT NOT NULL DEFAULT ''
            )
        """)
        pcols = [r[1] for r in con.execute("PRAGMA table_info(players)").fetchall()]
        for col, ddl in [
            ("discs", "ALTER TABLE players ADD COLUMN discs INTEGER NOT NULL DEFAULT 0"),
            ("super_ready", "ALTER TABLE players ADD COLUMN super_ready INTEGER NOT NULL DEFAULT 0"),
            ("super_counter", "ALTER TABLE players ADD COLUMN super_counter INTEGER NOT NULL DEFAULT 0"),
            ("battle_hollow_id", "ALTER TABLE players ADD COLUMN battle_hollow_id INTEGER NOT NULL DEFAULT 0"),
            ("battle_idle_damage", "ALTER TABLE players ADD COLUMN battle_idle_damage INTEGER NOT NULL DEFAULT 0"),
            ("battle_msg_id", "ALTER TABLE players ADD COLUMN battle_msg_id INTEGER NOT NULL DEFAULT 0"),
            ("battle_chat_id", "ALTER TABLE players ADD COLUMN battle_chat_id INTEGER NOT NULL DEFAULT 0"),
            ("chase_remaining", "ALTER TABLE players ADD COLUMN chase_remaining INTEGER NOT NULL DEFAULT 0"),
            ("chase_hollow_id", "ALTER TABLE players ADD COLUMN chase_hollow_id INTEGER NOT NULL DEFAULT 0"),
            ("debuff_json", "ALTER TABLE players ADD COLUMN debuff_json TEXT NOT NULL DEFAULT ''"),
        ]:
            if col not in pcols:
                con.execute(ddl)

        con.execute("""
            CREATE TABLE IF NOT EXISTS session (
                user_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 0,
                msg_id INTEGER NOT NULL DEFAULT 0,
                chat_id INTEGER NOT NULL DEFAULT 0,
                start_discs INTEGER NOT NULL DEFAULT 0,
                doors_opened INTEGER NOT NULL DEFAULT 0,
                hollows_killed INTEGER NOT NULL DEFAULT 0,
                dead_ends INTEGER NOT NULL DEFAULT 0,
                items_found INTEGER NOT NULL DEFAULT 0,
                items_used INTEGER NOT NULL DEFAULT 0,
                damage_dealt INTEGER NOT NULL DEFAULT 0,
                damage_taken INTEGER NOT NULL DEFAULT 0,
                exit_reason TEXT NOT NULL DEFAULT '',
                shop_open INTEGER NOT NULL DEFAULT 0,
                shop_bought TEXT NOT NULL DEFAULT ''
            )
        """)
        scols = [r[1] for r in con.execute("PRAGMA table_info(session)").fetchall()]
        for col, ddl in [
            ("shop_open", "ALTER TABLE session ADD COLUMN shop_open INTEGER NOT NULL DEFAULT 0"),
            ("shop_bought", "ALTER TABLE session ADD COLUMN shop_bought TEXT NOT NULL DEFAULT ''"),
        ]:
            if col not in scols:
                con.execute(ddl)

        con.execute("""
            CREATE TABLE IF NOT EXISTS player_miracles (
                user_id INTEGER NOT NULL,
                miracle_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, miracle_id)
            )
        """)
        con.commit()

# ============ Мираклы ============
M_FIELDS = ["id","name","description","hp","damage","stamina","luck",
            "super_damage","super_charges","traits","price","photo_id"]

def get_miracles():
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,description,hp,damage,stamina,luck,"
            "super_damage,super_charges,traits,price,photo_id FROM miracles ORDER BY id"
        ).fetchall()

def get_miracle(mid):
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,description,hp,damage,stamina,luck,"
            "super_damage,super_charges,traits,price,photo_id FROM miracles WHERE id=?",
            (mid,),
        ).fetchone()

def save_miracle(name, description, hp, damage, stamina, luck,
                 super_damage, super_charges, traits, price, photo_id):
    with closing(db()) as con:
        con.execute(
            """INSERT INTO miracles(name,description,hp,damage,stamina,luck,
                                    super_damage,super_charges,traits,price,photo_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (name, description, hp, damage, stamina, luck,
             super_damage, super_charges, traits, price, photo_id),
        )
        con.commit()

def update_miracle_field(mid, field, value):
    allowed = {"name","description","hp","damage","stamina","luck",
               "super_damage","super_charges","traits","price","photo_id"}
    if field not in allowed:
        return
    with closing(db()) as con:
        con.execute(f"UPDATE miracles SET {field}=? WHERE id=?", (value, mid))
        con.commit()

TRAITS_INFO = {
    "crit":        ("🎯 Крит", "12% шанс двойного урона"),
    "lifesteal":   ("🩸 Вампиризм", "20% от нанесённого урона → тебе"),
    "thorns":      ("🌵 Шипы", "отражаешь 25% урона холлоу"),
    "lucky_find":  ("🍀 Удачливый", "+10% к шансу найти предмет"),
    "dodge":       ("💨 Уклонение", "10% шанс уклониться от атаки холлоу"),
    "first_strike":("⚡ Первый удар", "первый удар в бою ×1.5"),
    "super_shield":("🛡️ Суперщит", "суперудар даёт щит на 1 ход"),
    "regen":       ("💚 Регенерация", "+5 HP за каждую открытую дверь"),
}

def traits_list(m):
    if not m[9]:
        return []
    return [t.strip() for t in m[9].split("|") if t.strip()]

def miracle_card(m):
    price = "Бесплатно" if m[10] == 0 else f"{m[10]}💿"
    tr = traits_list(m)
    tr_text = ", ".join(TRAITS_INFO.get(t, (t,))[0] for t in tr) if tr else "—"
    return (
        f"✨ *{md(m[1])}*\n{md(m[2])}\n\n"
        f"♥️ Здоровье: {sector(m[3],750)}/5\n"
        f"⚔️ Урон: {sector(m[4],75)}/5\n"
        f"⚡️ Выносливость: {sector(m[5],300)}/5\n"
        f"🍀 Везучесть: {sector(m[6],300)}/5\n"
        f"💥 Супер: {m[7]} урона, за {m[8]} атак\n"
        f"🎭 Трейты: {md(tr_text)}\n"
        f"💿 Цена: {md(price)}"
    )

def is_unlocked(uid, mid):
    m = get_miracle(mid)
    if not m:
        return False
    if m[10] == 0:
        return True
    with closing(db()) as con:
        return bool(con.execute(
            "SELECT 1 FROM player_miracles WHERE user_id=? AND miracle_id=?",
            (uid, mid)).fetchone())

def unlock_miracle(uid, mid):
    with closing(db()) as con:
        con.execute("INSERT OR IGNORE INTO player_miracles(user_id,miracles_id) VALUES(?,?)"
                    if False else "INSERT OR IGNORE INTO player_miracles(user_id,miracle_id) VALUES(?,?)",
                    (uid, mid))
        con.commit()

# ============ Холлоу ============
def get_hollows():
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,hp,damage,idle_damage,chase_doors,"
            "debuff_type,debuff_value,debuff_growing,debuff_rooms,photo_id "
            "FROM hollows ORDER BY id"
        ).fetchall()

def get_hollow(hid):
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,hp,damage,idle_damage,chase_doors,"
            "debuff_type,debuff_value,debuff_growing,debuff_rooms,photo_id "
            "FROM hollows WHERE id=?", (hid,),
        ).fetchone()

def save_hollow(name, hp, damage, idle, chase, d_type, d_val, d_grow, d_rooms, photo_id):
    with closing(db()) as con:
        con.execute(
            """INSERT INTO hollows(name,hp,damage,idle_damage,chase_doors,
                                   debuff_type,debuff_value,debuff_growing,debuff_rooms,photo_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (name, hp, damage, idle, chase, d_type, d_val, d_grow, d_rooms, photo_id),
        )
        con.commit()

def update_hollow_field(hid, field, value):
    allowed = {"name","hp","damage","idle_damage","chase_doors",
               "debuff_type","debuff_value","debuff_growing","debuff_rooms","photo_id"}
    if field not in allowed:
        return
    with closing(db()) as con:
        con.execute(f"UPDATE hollows SET {field}=? WHERE id=?", (value, hid))
        con.commit()

def hollow_card(h):
    deb = "нет"
    if h[6] != "none" and h[7] > 0:
        names = {"poison":"☠️ Отравление","spawn_up":"👁️ Призыв"}
        deb = f"{names.get(h[6],h[6])} +{h[7]}% на {h[9]} комн."
        if h[8]: deb += " (нарастающий)"
    return (
        f"👁️ *{md(h[1])}*\n"
        f"❤️ HP: {h[2]} · ⚔️ {h[3]} · ⏳ {h[4]}/сек\n"
        f"🏃 Преследование: {h[5]} дверей\n"
        f"🧪 Дебафф: {md(deb)}"
    )

# ============ Игрок ============
def get_player(uid):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        if row:
            return row
        con.execute("INSERT INTO players(user_id) VALUES(?)", (uid,))
        con.commit()
        return con.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()

def set_player(uid, **fields):
    if not fields:
        return
    allowed = {"miracle_id","hp","floor","door","next_safe","items",
               "battle_hp","battle_hollow_hp","battle_damage","battle_active",
               "discs","super_ready","super_counter","battle_hollow_id",
               "battle_idle_damage","battle_msg_id","battle_chat_id",
               "chase_remaining","chase_hollow_id","debuff_json"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    with closing(db()) as con:
        con.execute(f"UPDATE players SET {assignments} WHERE user_id=?",
                    (*fields.values(), uid))
        con.commit()

# ============ Сессия ============
def get_session(uid):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM session WHERE user_id=?", (uid,)).fetchone()
        if row:
            return row
        con.execute("INSERT INTO session(user_id) VALUES(?)", (uid,))
        con.commit()
        return con.execute("SELECT * FROM session WHERE user_id=?", (uid,)).fetchone()

def set_session(uid, **fields):
    allowed = {"active","msg_id","chat_id","start_discs","doors_opened",
               "hollows_killed","dead_ends","items_found","items_used",
               "damage_dealt","damage_taken","exit_reason","shop_open","shop_bought"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    with closing(db()) as con:
        con.execute(f"UPDATE session SET {assignments} WHERE user_id=?",
                    (*fields.values(), uid))
        con.commit()

def session_active(uid):
    return bool(get_session(uid)[1])

def reset_session_stats(uid, msg_id=0, chat_id=0, start_discs=0):
    set_session(uid, active=1, msg_id=msg_id, chat_id=chat_id, start_discs=start_discs,
                doors_opened=0, hollows_killed=0, dead_ends=0, items_found=0,
                items_used=0, damage_dealt=0, damage_taken=0, exit_reason="",
                shop_open=0, shop_bought="")

# ============ Утилиты ============
def sector(v, mx):
    return max(1, min(5, (max(1, v) - 1) * 5 // mx + 1))

def floor_name(f):
    if f == 0: return "ЗАДВЕРЬЕ"
    if f == 26: return "S-000"
    return f"Этаж {f}"

def hollow_spawn_chance(floor, debuff_bonus=0):
    if floor <= 0: return 0.0
    return min(80.0, 5.0 + 1.0 * (floor - 1) + debuff_bonus)

def is_shop_lift(floor):
    """Пре-ран шоп: после задверья и на каждом 5-м этаже."""
    return floor == 0 or (floor > 0 and floor % 5 == 0 and floor <= MAX_FLOOR)

def item_dict(raw):
    r = {}
    if raw:
        for part in raw.split("|"):
            if ":" in part:
                k, v = part.split(":", 1)
                try: r[k] = int(v)
                except ValueError: pass
    return r

def items_string(items):
    return "|".join(f"{k}:{v}" for k, v in items.items() if v > 0)

ITEM_LABELS = {
    "plaster":"🩹 Пластырь",
    "magnifier":"🔍 Лупа",
    "helper":"📟 Хелпер",
    "staff_key":"🗝️ Ключ персонала",
    "admin_key":"🔑 Ключ администратора",
}

def item_text(items):
    if not items: return "Нет предметов"
    lines = [f"{ITEM_LABELS.get(k,k)} ×{v}" for k,v in items.items() if v > 0]
    return "\n".join(lines) or "Нет предметов"

def current_miracle(uid):
    p = get_player(uid)
    if not p[1]: return None
    return get_miracle(p[1])

def get_active_debuff(uid):
    p = get_player(uid)
    if not p[19]:
        return None
    try:
        return json.loads(p[19])
    except Exception:
        return None

def set_active_debuff(uid, data):
    set_player(uid, debuff_json=json.dumps(data) if data else "")

# ============ Клавиатуры ============
def main_menu_keyboard(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Играть", callback_data="play")],
        [InlineKeyboardButton("✨ Мираклы", callback_data="list_miracles"),
         InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("📖 Правила", callback_data="rules")],
    ])

def battle_keyboard(super_ready, counter, need):
    if super_ready:
        lbl = "💥 СУПЕРУДАР!"
    else:
        lbl = f"💥 Супер {counter}/{need}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚔️ Атаковать", callback_data="fight:attack"),
         InlineKeyboardButton(lbl, callback_data="fight:super")],
        [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
    ])

def game_text(uid):
    p = get_player(uid)
    m = current_miracle(uid)
    if not m: return "Миракл не выбран."
    floor, door = p[3], p[4]
    max_doors = 5 if floor == 0 else 10
    if p[10]:
        return (
            f"👁️ *Бой!*\n"
            f"❤️ HP холлоу: {p[8]}\n"
            f"♥️ Твоё здоровье: {p[7]}/{m[3]}\n"
            f"💥 Супер: {'готов' if p[12] else f'{p[13]}/{m[8]}'}"
        )
    deb = get_active_debuff(uid)
    deb_line = ""
    if deb:
        deb_line = f"\n☠️ Дебафф: {md(deb.get('type',''))} ×{deb.get('rooms',0)}"
    return (
        f"🌙 *{md(floor_name(floor))}* · дверь {door}/{max_doors}\n"
        f"♥️ {p[2]}/{m[3]} · 💿 {p[11]}\n"
        f"💥 Супер: {'готов' if p[12] else f'{p[13]}/{m[8]}'}{deb_line}\n\n"
        f"Куда пойдёшь?"
    )

def game_keyboard(uid):
    p = get_player(uid)
    if p[10]:
        m = current_miracle(uid)
        return battle_keyboard(p[12], p[13], m[8] if m else 1)
    floor, door = p[3], p[4]
    max_doors = 5 if floor == 0 else 10
    if floor == 26:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Выйти в лобби", callback_data="emergency_exit")],
        ])
    if door >= max_doors:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🛗 Лифт", callback_data="lift")],
            [InlineKeyboardButton("🎒 Инвентарь", callback_data="session_inventory")],
            [InlineKeyboardButton("🚪 Выйти в лобби", callback_data="emergency_exit")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚪", callback_data="branch:1"),
         InlineKeyboardButton("🚪", callback_data="branch:2"),
         InlineKeyboardButton("🚪", callback_data="branch:3")],
        [InlineKeyboardButton("🎒 Инвентарь", callback_data="session_inventory")],
        [InlineKeyboardButton("🚪 Выйти в лобби", callback_data="emergency_exit")],
    ])

# ============ Рендер игрового сообщения ============
async def render_game(app, uid, text=None, keyboard=None):
    s = get_session(uid)
    if text is None: text = game_text(uid)
    if keyboard is None: keyboard = game_keyboard(uid)
    if s[2] and s[3]:
        try:
            await app.bot.edit_message_text(
                chat_id=s[3], message_id=s[2],
                text=text, parse_mode="MarkdownV2", reply_markup=keyboard)
            return
        except Exception:
            pass
    msg = await app.bot.send_message(
        chat_id=uid, text=text, parse_mode="MarkdownV2", reply_markup=keyboard)
    set_session(uid, msg_id=msg.message_id, chat_id=msg.chat_id)

# ============ Меню ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_player(uid); get_session(uid)
    if session_active(uid):
        await render_game(context.application, uid)
        return
    await update.message.reply_text(
        "🌙 *WONDERWORLD*\nДобро пожаловать\\.",
        parse_mode="MarkdownV2",
        reply_markup=main_menu_keyboard(uid))

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if session_active(uid):
        await q.message.reply_text("⚠️ Ты в забеге\\.",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
            ]))
        return
    await q.message.reply_text("🌙 *Главное меню*",
        parse_mode="MarkdownV2", reply_markup=main_menu_keyboard(uid))

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    p = get_player(uid)
    with closing(db()) as con:
        cnt = con.execute("SELECT COUNT(*) FROM player_miracles WHERE user_id=?",
                          (uid,)).fetchone()[0]
    free = sum(1 for m in get_miracles() if m[10] == 0)
    await q.message.reply_text(
        f"👤 *Профиль*\n💿 Дисков: *{p[11]}*\n"
        f"✨ Мираклов: {cnt + free}/{len(get_miracles())}",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад", callback_data="menu")],
        ]))

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    text = (
        "📖 *Правила*\n"
        "• Дверь: \\+1💿\\. Тупик \\(5%\\): 5–10💿 или предмет\n"
        "• Холлоу: 5% \\+1% за этаж\\. Победа: \\+25💿\n"
        "• Суперудар копится за N обычных атак\n"
        "• Лифт: 10 сек, каждые 5 этажей и после ЗАДВЕРЬЯ — 30 сек с магазином\n"
        "• Пре\\-ран шоп: 🩹 50💿, 🔍 75💿, 🗝️ 150💿\n"
        "• Экстренный выход — 2 подтверждения, покажет статистику\n"
        "• Валюта сохраняется, HP/предметы/заряды — только на забег"
    )
    await q.message.reply_text(text, parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад", callback_data="menu")],
        ]))

# ============ Мираклы ============
def miracle_list_keyboard(uid):
    ms = get_miracles()
    if not ms: return None
    btns = []
    for m in ms:
        if is_unlocked(uid, m[0]):
            btns.append([InlineKeyboardButton(f"✅ {m[1]}", callback_data=f"view_miracle:{m[0]}")])
        else:
            btns.append([InlineKeyboardButton(f"🔒 {m[1]} — {m[10]}💿", callback_data=f"unlock_prompt:{m[0]}")])
    btns.append([InlineKeyboardButton("↩️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(btns)

async def list_miracles_cb(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    kb = miracle_list_keyboard(uid)
    if not kb:
        await q.message.reply_text("Мираклов нет.")
        return
    await q.message.reply_text("✨ *Мираклы*", parse_mode="MarkdownV2", reply_markup=kb)

async def view_miracle(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    mid = int(q.data.split(":")[1])
    m = get_miracle(mid)
    if not m:
        await q.message.reply_text("Не найден.")
        return
    kb = InlineKeyboardMarkup([
        ([InlineKeyboardButton("▶️ Играть", callback_data=f"play_pick:{mid}")]
         if is_unlocked(uid, mid) else
         [InlineKeyboardButton(f"🔓 Разблокировать ({m[10]}💿)", callback_data=f"unlock:{mid}")]),
        [InlineKeyboardButton("↩️ Назад", callback_data="list_miracles")],
    ])
    if m[11]:
        await q.message.reply_photo(photo=m[11], caption=miracle_card(m),
            parse_mode="MarkdownV2", reply_markup=kb)
    else:
        await q.message.reply_text(miracle_card(m), parse_mode="MarkdownV2", reply_markup=kb)

async def unlock_prompt(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    mid = int(q.data.split(":")[1])
    m = get_miracle(mid)
    if not m: return
    p = get_player(uid)
    text = miracle_card(m) + f"\n\n💿 У тебя: {p[11]}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔓 Разблокировать ({m[10]}💿)", callback_data=f"unlock:{mid}")],
        [InlineKeyboardButton("↩️ Назад", callback_data="list_miracles")],
    ])
    if m[11]:
        await q.message.reply_photo(photo=m[11], caption=text, parse_mode="MarkdownV2", reply_markup=kb)
    else:
        await q.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)

async def unlock_miracle_cb(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    mid = int(q.data.split(":")[1])
    m = get_miracle(mid)
    if not m: return
    if is_unlocked(uid, mid):
        await q.message.reply_text("Уже разблокирован.")
        return
    p = get_player(uid)
    if p[11] < m[10]:
        await q.message.reply_text(f"Нужно {m[10]}💿, у тебя {p[11]}.")
        return
    set_player(uid, discs=p[11] - m[10])
    unlock_miracle(uid, mid)
    await q.message.reply_text(f"✅ «{md(m[1])}» разблокирован!",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Играть", callback_data=f"play_pick:{mid}")],
            [InlineKeyboardButton("↩️ К мираклам", callback_data="list_miracles")],
        ]))

# ============ Играть ============
async def play_cb(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if session_active(uid):
        await q.message.reply_text("Ты в забеге.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚪 Продолжить", callback_data="resume")],
            ]))
        return
    ms = get_miracles()
    if not ms:
        await q.message.reply_text("Мираклов нет.")
        return
    btns = []
    for m in ms:
        if is_unlocked(uid, m[0]):
            btns.append([InlineKeyboardButton(f"▶️ {m[1]}", callback_data=f"play_pick:{m[0]}")])
    btns.append([InlineKeyboardButton("↩️ Назад", callback_data="menu")])
    await q.message.reply_text("Выбери миракла для забега:", reply_markup=InlineKeyboardMarkup(btns))

async def resume_game(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if not session_active(uid):
        await q.message.reply_text("Забега нет.")
        return
    await render_game(context.application, uid)

async def play_pick(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    mid = int(q.data.split(":")[1])
    m = get_miracle(mid)
    if not m or not is_unlocked(uid, mid):
        await q.message.reply_text("Недоступно.")
        return
    if session_active(uid):
        await q.message.reply_text("Ты уже в забеге.")
        return
    p = get_player(uid)
    set_player(uid, miracle_id=mid, hp=m[3], floor=0, door=0, next_safe=0,
               items="", battle_hp=0, battle_hollow_hp=0, battle_active=0,
               super_ready=0, super_counter=0, battle_hollow_id=0,
               battle_idle_damage=0, battle_msg_id=0, battle_chat_id=0,
               chase_remaining=0, chase_hollow_id=0, debuff_json="")
    reset_session_stats(uid, start_discs=p[11])
    await render_game(context.application, uid)

# ============ Выход ============
async def emergency_exit(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if not session_active(uid):
        await q.message.reply_text("Забега нет.")
        return
    await q.message.reply_text(
        "⚠️ *Экстренный выход*\nЗабег завершится\\. Продолжить?",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Да, выйти", callback_data="exit_confirm2")],
            [InlineKeyboardButton("↩️ Остаться", callback_data="resume")],
        ]))

async def exit_confirm2(update, context):
    q = update.callback_query; await q.answer()
    await q.message.reply_text(
        "⚠️ Последнее подтверждение?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Точно выйти", callback_data="exit_final")],
            [InlineKeyboardButton("↩️ Остаться", callback_data="resume")],
        ]))

async def exit_final(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if not session_active(uid):
        await q.message.reply_text("Забег уже завершён.")
        return
    await finish_session(context.application, uid, "manual_exit", chat_id=q.message.chat_id)

async def finish_session(app, uid, reason, chat_id=None):
    p = get_player(uid)
    m = current_miracle(uid)
    s = get_session(uid)
    earned = p[11] - s[4]
    text = (
        f"🏁 *Забег завершён*\n"
        f"Причина: {md(reason_label(reason))}\n\n"
        f"💿 Получено: *{earned:+d}*\n"
        f"🚪 Дверей: {s[5]}\n"
        f"👁️ Холлоу убито: {s[6]}\n"
        f"🔀 Тупиков: {s[7]}\n"
        f"🎁 Найдено: {s[8]}\n"
        f"🩹 Использовано: {s[9]}\n"
        f"⚔️ Урона нанесено: {s[10]}\n"
        f"💔 Получено: {s[11]}\n"
        f"💰 Баланс: {p[11]}💿"
    )
    base_hp = m[3] if m else 0
    set_player(uid, hp=base_hp, floor=0, door=0, next_safe=0, items="",
               battle_hp=0, battle_hollow_hp=0, battle_damage=0, battle_active=0,
               super_ready=0, super_counter=0, battle_hollow_id=0,
               battle_idle_damage=0, battle_msg_id=0, battle_chat_id=0,
               chase_remaining=0, chase_hollow_id=0, debuff_json="")
    set_session(uid, active=0, msg_id=0, chat_id=0, exit_reason=reason,
                shop_open=0, shop_bought="")
    try:
        await app.bot.send_message(chat_id=chat_id or uid, text=text,
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ В лобби", callback_data="menu")],
            ]))
    except Exception:
        log.exception("finish_session")

def reason_label(r):
    return {"manual_exit":"досрочный выход","death":"поражение","victory":"победа"}.get(r,r)

# ============ Пре-ран шоп ============
PRICES = {"plaster":50, "magnifier":75, "staff_key":150}

async def lift(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if not session_active(uid): return
    p = get_player(uid)
    if p[10]: return
    floor, door = p[3], p[4]
    max_doors = 5 if floor == 0 else 10
    if floor == 26 or door < max_doors:
        await render_game(context.application, uid)
        return
    if context.user_data.get("lift_waiting"): return
    context.user_data["lift_waiting"] = True
    new_floor = 1 if floor == 0 else (26 if floor >= MAX_FLOOR else floor + 1)
    shop_lift = is_shop_lift(floor)
    duration = LIFT_SHOP_SECONDS if shop_lift else LIFT_SECONDS

    # Сброс магазина
    set_session(uid, shop_open=1 if shop_lift else 0, shop_bought="")

    async def tick():
        nonlocal duration
        remaining = duration
        while remaining > 0:
            if context.user_data.get("lift_cancel"):
                context.user_data["lift_cancel"] = False
                break
            if shop_lift:
                p2 = get_player(uid)
                items = item_dict(p2[6])
                bought = get_session(uid)[12]
                bought_set = set(bought.split("|")) if bought else set()
                rows = []
                for it in ["plaster","magnifier","staff_key"]:
                    price = PRICES[it]
                    mark = "✅ " if it in bought_set else ""
                    rows.append([InlineKeyboardButton(
                        f"{mark}{ITEM_LABELS[it]} — {price}💿",
                        callback_data=f"shop_buy:{it}")])
                rows.append([InlineKeyboardButton("⏩ Закончить", callback_data="lift_skip")])
                rows.append([InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")])
                await render_game(context.application, uid,
                    text=f"🛗 *Лифт \\+ магазин* · {remaining} сек\\.\n"
                         f"💿 У тебя: {p2[11]}\n"
                         f"Цель: *{md(floor_name(new_floor))}*",
                    keyboard=InlineKeyboardMarkup(rows))
            else:
                await render_game(context.application, uid,
                    text=f"🛗 Лифт едет\\.\\.\\. осталось {remaining} сек\\.\n"
                         f"Цель: *{md(floor_name(new_floor))}*",
                    keyboard=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
                    ]))
            await asyncio.sleep(1)
            remaining -= 1
        context.user_data["lift_waiting"] = False
        set_player(uid, floor=new_floor, door=0)
        set_session(uid, shop_open=0, shop_bought="")
        await render_game(context.application, uid)

    context.application.create_task(tick())

async def lift_skip(update, context):
    q = update.callback_query; await q.answer()
    context.user_data["lift_cancel"] = True
    await q.message.reply_text("⏩ Лифт ускоряется…")

async def shop_buy(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if not session_active(uid): return
    s = get_session(uid)
    if not s[11]:  # shop_open
        await q.message.reply_text("Магазин закрыт.")
        return
    item = q.data.split(":")[1]
    if item not in PRICES:
        return
    price = PRICES[item]
    p = get_player(uid)
    if p[11] < price:
        await q.answer(f"Не хватает {price - p[11]}💿", show_alert=True)
        return
    items = item_dict(p[6])
    items[item] = items.get(item, 0) + 1
    bought = s[12]
    bought_set = set(bought.split("|")) if bought else set()
    if item in bought_set:
        await q.answer("Уже куплено в этом лифте", show_alert=True)
        return
    bought_set.add(item)
    set_player(uid, discs=p[11] - price, items=items_string(items))
    set_session(uid, shop_bought="|".join(sorted(bought_set)))
    await q.answer(f"Куплено: {ITEM_LABELS[item]}")

# ============ Забег: двери ============
async def branch(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if not session_active(uid): return
    p = get_player(uid)
    m = current_miracle(uid)
    if not m or p[10]: return
    floor, door = p[3], p[4]
    if floor == 26:
        await render_game(context.application, uid); return
    max_doors = 5 if floor == 0 else 10
    if door >= max_doors:
        await render_game(context.application, uid); return

    if context.user_data.pop("peek_next", False):
        deb = get_active_debuff(uid)
        bonus = deb["value"] if deb and deb["type"] == "spawn_up" else 0
        spawn = hollow_spawn_chance(floor, bonus) / 100
        peek = "🔍 Похоже, пусто." if random.random() > spawn * 3 else "🔍 Возможны признаки холлоу."
        await render_game(context.application, uid,
            text=game_text(uid) + "\n\n" + md(peek)); return

    items = item_dict(p[6])
    s = get_session(uid)
    set_session(uid, doors_opened=s[5] + 1)

    # Применение ключа?
    key_used = context.user_data.pop("use_key_next", None)

    if floor == 0:
        set_player(uid, door=door + 1, discs=p[11] + 1)
        await after_door(context.application, uid, floor, m)
        return

    # Тупик 5%
    if random.random() < 0.05 and not p[5] and not key_used:
        if random.random() < 0.5:
            delta, rtext = random.randint(5, 10), None
            set_player(uid, discs=p[11] + delta)
            rtext = f"+{delta}💿"
        else:
            tool = random.choice(["plaster","magnifier","helper"])
            items[tool] = items.get(tool, 0) + (3 if tool == "magnifier" else 1)
            set_player(uid, items=items_string(items))
            rtext = ITEM_LABELS[tool]
            set_session(uid, items_found=s[8] + 1)
        set_player(uid, door=door + 1)
        s2 = get_session(uid)
        set_session(uid, dead_ends=s2[7] + 1)
        await render_game(context.application, uid,
            text=f"🚪 *Тупик!* Награда: {md(rtext)}\n\n" + game_text(uid))
        return

    # Холлоу?
    if key_used:
        is_hollow = False
    elif p[5]:
        is_hollow = False
        set_player(uid, next_safe=0)
    elif p[16] > 0 and p[17]:  # chase
        is_hollow = True
    else:
        deb = get_active_debuff(uid)
        bonus = deb["value"] if deb and deb["type"] == "spawn_up" else 0
        is_hollow = random.random() < hollow_spawn_chance(floor, bonus) / 100

    if is_hollow:
        # Если chase — используем сохранённый id
        if p[16] > 0 and p[17]:
            hid = p[17]
            set_player(uid, chase_remaining=p[16] - 1,
                       chase_hollow_id=p[17] if p[16] - 1 > 0 else 0)
            await start_battle(context.application, uid, floor, p, m, forced_id=hid)
        else:
            await start_battle(context.application, uid, floor, p, m)
        return

    set_player(uid, door=door + 1, discs=p[11] + 1)
    await after_door(context.application, uid, floor, m)

async def after_door(app, uid, floor, m):
    """Применяет регенерацию, дебафф, обновляет сообщение."""
    p = get_player(uid)
    tr = traits_list(m)
    # регенерация
    if "regen" in tr and p[2] < m[3]:
        set_player(uid, hp=min(m[3], p[2] + 5))
        p = get_player(uid)
    # уменьшение дебаффа
    deb = get_active_debuff(uid)
    if deb and deb.get("rooms", 0) > 0:
        deb["rooms"] -= 1
        if deb["rooms"] <= 0:
            set_active_debuff(uid, None)
        else:
            set_active_debuff(uid, deb)
    await render_game(app, uid)

# ============ Бой ============
def pick_hollow(floor, forced_id=None):
    base_hp, base_dmg = HOLLOW_BASE_HP + (floor - 1) * 20, HOLLOW_BASE_DAMAGE + (floor - 1) * 3
    scale = max(1.0, base_hp / max(1, HOLLOW_BASE_HP))
    if forced_id:
        h = get_hollow(forced_id)
        if h:
            return (h[0], h[1], max(1, int(h[2] * scale)),
                    max(1, int(h[3] * scale)), max(1, int(h[4] * scale)),
                    h[10], h[5], h[6], h[7], h[8], h[9])
    hs = get_hollows()
    if not hs:
        return (0, "Холлоу", base_hp, base_dmg, max(1, base_dmg // 5),
                None, 0, "none", 0, 0, 0)
    h = random.choice(hs)
    return (h[0], h[1], max(1, int(h[2] * scale)),
            max(1, int(h[3] * scale)), max(1, int(h[4] * scale)),
            h[10], h[5], h[6], h[7], h[8], h[9])

async def start_battle(app, uid, floor, p, m, forced_id=None):
    hid, hname, hp, dmg, idle, photo, chase, deb_t, deb_v, deb_g, deb_r = pick_hollow(floor, forced_id)
    set_player(uid, door=p[4] + 1,
               battle_hp=p[2], battle_hollow_hp=hp, battle_damage=dmg,
               battle_active=1, battle_hollow_id=hid, battle_idle_damage=idle)
    # сохраняем chase и debuff на случай отступления/поражения
    context_battle = {
        "chase": chase, "deb_type": deb_t, "deb_value": deb_v,
        "deb_grow": deb_g, "deb_rooms": deb_r, "hollow_id": hid
    }
    app.user_data.setdefault("battles", {})[uid] = context_battle

    text = (
        f"👁️ *{md(hname)}* преграждает путь\\!\n"
        f"❤️ HP: {hp} · ⚔️ {dmg} · ⏳ {idle}/сек\n"
        f"♥️ Твоё здоровье: {p[2]}/{m[3]}\n"
        f"💥 Супер: {'готов' if p[12] else f'{p[13]}/{m[8]}'}"
    )
    kb = battle_keyboard(p[12], p[13], m[8])
    if photo:
        try:
            await app.bot.edit_message_media(
                chat_id=get_session(uid)[3], message_id=get_session(uid)[2],
                media=photo, reply_markup=kb,
            )
        except Exception:
            msg = await app.bot.send_photo(
                chat_id=uid, photo=photo, caption=text,
                parse_mode="MarkdownV2", reply_markup=kb)
            set_session(uid, msg_id=msg.message_id, chat_id=msg.chat_id)
    else:
        await render_game(app, uid, text=text, keyboard=kb)
    app.create_task(idle_ticker(app, uid, get_session(uid)[2], get_session(uid)[3]))

async def idle_ticker(app, uid, msg_id, chat_id):
    await asyncio.sleep(2)
    while True:
        p = get_player(uid)
        if not p[10]: return
        if get_session(uid)[2] != msg_id: return
        m = current_miracle(uid)
        if not m: return
        # дебафф отравления — дополнительный урон за бездействие? Нет, только обычный idle
        new_hp = p[7] - p[14]
        if new_hp <= 0:
            await battle_defeat(app, uid)
            return
        set_player(uid, battle_hp=new_hp)
        s = get_session(uid)
        set_session(uid, damage_taken=s[11] + p[14])
        text = (
            f"👁️ Холлоу бьёт, пока ты думаешь\\!\n"
            f"❤️ HP холлоу: {p[8]}\n"
            f"♥️ Твоё: {new_hp}/{m[3]}\n"
            f"💥 Супер: {'готов' if p[12] else f'{p[13]}/{m[8]}'}\n\n"
            f"_−{p[14]} HP/сек_"
        )
        try:
            await app.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                text=text, parse_mode="MarkdownV2",
                reply_markup=battle_keyboard(p[12], p[13], m[8]))
        except Exception:
            pass
        await asyncio.sleep(1)

async def fight(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    p = get_player(uid)
    m = current_miracle(uid)
    if not m or not p[10] or not session_active(uid): return
    action = q.data.split(":")[1]
    tr = traits_list(m)
    s = get_session(uid)

    player_hp, hollow_hp, hollow_dmg = p[7], p[8], p[9]
    battle = context.application.user_data.get("battles", {}).get(uid, {})

    if action == "super":
        if not p[12]:
            await q.answer("Супер не готов", show_alert=True); return
        dmg = m[7]
        # super_shield
        if "super_shield" in tr:
            player_hp += 0  # щит обрабатывается ниже
    else:
        dmg = m[4]
        # first_strike
        if "first_strike" in tr and hollow_hp == p[8]:
            dmg = int(dmg * 1.5)
        # crit
        if "crit" in tr and random.random() < 0.12:
            dmg *= 2

    hollow_hp -= dmg
    set_session(uid, damage_dealt=s[10] + dmg)

    if hollow_hp <= 0:
        set_player(uid, hp=player_hp, battle_active=0, battle_hp=0, battle_hollow_hp=0,
                   discs=p[11] + 25, battle_hollow_id=0)
        set_session(uid, hollows_killed=get_session(uid)[6] + 1)
        # Сброс chase, если победа над ним
        set_player(uid, chase_remaining=0, chase_hollow_id=0)
        # Применяем дебафф
        if battle.get("deb_type") and battle["deb_type"] != "none" and battle["deb_value"] > 0:
            apply_debuff(uid, battle)
        await render_game(context.application, uid,
            text=f"✨ Победа\\! \\+25💿\n♥️ {player_hp}/{m[3]}\n\n" + game_text(uid))
        return

    # Урон холлоу по игроку
    if "dodge" in tr and random.random() < 0.10:
        dealt = 0
    else:
        dealt = hollow_dmg
        if action == "super" and "super_shield" in tr:
            dealt = 0
    player_hp -= dealt
    set_session(uid, damage_taken=get_session(uid)[11] + dealt)

    # thorns
    if "thorns" in tr and dealt > 0:
        reflect = int(dealt * 0.25)
        hollow_hp -= reflect
        if hollow_hp <= 0:
            set_player(uid, hp=player_hp, battle_active=0, battle_hp=0,
                       battle_hollow_hp=0, discs=p[11] + 25, battle_hollow_id=0,
                       chase_remaining=0, chase_hollow_id=0)
            set_session(uid, hollows_killed=get_session(uid)[6] + 1)
            await render_game(context.application, uid,
                text=f"🌵 Шипы убили холлоу\\! \\+25💿\n\n" + game_text(uid))
            return

    # lifesteal
    if "lifesteal" in tr and dealt > 0:
        heal = int(dmg * 0.20)
        player_hp = min(m[3], player_hp + heal)

    if player_hp <= 0:
        await battle_defeat(context.application, uid)
        return

    # Обновление счётчика супера
    new_ready = p[12]
    new_counter = p[13]
    if action == "super":
        new_ready = 0
        new_counter = 0
    elif not p[12]:
        new_counter += 1
        if new_counter >= m[8]:
            new_ready = 1
            new_counter = 0

    set_player(uid, battle_hp=player_hp, battle_hollow_hp=hollow_hp,
               super_ready=new_ready, super_counter=new_counter)
    text = (
        f"{'💥 Супер' if action == 'super' else '⚔️ Удар'}: −{dmg}\n"
        f"👁️ HP холлоу: {hollow_hp}\n"
        f"♥️ Твоё: {player_hp}/{m[3]}\n"
        f"Холлоу: −{dealt}\n"
        f"💥 Супер: {'готов' if new_ready else f'{new_counter}/{m[8]}'}"
    )
    await render_game(context.application, uid, text=text,
        keyboard=battle_keyboard(new_ready, new_counter, m[8]))

def apply_debuff(uid, battle):
    dtype = battle.get("deb_type", "none")
    if dtype == "none" or battle.get("deb_value", 0) <= 0:
        return
    val = battle["deb_value"]
    rooms = battle.get("deb_rooms", 0)
    if rooms <= 0: return
    set_active_debuff(uid, {"type": dtype, "value": val, "rooms": rooms,
                            "grow": bool(battle.get("deb_grow"))})

async def battle_defeat(app, uid):
    """Поражение в бою — chase + debuff продолжают действовать, HP сброс."""
    p = get_player(uid)
    m = current_miracle(uid)
    battle = app.user_data.get("battles", {}).get(uid, {})
    chase = battle.get("chase", 0)
    # применяем дебафф (как при победе)
    apply_debuff(uid, battle)
    base_hp = m[3] if m else 0
    set_player(uid, hp=base_hp, floor=0, door=0, battle_active=0,
               battle_hp=0, battle_hollow_hp=0, super_ready=0, super_counter=0,
               battle_hollow_id=0, chase_remaining=chase,
               chase_hollow_id=battle.get("hollow_id", 0) if chase > 0 else 0)
    # Сбрасываем забег (возврат в задверье), но chase остаётся — игрок выйдет снова начнёт с ним
    s = get_session(uid)
    set_session(uid, doors_opened=s[5], damage_taken=s[11])
    await finish_session(app, uid, "death", chat_id=get_session(uid)[3])

# ============ Инвентарь ============
async def session_inventory(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if not session_active(uid): return
    p = get_player(uid)
    items = item_dict(p[6])
    rows = []
    if items.get("plaster",0) > 0:
        rows.append([InlineKeyboardButton("🩹 Пластырь (+50 HP)", callback_data="use:plaster")])
    if items.get("magnifier",0) > 0:
        rows.append([InlineKeyboardButton("🔍 Лупа", callback_data="use:magnifier")])
    if items.get("helper",0) > 0:
        rows.append([InlineKeyboardButton("📟 Хелпер", callback_data="use:helper")])
    if items.get("staff_key",0) > 0:
        rows.append([InlineKeyboardButton("🗝️ Ключ персонала (1 дверь)", callback_data="use:staff_key")])
    if items.get("admin_key",0) > 0:
        rows.append([InlineKeyboardButton(f"🔑 Ключ админа ({items['admin_key']})", callback_data="use:admin_key")])
    rows.append([InlineKeyboardButton("↩️ К забегу", callback_data="resume")])
    await q.message.reply_text(
        "🎒 *Инвентарь забега*\n" + md(item_text(items)),
        parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup(rows))

async def use_item(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if not session_active(uid): return
    p = get_player(uid)
    items = item_dict(p[6])
    kind = q.data.split(":")[1]
    m = current_miracle(uid)
    if not m or items.get(kind, 0) <= 0:
        await q.message.reply_text("Нет такого предмета.")
        return
    if kind == "plaster":
        if p[2] >= m[3]:
            await q.message.reply_text("HP полное.")
            return
        set_player(uid, hp=min(m[3], p[2] + 50))
        items[kind] -= 1
        set_player(uid, items=items_string(items))
    elif kind == "magnifier":
        if p[3] in (0, 26):
            await q.message.reply_text("Здесь не нужно.")
            return
        items[kind] -= 1
        set_player(uid, items=items_string(items))
        context.user_data["peek_next"] = True
    elif kind == "helper":
        items[kind] -= 1
        set_player(uid, items=items_string(items), next_safe=1)
    elif kind in ("staff_key", "admin_key"):
        items[kind] -= 1
        set_player(uid, items=items_string(items), next_safe=1)
    s = get_session(uid)
    set_session(uid, items_used=s[9] + 1)
    await render_game(context.application, uid)

# ============ Админка: мираклы ============
async def add_miracle_start(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return ConversationHandler.END
    context.user_data["new_miracle"] = {}
    await update.message.reply_text("Имя миракла:")
    return NAME

async def add_name(update, context):
    context.user_data["new_miracle"]["name"] = update.message.text.strip()[:60]
    await update.message.reply_text("Описание:")
    return DESCRIPTION

async def add_description(update, context):
    context.user_data["new_miracle"]["description"] = update.message.text.strip()[:1000]
    await update.message.reply_text("HP (1–750):")
    return HP

async def add_hp(update, context):
    v = parse_stat(update.message.text, 1, 750)
    if v is None: await update.message.reply_text("1–750:"); return HP
    context.user_data["new_miracle"]["hp"] = v
    await update.message.reply_text("Урон (1–75):"); return DAMAGE

async def add_damage(update, context):
    v = parse_stat(update.message.text, 1, 75)
    if v is None: await update.message.reply_text("1–75:"); return DAMAGE
    context.user_data["new_miracle"]["damage"] = v
    await update.message.reply_text("Выносливость (1–300):"); return STAMINA

async def add_stamina(update, context):
    v = parse_stat(update.message.text, 1, 300)
    if v is None: await update.message.reply_text("1–300:"); return STAMINA
    context.user_data["new_miracle"]["stamina"] = v
    await update.message.reply_text("Везучесть (1–300):"); return LUCK

async def add_luck(update, context):
    v = parse_stat(update.message.text, 1, 300)
    if v is None: await update.message.reply_text("1–300:"); return LUCK
    context.user_data["new_miracle"]["luck"] = v
    await update.message.reply_text("Урон суперудара (1–500):"); return SUPER_DAMAGE

async def add_super_damage(update, context):
    v = parse_stat(update.message.text, 1, 500)
    if v is None: await update.message.reply_text("1–500:"); return SUPER_DAMAGE
    context.user_data["new_miracle"]["super_damage"] = v
    await update.message.reply_text("Атак до суперудара (1–10):"); return SUPER_CHARGES

async def add_super_charges(update, context):
    v = parse_stat(update.message.text, 1, 10)
    if v is None: await update.message.reply_text("1–10:"); return SUPER_CHARGES
    context.user_data["new_miracle"]["super_charges"] = v
    tr_list = "\n".join(f"• `{k}` — {v[0]} ({v[1]})" for k,v in TRAITS_INFO.items())
    await update.message.reply_text(
        "Трейты через запятую, или /skip:\n" + tr_list,
        parse_mode="MarkdownV2")
    return TRAITS

async def add_traits(update, context):
    text = update.message.text.strip()
    codes = [c.strip() for c in text.split(",") if c.strip() in TRAITS_INFO]
    context.user_data["new_miracle"]["traits"] = "|".join(codes)
    await update.message.reply_text("Цена в 💿 (0 = бесплатно):"); return PRICE

async def add_skip_traits(update, context):
    context.user_data["new_miracle"]["traits"] = ""
    await update.message.reply_text("Цена в 💿 (0 = бесплатно):")
    return PRICE

async def add_price(update, context):
    v = parse_stat(update.message.text, 0, 10000)
    if v is None: await update.message.reply_text("0–10000:"); return PRICE
    context.user_data["new_miracle"]["price"] = v
    await update.message.reply_text("Фото или /skip:"); return PHOTO

async def add_photo(update, context):
    await save_new_miracle(update, context, update.message.photo[-1].file_id)
    return ConversationHandler.END

async def add_skip_photo(update, context):
    await save_new_miracle(update, context, None)
    return ConversationHandler.END

async def save_new_miracle(update, context, photo_id):
    d = context.user_data.pop("new_miracle", {})
    if not d: await update.message.reply_text("Отменено."); return
    save_miracle(d["name"], d["description"], d["hp"], d["damage"], d["stamina"],
                 d["luck"], d["super_damage"], d["super_charges"],
                 d["traits"], d["price"], photo_id)
    await update.message.reply_text(f"✅ Миракл «{d['name']}» сохранён!")

def parse_stat(text, low, high):
    try:
        v = int(text.strip())
        return v if low <= v <= high else None
    except (ValueError, AttributeError):
        return None

async def cancel_add(update, context):
    context.user_data.pop("new_miracle", None)
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ============ Админка: холлоу ============
async def add_hollow_start(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return ConversationHandler.END
    context.user_data["new_hollow"] = {}
    await update.message.reply_text("Имя холлоу:")
    return H_NAME

async def h_name(update, context):
    context.user_data["new_hollow"]["name"] = update.message.text.strip()[:60]
    await update.message.reply_text("HP (1–5000):"); return H_HP

async def h_hp(update, context):
    v = parse_stat(update.message.text, 1, 5000)
    if v is None: await update.message.reply_text("1–5000:"); return H_HP
    context.user_data["new_hollow"]["hp"] = v
    await update.message.reply_text("Урон (1–500):"); return H_DMG

async def h_dmg(update, context):
    v = parse_stat(update.message.text, 1, 500)
    if v is None: await update.message.reply_text("1–500:"); return H_DMG
    context.user_data["new_hollow"]["damage"] = v
    await update.message.reply_text("Пассивный урон/сек (1–100):"); return H_IDLE

async def h_idle(update, context):
    v = parse_stat(update.message.text, 1, 100)
    if v is None: await update.message.reply_text("1–100:"); return H_IDLE
    context.user_data["new_hollow"]["idle"] = v
    await update.message.reply_text("Преследование (0–10 дверей, 0 = нет):"); return H_CHASE

async def h_chase(update, context):
    v = parse_stat(update.message.text, 0, 10)
    if v is None: await update.message.reply_text("0–10:"); return H_CHASE
    context.user_data["new_hollow"]["chase"] = v
    await update.message.reply_text(
        "Дебафф: poison / spawn_up / none (или /skip):"); return H_DEBUFF_TYPE

async def h_debuff_type(update, context):
    t = update.message.text.strip().lower()
    if t not in ("poison","spawn_up","none"):
        await update.message.reply_text("poison / spawn_up / none:"); return H_DEBUFF_TYPE
    context.user_data["new_hollow"]["deb_type"] = t
    if t == "none":
        context.user_data["new_hollow"].update({"deb_value":0,"deb_grow":0,"deb_rooms":0})
        await update.message.reply_text("Фото или /skip:"); return H_PHOTO
    await update.message.reply_text("Значение дебаффа (1–100, %):"); return H_DEBUFF_VALUE

async def h_debuff_value(update, context):
    v = parse_stat(update.message.text, 1, 100)
    if v is None: await update.message.reply_text("1–100:"); return H_DEBUFF_VALUE
    context.user_data["new_hollow"]["deb_value"] = v
    await update.message.reply_text("Нарастающий? 1=да, 0=нет:"); return H_DEBUFF_GROWING

async def h_debuff_growing(update, context):
    v = parse_stat(update.message.text, 0, 1)
    if v is None: await update.message.reply_text("0 или 1:"); return H_DEBUFF_GROWING
    context.user_data["new_hollow"]["deb_grow"] = v
    await update.message.reply_text("На сколько комнат действует (1–50):"); return H_DEBUFF_ROOMS

async def h_debuff_rooms(update, context):
    v = parse_stat(update.message.text, 1, 50)
    if v is None: await update.message.reply_text("1–50:"); return H_DEBUFF_ROOMS
    context.user_data["new_hollow"]["deb_rooms"] = v
    await update.message.reply_text("Фото или /skip:"); return H_PHOTO

async def h_photo(update, context):
    await save_new_hollow(update, context, update.message.photo[-1].file_id)
    return ConversationHandler.END

async def h_skip(update, context):
    await save_new_hollow(update, context, None)
    return ConversationHandler.END

async def save_new_hollow(update, context, photo_id):
    d = context.user_data.pop("new_hollow", {})
    if not d: await update.message.reply_text("Отменено."); return
    save_hollow(d.get("name","Холлоу"), d.get("hp",100), d.get("damage",15),
                d.get("idle",3), d.get("chase",0), d.get("deb_type","none"),
                d.get("deb_value",0), d.get("deb_grow",0), d.get("deb_rooms",0), photo_id)
    await update.message.reply_text(f"✅ Холлоу «{d.get('name')}» сохранён!")

async def cancel_hollow(update, context):
    context.user_data.pop("new_hollow", None)
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ============ Админка: редактирование ============
async def editmiracle_start(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return ConversationHandler.END
    ms = get_miracles()
    if not ms: await update.message.reply_text("Мираклов нет."); return ConversationHandler.END
    btns = [[InlineKeyboardButton(m[1], callback_data=f"edit_m:{m[0]}")] for m in ms]
    await update.message.reply_text("Кого редактируем?", reply_markup=InlineKeyboardMarkup(btns))
    return EDIT_FIELD

async def edit_m_field(update, context):
    q = update.callback_query; await q.answer()
    mid = int(q.data.split(":")[1])
    context.user_data["edit_mid"] = mid
    m = get_miracle(mid)
    fields = ["name","description","hp","damage","stamina","luck",
              "super_damage","super_charges","traits","price","photo_id"]
    btns = [[InlineKeyboardButton(f, callback_data=f"edit_mf:{f}")] for f in fields]
    btns.append([InlineKeyboardButton("↩️ Отмена", callback_data="cancel_edit")])
    await q.message.reply_text(f"Поля «{m[1]}»:", reply_markup=InlineKeyboardMarkup(btns))
    return EDIT_VALUE

async def edit_m_value_start(update, context):
    q = update.callback_query; await q.answer()
    field = q.data.split(":")[1]
    context.user_data["edit_field"] = field
    await q.message.reply_text(f"Новое значение для {field}:")
    return EDIT_VALUE

async def edit_m_value(update, context):
    mid = context.user_data.get("edit_mid")
    field = context.user_data.get("edit_field")
    if not mid or not field:
        await update.message.reply_text("Ошибка.")
        return ConversationHandler.END
    val = update.message.text.strip()
    if field in ("hp","damage","stamina","luck","super_damage","super_charges","price"):
        try: val = int(val)
        except ValueError: await update.message.reply_text("Число:"); return EDIT_VALUE
    if field == "traits":
        val = "|".join(c.strip() for c in val.split(",") if c.strip() in TRAITS_INFO)
    update_miracle_field(mid, field, val)
    await update.message.reply_text(f"✅ Поле {field} обновлено.")
    return ConversationHandler.END

async def edithollow_start(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔")
        return ConversationHandler.END
    hs = get_hollows()
    if not hs: await update.message.reply_text("Холлоу нет."); return ConversationHandler.END
    btns = [[InlineKeyboardButton(h[1], callback_data=f"edit_h:{h[0]}")] for h in hs]
    await update.message.reply_text("Кого редактируем?", reply_markup=InlineKeyboardMarkup(btns))
    return EDIT_H_FIELD

async def edit_h_field(update, context):
    q = update.callback_query; await q.answer()
    hid = int(q.data.split(":")[1])
    context.user_data["edit_hid"] = hid
    fields = ["name","hp","damage","idle_damage","chase_doors",
              "debuff_type","debuff_value","debuff_growing","debuff_rooms","photo_id"]
    btns = [[InlineKeyboardButton(f, callback_data=f"edit_hf:{f}")] for f in fields]
    btns.append([InlineKeyboardButton("↩️ Отмена", callback_data="cancel_edit")])
    await q.message.reply_text("Поля:", reply_markup=InlineKeyboardMarkup(btns))
    return EDIT_H_VALUE

async def edit_h_value_start(update, context):
    q = update.callback_query; await q.answer()
    field = q.data.split(":")[1]
    context.user_data["edit_hfield"] = field
    await q.message.reply_text(f"Новое значение для {field}:")
    return EDIT_H_VALUE

async def edit_h_value(update, context):
    hid = context.user_data.get("edit_hid")
    field = context.user_data.get("edit_hfield")
    if not hid or not field:
        await update.message.reply_text("Ошибка.")
        return ConversationHandler.END
    val = update.message.text.strip()
    if field in ("hp","damage","idle_damage","chase_doors","debuff_value","debuff_growing","debuff_rooms"):
        try: val = int(val)
        except ValueError: await update.message.reply_text("Число:"); return EDIT_H_VALUE
    if field == "debuff_type" and val not in ("poison","spawn_up","none"):
        await update.message.reply_text("poison / spawn_up / none:"); return EDIT_H_VALUE
    update_hollow_field(hid, field, val)
    await update.message.reply_text(f"✅ {field} обновлено.")
    return ConversationHandler.END

async def cancel_edit(update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Отменено.")
    return ConversationHandler.END

async def list_miracles(update, context):
    ms = get_miracles()
    if not ms: await update.message.reply_text("Пусто."); return
    await update.message.reply_text(
        "✨ *Мираклы*\n" + "\n".join(f"• {md(m[1])} · 💿 {m[10]}" for m in ms),
        parse_mode="MarkdownV2")

async def list_hollows(update, context):
    hs = get_hollows()
    if not hs: await update.message.reply_text("Пусто."); return
    await update.message.reply_text(
        "👁️ *Холлоу*\n" + "\n".join(f"• {md(h[1])} · HP {h[2]}" for h in hs),
        parse_mode="MarkdownV2")

async def unknown(update, context):
    await update.message.reply_text("Напиши /start.")

# ============ main ============
def main():
    if not TOKEN: raise RuntimeError("Нет TOKEN")
    init_db()
    app = Application.builder().token(TOKEN).build()

    miracle_conv = ConversationHandler(
        entry_points=[CommandHandler("addmiracle", add_miracle_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_description)],
            HP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_hp)],
            DAMAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_damage)],
            STAMINA: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stamina)],
            LUCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_luck)],
            SUPER_DAMAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_super_damage)],
            SUPER_CHARGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_super_charges)],
            TRAITS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_traits),
                     CommandHandler("skip", add_skip_traits)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
            PHOTO: [MessageHandler(filters.PHOTO, add_photo),
                    CommandHandler("skip", add_skip_photo)],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)], allow_reentry=True,
    )

    hollow_conv = ConversationHandler(
        entry_points=[CommandHandler("addhollow", add_hollow_start)],
        states={
            H_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_name)],
            H_HP: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_hp)],
            H_DMG: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_dmg)],
            H_IDLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_idle)],
            H_CHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_chase)],
            H_DEBUFF_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_debuff_type),
                            CommandHandler("skip", h_skip)],
            H_DEBUFF_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_debuff_value)],
            H_DEBUFF_GROWING: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_debuff_growing)],
            H_DEBUFF_ROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_debuff_rooms)],
            H_PHOTO: [MessageHandler(filters.PHOTO, h_photo),
                      CommandHandler("skip", h_skip)],
        },
        fallbacks=[CommandHandler("cancel", cancel_hollow)], allow_reentry=True,
    )

    edit_m_conv = ConversationHandler(
        entry_points=[CommandHandler("editmiracle", editmiracle_start)],
        states={
            EDIT_FIELD: [CallbackQueryHandler(edit_m_field, pattern="^edit_m:")],
            EDIT_VALUE: [
                CallbackQueryHandler(edit_m_value_start, pattern="^edit_mf:"),
                CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_m_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)], allow_reentry=True,
    )

    edit_h_conv = ConversationHandler(
        entry_points=[CommandHandler("edithollow", edithollow_start)],
        states={
            EDIT_H_FIELD: [CallbackQueryHandler(edit_h_field, pattern="^edit_h:")],
            EDIT_H_VALUE: [
                CallbackQueryHandler(edit_h_value_start, pattern="^edit_hf:"),
                CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_h_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_hollow)], allow_reentry=True,
    )

    app.add_handler(miracle_conv)
    app.add_handler(hollow_conv)
    app.add_handler(edit_m_conv)
    app.add_handler(edit_h_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("miracles", list_miracles))
    app.add_handler(CommandHandler("hollows", list_hollows))

    app.add_handler(CallbackQueryHandler(menu, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(rules, pattern="^rules$"))
    app.add_handler(CallbackQueryHandler(list_miracles_cb, pattern="^list_miracles$"))
    app.add_handler(CallbackQueryHandler(view_miracle, pattern="^view_miracle:"))
    app.add_handler(CallbackQueryHandler(unlock_prompt, pattern="^unlock_prompt:"))
    app.add_handler(CallbackQueryHandler(unlock_miracle_cb, pattern="^unlock:"))

    app.add_handler(CallbackQueryHandler(play_cb, pattern="^play$"))
    app.add_handler(CallbackQueryHandler(play_pick, pattern="^play_pick:"))
    app.add_handler(CallbackQueryHandler(resume_game, pattern="^resume$"))

    app.add_handler(CallbackQueryHandler(emergency_exit, pattern="^emergency_exit$"))
    app.add_handler(CallbackQueryHandler(exit_confirm2, pattern="^exit_confirm2$"))
    app.add_handler(CallbackQueryHandler(exit_final, pattern="^exit_final$"))

    app.add_handler(CallbackQueryHandler(branch, pattern="^branch:"))
    app.add_handler(CallbackQueryHandler(fight, pattern="^fight:"))
    app.add_handler(CallbackQueryHandler(lift, pattern="^lift$"))
    app.add_handler(CallbackQueryHandler(lift_skip, pattern="^lift_skip$"))
    app.add_handler(CallbackQueryHandler(shop_buy, pattern="^shop_buy:"))
    app.add_handler(CallbackQueryHandler(session_inventory, pattern="^session_inventory$"))
    app.add_handler(CallbackQueryHandler(use_item, pattern="^use:"))

    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    log.info("WONDERWORLD started")
    app.run_polling()

if __name__ == "__main__":
    main()
