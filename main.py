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
BREAK_SECONDS = 30

# Шаги создания миракла
NAME, DESCRIPTION, HP, DAMAGE, STAMINA, LUCK, SUPER_DAMAGE, SUPER_CHARGES, PHOTO = range(9)

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
                photo_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # миграция старых БД
        cols = [r[1] for r in con.execute("PRAGMA table_info(miracles)").fetchall()]
        if "super_damage" not in cols:
            con.execute("ALTER TABLE miracles ADD COLUMN super_damage INTEGER NOT NULL DEFAULT 0")
        if "super_charges" not in cols:
            con.execute("ALTER TABLE miracles ADD COLUMN super_charges INTEGER NOT NULL DEFAULT 0")

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
        con.commit()

# ---------- Мираклы ----------

def get_miracles():
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,description,hp,damage,stamina,luck,photo_id,super_damage,super_charges FROM miracles ORDER BY id"
        ).fetchall()

def get_miracle(mid):
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,description,hp,damage,stamina,luck,photo_id,super_damage,super_charges FROM miracles WHERE id=?",
            (mid,),
        ).fetchone()

def save_miracle(name, description, hp, damage, stamina, luck, super_damage, super_charges, photo_id):
    with closing(db()) as con:
        con.execute(
            """INSERT INTO miracles(name,description,hp,damage,stamina,luck,super_damage,super_charges,photo_id)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (name, description, hp, damage, stamina, luck, super_damage, super_charges, photo_id),
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

def get_player(user_id):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM players WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return row
        con.execute("INSERT INTO players(user_id) VALUES(?)", (user_id,))
        con.commit()
        return con.execute("SELECT * FROM players WHERE user_id=?", (user_id,)).fetchone()

def set_player(user_id, **fields):
    if not fields:
        return
    allowed = {
        "miracle_id", "hp", "floor", "door", "next_safe", "items",
        "battle_hp", "battle_hollow_hp", "battle_damage", "battle_active",
        "discs", "super_charges", "battle_hollow_id", "battle_idle_damage",
        "battle_msg_id", "battle_chat_id",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    assignments = ", ".join(f"{key}=?" for key in fields)
    with closing(db()) as con:
        con.execute(
            f"UPDATE players SET {assignments} WHERE user_id=?",
            (*fields.values(), user_id),
        )
        con.commit()

# players columns:
# 0 user_id, 1 miracle_id, 2 hp, 3 floor, 4 door, 5 next_safe, 6 items,
# 7 battle_hp, 8 battle_hollow_hp, 9 battle_damage, 10 battle_active,
# 11 discs, 12 super_charges, 13 battle_hollow_id, 14 battle_idle_damage,
# 15 battle_msg_id, 16 battle_chat_id

def sector(value, maximum):
    return max(1, min(5, (max(1, value) - 1) * 5 // maximum + 1))

def stat_bar(value, maximum):
    return f"{sector(value, maximum)}/5"

def miracle_card(m):
    # id,name,desc,hp,damage,stamina,luck,photo,super_damage,super_charges
    return (
        f"✨ *{m[1]}*\n{m[2]}\n\n"
        f"♥️ Здоровье: {stat_bar(m[3], 750)}\n"
        f"⚔️ Урон: {stat_bar(m[4], 75)}\n"
        f"⚡️ Выносливость: {stat_bar(m[5], 300)}\n"
        f"🍀 Везучесть: {stat_bar(m[6], 300)}\n"
        f"💥 Суперудар: {m[8]} урона · {m[9]} зарядов"
    )

def player_row(user_id):
    return get_player(user_id)

def current_miracle(user_id):
    p = player_row(user_id)
    if not p[1]:
        return None
    return get_miracle(p[1])

def floor_name(floor):
    if floor == 0:
        return "ЗАДВЕРЬЕ"
    if floor == 26:
        return "S-000"
    return f"Этаж {floor}"

def hollow_spawn_chance(floor):
    # 2.5% на 1-м, +0.5% за этаж, потолок 60%
    if floor <= 0:
        return 0.0
    return min(60.0, 2.5 + 0.5 * (floor - 1))

def hollow_stats_for(floor):
    return HOLLOW_BASE_HP + (floor - 1) * 20, HOLLOW_BASE_DAMAGE + (floor - 1) * 3

def safe_chance(floor, luck):
    return max(5, min(95, 10 + floor * 2.5 - (luck // 100)))

def item_dict(raw):
    result = {}
    if raw:
        for part in raw.split("|"):
            if ":" in part:
                key, value = part.split(":", 1)
                try:
                    result[key] = int(value)
                except ValueError:
                    pass
    return result

def items_string(items):
    return "|".join(f"{k}:{v}" for k, v in items.items() if v > 0)

def item_text(items):
    labels = {"plaster": "🩹 Пластырь", "magnifier": "🔍 Лупа", "helper": "📟 Хелпер"}
    if not items:
        return "Нет предметов"
    return "\n".join(f"{labels.get(k,k)} ×{v}" for k, v in items.items() if v > 0) or "Нет предметов"

# ---------- Клавиатуры ----------

def main_keyboard(user_id):
    p = player_row(user_id)
    if p[1]:
        floor, door = p[3], p[4]
        max_doors = 5 if floor == 0 else 10
        if floor == 26:
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("🎒 Инвентарь", callback_data="inventory"),
                 InlineKeyboardButton("✨ Мой миракл", callback_data="my_miracle")],
            ])
        if door >= max_doors:
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("🛗 Лифт", callback_data="lift")],
                [InlineKeyboardButton("🎒 Инвентарь", callback_data="inventory"),
                 InlineKeyboardButton("✨ Мой миракл", callback_data="my_miracle")],
            ])
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪", callback_data="branch:1"),
             InlineKeyboardButton("🚪", callback_data="branch:2"),
             InlineKeyboardButton("🚪", callback_data="branch:3")],
            [InlineKeyboardButton("🎒 Инвентарь", callback_data="inventory"),
             InlineKeyboardButton("✨ Мой миракл", callback_data="my_miracle")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ Выбрать миракла", callback_data="choose_miracle")],
        [InlineKeyboardButton("📖 Правила", callback_data="rules")],
    ])

def battle_keyboard(charges):
    super_label = f"💥 Суперудар ({charges})" if charges > 0 else "💥 Суперудар (нет)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚔️ Атаковать", callback_data="fight:attack"),
         InlineKeyboardButton(super_label, callback_data="fight:super")],
    ])

# ---------- Основные хэндлеры ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_player(user.id)
    await update.message.reply_text(
        "🌙 *WONDERWORLD*\nДобро пожаловать в странное место за дверью.\n"
        "Выбери миракла, чтобы начать путешествие.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(user.id),
    )

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📖 *Правила WONDERWORLD*\n"
        "• ЗАДВЕРЬЕ: 5 безопасных дверей, затем лифт.\n"
        "• Этажи 1–25: по 10 дверей. В конце — лифт.\n"
        "• Двери показаны как 🚪 🚪 🚪. Одна из них может вести в тупик (5%).\n"
        "• За открытую дверь — 10💿. За победу над холлоу — 25💿.\n"
        "• Тупик даёт награду: 10💿 / тулс / 15💿.\n"
        "• Холлоу появляется редко: 2.5% на 1 этаже +0.5% за этаж (потолок 60%).\n"
        "• Пока игрок думает в бою, холлоу бьёт пассивно (урон/сек, сообщение обновляется).\n"
        "• Суперудар миракла тратит заряды; бить и бить супером в один ход нельзя.\n"
        "• Лифт едет 30 сек с обратным отсчётом.\n\n"
        "Шанс безопасной двери: 10% + 2,5% за этаж − 1% за 100 везучести (5–95%).",
        parse_mode="Markdown",
    )

async def choose_miracle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    miracles = get_miracles()
    if not miracles:
        await query.message.reply_text("Пока мираклов нет. Дождись, пока администратор добавит первого.")
        return
    buttons = [[InlineKeyboardButton(m[1], callback_data=f"pick:{m[0]}")] for m in miracles]
    await query.message.reply_text("Выбери миракла:", reply_markup=InlineKeyboardMarkup(buttons))

async def pick_miracle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mid = int(query.data.split(":")[1])
    m = get_miracle(mid)
    if not m:
        await query.message.reply_text("Этот миракл больше не найден.")
        return
    uid = query.from_user.id
    set_player(uid, miracle_id=mid, hp=m[3], floor=0, door=0, next_safe=0,
               items="", battle_hp=0, battle_hollow_hp=0, battle_active=0,
               super_charges=m[9], battle_hollow_id=0, battle_idle_damage=0,
               battle_msg_id=0, battle_chat_id=0)
    caption = (
        "✨ Твой миракл выбран!\n\n" + miracle_card(m) +
        "\n\nТы появляешься в ЗАДВЕРЬЕ. Здесь холлоу не встречаются."
    )
    if m[7]:
        await query.message.reply_photo(
            photo=m[7], caption=caption, parse_mode="Markdown",
            reply_markup=main_keyboard(uid),
        )
    else:
        await query.message.reply_text(caption, parse_mode="Markdown", reply_markup=main_keyboard(uid))

async def my_miracle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    m = current_miracle(query.from_user.id)
    p = player_row(query.from_user.id)
    if not m:
        await query.message.reply_text("Сначала выбери миракла.")
        return
    caption = (
        miracle_card(m) +
        f"\n\nТекущее здоровье: {p[2]}/{m[3]}\n"
        f"💿 Вондер-диски: {p[11]}\n"
        f"💥 Заряды суперудара: {p[12]}/{m[9]}\n"
        f"Этаж: {floor_name(p[3])}, дверь: {p[4]}"
    )
    if m[7]:
        await query.message.reply_photo(photo=m[7], caption=caption, parse_mode="Markdown")
    else:
        await query.message.reply_text(caption, parse_mode="Markdown")

async def inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    p = player_row(query.from_user.id)
    await query.message.reply_text(
        "🎒 *Инвентарь*\n" + item_text(item_dict(p[6])) + f"\n\n💿 Вондер-диски: {p[11]}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🩹 Использовать пластырь", callback_data="use:plaster")],
            [InlineKeyboardButton("🔍 Осмотреть дверь (лупа)", callback_data="use:magnifier")],
            [InlineKeyboardButton("📟 Хелпер: безопасная дверь", callback_data="use:helper")],
        ]),
    )

async def use_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = player_row(uid)
    items = item_dict(p[6])
    kind = query.data.split(":")[1]
    m = current_miracle(uid)
    if not m:
        await query.message.reply_text("Сначала выбери миракла.")
        return
    if items.get(kind, 0) <= 0:
        await query.message.reply_text("Такого предмета в инвентаре нет.")
        return
    if kind == "plaster":
        if p[2] >= m[3]:
            await query.message.reply_text("Здоровье уже полное.")
            return
        set_player(uid, hp=min(m[3], p[2] + 50))
        items[kind] -= 1
        set_player(uid, items=items_string(items))
        await query.message.reply_text("🩹 Пластырь использован: +50 здоровья.")
    elif kind == "magnifier":
        if p[3] == 0 or p[3] == 26:
            await query.message.reply_text("Здесь лупа не нужна: этаж безопасный.")
            return
        items[kind] -= 1
        set_player(uid, items=items_string(items))
        context.user_data["magnifier_ready"] = True
        await query.message.reply_text("🔍 Лупа готова. Теперь открой любую дверь — бот покажет примерный исход.")
    elif kind == "helper":
        items[kind] -= 1
        set_player(uid, items=items_string(items), next_safe=1)
        await query.message.reply_text("📟 Хелпер активирован: следующая дверь будет безопасной.")
    await query.message.reply_text("🎒 Инвентарь обновлён.", reply_markup=main_keyboard(uid))

# ---------- Двери ----------

async def branch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = player_row(uid)
    m = current_miracle(uid)
    if not m or p[10]:
        await query.message.reply_text("Сейчас нельзя выбрать дверь.")
        return
    floor, door = p[3], p[4]
    if floor == 0:
        set_player(uid, door=door + 1, discs=p[11] + 10)
        await query.message.reply_text(
            f"🚪 Дверь {door + 1}/5 открыта. В ЗАДВЕРЬЕ безопасно. +10💿",
            reply_markup=main_keyboard(uid),
        )
        return
    if floor == 26:
        await query.message.reply_text("S-000 — безопасная зона.")
        return
    max_doors = 10
    if door >= max_doors:
        await query.message.reply_text("Двери этажа закончились. Лифт уже доступен.")
        return

    # Лупа — только предпросмотр, дверь не тратится
    if context.user_data.pop("peek_next", False):
        spawn = hollow_spawn_chance(floor) / 100
        await query.message.reply_text(
            "🔍 Лупа показывает: " +
            ("внутри могут быть признаки холлоу." if random.random() < spawn * 3 else "похоже, дверь пустая.")
        )
        return

    items = item_dict(p[6])

    # Тупик 5%
    if random.random() < 0.05 and not p[5]:
        if random.random() < 0.4:
            reward_text, ddelta, item_delta = "10💿", 10, None
        elif random.random() < 0.6:
            reward_text, ddelta, item_delta = "15💿", 15, None
        else:
            tool = random.choice(["plaster", "magnifier", "helper"])
            reward_text = {"plaster": "🩹 Пластырь", "magnifier": "🔍 Лупа", "helper": "📟 Хелпер"}[tool]
            ddelta, item_delta = 0, (tool, 3 if tool == "magnifier" else 1)
        if item_delta:
            items[item_delta[0]] = items.get(item_delta[0], 0) + item_delta[1]
        set_player(uid, door=door + 1, items=items_string(items), discs=p[11] + ddelta)
        await query.message.reply_text(
            f"🚪 ТУПИК! Дальше пути нет, но ты находишь: *{reward_text}*.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Выйти назад", callback_data="back")],
            ]),
        )
        return

    # Холлоу?
    if p[5]:
        is_hollow = False
        set_player(uid, next_safe=0)
    else:
        is_hollow = random.random() < hollow_spawn_chance(floor) / 100

    if is_hollow:
        return await start_battle(query, uid, floor, p, m)

    # Обычная безопасная дверь — 10💿
    set_player(uid, door=door + 1, discs=p[11] + 10)
    await query.message.reply_text(
        "✅ Дверь открыта. За ней пусто. +10💿",
        reply_markup=main_keyboard(uid),
    )

async def back_from_dead_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = player_row(uid)
    await query.message.reply_text(
        f"↩️ Ты вернулся. {floor_name(p[3])} · дверь {p[4]}/10.",
        reply_markup=main_keyboard(uid),
    )

# ---------- Бой ----------

def pick_hollow(floor):
    hollows = get_hollows()
    if not hollows:
        return None
    base_hp, base_dmg = hollow_stats_for(floor)
    h = random.choice(hollows)
    # масштабируем базовые статы шаблона по этажу
    scale = max(1.0, (base_hp / max(1, HOLLOW_BASE_HP)))
    hp = max(1, int(h[2] * scale))
    dmg = max(1, int(h[3] * scale))
    idle = max(1, int(h[4] * scale))
    return (h[0], h[1], hp, dmg, idle, h[5])

async def start_battle(query, uid, floor, p, m):
    h = pick_hollow(floor)
    if not h:
        # Если шаблонов нет — фолбэк на дефолтные статы
        hhp, hdmg = hollow_stats_for(floor)
        hid, hname, hp, dmg, idle, photo = 0, "Холлоу", hhp, hdmg, max(1, hdmg // 5), None
    else:
        hid, hname, hp, dmg, idle, photo = h

    set_player(
        uid, door=p[4] + 1,
        battle_hp=p[2], battle_hollow_hp=hp, battle_damage=dmg,
        battle_active=1, battle_hollow_id=hid, battle_idle_damage=idle,
    )
    text = (
        f"👁️ *{hname}* преграждает путь!\n"
        f"❤️ HP холлоу: {hp}\n"
        f"⚔️ Урон: {dmg}\n"
        f"⏳ Пассивный урон/сек: {idle}\n"
        f"♥️ Твоё здоровье: {p[2]}/{m[3]}\n"
        f"💥 Заряды супера: {p[12]}"
    )
    kb = battle_keyboard(p[12])
    if photo:
        msg = await query.message.reply_photo(photo=photo, caption=text, parse_mode="Markdown", reply_markup=kb)
    else:
        msg = await query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    set_player(uid, battle_msg_id=msg.message_id, battle_chat_id=msg.chat_id)
    # запускаем idle-таймер
    context.application.create_task(idle_ticker(context.application, uid, msg.chat_id, msg.message_id))

async def idle_ticker(app, uid, chat_id, msg_id):
    """Каждую секунду отнимает idle_damage, пока бой активен и игрок молчит."""
    await asyncio.sleep(2)  # небольшой «подарок» игроку на первый ход
    while True:
        p = player_row(uid)
        if not p[10]:  # бой не активен
            return
        if p[15] != msg_id:
            return
        m = current_miracle(uid)
        if not m:
            return
        new_hp = p[7] - p[14]
        hh = p[8]
        if new_hp <= 0:
            set_player(uid, hp=m[3], floor=0, door=0, battle_active=0,
                       battle_hp=0, battle_hollow_hp=0, battle_hollow_id=0,
                       super_charges=m[9])
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text="💫 Холлоу одолел тебя, пока ты медлил. Возврат в ЗАДВЕРЬЕ; здоровье восстановлено.",
                )
            except Exception:
                pass
            try:
                await app.bot.send_message(chat_id=chat_id, text="Ты снова в ЗАДВЕРЬЕ.",
                                           reply_markup=main_keyboard(uid))
            except Exception:
                pass
            return
        set_player(uid, battle_hp=new_hp)
        text = (
            f"👁️ Холлоу атакует, пока ты думаешь!\n"
            f"❤️ HP холлоу: {hh}\n"
            f"♥️ Твоё здоровье: {new_hp}/{m[3]}\n"
            f"💥 Заряды супера: {p[12]}\n\n"
            f"_−{p[14]} HP/сек_"
        )
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text,
                parse_mode="Markdown", reply_markup=battle_keyboard(p[12]),
            )
        except Exception:
            pass
        await asyncio.sleep(1)

async def fight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = player_row(uid)
    m = current_miracle(uid)
    if not m or not p[10]:
        await query.message.reply_text("Сейчас боя нет.")
        return
    action = query.data.split(":")[1]

    player_hp, hollow_hp, hollow_damage = p[7], p[8], p[9]

    if action == "super":
        if p[12] <= 0:
            await query.message.reply_text("Суперудар недоступен: заряды кончились.")
            return
        dmg = m[8]
        charges = p[12] - 1
    else:
        dmg = m[4]
        charges = p[12]

    hollow_hp -= dmg
    if hollow_hp <= 0:
        set_player(uid, hp=player_hp, battle_active=0, battle_hp=0, battle_hollow_hp=0,
                   discs=p[11] + 25, super_charges=charges, battle_hollow_id=0)
        try:
            await query.message.edit_text(
                f"✨ Победа! Холлоу исчез. +25💿\n♥️ Осталось здоровья: {player_hp}/{m[3]}",
                reply_markup=main_keyboard(uid),
            )
        except Exception:
            pass
        return

    player_hp -= hollow_damage
    if player_hp <= 0:
        set_player(uid, hp=m[3], floor=0, door=0, battle_active=0,
                   battle_hp=0, battle_hollow_hp=0, super_charges=m[9], battle_hollow_id=0)
        try:
            await query.message.edit_text(
                "💫 Миракл пал. Возврат в ЗАДВЕРЬЕ; здоровье восстановлено, прогресс этажа сброшен.",
                reply_markup=main_keyboard(uid),
            )
        except Exception:
            pass
        return

    set_player(uid, battle_hp=player_hp, battle_hollow_hp=hollow_hp, super_charges=charges)
    text = (
        f"{'💥 Суперудар' if action == 'super' else '⚔️ Удар'}: −{dmg} HP холлоу.\n"
        f"👁️ HP холлоу: {hollow_hp}\n"
        f"♥️ Твоё здоровье: {player_hp}/{m[3]}\n"
        f"Холлоу отвечает и наносит {hollow_damage}.\n"
        f"💥 Заряды супера: {charges}"
    )
    try:
        await query.message.edit_text(text, reply_markup=battle_keyboard(charges))
    except Exception:
        await query.message.reply_text(text, reply_markup=battle_keyboard(charges))

# ---------- Лифт ----------

async def lift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = player_row(uid)
    if not p[1]:
        await query.message.reply_text("Сначала выбери миракла.")
        return
    if p[10]:
        await query.message.reply_text("Нельзя войти в лифт во время боя.")
        return
    floor, door = p[3], p[4]
    max_doors = 5 if floor == 0 else 10
    if floor == 26:
        await query.message.reply_text("🛡️ Ты уже на S-000, безопасном этаже.")
        return
    if door < max_doors:
        await query.message.reply_text(f"Лифт появится после прохождения всех {max_doors} дверей.")
        return
    if context.user_data.get("lift_waiting"):
        await query.message.reply_text("Лифт уже готовится. Подожди ещё немного.")
        return
    context.user_data["lift_waiting"] = True
    msg = await query.message.reply_text(f"🛗 Двери лифта закрываются. Следующий этаж через {BREAK_SECONDS} секунд…")

    async def countdown():
        try:
            for left in range(BREAK_SECONDS - 1, -1, -1):
                await asyncio.sleep(1)
                try:
                    await msg.edit_text(f"🛗 Лифт едет… осталось {left} сек.")
                except Exception:
                    pass
            context.user_data["lift_waiting"] = False
            new_floor = 1 if floor == 0 else (26 if floor >= MAX_FLOOR else floor + 1)
            set_player(uid, floor=new_floor, door=0)
            await msg.edit_text(
                f"🛗 Лифт прибыл: *{floor_name(new_floor)}*.",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid),
            )
        except Exception:
            log.exception("Ошибка отсчёта лифта")
            context.user_data["lift_waiting"] = False

    context.application.create_task(countdown())

# ---------- Админка: мираклы ----------

async def add_miracle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Эта команда доступна только администратору.")
        return ConversationHandler.END
    context.user_data["new_miracle"] = {}
    await update.message.reply_text("Введи имя миракла:")
    return NAME

async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_miracle"]["name"] = update.message.text.strip()[:60]
    await update.message.reply_text("Введи описание миракла:")
    return DESCRIPTION

async def add_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_miracle"]["description"] = update.message.text.strip()[:1000]
    await update.message.reply_text("Здоровье (1–750):")
    return HP

async def add_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 750)
    if v is None:
        await update.message.reply_text("Нужно число от 1 до 750. Попробуй ещё раз:")
        return HP
    context.user_data["new_miracle"]["hp"] = v
    await update.message.reply_text("Урон (1–75):")
    return DAMAGE

async def add_damage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 75)
    if v is None:
        await update.message.reply_text("Нужно число от 1 до 75. Попробуй ещё раз:")
        return DAMAGE
    context.user_data["new_miracle"]["damage"] = v
    await update.message.reply_text("Выносливость (1–300):")
    return STAMINA

async def add_stamina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 300)
    if v is None:
        await update.message.reply_text("Нужно число от 1 до 300. Попробуй ещё раз:")
        return STAMINA
    context.user_data["new_miracle"]["stamina"] = v
    await update.message.reply_text("Везучесть (1–300):")
    return LUCK

async def add_luck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 300)
    if v is None:
        await update.message.reply_text("Нужно число от 1 до 300. Попробуй ещё раз:")
        return LUCK
    context.user_data["new_miracle"]["luck"] = v
    await update.message.reply_text("Урон суперудара (1–500):")
    return SUPER_DAMAGE

async def add_super_damage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 500)
    if v is None:
        await update.message.reply_text("Нужно число от 1 до 500. Попробуй ещё раз:")
        return SUPER_DAMAGE
    context.user_data["new_miracle"]["super_damage"] = v
    await update.message.reply_text("Сколько зарядов суперудара (1–10):")
    return SUPER_CHARGES

async def add_super_charges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 10)
    if v is None:
        await update.message.reply_text("Нужно число от 1 до 10. Попробуй ещё раз:")
        return SUPER_CHARGES
    context.user_data["new_miracle"]["super_charges"] = v
    await update.message.reply_text("Отправь фото миракла или /skip:")
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
        await update.message.reply_text("Создание отменено.")
        return
    save_miracle(d["name"], d["description"], d["hp"], d["damage"], d["stamina"], d["luck"],
                 d["super_damage"], d["super_charges"], photo_id)
    await update.message.reply_text(
        f"✅ Миракл «{d['name']}» сохранён!",
        reply_markup=main_keyboard(update.effective_user.id),
    )

def parse_stat(text, low, high):
    try:
        v = int(text.strip())
        return v if low <= v <= high else None
    except (ValueError, AttributeError):
        return None

async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_miracle", None)
    await update.message.reply_text("Создание миракла отменено.")
    return ConversationHandler.END

# ---------- Админка: холлоу ----------

H_NAME, H_HP, H_DMG, H_IDLE, H_PHOTO = range(5)

async def add_hollow_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Только для админа.")
        return ConversationHandler.END
    context.user_data["new_hollow"] = {}
    await update.message.reply_text("Имя холлоу:")
    return H_NAME

async def h_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_hollow"]["name"] = update.message.text.strip()[:60]
    await update.message.reply_text("Базовое HP холлоу (1–5000):")
    return H_HP

async def h_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 5000)
    if v is None:
        await update.message.reply_text("Число от 1 до 5000:")
        return H_HP
    context.user_data["new_hollow"]["hp"] = v
    await update.message.reply_text("Базовый урон (1–500):")
    return H_DMG

async def h_dmg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 500)
    if v is None:
        await update.message.reply_text("Число от 1 до 500:")
        return H_DMG
    context.user_data["new_hollow"]["damage"] = v
    await update.message.reply_text("Пассивный урон в секунду бездействия (1–100):")
    return H_IDLE

async def h_idle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_stat(update.message.text, 1, 100)
    if v is None:
        await update.message.reply_text("Число от 1 до 100:")
        return H_IDLE
    context.user_data["new_hollow"]["idle"] = v
    await update.message.reply_text("Отправь фото холлоу или /skip:")
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
    await update.message.reply_text("Создание холлоу отменено.")
    return ConversationHandler.END

async def list_miracles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    miracles = get_miracles()
    if not miracles:
        await update.message.reply_text("Мираклов пока нет.")
        return
    await update.message.reply_text(
        "✨ *Мираклы:*\n" + "\n\n".join(
            f"• {m[1]} · ♥️ {stat_bar(m[3],750)} · ⚔️ {stat_bar(m[4],75)} · "
            f"💥 {m[8]}×{m[9]} · ⚡️ {stat_bar(m[5],300)} · 🍀 {stat_bar(m[6],300)}"
            for m in miracles
        ),
        parse_mode="Markdown",
    )

async def list_hollows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hs = get_hollows()
    if not hs:
        await update.message.reply_text("Холлоу пока нет (используются стандартные статы).")
        return
    await update.message.reply_text(
        "👁️ *Холлоу:*\n" + "\n".join(
            f"• {h[1]} · HP {h[2]} · ⚔️ {h[3]} · ⏳ {h[4]}/сек" for h in hs
        ),
        parse_mode="Markdown",
    )

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не понял. Напиши /start.")

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
    app.add_handler(CallbackQueryHandler(choose_miracle, pattern="^choose_miracle$"))
    app.add_handler(CallbackQueryHandler(pick_miracle, pattern="^pick:"))
    app.add_handler(CallbackQueryHandler(my_miracle, pattern="^my_miracle$"))
    app.add_handler(CallbackQueryHandler(inventory, pattern="^inventory$"))
    app.add_handler(CallbackQueryHandler(use_item, pattern="^use:"))
    app.add_handler(CallbackQueryHandler(branch, pattern="^branch:"))
    app.add_handler(CallbackQueryHandler(back_from_dead_end, pattern="^back$"))
    app.add_handler(CallbackQueryHandler(fight, pattern="^fight:"))
    app.add_handler(CallbackQueryHandler(lift, pattern="^lift$"))
    app.add_handler(CallbackQueryHandler(rules, pattern="^rules$"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    log.info("WONDERWORLD bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
