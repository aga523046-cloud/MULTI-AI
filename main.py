import os
import random
import sqlite3
import asyncio
import json
import logging
import re
from contextlib import closing
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
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

# ── Параметры нового цикла ──
LEVER_MIN_SEARCH = 4          # минимум шагов до рычага
LEVER_MAX_SEARCH = 13         # максимум шагов до рычага
MAX_DEPTH = 15                # максимальная «глубина» этажа (шагов)
STEP_COST = 1                 # ходов за 1 шаг вперёд
RETURN_COST_PER_STEP = 1      # ходов за 1 шаг возвращения
TOTAL_STEPS = 80              # общий запас ходов на забег

# ============ Индексы session ============
S_ACTIVE = 1
S_MSG = 2
S_CHAT = 3
S_START = 4
S_DOORS = 5
S_KILLS = 6
S_DEAD = 7
S_FOUND = 8
S_USED = 9
S_DEALT = 10
S_TAKEN = 11
S_REASON = 12
S_SHOP = 13
S_BOUGHT = 14
S_STEPS = 15
S_DEPTH = 16          # насколько глубоко зашёл на этаже
S_LEVER = 17          # сколько шагов нужно, чтобы найти рычаг (0 = не рассчитано)
S_LEVER_FOUND = 18    # рычаг найден (1/0)
S_RETURNING = 19      # сейчас возвращается (1/0)
S_RETURN_POS = 20     # текущая позиция возвращения (0..depth)
S_FLOOR_LIT = 21      # освещён ли этаж (1/0)
S_FLOOR_SAFE = 22     # безопасный ли этаж (1/0)
S_FLOOR_KIND = 23     # тип этажа (atrium / ticket / bakery / hall)

# ============ Индексы players ============
P_LOBBY_MSG = 21
P_LOBBY_CHAT = 22

# ============ Markdown V2 escape ============
MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
def md(s):
    if s is None:
        return ""
    s = str(s)
    return re.sub(r"([" + re.escape(MD_SPECIAL) + r"])", r"\\\1", s)

# ============ Шаги ConversationHandler ============
(NAME, DESCRIPTION, HP, DAMAGE, STAMINA, LUCK,
 SUPER_DAMAGE, SUPER_CHARGES, PRICE, PHOTO) = range(10)

(H_NAME, H_HP, H_DMG, H_IDLE, H_CHASE,
 H_DEBUFF_TYPE, H_DEBUFF_VALUE, H_DEBUFF_GROWING, H_DEBUFF_ROOMS, H_PHOTO) = range(10)

(EDIT_FIELD, EDIT_VALUE) = range(2)
(EDIT_H_FIELD, EDIT_H_VALUE) = range(2)
(SKIN_MIRACLE, SKIN_NAME, SKIN_PRICE, SKIN_PHOTO) = range(4)

# ============ БД ============
def db():
    return sqlite3.connect(DB_PATH, timeout=10)

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
                debuff_json TEXT NOT NULL DEFAULT '',
                active_skin_id INTEGER NOT NULL DEFAULT 0,
                lobby_msg_id INTEGER NOT NULL DEFAULT 0,
                lobby_chat_id INTEGER NOT NULL DEFAULT 0
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
            ("active_skin_id", "ALTER TABLE players ADD COLUMN active_skin_id INTEGER NOT NULL DEFAULT 0"),
            ("lobby_msg_id", "ALTER TABLE players ADD COLUMN lobby_msg_id INTEGER NOT NULL DEFAULT 0"),
            ("lobby_chat_id", "ALTER TABLE players ADD COLUMN lobby_chat_id INTEGER NOT NULL DEFAULT 0"),
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
                shop_bought TEXT NOT NULL DEFAULT '',
                steps_left INTEGER NOT NULL DEFAULT 0,
                depth INTEGER NOT NULL DEFAULT 0,
                lever_target INTEGER NOT NULL DEFAULT 0,
                lever_found INTEGER NOT NULL DEFAULT 0,
                returning INTEGER NOT NULL DEFAULT 0,
                return_pos INTEGER NOT NULL DEFAULT 0,
                floor_lit INTEGER NOT NULL DEFAULT 1,
                floor_safe INTEGER NOT NULL DEFAULT 0,
                floor_kind TEXT NOT NULL DEFAULT 'hall'
            )
        """)
        scols = [r[1] for r in con.execute("PRAGMA table_info(session)").fetchall()]
        for col, ddl in [
            ("shop_open", "ALTER TABLE session ADD COLUMN shop_open INTEGER NOT NULL DEFAULT 0"),
            ("shop_bought", "ALTER TABLE session ADD COLUMN shop_bought TEXT NOT NULL DEFAULT ''"),
            ("steps_left", "ALTER TABLE session ADD COLUMN steps_left INTEGER NOT NULL DEFAULT 0"),
            ("depth", "ALTER TABLE session ADD COLUMN depth INTEGER NOT NULL DEFAULT 0"),
            ("lever_target", "ALTER TABLE session ADD COLUMN lever_target INTEGER NOT NULL DEFAULT 0"),
            ("lever_found", "ALTER TABLE session ADD COLUMN lever_found INTEGER NOT NULL DEFAULT 0"),
            ("returning", "ALTER TABLE session ADD COLUMN returning INTEGER NOT NULL DEFAULT 0"),
            ("return_pos", "ALTER TABLE session ADD COLUMN return_pos INTEGER NOT NULL DEFAULT 0"),
            ("floor_lit", "ALTER TABLE session ADD COLUMN floor_lit INTEGER NOT NULL DEFAULT 1"),
            ("floor_safe", "ALTER TABLE session ADD COLUMN floor_safe INTEGER NOT NULL DEFAULT 0"),
            ("floor_kind", "ALTER TABLE session ADD COLUMN floor_kind TEXT NOT NULL DEFAULT 'hall'"),
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

        con.execute("""
            CREATE TABLE IF NOT EXISTS skins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                miracle_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                price INTEGER NOT NULL DEFAULT 0,
                photo_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS player_skins (
                user_id INTEGER NOT NULL,
                skin_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, skin_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS shop_daily (
                slot INTEGER PRIMARY KEY,
                skin_id INTEGER NOT NULL,
                date_key TEXT NOT NULL
            )
        """)
        con.commit()

# ============ Мираклы ============
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
                 super_damage, super_charges, price, photo_id):
    with closing(db()) as con:
        con.execute(
            """INSERT INTO miracles(name,description,hp,damage,stamina,luck,
                                    super_damage,super_charges,traits,price,photo_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (name, description, hp, damage, stamina, luck,
             super_damage, super_charges, "", price, photo_id),
        )
        con.commit()

def update_miracle_field(mid, field, value):
    allowed = {"name","description","hp","damage","stamina","luck",
               "super_damage","super_charges","price","photo_id"}
    if field not in allowed:
        return
    with closing(db()) as con:
        con.execute(f"UPDATE miracles SET {field}=? WHERE id=?", (value, mid))
        con.commit()

def miracle_card(m):
    price = "Бесплатно" if m[10] == 0 else f"{m[10]}💿"
    return (
        f"✨ *{md(m[1])}*\n{md(m[2])}\n\n"
        f"♥️ Здоровье: {sector(m[3],750)}/5\n"
        f"⚔️ Урон: {sector(m[4],75)}/5\n"
        f"⚡️ Выносливость: {sector(m[5],300)}/5\n"
        f"🍀 Везучесть: {sector(m[6],300)}/5\n"
        f"💥 Супер: {m[7]} урона, за {m[8]} атак\n"
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
        con.execute("INSERT OR IGNORE INTO player_miracles(user_id,miracle_id) VALUES(?,?)",
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

# ============ Скины ============
def get_skins_for_miracle(mid):
    with closing(db()) as con:
        return con.execute(
            "SELECT id,miracle_id,name,price,photo_id FROM skins WHERE miracle_id=? ORDER BY id",
            (mid,)).fetchall()

def get_skin(sid):
    with closing(db()) as con:
        return con.execute(
            "SELECT id,miracle_id,name,price,photo_id FROM skins WHERE id=?",
            (sid,)).fetchone()

def save_skin(mid, name, price, photo_id):
    with closing(db()) as con:
        con.execute(
            "INSERT INTO skins(miracle_id,name,price,photo_id) VALUES(?,?,?,?)",
            (mid, name, price, photo_id))
        con.commit()

def is_skin_owned(uid, sid):
    with closing(db()) as con:
        return bool(con.execute(
            "SELECT 1 FROM player_skins WHERE user_id=? AND skin_id=?",
            (uid, sid)).fetchone())

def own_skin(uid, sid):
    with closing(db()) as con:
        con.execute("INSERT OR IGNORE INTO player_skins(user_id,skin_id) VALUES(?,?)",
                    (uid, sid))
        con.commit()

def get_skin_photo(uid, mid):
    p = get_player(uid)
    if p[1] == mid and p[20]:
        s = get_skin(p[20])
        if s and s[1] == mid:
            return s[4]
    return None

def today_msk_key():
    now = datetime.utcnow() + timedelta(hours=3)
    return now.strftime("%Y-%m-%d")

def refresh_daily_shop(force=False):
    key = today_msk_key()
    with closing(db()) as con:
        existing = con.execute("SELECT skin_id, date_key FROM shop_daily").fetchall()
        if not force and len(existing) == 3 and all(r[1] == key for r in existing):
            return
        all_skins = con.execute("SELECT id FROM skins").fetchall()
        skin_ids = [r[0] for r in all_skins]
        random.shuffle(skin_ids)
        chosen = skin_ids[:3]
        con.execute("DELETE FROM shop_daily")
        for i, sid in enumerate(chosen):
            con.execute("INSERT INTO shop_daily(slot, skin_id, date_key) VALUES(?,?,?)",
                        (i, sid, key))
        con.commit()

def get_daily_shop():
    with closing(db()) as con:
        rows = con.execute(
            "SELECT s.slot, s.skin_id, k.miracle_id, k.name, k.price, k.photo_id "
            "FROM shop_daily s JOIN skins k ON k.id = s.skin_id ORDER BY s.slot"
        ).fetchall()
    return rows

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
               "chase_remaining","chase_hollow_id","debuff_json","active_skin_id",
               "lobby_msg_id","lobby_chat_id"}
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
               "damage_dealt","damage_taken","exit_reason","shop_open",
               "shop_bought","steps_left","depth","lever_target","lever_found",
               "returning","return_pos","floor_lit","floor_safe","floor_kind"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    with closing(db()) as con:
        con.execute(f"UPDATE session SET {assignments} WHERE user_id=?",
                    (*fields.values(), uid))
        con.commit()

def session_active(uid):
    return bool(get_session(uid)[S_ACTIVE])

def reset_session_stats(uid, msg_id=0, chat_id=0, start_discs=0, steps=TOTAL_STEPS):
    set_session(uid, active=1, msg_id=msg_id, chat_id=chat_id, start_discs=start_discs,
                doors_opened=0, hollows_killed=0, dead_ends=0, items_found=0,
                items_used=0, damage_dealt=0, damage_taken=0, exit_reason="",
                shop_open=0, shop_bought="", steps_left=steps,
                depth=0, lever_target=0, lever_found=0,
                returning=0, return_pos=0,
                floor_lit=1, floor_safe=0, floor_kind="hall")

# ============ Утилиты ============
def sector(v, mx):
    return max(1, min(5, (max(1, v) - 1) * 5 // mx + 1))

def floor_name(f):
    if f == 0: return "ЗАДВЕРЬЕ"
    if f == 26: return "S-000"
    return f"Этаж {f}"

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

# ============ Этажи ============
FLOOR_KINDS = {
    "atrium": {"name": "Атриум", "lit_chance": 0.4, "safe_chance": 0.3, "loot_chance": 0.5},
    "ticket": {"name": "Билетная касса", "lit_chance": 0.9, "safe_chance": 0.9, "loot_chance": 0.6},
    "bakery": {"name": "Пекарня", "lit_chance": 0.7, "safe_chance": 0.4, "loot_chance": 0.8},
    "hall":   {"name": "Холл", "lit_chance": 0.5, "safe_chance": 0.5, "loot_chance": 0.5},
}

def roll_floor(floor_num):
    """Генерирует свойства этажа."""
    if floor_num == 1:
        kind = "ticket"
        lit = 1
        safe = 1
    else:
        if floor_num % 10 == 0 or floor_num % 20 == 0:
            kind = "ticket"
        else:
            r = random.random()
            if r < 0.35: kind = "hall"
            elif r < 0.60: kind = "atrium"
            elif r < 0.80: kind = "bakery"
            else: kind = "hall"
        k = FLOOR_KINDS[kind]
        lit = 1 if random.random() < k["lit_chance"] else 0
        safe = 1 if random.random() < k["safe_chance"] else 0
    return kind, lit, safe

def calc_lever_target(floor_num):
    """Сколько шагов до рычага на этом этаже."""
    base = random.randint(LEVER_MIN_SEARCH, LEVER_MAX_SEARCH)
    # чем выше этаж, тем чуть дальше рычаг
    extra = max(0, (floor_num - 1) // 5)
    return min(MAX_DEPTH, base + extra)

# ============ Клавиатуры ============
def main_menu_keyboard(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Играть", callback_data="play")],
        [InlineKeyboardButton("✨ Мираклы", callback_data="list_miracles"),
         InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("🛒 Магазин", callback_data="shop")],
        [InlineKeyboardButton("📖 Правила", callback_data="rules")],
    ])

def lobby_text(uid):
    p = get_player(uid)
    return (
        f"🌙 *WONDERWORLD*\n"
        f"💿 Дисков: *{p[11]}*\n\n"
        f"Выбери действие:"
    )

def battle_keyboard(super_ready, counter, need):
    need = max(1, need or 1)
    if super_ready:
        lbl = "💥 СУПЕРУДАР!"
    else:
        lbl = f"💥 Супер {counter}/{need}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚔️ Атаковать", callback_data="fight:attack"),
         InlineKeyboardButton(lbl, callback_data="fight:super")],
        [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
    ])

# ── Текст и клавиатура этажа ──
def floor_status_lines(uid):
    s = get_session(uid)
    kind = s[S_FLOOR_KIND]
    name = FLOOR_KINDS.get(kind, {"name":"Холл"})["name"]
    depth = s[S_DEPTH]
    lever_found = s[S_LEVER_FOUND]
    lit = s[S_FLOOR_LIT]
    safe = s[S_FLOOR_SAFE]
    returning = s[S_RETURNING]
    ret_pos = s[S_RETURN_POS]
    steps = s[S_STEPS]
    lines = []
    if returning:
        lines.append(f"🛗 *{md(name)}* · возвращение")
        lines.append(f"📍 До лифта: *{ret_pos}* м")
        lines.append(f"⏳ Ходов: {steps}")
    else:
        lines.append(f"🌙 *{md(name)}* · этаж {get_player(uid)[3]}")
        lines.append(f"📍 Глубина: *{depth}/{MAX_DEPTH}*")
        lines.append(f"⏳ Ходов: {steps}")
        lines.append(f"💡 {'светло' if lit else 'темно'} · ⚠️ {'безопасно' if safe else 'опасно'}")
        if lever_found:
            lines.append("⚙️ *Рычаг найден!*")
    return "\n".join(lines)

def floor_keyboard(uid):
    s = get_session(uid)
    returning = s[S_RETURNING]
    if returning:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚶 Идти", callback_data="ret:go")],
            [InlineKeyboardButton("🤫 Замереть", callback_data="ret:wait"),
             InlineKeyboardButton("🏃 Бежать", callback_data="ret:run")],
            [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
        ])
    lever_found = s[S_LEVER_FOUND]
    depth = s[S_DEPTH]
    if lever_found:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🛗 Вернуться к лифту", callback_data="ret:start")],
            [InlineKeyboardButton("🎒 Инвентарь", callback_data="session_inventory")],
            [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
        ])
    rows = []
    if depth < MAX_DEPTH:
        rows.append([InlineKeyboardButton("🚶 Идти дальше (1 ход)", callback_data="floor:advance")])
    rows.append([InlineKeyboardButton("🎒 Инвентарь", callback_data="session_inventory")])
    rows.append([InlineKeyboardButton("🛗 К лифту", callback_data="floor:to_lift")])
    rows.append([InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")])
    return InlineKeyboardMarkup(rows)

def floor_text(uid):
    p = get_player(uid)
    m = current_miracle(uid)
    if not m:
        return "Миракл не выбран\\. Напиши /start\\."
    if p[10]:
        need = max(1, m[8] or 1)
        return (
            f"👁️ *Бой\\!*\n"
            f"❤️ HP холлоу: {p[8]}\n"
            f"♥️ Твоё здоровье: {p[7]}/{m[3]}\n"
            f"💥 Супер: {'готов' if p[12] else f'{p[13]}/{need}'}"
        )
    body = floor_status_lines(uid)
    deb = get_active_debuff(uid)
    if deb:
        body += f"\n☠️ Дебафф: {md(deb.get('type',''))} ×{deb.get('rooms',0)}"
    body += "\n\nЧто делаешь?"
    return body

def game_keyboard(uid):
    p = get_player(uid)
    if p[10]:
        m = current_miracle(uid)
        if not m:
            set_player(uid, battle_active=0)
            return main_menu_keyboard(uid)
        return battle_keyboard(p[12], p[13], m[8])
    return floor_keyboard(uid)

def game_text(uid):
    return floor_text(uid)

# ============ Единое редактируемое сообщение ============
async def show_screen(app, uid, text, keyboard, photo=None):
    p = get_player(uid)
    msg_id, chat_id = p[P_LOBBY_MSG], p[P_LOBBY_CHAT]
    if msg_id and chat_id:
        try:
            if photo:
                try:
                    await app.bot.edit_message_media(
                        chat_id=chat_id, message_id=msg_id,
                        media=InputMediaPhoto(media=photo, caption=text,
                                              parse_mode="MarkdownV2"),
                        reply_markup=keyboard)
                    return
                except Exception as e:
                    if "not modified" in str(e).lower():
                        return
                    log.info("edit_media failed: %s", e)
                    await app.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id,
                        text=text, parse_mode="MarkdownV2", reply_markup=keyboard)
                    return
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, parse_mode="MarkdownV2", reply_markup=keyboard)
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            log.info("edit_message_text failed: %s", e)
    try:
        if photo:
            msg = await app.bot.send_photo(
                chat_id=uid, photo=photo, caption=text,
                parse_mode="MarkdownV2", reply_markup=keyboard)
        else:
            msg = await app.bot.send_message(
                chat_id=uid, text=text, parse_mode="MarkdownV2", reply_markup=keyboard)
        set_player(uid, lobby_msg_id=msg.message_id, lobby_chat_id=msg.chat_id)
    except Exception:
        log.exception("show_screen send failed")

async def render_game(app, uid, text=None, keyboard=None):
    try:
        s = get_session(uid)
        if not s[S_ACTIVE]:
            await show_lobby(app, uid)
            return
        if text is None:
            text = game_text(uid)
        if keyboard is None:
            keyboard = game_keyboard(uid)
        await show_screen(app, uid, text, keyboard)
        p = get_player(uid)
        set_session(uid, msg_id=p[P_LOBBY_MSG], chat_id=p[P_LOBBY_CHAT])
    except Exception:
        log.exception("render_game")

async def show_lobby(app, uid):
    await show_screen(app, uid, lobby_text(uid), main_menu_keyboard(uid))

# ============ Быстрые команды ============
def _clear_user_data(context):
    for key in ("lift_waiting", "lift_cancel", "peek_next", "use_key_next",
                "new_miracle", "new_hollow", "edit_mid", "edit_hid",
                "edit_field", "edit_hfield", "magnifier_ready",
                "skin_mid", "skin_name", "skin_price"):
        context.user_data.pop(key, None)

def _stop_session(uid):
    p_before = get_player(uid)
    m_before = current_miracle(uid)
    s = get_session(uid)
    earned = p_before[11] - s[S_START]
    stats_text = (
        f"🛑 *Забег остановлен*\n"
        f"💿 Получено: *{earned:+d}*\n"
        f"👁️ Холлоу убито: {s[S_KILLS]}\n"
        f"🎁 Найдено: {s[S_FOUND]}\n"
        f"⚔️ Урона нанесено: {s[S_DEALT]}\n"
        f"💔 Получено: {s[S_TAKEN]}\n"
        f"💰 Баланс: {p_before[11]}💿"
    )
    set_session(uid, active=0, msg_id=0, chat_id=0, shop_open=0, shop_bought="",
                exit_reason="stop_command", steps_left=0,
                returning=0, return_pos=0, depth=0, lever_found=0)
    base_hp = m_before[3] if m_before else 0
    set_player(uid, hp=base_hp, floor=0, door=0, next_safe=0, items="",
               battle_hp=0, battle_hollow_hp=0, battle_damage=0, battle_active=0,
               super_ready=0, super_counter=0, battle_hollow_id=0,
               battle_idle_damage=0, battle_msg_id=0, battle_chat_id=0,
               chase_remaining=0, chase_hollow_id=0, debuff_json="")
    return stats_text

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.effective_user.id
        _clear_user_data(context)
        battles = context.application.user_data.get("battles")
        if isinstance(battles, dict):
            battles.pop(uid, None)
        if not session_active(uid):
            await show_lobby(context.application, uid)
            return
        _stop_session(uid)
        await show_lobby(context.application, uid)
    except Exception:
        log.exception("cmd_stop")

async def cmd_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.effective_user.id
        if session_active(uid):
            await render_game(context.application, uid)
            return
        await show_lobby(context.application, uid)
    except Exception:
        log.exception("cmd_lobby")

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_lobby(update, context)

async def cmd_stop_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        await q.answer("Завершаю забег…")
        uid = q.from_user.id
        _clear_user_data(context)
        battles = context.application.user_data.get("battles")
        if isinstance(battles, dict):
            battles.pop(uid, None)
        if not session_active(uid):
            await show_lobby(context.application, uid)
            return
        _stop_session(uid)
        await show_lobby(context.application, uid)
    except Exception:
        log.exception("cmd_stop_inline")

# ============ Старт ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.effective_user.id
        _clear_user_data(context)
        get_player(uid)
        get_session(uid)
        s = get_session(uid)
        if s[S_ACTIVE] and (not s[S_MSG] or not s[S_CHAT]):
            set_session(uid, active=0, msg_id=0, chat_id=0, shop_open=0,
                        shop_bought="", steps_left=0, returning=0, return_pos=0)
        p = get_player(uid)
        if p[1] and not get_miracle(p[1]):
            set_player(uid, miracle_id=0, battle_active=0, battle_hp=0,
                       battle_hollow_hp=0, super_ready=0, super_counter=0,
                       chase_remaining=0, chase_hollow_id=0, debuff_json="")
        set_player(uid, lobby_msg_id=0, lobby_chat_id=0)
        if session_active(uid):
            await render_game(context.application, uid)
            return
        await show_lobby(context.application, uid)
    except Exception:
        log.exception("start")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if session_active(uid):
            await render_game(context.application, uid)
            return
        await show_lobby(context.application, uid)
    except Exception:
        log.exception("menu_callback")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        p = get_player(uid)
        with closing(db()) as con:
            cnt = con.execute("SELECT COUNT(*) FROM player_miracles WHERE user_id=?",
                              (uid,)).fetchone()[0]
        free = sum(1 for m in get_miracles() if m[10] == 0)
        text = (
            f"👤 *Профиль*\n💿 Дисков: *{p[11]}*\n"
            f"✨ Мираклов: {cnt + free}/{len(get_miracles())}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад", callback_data="menu")],
        ])
        await show_screen(context.application, uid, text, kb)
    except Exception:
        log.exception("profile")

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query; await q.answer()
        text = (
            "📖 *Правила*\n"
            "• Этаж — это комнаты\\. Жми «Идти дальше», чтобы углубляться\\.\n"
            "• Чем глубже — тем выше шанс найти рычаг, но и шанс встретить холлоу\\.\n"
            "• Найдя рычаг, возвращайся к лифту\\. Возвращение пошаговое: идти / замереть / бежать\\.\n"
            "• Темно — можно споткнуться\\. Безопасно — холлоу нет\\.\n"
            "• Ходы \\(⏳\\) тратятся на шаги и бои\\. Кончились — забег завершён\\.\n"
            "• /stop — завершить забег, /start — новое сообщение, /lobby — меню\\.\n"
            "• Магазин скинов обновляется в 23:00 МСК\\."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад", callback_data="menu")],
        ])
        await show_screen(context.application, uid, text, kb)
    except Exception:
        log.exception("rules")

# ============ Магазин скинов ============
async def shop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        await q.answer()
        uid = q.from_user.id
        refresh_daily_shop()
        items = get_daily_shop()
        if not items:
            text = "🛒 *Магазин*\nСегодня скинов нет. Заходи позже!"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Назад", callback_data="menu")],
            ])
            await show_screen(context.application, uid, text, kb)
            return
        p = get_player(uid)
        rows = []
        for slot, sid, mid, name, price, photo in items:
            m = get_miracle(mid)
            mname = m[1] if m else "?"
            owned = is_skin_owned(uid, sid)
            mark = "✅ " if owned else ""
            rows.append([InlineKeyboardButton(
                f"{mark}{name} ({mname}) — {price}💿",
                callback_data=f"shop_skin:{sid}")])
        rows.append([InlineKeyboardButton("↩️ Назад", callback_data="menu")])
        text = (
            f"🛒 *Магазин скинов*\n"
            f"💿 У тебя: *{p[11]}*\n"
            f"_Обновление в 23:00 МСК_\n\n"
            f"Выбери скин для просмотра:"
        )
        await show_screen(context.application, uid, text,
                          InlineKeyboardMarkup(rows))
    except Exception:
        log.exception("shop_cb")

async def shop_skin_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        await q.answer()
        uid = q.from_user.id
        sid = int(q.data.split(":")[1])
        s = get_skin(sid)
        if not s:
            await show_screen(context.application, uid, "Скин не найден.",
                              main_menu_keyboard(uid))
            return
        m = get_miracle(s[1])
        mname = m[1] if m else "?"
        owned = is_skin_owned(uid, sid)
        p = get_player(uid)
        caption = (
            f"🎨 *{md(s[2])}*\n"
            f"Для миракла: *{md(mname)}*\n"
            f"💿 Цена: {s[3]}\n"
            f"💰 У тебя: {p[11]}💿"
        )
        if owned:
            btn_text = "✅ Куплено"
            btn_cb = "shop_noop"
        elif p[11] < s[3]:
            btn_text = "❌ Не хватает 💿"
            btn_cb = "shop_noop"
        else:
            btn_text = f"🛒 Купить ({s[3]}💿)"
            btn_cb = f"shop_buy_skin:{sid}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_text, callback_data=btn_cb)],
            [InlineKeyboardButton("↩️ К магазину", callback_data="shop")],
        ])
        await show_screen(context.application, uid, caption, kb, photo=s[4])
    except Exception:
        log.exception("shop_skin_view")

async def shop_buy_skin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        uid = q.from_user.id
        sid = int(q.data.split(":")[1])
        s = get_skin(sid)
        if not s:
            await q.answer("Скин не найден", show_alert=True)
            return
        if is_skin_owned(uid, sid):
            await q.answer("Уже куплено", show_alert=True)
            return
        p = get_player(uid)
        if p[11] < s[3]:
            await q.answer("Не хватает дисков", show_alert=True)
            return
        set_player(uid, discs=p[11] - s[3])
        own_skin(uid, sid)
        await q.answer("Куплено!", show_alert=True)
        await shop_skin_view(update, context)
    except Exception:
        log.exception("shop_buy_skin")

async def shop_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        await q.answer()
    except Exception:
        pass

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
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        kb = miracle_list_keyboard(uid)
        if not kb:
            await show_screen(context.application, uid, "Мираклов нет.",
                              main_menu_keyboard(uid))
            return
        await show_screen(context.application, uid, "✨ *Мираклы*", kb)
    except Exception:
        log.exception("list_miracles_cb")

async def view_miracle(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        mid = int(q.data.split(":")[1])
        m = get_miracle(mid)
        if not m:
            await show_screen(context.application, uid, "Не найден.",
                              main_menu_keyboard(uid))
            return
        extra_rows = []
        if is_unlocked(uid, mid):
            owned_skins = [s for s in get_skins_for_miracle(mid) if is_skin_owned(uid, s[0])]
            if owned_skins:
                extra_rows.append([InlineKeyboardButton("🎨 Сменить скин", callback_data=f"pick_skin:{mid}")])
        kb = InlineKeyboardMarkup(extra_rows + [
            ([InlineKeyboardButton("▶️ Играть", callback_data=f"play_pick:{mid}")]
             if is_unlocked(uid, mid) else
             [InlineKeyboardButton(f"🔓 Разблокировать ({m[10]}💿)", callback_data=f"unlock:{mid}")]),
            [InlineKeyboardButton("↩️ Назад", callback_data="list_miracles")],
        ])
        photo = get_skin_photo(uid, mid) or m[11]
        await show_screen(context.application, uid, miracle_card(m), kb, photo=photo)
    except Exception:
        log.exception("view_miracle")

async def pick_skin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        mid = int(q.data.split(":")[1])
        skins = [s for s in get_skins_for_miracle(mid) if is_skin_owned(uid, s[0])]
        if not skins:
            await show_screen(context.application, uid,
                              "Нет купленных скинов для этого миракла.",
                              main_menu_keyboard(uid))
            return
        p = get_player(uid)
        active_id = p[20] if p[1] == mid else 0
        rows = []
        for s in skins:
            mark = "✅ " if s[0] == active_id else ""
            rows.append([InlineKeyboardButton(f"{mark}{s[2]}", callback_data=f"apply_skin:{s[0]}")])
        rows.append([InlineKeyboardButton("🔄 Снять скин", callback_data="apply_skin:0")])
        rows.append([InlineKeyboardButton("↩️ Назад", callback_data=f"view_miracle:{mid}")])
        await show_screen(context.application, uid, "Выбери скин:",
                          InlineKeyboardMarkup(rows))
    except Exception:
        log.exception("pick_skin")

async def apply_skin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        sid = int(q.data.split(":")[1])
        if sid == 0:
            set_player(uid, active_skin_id=0)
            await q.answer("Скин снят", show_alert=True)
            await show_lobby(context.application, uid)
            return
        s = get_skin(sid)
        if not s or not is_skin_owned(uid, sid):
            await q.answer("Недоступно", show_alert=True)
            return
        p = get_player(uid)
        if p[1] != s[1]:
            await q.answer("Этот скин для другого миракла", show_alert=True)
            return
        set_player(uid, active_skin_id=sid)
        await q.answer("Скин надет!", show_alert=True)
        m = get_miracle(s[1])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Играть", callback_data=f"play_pick:{s[1]}")],
            [InlineKeyboardButton("🎨 Сменить скин", callback_data=f"pick_skin:{s[1]}")],
            [InlineKeyboardButton("↩️ Назад", callback_data="list_miracles")],
        ])
        await show_screen(context.application, uid, miracle_card(m), kb, photo=s[4])
    except Exception:
        log.exception("apply_skin")

async def unlock_prompt(update, context):
    try:
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
        await show_screen(context.application, uid, text, kb, photo=m[11])
    except Exception:
        log.exception("unlock_prompt")

async def unlock_miracle_cb(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        mid = int(q.data.split(":")[1])
        m = get_miracle(mid)
        if not m: return
        if is_unlocked(uid, mid):
            await show_screen(context.application, uid, "Уже разблокирован.",
                              main_menu_keyboard(uid))
            return
        p = get_player(uid)
        if p[11] < m[10]:
            await show_screen(context.application, uid,
                              f"Нужно {m[10]}💿, у тебя {p[11]}.",
                              main_menu_keyboard(uid))
            return
        set_player(uid, discs=p[11] - m[10])
        unlock_miracle(uid, mid)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Играть", callback_data=f"play_pick:{mid}")],
            [InlineKeyboardButton("↩️ К мираклам", callback_data="list_miracles")],
        ])
        await show_screen(context.application, uid,
                          f"✅ «{md(m[1])}» разблокирован!", kb)
    except Exception:
        log.exception("unlock_miracle_cb")

# ============ Играть ============
async def play_cb(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if session_active(uid):
            await render_game(context.application, uid)
            return
        ms = get_miracles()
        if not ms:
            await show_screen(context.application, uid, "Мираклов нет.",
                              main_menu_keyboard(uid))
            return
        btns = []
        for m in ms:
            if is_unlocked(uid, m[0]):
                btns.append([InlineKeyboardButton(f"▶️ {m[1]}", callback_data=f"play_pick:{m[0]}")])
        btns.append([InlineKeyboardButton("↩️ Назад", callback_data="menu")])
        await show_screen(context.application, uid,
                          "Выбери миракла для забега:",
                          InlineKeyboardMarkup(btns))
    except Exception:
        log.exception("play_cb")

async def resume_game(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if not session_active(uid):
            await show_lobby(context.application, uid)
            return
        await render_game(context.application, uid)
    except Exception:
        log.exception("resume_game")

async def play_pick(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        mid = int(q.data.split(":")[1])
        m = get_miracle(mid)
        if not m or not is_unlocked(uid, mid):
            await show_screen(context.application, uid, "Недоступно.",
                              main_menu_keyboard(uid))
            return
        if session_active(uid):
            await render_game(context.application, uid)
            return
        p = get_player(uid)
        set_player(uid, miracle_id=mid, hp=m[3], floor=0, door=0, next_safe=0,
                   items="", battle_hp=0, battle_hollow_hp=0, battle_active=0,
                   super_ready=0, super_counter=0, battle_hollow_id=0,
                   battle_idle_damage=0, battle_msg_id=0, battle_chat_id=0,
                   chase_remaining=0, chase_hollow_id=0, debuff_json="")
        reset_session_stats(uid, start_discs=p[11], steps=TOTAL_STEPS)
        # вход на первый этаж (0 = ЗАДВЕРЬЕ → сразу 1)
        await enter_floor(context.application, uid, 1)
    except Exception:
        log.exception("play_pick")

async def enter_floor(app, uid, floor_num):
    """Настраивает этаж и рендерит его."""
    try:
        p = get_player(uid)
        kind, lit, safe = roll_floor(floor_num)
        lever_target = calc_lever_target(floor_num)
        set_player(uid, floor=floor_num, door=0, battle_active=0,
                   battle_hp=0, battle_hollow_hp=0, battle_hollow_id=0,
                   next_safe=0)
        set_session(uid, depth=0, lever_target=lever_target, lever_found=0,
                    returning=0, return_pos=0,
                    floor_lit=lit, floor_safe=safe, floor_kind=kind)
        await render_game(app, uid)
    except Exception:
        log.exception("enter_floor")

# ============ Шаги по этажу ============
def _spend(uid, cost):
    s = get_session(uid)
    left = s[S_STEPS] - cost
    set_session(uid, steps_left=max(0, left))
    return left

async def floor_advance(update, context):
    """Игрок идёт глубже в этаж."""
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if not session_active(uid): return
        s = get_session(uid)
        if s[S_RETURNING]:
            await render_game(context.application, uid); return
        if s[S_LEVER_FOUND]:
            await render_game(context.application, uid); return
        depth = s[S_DEPTH]
        if depth >= MAX_DEPTH:
            await render_game(context.application, uid); return
        if s[S_STEPS] - STEP_COST <= 0:
            await finish_session(context.application, uid, "out_of_steps")
            return
        set_session(uid, steps_left=s[S_STEPS] - STEP_COST, depth=depth + 1)
        s = get_session(uid)
        new_depth = s[S_DEPTH]

        p = get_player(uid)
        m = current_miracle(uid)
        if not m: return

        # шанс найти рычаг
        if new_depth >= s[S_LEVER_TARGET]:
            set_session(uid, lever_found=1)
            await render_game(context.application, uid,
                text=floor_text(uid) + "\n\n⚙️ *Ты нашёл рычаг\\!* Потяни его и возвращайся к лифту\\.")
            return

        # шанс на предмет
        kind = s[S_FLOOR_KIND]
        loot_chance = FLOOR_KINDS.get(kind, {"loot_chance": 0.5})["loot_chance"]
        if random.random() < loot_chance * 0.4:
            tool = random.choice(["plaster","magnifier","helper","staff_key"])
            items = item_dict(p[6])
            items[tool] = items.get(tool, 0) + (3 if tool == "magnifier" else 1)
            set_player(uid, items=items_string(items))
            s = get_session(uid)
            set_session(uid, items_found=s[S_FOUND] + 1)
            await render_game(context.application, uid,
                text=floor_text(uid) + f"\n\n🎁 Найдено: *{md(ITEM_LABELS[tool])}*\\.")
            return

        # шанс на холлоу (растёт с глубиной)
        hollow_chance = 0.05 + 0.03 * (new_depth - 1)
        if not s[S_FLOOR_SAFE] and random.random() < hollow_chance:
            await start_battle(context.application, uid, p, m)
            return

        # тёмный этаж — шанс споткнуться
        if not s[S_FLOOR_LIT] and random.random() < 0.15:
            dmg = random.randint(5, 12)
            new_hp = max(1, p[2] - dmg)
            set_player(uid, hp=new_hp)
            s = get_session(uid)
            set_session(uid, damage_taken=s[S_TAKEN] + dmg)
            await render_game(context.application, uid,
                text=floor_text(uid) + f"\n\n🕳️ *Ты споткнулся\\.* \\-{dmg} HP\\.")
            return

        await render_game(context.application, uid,
            text=floor_text(uid) + "\n\nТы идёшь дальше\\.\\.\\.")
    except Exception:
        log.exception("floor_advance")

async def floor_to_lift(update, context):
    """Игрок решает вернуться к лифту без рычага."""
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if not session_active(uid): return
        s = get_session(uid)
        if s[S_RETURNING]:
            await render_game(context.application, uid); return
        if s[S_DEPTH] == 0:
            # уже у лифта — просто едем
            await do_lift(context.application, uid)
            return
        await start_return(context.application, uid)
    except Exception:
        log.exception("floor_to_lift")

async def start_return(app, uid):
    """Начинает возвращение к лифту (пошагово)."""
    try:
        s = get_session(uid)
        set_session(uid, returning=1, return_pos=s[S_DEPTH])
        await render_game(app, uid)
    except Exception:
        log.exception("start_return")

# ============ Возвращение пошагово ============
async def return_step(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if not session_active(uid): return
        s = get_session(uid)
        if not s[S_RETURNING]:
            await render_game(context.application, uid); return

        action = q.data.split(":")[1]  # go / wait / run
        pos = s[S_RETURN_POS]
        p = get_player(uid)
        m = current_miracle(uid)
        if not m: return

        # проверка ходов
        if s[S_STEPS] - RETURN_COST_PER_STEP <= 0:
            await finish_session(context.application, uid, "out_of_steps")
            return
        set_session(uid, steps_left=s[S_STEPS] - RETURN_COST_PER_STEP)

        # определяем, будет ли шорох в этот такт
        noise_chance = 0.35
        if not s[S_FLOOR_LIT]:
            noise_chance += 0.15
        if not s[S_FLOOR_SAFE]:
            noise_chance += 0.10
        has_noise = (random.random() < noise_chance)

        if action == "wait":
            # пропускаем такт, шорох проходит
            await render_game(context.application, uid,
                text=floor_text(uid) + "\n\n🤫 Ты замираешь\\. Шорох стихает\\.")
            return

        if action == "run":
            # бежим: 2 позиции, но получаем урон и точно триггерим холлоу при шорохе
            dmg = random.randint(4, 10)
            new_hp = max(1, p[2] - dmg)
            set_player(uid, hp=new_hp)
            s = get_session(uid)
            set_session(uid, damage_taken=s[S_TAKEN] + dmg)
            new_pos = max(0, pos - 2)
            if has_noise and not s[S_FLOOR_SAFE]:
                set_session(uid, return_pos=new_pos)
                await start_battle(context.application, uid, p, m)
                return
            set_session(uid, return_pos=new_pos)
            s = get_session(uid)
            if new_pos <= 0:
                await finish_return_to_lift(context.application, uid)
                return
            await render_game(context.application, uid,
                text=floor_text(uid) + f"\n\n🏃 Ты бежишь\\. \\-{dmg} HP\\.")
            return

        # action == "go" — идём один шаг
        new_pos = max(0, pos - 1)
        if has_noise and not s[S_FLOOR_SAFE]:
            set_session(uid, return_pos=new_pos)
            await start_battle(context.application, uid, p, m)
            return
        set_session(uid, return_pos=new_pos)
        if new_pos <= 0:
            await finish_return_to_lift(context.application, uid)
            return
        await render_game(context.application, uid,
            text=floor_text(uid) + "\n\n🚶 Ты идёшь дальше\\.")
    except Exception:
        log.exception("return_step")

async def finish_return_to_lift(app, uid):
    try:
        set_session(uid, returning=0, return_pos=0)
        await render_game(app, uid,
            text=floor_text(uid) + "\n\n🛗 *Ты у лифта\\!*")
    except Exception:
        log.exception("finish_return_to_lift")

# ============ Лифт ============
async def lift(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if not session_active(uid): return
        await do_lift(context.application, uid)
    except Exception:
        log.exception("lift")

async def do_lift(app, uid):
    try:
        s = get_session(uid)
        if s[S_RETURNING]:
            await render_game(app, uid); return
        if s[S_DEPTH] > 0:
            # сначала нужно вернуться
            await render_game(app, uid,
                text=floor_text(uid) + "\n\n⚠️ Сначала вернись к лифту\\.")
            return
        if context_user_lift_waiting(app, uid):
            return
        p = get_player(uid)
        floor = p[3]
        if floor >= MAX_FLOOR + 1:
            await finish_session(app, uid, "victory")
            return
        new_floor = floor + 1
        if new_floor > MAX_FLOOR + 1:
            await finish_session(app, uid, "victory")
            return

        app.user_data["lift_waiting"] = app.user_data.get("lift_waiting", set())
        app.user_data["lift_waiting"].add(uid)

        async def tick():
            try:
                remaining = LIFT_SECONDS
                while remaining > 0:
                    if not session_active(uid):
                        return
                    await show_screen(app, uid,
                        f"🛗 Лифт едет\\.\\.\\. осталось {remaining} сек\\.\n"
                        f"Цель: *{md(floor_name(new_floor))}*",
                        InlineKeyboardMarkup([
                            [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
                        ]))
                    await asyncio.sleep(1)
                    remaining -= 1
                if not session_active(uid):
                    return
                await enter_floor(app, uid, new_floor)
            except Exception:
                log.exception("lift tick")
            finally:
                app.user_data["lift_waiting"].discard(uid)

        app.create_task(tick())
    except Exception:
        log.exception("do_lift")

def context_user_lift_waiting(app, uid):
    return uid in app.user_data.get("lift_waiting", set())

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

async def start_battle(app, uid, p, m, forced_id=None):
    try:
        floor = p[3]
        hid, hname, hp, dmg, idle, photo, chase, deb_t, deb_v, deb_g, deb_r = pick_hollow(floor, forced_id)
        set_player(uid, battle_hp=p[2], battle_hollow_hp=hp, battle_damage=dmg,
                   battle_active=1, battle_hollow_id=hid, battle_idle_damage=idle)
        app.user_data.setdefault("battles", {})[uid] = {
            "chase": chase, "deb_type": deb_t, "deb_value": deb_v,
            "deb_grow": deb_g, "deb_rooms": deb_r, "hollow_id": hid
        }
        need = max(1, m[8] or 1)
        text = (
            f"👁️ *{md(hname)}* преграждает путь\\!\n"
            f"❤️ HP: {hp} · ⚔️ {dmg} · ⏳ {idle}/сек\n"
            f"♥️ Твоё здоровье: {p[2]}/{m[3]}\n"
            f"💥 Супер: {'готов' if p[12] else f'{p[13]}/{need}'}"
        )
        kb = battle_keyboard(p[12], p[13], need)
        await show_screen(app, uid, text, kb, photo=photo)
        pp = get_player(uid)
        app.create_task(idle_ticker(app, uid, pp[P_LOBBY_MSG], pp[P_LOBBY_CHAT]))
    except Exception:
        log.exception("start_battle")

async def idle_ticker(app, uid, msg_id, chat_id):
    await asyncio.sleep(2)
    while True:
        try:
            s = get_session(uid)
            if not s[S_ACTIVE]:
                return
            p = get_player(uid)
            if not p[10]:
                return
            if p[P_LOBBY_MSG] != msg_id:
                return
            m = current_miracle(uid)
            if not m:
                return
            idle = p[14] or 1
            new_hp = p[7] - idle
            if new_hp <= 0:
                await battle_defeat(app, uid)
                return
            set_player(uid, battle_hp=new_hp)
            s = get_session(uid)
            set_session(uid, damage_taken=s[S_TAKEN] + idle)
            need = max(1, m[8] or 1)
            text = (
                f"👁️ Холлоу бьёт, пока ты думаешь\\!\n"
                f"❤️ HP холлоу: {p[8]}\n"
                f"♥️ Твоё: {new_hp}/{m[3]}\n"
                f"💥 Супер: {'готов' if p[12] else f'{p[13]}/{need}'}\n\n"
                f"_−{idle} HP/сек_"
            )
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=text, parse_mode="MarkdownV2",
                    reply_markup=battle_keyboard(p[12], p[13], need))
            except Exception:
                pass
            await asyncio.sleep(1)
        except Exception:
            log.exception("idle_ticker")
            return

async def fight(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        p = get_player(uid)
        m = current_miracle(uid)
        if not m or not p[10] or not session_active(uid): return
        action = q.data.split(":")[1]
        need = max(1, m[8] or 1)

        p = get_player(uid)
        player_hp, hollow_hp, hollow_dmg = p[7], p[8], p[9]
        battle = context.application.user_data.get("battles", {}).get(uid, {})

        if action == "super":
            if not p[12]:
                await q.answer("Супер не готов", show_alert=True); return
            dmg = m[7]
        else:
            dmg = m[4]

        hollow_hp -= dmg
        s = get_session(uid)
        set_session(uid, damage_dealt=s[S_DEALT] + dmg)

        if hollow_hp <= 0:
            set_player(uid, hp=player_hp, battle_active=0, battle_hp=0, battle_hollow_hp=0,
                       discs=p[11] + 25, battle_hollow_id=0,
                       chase_remaining=0, chase_hollow_id=0)
            s = get_session(uid)
            set_session(uid, hollows_killed=s[S_KILLS] + 1)
            if battle.get("deb_type") and battle["deb_type"] != "none" and battle.get("deb_value", 0) > 0:
                apply_debuff(uid, battle)
            # после победы продолжаем на этаже
            await render_game(context.application, uid,
                text=f"✨ Победа\\! \\+25💿\n♥️ {player_hp}/{m[3]}\n\n" + floor_text(uid))
            return

        dealt = hollow_dmg
        player_hp -= dealt
        s = get_session(uid)
        set_session(uid, damage_taken=s[S_TAKEN] + dealt)

        if player_hp <= 0:
            await battle_defeat(context.application, uid)
            return

        new_ready = p[12]
        new_counter = p[13]
        if action == "super":
            new_ready = 0
            new_counter = 0
        elif not p[12]:
            new_counter += 1
            if new_counter >= need:
                new_ready = 1
                new_counter = 0

        set_player(uid, battle_hp=player_hp, battle_hollow_hp=hollow_hp,
                   super_ready=new_ready, super_counter=new_counter)
        text = (
            f"{'💥 Супер' if action == 'super' else '⚔️ Удар'}: −{dmg}\n"
            f"👁️ HP холлоу: {hollow_hp}\n"
            f"♥️ Твоё: {player_hp}/{m[3]}\n"
            f"Холлоу: −{dealt}\n"
            f"💥 Супер: {'готов' if new_ready else f'{new_counter}/{need}'}"
        )
        await render_game(context.application, uid, text=text,
            keyboard=battle_keyboard(new_ready, new_counter, need))
    except Exception:
        log.exception("fight")

def apply_debuff(uid, battle):
    try:
        dtype = battle.get("deb_type", "none")
        if dtype == "none" or battle.get("deb_value", 0) <= 0:
            return
        val = battle["deb_value"]
        rooms = battle.get("deb_rooms", 0)
        if rooms <= 0: return
        set_active_debuff(uid, {"type": dtype, "value": val, "rooms": rooms,
                                "grow": bool(battle.get("deb_grow"))})
    except Exception:
        log.exception("apply_debuff")

async def battle_defeat(app, uid):
    try:
        p = get_player(uid)
        m = current_miracle(uid)
        battle = app.user_data.get("battles", {}).get(uid, {})
        chase = battle.get("chase", 0)
        apply_debuff(uid, battle)
        base_hp = m[3] if m else 0
        set_player(uid, hp=base_hp, floor=0, door=0, battle_active=0,
                   battle_hp=0, battle_hollow_hp=0, super_ready=0, super_counter=0,
                   battle_hollow_id=0, chase_remaining=chase,
                   chase_hollow_id=battle.get("hollow_id", 0) if chase > 0 else 0)
        await finish_session(app, uid, "death")
    except Exception:
        log.exception("battle_defeat")

# ============ Инвентарь ============
async def session_inventory(update, context):
    try:
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
            rows.append([InlineKeyboardButton("🗝️ Ключ персонала", callback_data="use:staff_key")])
        if items.get("admin_key",0) > 0:
            rows.append([InlineKeyboardButton(f"🔑 Ключ админа ({items['admin_key']})", callback_data="use:admin_key")])
        rows.append([InlineKeyboardButton("↩️ К этажу", callback_data="resume")])
        await show_screen(context.application, uid,
            "🎒 *Инвентарь забега*\n" + md(item_text(items)),
            InlineKeyboardMarkup(rows))
    except Exception:
        log.exception("session_inventory")

async def use_item(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if not session_active(uid): return
        p = get_player(uid)
        items = item_dict(p[6])
        kind = q.data.split(":")[1]
        m = current_miracle(uid)
        if not m or items.get(kind, 0) <= 0:
            await q.answer("Нет такого предмета")
            return
        if kind == "plaster":
            if p[2] >= m[3]:
                await q.answer("HP полное")
                return
            set_player(uid, hp=min(m[3], p[2] + 50))
            items[kind] -= 1
            set_player(uid, items=items_string(items))
        elif kind == "magnifier":
            if get_session(uid)[S_RETURNING]:
                await q.answer("Не сейчас")
                return
            items[kind] -= 1
            set_player(uid, items=items_string(items))
            s = get_session(uid)
            peek = "🔍 Этаж кажется пустым." if s[S_FLOOR_SAFE] else "🔍 Возможны холлоу."
            await show_screen(context.application, uid,
                floor_text(uid) + f"\n\n{md(peek)}",
                floor_keyboard(uid))
            return
        elif kind == "helper":
            items[kind] -= 1
            set_player(uid, items=items_string(items), next_safe=1)
            # форсим безопасность этажа
            set_session(uid, floor_safe=1)
        elif kind in ("staff_key", "admin_key"):
            items[kind] -= 1
            set_player(uid, items=items_string(items), next_safe=1)
            set_session(uid, floor_safe=1)
        s = get_session(uid)
        set_session(uid, items_used=s[S_USED] + 1)
        await render_game(context.application, uid)
    except Exception:
        log.exception("use_item")

# ============ Выход ============
async def emergency_exit(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if not session_active(uid):
            await show_lobby(context.application, uid)
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Да, выйти", callback_data="exit_confirm2")],
            [InlineKeyboardButton("↩️ Остаться", callback_data="resume")],
        ])
        await show_screen(context.application, uid,
            "⚠️ *Экстренный выход*\nЗабег завершится\\. Продолжить?", kb)
    except Exception:
        log.exception("emergency_exit")

async def exit_confirm2(update, context):
    try:
        q = update.callback_query; await q.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Точно выйти", callback_data="exit_final")],
            [InlineKeyboardButton("↩️ Остаться", callback_data="resume")],
        ])
        await show_screen(context.application, q.from_user.id,
            "⚠️ Последнее подтверждение?", kb)
    except Exception:
        log.exception("exit_confirm2")

async def exit_final(update, context):
    try:
        q = update.callback_query; await q.answer()
        uid = q.from_user.id
        if not session_active(uid):
            await show_lobby(context.application, uid)
            return
        await finish_session(context.application, uid, "manual_exit")
    except Exception:
        log.exception("exit_final")

def reason_label(r):
    return {"manual_exit":"досрочный выход","death":"поражение",
            "victory":"победа","stop_command":"команда /stop",
            "out_of_steps":"кончились ходы"}.get(r,r)

async def finish_session(app, uid, reason):
    try:
        p = get_player(uid)
        m = current_miracle(uid)
        s = get_session(uid)
        earned = p[11] - s[S_START]
        text = (
            f"🏁 *Забег завершён*\n"
            f"Причина: {md(reason_label(reason))}\n\n"
            f"💿 Получено: *{earned:+d}*\n"
            f"👁️ Холлоу убито: {s[S_KILLS]}\n"
            f"🎁 Найдено: {s[S_FOUND]}\n"
            f"🩹 Использовано: {s[S_USED]}\n"
            f"⚔️ Урона нанесено: {s[S_DEALT]}\n"
            f"💔 Получено: {s[S_TAKEN]}\n"
            f"💰 Баланс: {p[11]}💿"
        )
        base_hp = m[3] if m else 0
        set_player(uid, hp=base_hp, floor=0, door=0, next_safe=0, items="",
                   battle_hp=0, battle_hollow_hp=0, battle_damage=0, battle_active=0,
                   super_ready=0, super_counter=0, battle_hollow_id=0,
                   battle_idle_damage=0, battle_msg_id=0, battle_chat_id=0,
                   chase_remaining=0, chase_hollow_id=0, debuff_json="")
        set_session(uid, active=0, msg_id=0, chat_id=0, exit_reason=reason,
                    shop_open=0, shop_bought="", steps_left=0,
                    returning=0, return_pos=0, depth=0, lever_found=0)
        battles = app.user_data.get("battles")
        if isinstance(battles, dict):
            battles.pop(uid, None)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ В лобби", callback_data="menu")],
        ])
        await show_screen(app, uid, text, kb)
    except Exception:
        log.exception("finish_session")
        try:
            set_session(uid, active=0, msg_id=0, chat_id=0)
            set_player(uid, battle_active=0)
            await show_lobby(app, uid)
        except Exception:
            pass

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
    if v is None:
        await update.message.reply_text("1–10:")
        return SUPER_CHARGES
    context.user_data["new_miracle"]["super_charges"] = v
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
                 d["price"], photo_id)
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
    await update.message.reply_text("Дебафф: poison / spawn_up / none (или /skip):"); return H_DEBUFF_TYPE

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
    await update.message.reply_text("На сколько комнат (1–50):"); return H_DEBUFF_ROOMS

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

# ============ Админка: скины ============
async def addskin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return ConversationHandler.END
    ms = get_miracles()
    if not ms:
        await update.message.reply_text("Сначала создай хотя бы одного миракла.")
        return ConversationHandler.END
    btns = [[InlineKeyboardButton(m[1], callback_data=f"skin_m:{m[0]}")] for m in ms]
    await update.message.reply_text("Для какого миракла скин?",
        reply_markup=InlineKeyboardMarkup(btns))
    return SKIN_MIRACLE

async def skin_pick_miracle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mid = int(q.data.split(":")[1])
    context.user_data["skin_mid"] = mid
    m = get_miracle(mid)
    await q.message.reply_text(f"Миракл: {m[1]}\nНазвание скина:")
    return SKIN_NAME

async def skin_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["skin_name"] = update.message.text.strip()[:60]
    await update.message.reply_text("Цена скина в 💿 (0–10000):")
    return SKIN_PRICE

async def skin_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 0, 10000)
    if v is None:
        await update.message.reply_text("Нужно число 0–10000:")
        return SKIN_PRICE
    context.user_data["skin_price"] = v
    await update.message.reply_text("Отправь фото скина (обязательно):")
    return SKIN_PHOTO

async def skin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото:")
        return SKIN_PHOTO
    photo_id = update.message.photo[-1].file_id
    mid = context.user_data.get("skin_mid")
    name = context.user_data.get("skin_name")
    price = context.user_data.get("skin_price")
    if not mid or not name or price is None:
        await update.message.reply_text("Ошибка, попробуй снова /addskin")
        context.user_data.pop("skin_mid", None)
        return ConversationHandler.END
    save_skin(mid, name, price, photo_id)
    context.user_data.pop("skin_mid", None)
    context.user_data.pop("skin_name", None)
    context.user_data.pop("skin_price", None)
    await update.message.reply_text(f"✅ Скин «{name}» добавлен!")
    return ConversationHandler.END

async def cancel_skin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("skin_mid", None)
    context.user_data.pop("skin_name", None)
    context.user_data.pop("skin_price", None)
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
              "super_damage","super_charges","price","photo_id"]
    btns = [[InlineKeyboardButton(f, callback_data=f"edit_mf:{f}")] for f in fields]
    btns.append([InlineKeyboardButton("↩️ Отмена", callback_data="cancel_edit")])
    await q.message.reply_text(f"Поля «{m[1]}»:", reply_markup=InlineKeyboardMarkup(btns))
    return EDIT_VALUE

async def edit_m_value_start(update, context):
    q = update.callback_query; await q.answer()
    field = q.data.split(":")[1]
    context.user_data["edit_field"] = field
    if field == "photo_id":
        await q.message.reply_text("Отправь новое фото миракла:")
    else:
        await q.message.reply_text(f"Новое значение для {field}:")
    return EDIT_VALUE

async def edit_m_value(update, context):
    mid = context.user_data.get("edit_mid")
    field = context.user_data.get("edit_field")
    if not mid or not field:
        await update.message.reply_text("Ошибка.")
        return ConversationHandler.END
    if field == "photo_id":
        if not update.message.photo:
            await update.message.reply_text("Нужно фото. Отправь или /cancel.")
            return EDIT_VALUE
        update_miracle_field(mid, "photo_id", update.message.photo[-1].file_id)
        await update.message.reply_text("✅ Фото миракла обновлено.")
        return ConversationHandler.END
    val = update.message.text.strip()
    if field in ("hp","damage","stamina","luck","super_damage","super_charges","price"):
        try: val = int(val)
        except ValueError:
            await update.message.reply_text("Число:"); return EDIT_VALUE
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
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Отменено.")
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

# ============ global error ============
async def global_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Global error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_user:
            uid = update.effective_user.id
            await context.bot.send_message(
                chat_id=uid,
                text="⚠️ Внутренняя ошибка\\. Попробуй ещё раз\\.",
                parse_mode="MarkdownV2")
    except Exception:
        pass

# ============ Планировщик магазина ============
async def daily_shop_loop(app):
    while True:
        try:
            refresh_daily_shop()
        except Exception:
            log.exception("daily_shop_loop")
        await asyncio.sleep(60)

async def post_init(app):
    try:
        refresh_daily_shop()
    except Exception:
        log.exception("post_init")
    app.user_data["lift_waiting"] = set()
    app.create_task(daily_shop_loop(app))

# ============ main ============
def main():
    if not TOKEN: raise RuntimeError("Нет TOKEN")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()

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
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
            PHOTO: [MessageHandler(filters.PHOTO, add_photo),
                    CommandHandler("skip", add_skip_photo)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_add),
            CommandHandler("start", start),
            CommandHandler("stop", cmd_stop),
            CommandHandler("lobby", cmd_lobby),
            CommandHandler("menu", cmd_menu),
        ], allow_reentry=True,
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
        fallbacks=[
            CommandHandler("cancel", cancel_hollow),
            CommandHandler("start", start),
            CommandHandler("stop", cmd_stop),
            CommandHandler("lobby", cmd_lobby),
            CommandHandler("menu", cmd_menu),
        ], allow_reentry=True,
    )

    edit_m_conv = ConversationHandler(
        entry_points=[CommandHandler("editmiracle", editmiracle_start)],
        states={
            EDIT_FIELD: [CallbackQueryHandler(edit_m_field, pattern="^edit_m:")],
            EDIT_VALUE: [
                CallbackQueryHandler(edit_m_value_start, pattern="^edit_mf:"),
                CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_m_value),
                MessageHandler(filters.PHOTO, edit_m_value),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_add),
            CommandHandler("start", start),
            CommandHandler("stop", cmd_stop),
            CommandHandler("lobby", cmd_lobby),
            CommandHandler("menu", cmd_menu),
        ], allow_reentry=True,
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
        fallbacks=[
            CommandHandler("cancel", cancel_hollow),
            CommandHandler("start", start),
            CommandHandler("stop", cmd_stop),
            CommandHandler("lobby", cmd_lobby),
            CommandHandler("menu", cmd_menu),
        ], allow_reentry=True,
    )

    skin_conv = ConversationHandler(
        entry_points=[CommandHandler("addskin", addskin_start)],
        states={
            SKIN_MIRACLE: [CallbackQueryHandler(skin_pick_miracle, pattern="^skin_m:")],
            SKIN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, skin_name)],
            SKIN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, skin_price)],
            SKIN_PHOTO: [MessageHandler(filters.PHOTO, skin_photo)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_skin),
            CommandHandler("start", start),
            CommandHandler("stop", cmd_stop),
        ], allow_reentry=True,
    )

    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("lobby", cmd_lobby))
    app.add_handler(CommandHandler("menu", cmd_menu))

    app.add_handler(miracle_conv)
    app.add_handler(hollow_conv)
    app.add_handler(edit_m_conv)
    app.add_handler(edit_h_conv)
    app.add_handler(skin_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("miracles", list_miracles))
    app.add_handler(CommandHandler("hollows", list_hollows))

    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(cmd_stop_inline, pattern="^cmd_stop_inline$"))
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

    # Новый цикл этажа
    app.add_handler(CallbackQueryHandler(floor_advance, pattern="^floor:advance$"))
    app.add_handler(CallbackQueryHandler(floor_to_lift, pattern="^floor:to_lift$"))
    app.add_handler(CallbackQueryHandler(return_step, pattern="^ret:"))
    app.add_handler(CallbackQueryHandler(lift, pattern="^lift$"))
    app.add_handler(CallbackQueryHandler(fight, pattern="^fight:"))
    app.add_handler(CallbackQueryHandler(session_inventory, pattern="^session_inventory$"))
    app.add_handler(CallbackQueryHandler(use_item, pattern="^use:"))

    app.add_handler(CallbackQueryHandler(shop_cb, pattern="^shop$"))
    app.add_handler(CallbackQueryHandler(shop_skin_view, pattern="^shop_skin:"))
    app.add_handler(CallbackQueryHandler(shop_buy_skin, pattern="^shop_buy_skin:"))
    app.add_handler(CallbackQueryHandler(shop_noop, pattern="^shop_noop$"))
    app.add_handler(CallbackQueryHandler(pick_skin, pattern="^pick_skin:"))
    app.add_handler(CallbackQueryHandler(apply_skin, pattern="^apply_skin:"))

    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.add_error_handler(global_error)
    log.info("WONDERWORLD started")
    app.run_polling()

if __name__ == "__main__":
    main()
