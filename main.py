import os
import random
import sqlite3
import asyncio
import logging
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

# Шаги создания миракла
NAME, DESCRIPTION, HP, DAMAGE, STAMINA, LUCK, SUPER_DAMAGE, SUPER_CHARGES, PRICE, PHOTO = range(10)
# Шаги создания холлоу
H_NAME, H_HP, H_DMG, H_IDLE, H_PHOTO = range(5)

# ---------- БД ----------

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
                super_charges INTEGER NOT NULL DEFAULT 0,
                price INTEGER NOT NULL DEFAULT 0,
                photo_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        mcols = [r[1] for r in con.execute("PRAGMA table_info(miracles)").fetchall()]
        for col, ddl in [
            ("super_damage", "ALTER TABLE miracles ADD COLUMN super_damage INTEGER NOT NULL DEFAULT 0"),
            ("super_charges", "ALTER TABLE miracles ADD COLUMN super_charges INTEGER NOT NULL DEFAULT 0"),
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
                photo_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
                super_charges INTEGER NOT NULL DEFAULT 0,
                battle_hollow_id INTEGER NOT NULL DEFAULT 0,
                battle_idle_damage INTEGER NOT NULL DEFAULT 0,
                battle_msg_id INTEGER NOT NULL DEFAULT 0,
                battle_chat_id INTEGER NOT NULL DEFAULT 0
            )
        """)
        pcols = [r[1] for r in con.execute("PRAGMA table_info(players)").fetchall()]
        for col, ddl in [
            ("discs", "ALTER TABLE players ADD COLUMN discs INTEGER NOT NULL DEFAULT 0"),
            ("super_charges", "ALTER TABLE players ADD COLUMN super_charges INTEGER NOT NULL DEFAULT 0"),
            ("battle_hollow_id", "ALTER TABLE players ADD COLUMN battle_hollow_id INTEGER NOT NULL DEFAULT 0"),
            ("battle_idle_damage", "ALTER TABLE players ADD COLUMN battle_idle_damage INTEGER NOT NULL DEFAULT 0"),
            ("battle_msg_id", "ALTER TABLE players ADD COLUMN battle_msg_id INTEGER NOT NULL DEFAULT 0"),
            ("battle_chat_id", "ALTER TABLE players ADD COLUMN battle_chat_id INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in pcols:
                con.execute(ddl)

        # сессия забега: одно сообщение, статистика матча
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
                exit_reason TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS player_miracles (
                user_id INTEGER NOT NULL,
                miracle_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, miracle_id)
            )
        """)
        con.commit()

# ---------- Мираклы ----------

def get_miracles():
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,description,hp,damage,stamina,luck,photo_id,super_damage,super_charges,price "
            "FROM miracles ORDER BY id"
        ).fetchall()

def get_miracle(mid):
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,description,hp,damage,stamina,luck,photo_id,super_damage,super_charges,price "
            "FROM miracles WHERE id=?", (mid,),
        ).fetchone()

def save_miracle(name, description, hp, damage, stamina, luck, super_damage, super_charges, price, photo_id):
    with closing(db()) as con:
        con.execute(
            """INSERT INTO miracles(name,description,hp,damage,stamina,luck,
                                    super_damage,super_charges,price,photo_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (name, description, hp, damage, stamina, luck, super_damage, super_charges, price, photo_id),
        )
        con.commit()

def is_unlocked(uid, mid):
    m = get_miracle(mid)
    if not m:
        return False
    if m[10] == 0:  # бесплатный
        return True
    with closing(db()) as con:
        row = con.execute(
            "SELECT 1 FROM player_miracles WHERE user_id=? AND miracle_id=?",
            (uid, mid),
        ).fetchone()
        return bool(row)

def unlock_miracle(uid, mid):
    with closing(db()) as con:
        con.execute(
            "INSERT OR IGNORE INTO player_miracles(user_id, miracle_id) VALUES(?,?)",
            (uid, mid),
        )
        con.commit()

# ---------- Холлоу ----------

def get_hollows():
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,hp,damage,idle_damage,photo_id FROM hollows ORDER BY id"
        ).fetchall()

def get_hollow(hid):
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,hp,damage,idle_damage,photo_id FROM hollows WHERE id=?",
            (hid,),
        ).fetchone()

def save_hollow(name, hp, damage, idle_damage, photo_id):
    with closing(db()) as con:
        con.execute(
            "INSERT INTO hollows(name,hp,damage,idle_damage,photo_id) VALUES(?,?,?,?,?)",
            (name, hp, damage, idle_damage, photo_id),
        )
        con.commit()

# ---------- Игрок ----------

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
    allowed = {
        "miracle_id","hp","floor","door","next_safe","items",
        "battle_hp","battle_hollow_hp","battle_damage","battle_active",
        "discs","super_charges","battle_hollow_id","battle_idle_damage",
        "battle_msg_id","battle_chat_id",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    assignments = ", ".join(f"{k}=?" for k in fields)
    with closing(db()) as con:
        con.execute(f"UPDATE players SET {assignments} WHERE user_id=?",
                    (*fields.values(), uid))
        con.commit()

# players columns:
# 0 user_id,1 miracle_id,2 hp,3 floor,4 door,5 next_safe,6 items,
# 7 battle_hp,8 battle_hollow_hp,9 battle_damage,10 battle_active,
# 11 discs,12 super_charges,13 battle_hollow_id,14 battle_idle_damage,
# 15 battle_msg_id,16 battle_chat_id

# ---------- Сессия ----------

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
               "damage_dealt","damage_taken","exit_reason"}
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
    set_session(uid,
        active=1, msg_id=msg_id, chat_id=chat_id, start_discs=start_discs,
        doors_opened=0, hollows_killed=0, dead_ends=0, items_found=0,
        items_used=0, damage_dealt=0, damage_taken=0, exit_reason="")

# ---------- Утилиты ----------

def sector(v, mx):
    return max(1, min(5, (max(1, v) - 1) * 5 // mx + 1))

def stat_bar(v, mx):
    return f"{sector(v, mx)}/5"

def miracle_card(m):
    # id,name,desc,hp,damage,stamina,luck,photo,sd,sc,price
    price = "Бесплатно" if m[10] == 0 else f"{m[10]}💿"
    return (
        f"✨ *{m[1]}*\n{m[2]}\n\n"
        f"♥️ Здоровье: {stat_bar(m[3], 750)}\n"
        f"⚔️ Урон: {stat_bar(m[4], 75)}\n"
        f"⚡️ Выносливость: {stat_bar(m[5], 300)}\n"
        f"🍀 Везучесть: {stat_bar(m[6], 300)}\n"
        f"💥 Суперудар: {m[8]} урона · {m[9]} зарядов\n"
        f"💿 Цена: {price}"
    )

def current_miracle(uid):
    p = get_player(uid)
    if not p[1]:
        return None
    return get_miracle(p[1])

def floor_name(f):
    if f == 0: return "ЗАДВЕРЬЕ"
    if f == 26: return "S-000"
    return f"Этаж {f}"

def hollow_spawn_chance(floor):
    # 5% на 1-м + 1% за этаж, потолок 60%
    if floor <= 0: return 0.0
    return min(60.0, 5.0 + 1.0 * (floor - 1))

def hollow_stats_for(floor):
    return HOLLOW_BASE_HP + (floor - 1) * 20, HOLLOW_BASE_DAMAGE + (floor - 1) * 3

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

def item_text(items):
    labels = {"plaster":"🩹 Пластырь","magnifier":"🔍 Лупа","helper":"📟 Хелпер"}
    if not items: return "Нет предметов"
    return "\n".join(f"{labels.get(k,k)} ×{v}" for k, v in items.items() if v > 0) or "Нет предметов"

# ---------- Клавиатуры ----------

def main_menu_keyboard(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Играть", callback_data="play")],
        [InlineKeyboardButton("✨ Мираклы", callback_data="list_miracles"),
         InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("📖 Правила", callback_data="rules")],
    ])

def play_choice_keyboard(uid):
    """Выбор миракла для забега — только разблокированные/бесплатные."""
    miracles = get_miracles()
    if not miracles:
        return None, "Мираклов пока нет."
    buttons = []
    any_avail = False
    for m in miracles:
        if is_unlocked(uid, m[0]):
            buttons.append([InlineKeyboardButton(f"▶️ {m[1]}", callback_data=f"play_pick:{m[0]}")])
            any_avail = True
    buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="menu")])
    text = "Выбери миракла для забега:"
    if not any_avail:
        text = "У тебя пока нет доступных мираклов. Загляни в «Мираклы» — бесплатные можно брать сразу."
    return InlineKeyboardMarkup(buttons), text

def miracle_list_keyboard(uid):
    """Список всех мираклов — разблокированные/бесплатные + заблокированные с ценой."""
    miracles = get_miracles()
    if not miracles:
        return None
    buttons = []
    for m in miracles:
        if is_unlocked(uid, m[0]):
            label = f"✅ {m[1]}"
            cb = f"view_miracle:{m[0]}"
        else:
            label = f"🔒 {m[1]} — {m[10]}💿"
            cb = f"unlock_prompt:{m[0]}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])
    buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)

def miracle_detail_keyboard(uid, mid):
    m = get_miracle(mid)
    if not m:
        return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Назад", callback_data="list_miracles")]])
    row = []
    if is_unlocked(uid, mid):
        row.append(InlineKeyboardButton("▶️ Играть", callback_data=f"play_pick:{mid}"))
    else:
        row.append(InlineKeyboardButton(f"🔓 Разблокировать ({m[10]}💿)", callback_data=f"unlock:{mid}"))
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton("↩️ Назад", callback_data="list_miracles")],
    ])

def game_keyboard(uid):
    """Клавиатура игрового забега — три двери / лифт / выход."""
    p = get_player(uid)
    if not p[10]:  # не в бою
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
    return battle_keyboard(get_player(uid)[12])

def battle_keyboard(charges):
    lbl = f"💥 Суперудар ({charges})" if charges > 0 else "💥 Суперудар (нет)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚔️ Атаковать", callback_data="fight:attack"),
         InlineKeyboardButton(lbl, callback_data="fight:super")],
        [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
    ])

def game_text(uid):
    p = get_player(uid)
    m = current_miracle(uid)
    if not m:
        return "Миракл не выбран."
    floor, door = p[3], p[4]
    max_doors = 5 if floor == 0 else 10
    if p[10]:
        return (
            f"👁️ Бой идёт!\n"
            f"❤️ HP холлоу: {p[8]}\n"
            f"♥️ Твоё здоровье: {p[7]}/{m[3]}\n"
            f"💥 Заряды супера: {p[12]}"
        )
    return (
        f"🌙 *{floor_name(floor)}* · дверь {door}/{max_doors}\n"
        f"♥️ {p[2]}/{m[3]} · 💿 {p[11]}\n"
        f"💥 Заряды супера: {p[12]}/{m[9]}\n\n"
        f"Куда пойдёшь?"
    )

# ---------- Редактор игрового сообщения ----------

async def render_game(app, uid, text=None, keyboard=None):
    """Редактирует существующее игровое сообщение забега или создаёт его."""
    s = get_session(uid)
    if text is None:
        text = game_text(uid)
    if keyboard is None:
        keyboard = game_keyboard(uid)
    if s[2] and s[3]:
        try:
            await app.bot.edit_message_text(
                chat_id=s[3], message_id=s[2],
                text=text, parse_mode="Markdown", reply_markup=keyboard,
            )
            return
        except Exception:
            pass
    # создать новое
    msg = await app.bot.send_message(
        chat_id=uid, text=text, parse_mode="Markdown", reply_markup=keyboard,
    )
    set_session(uid, msg_id=msg.message_id, chat_id=msg.chat_id)

# ---------- Главное меню ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_player(uid)
    get_session(uid)
    if session_active(uid):
        # если игрок в забеге — покажем игру
        await render_game(context.application, uid)
        return
    await update.message.reply_text(
        "🌙 *WONDERWORLD*\nДобро пожаловать в странное место за дверью.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(uid),
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if session_active(uid):
        await query.message.reply_text(
            "⚠️ Ты в забеге. Сначала выйди в лобби.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
            ]),
        )
        return
    await query.message.reply_text(
        "🌙 *Главное меню*",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(uid),
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = get_player(uid)
    with closing(db()) as con:
        unlocked_count = con.execute(
            "SELECT COUNT(*) FROM player_miracles WHERE user_id=?", (uid,),
        ).fetchone()[0]
    free_count = sum(1 for m in get_miracles() if m[10] == 0)
    await query.message.reply_text(
        "👤 *Профиль*\n"
        f"💿 Вондер-диски: *{p[11]}*\n"
        f"✨ Мираклов доступно: {unlocked_count + free_count}/{len(get_miracles())}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад", callback_data="menu")],
        ]),
    )

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📖 *Правила WONDERWORLD*\n"
        "• Играть → выбрать доступного миракла → забег.\n"
        "• ЗАДВЕРЬЕ: 5 дверей. Этажи 1–25: 10 дверей.\n"
        "• За открытую дверь +1💿. Тупик (5%) даёт 5–10💿 или предмет.\n"
        "• Холлоу: шанс 5% + 1% за этаж. Победа = +25💿.\n"
        "• Здоровье/предметы/заряды живут только в забеге. При выходе — сброс и статистика матча.\n"
        "• Лифт едет 10 сек. Экстренный выход — с двойным подтверждением.\n"
        "• Валюта общая на аккаунт и сохраняется между забегами.\n"
        "• Мираклы с ценой нужно разблокировать в меню «Мираклы».",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Назад", callback_data="menu")],
        ]),
    )

# ---------- Меню мираклов ----------

async def list_miracles_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    kb = miracle_list_keyboard(uid)
    if not kb:
        await query.message.reply_text("Мираклов пока нет.")
        return
    await query.message.reply_text(
        "✨ *Мираклы*\nВыбери для просмотра:",
        parse_mode="Markdown",
        reply_markup=kb,
    )

async def view_miracle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    mid = int(query.data.split(":")[1])
    m = get_miracle(mid)
    if not m:
        await query.message.reply_text("Миракл не найден.")
        return
    text = miracle_card(m)
    kb = miracle_detail_keyboard(uid, mid)
    if m[7]:
        await query.message.reply_photo(photo=m[7], caption=text, parse_mode="Markdown", reply_markup=kb)
    else:
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def unlock_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    mid = int(query.data.split(":")[1])
    m = get_miracle(mid)
    if not m:
        await query.message.reply_text("Миракл не найден.")
        return
    p = get_player(uid)
    text = miracle_card(m) + f"\n\n💿 У тебя: {p[11]}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔓 Разблокировать ({m[10]}💿)", callback_data=f"unlock:{mid}")],
        [InlineKeyboardButton("↩️ Назад", callback_data="list_miracles")],
    ])
    if m[7]:
        await query.message.reply_photo(photo=m[7], caption=text, parse_mode="Markdown", reply_markup=kb)
    else:
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def unlock_miracle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    mid = int(query.data.split(":")[1])
    m = get_miracle(mid)
    if not m:
        await query.message.reply_text("Миракл не найден.")
        return
    if is_unlocked(uid, mid):
        await query.message.reply_text("Уже разблокирован.")
        return
    p = get_player(uid)
    if p[11] < m[10]:
        await query.message.reply_text(
            f"Не хватает 💿: нужно {m[10]}, у тебя {p[11]}."
        )
        return
    set_player(uid, discs=p[11] - m[10])
    unlock_miracle(uid, mid)
    await query.message.reply_text(
        f"✅ Миракл «{m[1]}» разблокирован!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Играть", callback_data=f"play_pick:{mid}")],
            [InlineKeyboardButton("↩️ К мираклам", callback_data="list_miracles")],
        ]),
    )

# ---------- Играть ----------

async def play_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if session_active(uid):
        await query.message.reply_text(
            "Ты уже в забеге.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚪 Продолжить забег", callback_data="resume")],
            ]),
        )
        return
    kb, text = play_choice_keyboard(uid)
    await query.message.reply_text(text, reply_markup=kb)

async def resume_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not session_active(uid):
        await query.message.reply_text("Активного забега нет.")
        return
    await render_game(context.application, uid)

async def play_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    mid = int(query.data.split(":")[1])
    m = get_miracle(mid)
    if not m:
        await query.message.reply_text("Миракл не найден.")
        return
    if not is_unlocked(uid, mid):
        await query.message.reply_text("Сначала разблокируй миракла.")
        return
    if session_active(uid):
        await query.message.reply_text("Ты уже в забеге.")
        return
    p = get_player(uid)
    set_player(uid,
        miracle_id=mid, hp=m[3], floor=0, door=0, next_safe=0,
        items="", battle_hp=0, battle_hollow_hp=0, battle_active=0,
        super_charges=m[9], battle_hollow_id=0, battle_idle_damage=0,
        battle_msg_id=0, battle_chat_id=0,
    )
    reset_session_stats(uid, start_discs=p[11])
    set_session(uid, active=1)
    await render_game(context.application, uid)

# ---------- Экстренный выход с подтверждением ----------

async def emergency_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not session_active(uid):
        await query.message.reply_text("Забега нет.")
        return
    await query.message.reply_text(
        "⚠️ *Экстренный выход*\nЗабег будет завершён, здоровье/предметы/заряды сброшены. Продолжить?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Да, выйти", callback_data="exit_confirm2")],
            [InlineKeyboardButton("↩️ Остаться", callback_data="resume")],
        ]),
    )

async def exit_confirm2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    await query.message.reply_text(
        "⚠️ Последнее подтверждение: точно завершить забег?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Точно выйти", callback_data="exit_final")],
            [InlineKeyboardButton("↩️ Остаться", callback_data="resume")],
        ]),
    )

async def exit_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not session_active(uid):
        await query.message.reply_text("Забег уже завершён.")
        return
    await finish_session(context.application, uid, "manual_exit", chat_id=query.message.chat_id)

async def finish_session(app, uid, reason, chat_id=None):
    """Сбрасывает забег, показывает статистику матча."""
    p = get_player(uid)
    m = current_miracle(uid)
    s = get_session(uid)
    earned = p[11] - s[4]
    text = (
        "🏁 *Забег завершён*\n"
        f"Причина: {reason_label(reason)}\n\n"
        f"💿 Получено дисков: *{earned:+d}*\n"
        f"🚪 Открыто дверей: {s[5]}\n"
        f"👁️ Побеждено холлоу: {s[6]}\n"
        f"🔀 Тупиков: {s[7]}\n"
        f"🎁 Найдено предметов: {s[8]}\n"
        f"🩹 Использовано предметов: {s[9]}\n"
        f"⚔️ Нанесено урона: {s[10]}\n"
        f"💔 Получено урона: {s[11]}\n"
        f"💰 Баланс: {p[11]}💿"
    )
    # сброс игровых статов (валюта сохраняется)
    base_hp = m[3] if m else 0
    base_charges = m[9] if m else 0
    set_player(uid, hp=base_hp, floor=0, door=0, next_safe=0, items="",
               battle_hp=0, battle_hollow_hp=0, battle_damage=0, battle_active=0,
               super_charges=base_charges, battle_hollow_id=0,
               battle_idle_damage=0, battle_msg_id=0, battle_chat_id=0)
    set_session(uid, active=0, msg_id=0, chat_id=0, exit_reason=reason)
    try:
        await app.bot.send_message(
            chat_id=chat_id or uid, text=text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ В лобби", callback_data="menu")],
            ]),
        )
    except Exception:
        log.exception("Не удалось отправить статистику")

def reason_label(r):
    return {
        "manual_exit": "досрочный выход",
        "death": "поражение",
        "victory": "победа",
    }.get(r, r)

# ---------- Игровой забег: двери ----------

async def branch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not session_active(uid):
        await query.message.reply_text("Забег не активен.")
        return
    p = get_player(uid)
    m = current_miracle(uid)
    if not m or p[10]:
        return
    floor, door = p[3], p[4]
    if floor == 26:
        await render_game(context.application, uid)
        return
    max_doors = 5 if floor == 0 else 10
    if door >= max_doors:
        await render_game(context.application, uid)
        return

    # Лупа — предпросмотр
    if context.user_data.pop("peek_next", False):
        spawn = hollow_spawn_chance(floor) / 100
        peek = "🔍 Лупа: похоже, дверь пустая." if random.random() > spawn * 3 else "🔍 Лупа: возможны признаки холлоу."
        await render_game(context.application, uid, text=game_text(uid) + "\n\n" + peek)
        return

    items = item_dict(p[6])
    s = get_session(uid)
    set_session(uid, doors_opened=s[5] + 1)

    # ЗАДВЕРЬЕ: просто дверь, +1💿
    if floor == 0:
        set_player(uid, door=door + 1, discs=p[11] + 1)
        await render_game(context.application, uid)
        return

    # Тупик 5%
    if random.random() < 0.05 and not p[5]:
        reward_kind = random.choice(["discs_small", "discs_big", "item"])
        if reward_kind == "discs_small":
            delta = 5
            reward_text = "+5💿"
        elif reward_kind == "discs_big":
            delta = 10
            reward_text = "+10💿"
        else:
            delta = 0
            tool = random.choice(["plaster", "magnifier", "helper"])
            reward_text = {"plaster":"🩹 Пластырь","magnifier":"🔍 Лупа","helper":"📟 Хелпер"}[tool]
            items[tool] = items.get(tool, 0) + (3 if tool == "magnifier" else 1)
            set_session(uid, items_found=get_session(uid)[8] + 1)
        set_player(uid, door=door + 1, items=items_string(items), discs=p[11] + delta)
        s = get_session(uid)
        set_session(uid, dead_ends=s[7] + 1)
        await render_game(
            context.application, uid,
            text=f"🚪 *Тупик!* Награда: {reward_text}\n\n" + game_text(uid),
        )
        return

    # Холлоу?
    if p[5]:
        is_hollow = False
        set_player(uid, next_safe=0)
    else:
        is_hollow = random.random() < hollow_spawn_chance(floor) / 100

    if is_hollow:
        await start_battle(context.application, uid, floor, p, m)
        return

    # Обычная дверь +1💿
    set_player(uid, door=door + 1, discs=p[11] + 1)
    await render_game(context.application, uid)

# ---------- Бой ----------

def pick_hollow(floor):
    hs = get_hollows()
    base_hp, base_dmg = hollow_stats_for(floor)
    scale = max(1.0, base_hp / max(1, HOLLOW_BASE_HP))
    if not hs:
        return (0, "Холлоу", base_hp, base_dmg, max(1, base_dmg // 5), None)
    h = random.choice(hs)
    return (h[0], h[1], max(1, int(h[2] * scale)),
            max(1, int(h[3] * scale)), max(1, int(h[4] * scale)), h[5])

async def start_battle(app, uid, floor, p, m):
    hid, hname, hp, dmg, idle, photo = pick_hollow(floor)
    set_player(uid, door=p[4] + 1,
               battle_hp=p[2], battle_hollow_hp=hp, battle_damage=dmg,
               battle_active=1, battle_hollow_id=hid, battle_idle_damage=idle)
    s = get_session(uid)
    if photo:
        # Меняем игровое сообщение — фото не подменить edit_text'ом, поэтому шлём новое
        # и запоминаем как игровое.
        msg = await app.bot.send_photo(
            chat_id=uid, photo=photo,
            caption=f"👁️ *{hname}* преграждает путь!\n"
                    f"❤️ HP: {hp} · ⚔️ {dmg} · ⏳ {idle}/сек\n"
                    f"♥️ Твоё здоровье: {p[2]}/{m[3]} · 💥 {p[12]}",
            parse_mode="Markdown",
            reply_markup=battle_keyboard(p[12]),
        )
        set_session(uid, msg_id=msg.message_id, chat_id=msg.chat_id)
    else:
        await render_game(
            app, uid,
            text=f"👁️ *{hname}* преграждает путь!\n"
                 f"❤️ HP: {hp} · ⚔️ {dmg} · ⏳ {idle}/сек\n"
                 f"♥️ Твоё здоровье: {p[2]}/{m[3]} · 💥 {p[12]}",
            keyboard=battle_keyboard(p[12]),
        )
    # idle ticker
    app.create_task(idle_ticker(app, uid, get_session(uid)[2], get_session(uid)[3]))

async def idle_ticker(app, uid, msg_id, chat_id):
    await asyncio.sleep(2)
    while True:
        p = get_player(uid)
        if not p[10]:
            return
        s = get_session(uid)
        if s[2] != msg_id:
            return
        m = current_miracle(uid)
        if not m:
            return
        new_hp = p[7] - p[14]
        if new_hp <= 0:
            set_player(uid, hp=m[3], floor=0, door=0, battle_active=0,
                       battle_hp=0, battle_hollow_hp=0, battle_hollow_id=0,
                       super_charges=m[9])
            await finish_session(app, uid, "death", chat_id=chat_id)
            return
        set_player(uid, battle_hp=new_hp)
        s = get_session(uid)
        set_session(uid, damage_taken=s[11] + p[14])
        text = (
            f"👁️ Холлоу атакует, пока ты думаешь!\n"
            f"❤️ HP холлоу: {p[8]}\n"
            f"♥️ Твоё здоровье: {new_hp}/{m[3]}\n"
            f"💥 Заряды: {p[12]}\n\n_−{p[14]} HP/сек_"
        )
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, parse_mode="Markdown",
                reply_markup=battle_keyboard(p[12]),
            )
        except Exception:
            pass
        await asyncio.sleep(1)

async def fight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = get_player(uid)
    m = current_miracle(uid)
    if not m or not p[10] or not session_active(uid):
        return
    action = query.data.split(":")[1]
    player_hp, hollow_hp, hollow_damage = p[7], p[8], p[9]
    s = get_session(uid)

    if action == "super":
        if p[12] <= 0:
            return
        dmg = m[8]
        charges = p[12] - 1
    else:
        dmg = m[4]
        charges = p[12]

    hollow_hp -= dmg
    set_session(uid, damage_dealt=s[10] + dmg)

    if hollow_hp <= 0:
        set_player(uid, hp=player_hp, battle_active=0, battle_hp=0, battle_hollow_hp=0,
                   discs=p[11] + 25, super_charges=charges, battle_hollow_id=0)
        s = get_session(uid)
        set_session(uid, hollows_killed=s[6] + 1)
        await render_game(
            context.application, uid,
            text=f"✨ Победа! +25💿\n♥️ Здоровье: {player_hp}/{m[3]}\n\n" + game_text(uid),
        )
        return

    player_hp -= hollow_damage
    set_session(uid, damage_taken=s[11] + hollow_damage)
    if player_hp <= 0:
        set_player(uid, hp=m[3], floor=0, door=0, battle_active=0,
                   battle_hp=0, battle_hollow_hp=0, super_charges=m[9], battle_hollow_id=0)
        await finish_session(context.application, uid, "death", chat_id=get_session(uid)[3])
        return

    set_player(uid, battle_hp=player_hp, battle_hollow_hp=hollow_hp, super_charges=charges)
    text = (
        f"{'💥 Суперудар' if action == 'super' else '⚔️ Удар'}: −{dmg}\n"
        f"👁️ HP холлоу: {hollow_hp}\n"
        f"♥️ Твоё здоровье: {player_hp}/{m[3]}\n"
        f"Холлоу наносит {hollow_damage}.\n"
        f"💥 Заряды: {charges}"
    )
    await render_game(context.application, uid, text=text, keyboard=battle_keyboard(charges))

# ---------- Инвентарь забега ----------

async def session_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not session_active(uid):
        return
    p = get_player(uid)
    await query.message.reply_text(
        "🎒 *Инвентарь забега*\n" + item_text(item_dict(p[6])),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🩹 Пластырь", callback_data="use:plaster")],
            [InlineKeyboardButton("🔍 Лупа", callback_data="use:magnifier")],
            [InlineKeyboardButton("📟 Хелпер", callback_data="use:helper")],
            [InlineKeyboardButton("↩️ К забегу", callback_data="resume")],
        ]),
    )

async def use_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not session_active(uid):
        return
    p = get_player(uid)
    items = item_dict(p[6])
    kind = query.data.split(":")[1]
    m = current_miracle(uid)
    if not m or items.get(kind, 0) <= 0:
        await query.message.reply_text("Нет такого предмета.")
        return
    if kind == "plaster":
        if p[2] >= m[3]:
            await query.message.reply_text("Здоровье полное.")
            return
        set_player(uid, hp=min(m[3], p[2] + 50))
        items[kind] -= 1
        set_player(uid, items=items_string(items))
    elif kind == "magnifier":
        if p[3] in (0, 26):
            await query.message.reply_text("Здесь лупа не нужна.")
            return
        items[kind] -= 1
        set_player(uid, items=items_string(items))
        context.user_data["magnifier_ready"] = True
        context.user_data["peek_next"] = True
    elif kind == "helper":
        items[kind] -= 1
        set_player(uid, items=items_string(items), next_safe=1)
    s = get_session(uid)
    set_session(uid, items_used=s[9] + 1)
    await render_game(context.application, uid)

# ---------- Лифт ----------

async def lift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not session_active(uid):
        return
    p = get_player(uid)
    if p[10]:
        return
    floor, door = p[3], p[4]
    max_doors = 5 if floor == 0 else 10
    if floor == 26 or door < max_doors:
        await render_game(context.application, uid)
        return
    if context.user_data.get("lift_waiting"):
        return
    context.user_data["lift_waiting"] = True
    new_floor = 1 if floor == 0 else (26 if floor >= MAX_FLOOR else floor + 1)

    # обновляем игровое сообщение
    for left in range(LIFT_SECONDS - 1, -1, -1):
        await render_game(
            context.application, uid,
            text=f"🛗 Лифт едет… осталось {left} сек.\nЦель: *{floor_name(new_floor)}*",
            keyboard=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚪 Экстренный выход", callback_data="emergency_exit")],
            ]),
        )
        await asyncio.sleep(1)
    context.user_data["lift_waiting"] = False
    set_player(uid, floor=new_floor, door=0)
    await render_game(context.application, uid)

# ---------- Админка: мираклы ----------

async def add_miracle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return ConversationHandler.END
    context.user_data["new_miracle"] = {}
    await update.message.reply_text("Имя миракла:")
    return NAME

async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_miracle"]["name"] = update.message.text.strip()[:60]
    await update.message.reply_text("Описание:")
    return DESCRIPTION

async def add_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_miracle"]["description"] = update.message.text.strip()[:1000]
    await update.message.reply_text("HP (1–750):")
    return HP

async def add_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 750)
    if v is None:
        await update.message.reply_text("1–750:")
        return HP
    context.user_data["new_miracle"]["hp"] = v
    await update.message.reply_text("Урон (1–75):")
    return DAMAGE

async def add_damage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 75)
    if v is None:
        await update.message.reply_text("1–75:")
        return DAMAGE
    context.user_data["new_miracle"]["damage"] = v
    await update.message.reply_text("Выносливость (1–300):")
    return STAMINA

async def add_stamina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 300)
    if v is None:
        await update.message.reply_text("1–300:")
        return STAMINA
    context.user_data["new_miracle"]["stamina"] = v
    await update.message.reply_text("Везучесть (1–300):")
    return LUCK

async def add_luck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 300)
    if v is None:
        await update.message.reply_text("1–300:")
        return LUCK
    context.user_data["new_miracle"]["luck"] = v
    await update.message.reply_text("Урон суперудара (1–500):")
    return SUPER_DAMAGE

async def add_super_damage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 500)
    if v is None:
        await update.message.reply_text("1–500:")
        return SUPER_DAMAGE
    context.user_data["new_miracle"]["super_damage"] = v
    await update.message.reply_text("Заряды суперудара (1–10):")
    return SUPER_CHARGES

async def add_super_charges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 10)
    if v is None:
        await update.message.reply_text("1–10:")
        return SUPER_CHARGES
    context.user_data["new_miracle"]["super_charges"] = v
    await update.message.reply_text("Цена в 💿 (0 = бесплатно, до 10000):")
    return PRICE

async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 0, 10000)
    if v is None:
        await update.message.reply_text("0–10000:")
        return PRICE
    context.user_data["new_miracle"]["price"] = v
    await update.message.reply_text("Фото миракла или /skip:")
    return PHOTO

async def add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_new_miracle(update, context, update.message.photo[-1].file_id)
    return ConversationHandler.END

async def add_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_new_miracle(update, context, None)
    return ConversationHandler.END

async def save_new_miracle(update, context, photo_id):
    d = context.user_data.pop("new_miracle", {})
    if not d:
        await update.message.reply_text("Отменено.")
        return
    save_miracle(d["name"], d["description"], d["hp"], d["damage"], d["stamina"],
                 d["luck"], d["super_damage"], d["super_charges"], d["price"], photo_id)
    await update.message.reply_text(f"✅ Миракл «{d['name']}» сохранён!")

def parse_stat(text, low, high):
    try:
        v = int(text.strip())
        return v if low <= v <= high else None
    except (ValueError, AttributeError):
        return None

async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_miracle", None)
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ---------- Админка: холлоу ----------

async def add_hollow_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return ConversationHandler.END
    context.user_data["new_hollow"] = {}
    await update.message.reply_text("Имя холлоу:")
    return H_NAME

async def h_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_hollow"]["name"] = update.message.text.strip()[:60]
    await update.message.reply_text("Базовое HP (1–5000):")
    return H_HP

async def h_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 5000)
    if v is None:
        await update.message.reply_text("1–5000:")
        return H_HP
    context.user_data["new_hollow"]["hp"] = v
    await update.message.reply_text("Урон (1–500):")
    return H_DMG

async def h_dmg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 500)
    if v is None:
        await update.message.reply_text("1–500:")
        return H_DMG
    context.user_data["new_hollow"]["damage"] = v
    await update.message.reply_text("Пассивный урон/сек (1–100):")
    return H_IDLE

async def h_idle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 100)
    if v is None:
        await update.message.reply_text("1–100:")
        return H_IDLE
    context.user_data["new_hollow"]["idle"] = v
    await update.message.reply_text("Фото или /skip:")
    return H_PHOTO

async def h_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_new_hollow(update, context, update.message.photo[-1].file_id)
    return ConversationHandler.END

async def h_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_new_hollow(update, context, None)
    return ConversationHandler.END

async def save_new_hollow(update, context, photo_id):
    d = context.user_data.pop("new_hollow", {})
    if not d:
        await update.message.reply_text("Отменено.")
        return
    save_hollow(d["name"], d["hp"], d["damage"], d["idle"], photo_id)
    await update.message.reply_text(f"✅ Холлоу «{d['name']}» сохранён!")

async def cancel_hollow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_hollow", None)
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

async def list_miracles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ms = get_miracles()
    if not ms:
        await update.message.reply_text("Мираклов нет.")
        return
    await update.message.reply_text(
        "✨ *Мираклы:*\n" + "\n\n".join(
            f"• {m[1]} · ♥️ {stat_bar(m[3],750)} · ⚔️ {stat_bar(m[4],75)} · "
            f"💥 {m[8]}×{m[9]} · 💿 {m[10]}"
            for m in ms
        ),
        parse_mode="Markdown",
    )

async def list_hollows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hs = get_hollows()
    if not hs:
        await update.message.reply_text("Холлоу нет (используются стандартные).")
        return
    await update.message.reply_text(
        "👁️ *Холлоу:*\n" + "\n".join(
            f"• {h[1]} · HP {h[2]} · ⚔️ {h[3]} · ⏳ {h[4]}/сек" for h in hs
        ),
        parse_mode="Markdown",
    )

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши /start.")

def main():
    if not TOKEN:
        raise RuntimeError("Нет TELEGRAM_BOT_TOKEN.")
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
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
            PHOTO: [
                MessageHandler(filters.PHOTO, add_photo),
                CommandHandler("skip", add_skip_photo),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
        allow_reentry=True,
    )

    hollow_conv = ConversationHandler(
        entry_points=[CommandHandler("addhollow", add_hollow_start)],
        states={
            H_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_name)],
            H_HP: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_hp)],
            H_DMG: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_dmg)],
            H_IDLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, h_idle)],
            H_PHOTO: [
                MessageHandler(filters.PHOTO, h_photo),
                CommandHandler("skip", h_skip),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_hollow)],
        allow_reentry=True,
    )

    app.add_handler(miracle_conv)
    app.add_handler(hollow_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("miracles", list_miracles))
    app.add_handler(CommandHandler("hollows", list_hollows))

    # меню
    app.add_handler(CallbackQueryHandler(menu, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(rules, pattern="^rules$"))
    app.add_handler(CallbackQueryHandler(list_miracles_cb, pattern="^list_miracles$"))
    app.add_handler(CallbackQueryHandler(view_miracle, pattern="^view_miracle:"))
    app.add_handler(CallbackQueryHandler(unlock_prompt, pattern="^unlock_prompt:"))
    app.add_handler(CallbackQueryHandler(unlock_miracle_cb, pattern="^unlock:"))

    # играть
    app.add_handler(CallbackQueryHandler(play_cb, pattern="^play$"))
    app.add_handler(CallbackQueryHandler(play_pick, pattern="^play_pick:"))
    app.add_handler(CallbackQueryHandler(resume_game, pattern="^resume$"))

    # выход
    app.add_handler(CallbackQueryHandler(emergency_exit, pattern="^emergency_exit$"))
    app.add_handler(CallbackQueryHandler(exit_confirm2, pattern="^exit_confirm2$"))
    app.add_handler(CallbackQueryHandler(exit_final, pattern="^exit_final$"))

    # забег
    app.add_handler(CallbackQueryHandler(branch, pattern="^branch:"))
    app.add_handler(CallbackQueryHandler(fight, pattern="^fight:"))
    app.add_handler(CallbackQueryHandler(lift, pattern="^lift$"))
    app.add_handler(CallbackQueryHandler(session_inventory, pattern="^session_inventory$"))
    app.add_handler(CallbackQueryHandler(use_item, pattern="^use:"))

    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    log.info("WONDERWORLD bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
