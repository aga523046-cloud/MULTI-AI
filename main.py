import os
import random
import sqlite3
import logging
from contextlib import closing

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

# FadeHost → Environment:
# TELEGRAM_BOT_TOKEN = 8933408488:AAG1B7v_CceksmGxvnJMkZA15p0spsl6acE
# OWNER_ID = 7783222972 (замени, если твой актуальный ID изменился)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "7783222972"))
DB_PATH = os.getenv("DB_PATH", "wonderworld.sqlite3")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("wonderworld")

# Статы холлоу растут по этажам. Это базовые значения для 1-го этажа.
HOLLOW_BASE_HP = 100
HOLLOW_BASE_DAMAGE = 15
MAX_FLOOR = 25
BREAK_SECONDS = 30

# Шаги создания миракла
NAME, DESCRIPTION, HP, DAMAGE, STAMINA, LUCK, PHOTO = range(7)

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
                battle_active INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.commit()

def get_miracles():
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,description,hp,damage,stamina,luck,photo_id FROM miracles ORDER BY id"
        ).fetchall()

def get_miracle(mid):
    with closing(db()) as con:
        return con.execute(
            "SELECT id,name,description,hp,damage,stamina,luck,photo_id FROM miracles WHERE id=?",
            (mid,),
        ).fetchone()

def save_miracle(name, description, hp, damage, stamina, luck, photo_id):
    with closing(db()) as con:
        con.execute(
            """INSERT INTO miracles(name,description,hp,damage,stamina,luck,photo_id)
               VALUES(?,?,?,?,?,?,?)""",
            (name, description, hp, damage, stamina, luck, photo_id),
        )
        con.commit()

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
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    assignments = ", ".join(f"{key}=?" for key in fields)
    with closing(db()) as con:
        con.execute(
            f"UPDATE players SET {assignments} WHERE user_id=?",
            (*fields.values(), user_id),
        )
        con.commit()

def sector(value, maximum):
    """Делит диапазон 1..maximum на 5 равных секторов."""
    return max(1, min(5, (max(1, value) - 1) * 5 // maximum + 1))

def stat_bar(value, maximum):
    return f"{sector(value, maximum)}/5"

def miracle_card(m):
    mid, name, description, hp, damage, stamina, luck, photo_id = m
    return (
        f"✨ *{name}*\n{description}\n\n"
        f"♥️ Здоровье: {stat_bar(hp, 750)}\n"
        f"⚔️ Урон: {stat_bar(damage, 75)}\n"
        f"⚡️ Выносливость: {stat_bar(stamina, 300)}\n"
        f"🍀 Везучесть: {stat_bar(luck, 300)}"
    )

def player_row(user_id):
    row = get_player(user_id)
    # columns: user_id, miracle_id, hp, floor, door, next_safe, items,
    # battle_hp, battle_hollow_hp, battle_damage, battle_active
    return row

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

def hollow_stats(floor):
    # У холлоу есть запас здоровья и урон; с этажом они усиливаются.
    return HOLLOW_BASE_HP + (floor - 1) * 20, HOLLOW_BASE_DAMAGE + (floor - 1) * 3

def safe_chance(floor, luck):
    # По твоему правилу: базовые 10% + 2.5% за этаж, -1% за каждые 100 везучести.
    # Значение ограничено 5..95%, чтобы игра не ломалась на крайних этажах.
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

def main_keyboard(user_id):
    p = player_row(user_id)
    if p[1]:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚪 Исследовать", callback_data="explore")],
            [InlineKeyboardButton("🎒 Инвентарь", callback_data="inventory"),
             InlineKeyboardButton("✨ Мой миракл", callback_data="my_miracle")],
            [InlineKeyboardButton("🛗 Лифт", callback_data="lift")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ Выбрать миракла", callback_data="choose_miracle")],
        [InlineKeyboardButton("📖 Правила", callback_data="rules")],
    ])

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
        "• Этажи 1–25: по 10 дверей, у каждой три развилки.\n"
        "• В конце этажа доступен лифт. Переход занимает 30 секунд.\n"
        "• После 25-го этажа находится безопасный этаж S-000.\n"
        "• В каждой развилке может встретиться холлоу. Если его нет, есть шанс найти предмет.\n"
        "• Бой пошаговый: атакуй холлоу или попробуй отступить.\n\n"
        "Шанс безопасной двери: 10% + 2,5% за этаж − 1% за каждые 100 везучести.\n"
        "Это правило сейчас реализовано буквально; шанс ограничен 5–95%.",
        parse_mode="Markdown",
    )

async def choose_miracle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    miracles = get_miracles()
    if not miracles:
        await query.message.reply_text("Пока мираклов нет. Дождись, пока администратор добавит первого.")
        return
    buttons = [
        [InlineKeyboardButton(m[1], callback_data=f"pick:{m[0]}")]
        for m in miracles
    ]
    await query.message.reply_text(
        "Выбери миракла:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

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
               items="", battle_hp=0, battle_hollow_hp=0, battle_active=0)
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
        await query.message.reply_text(
            caption, parse_mode="Markdown", reply_markup=main_keyboard(uid),
        )

async def my_miracle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    m = current_miracle(query.from_user.id)
    p = player_row(query.from_user.id)
    if not m:
        await query.message.reply_text("Сначала выбери миракла.")
        return
    caption = (
        miracle_card(m) + f"\n\nТекущее здоровье: {p[2]}/{m[3]}\n"
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
        "🎒 *Инвентарь*\n" + item_text(item_dict(p[6])),
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
        await query.message.reply_text("🔍 Лупа готова. Теперь нажми «Исследовать» и выбери развилку: бот покажет её исход до входа.")
    elif kind == "helper":
        items[kind] -= 1
        set_player(uid, items=items_string(items), next_safe=1)
        await query.message.reply_text("📟 Хелпер активирован: следующая выбранная дверь будет безопасной.")
    await query.message.reply_text("🎒 Инвентарь обновлён.", reply_markup=main_keyboard(uid))

async def explore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = player_row(uid)
    if not p[1]:
        await query.message.reply_text("Сначала выбери миракла.")
        return
    if p[10]:
        await query.message.reply_text("Сначала заверши бой с холлоу.")
        return
    floor, door = p[3], p[4]
    if floor == 26:
        await query.message.reply_text("🛡️ S-000 — безопасная зона. Здесь можно отдыхать сколько угодно.")
        return
    max_doors = 5 if floor == 0 else 10
    if door >= max_doors:
        await query.message.reply_text("Двери этажа закончились. Нажми «Лифт», чтобы перейти дальше.")
        return
    if context.user_data.get("magnifier_ready"):
        context.user_data["magnifier_ready"] = False
        context.user_data["peek_next"] = True
    buttons = [
        [InlineKeyboardButton(f"↖️ Развилка 1", callback_data="branch:1"),
         InlineKeyboardButton(f"⬆️ Развилка 2", callback_data="branch:2"),
         InlineKeyboardButton(f"↗️ Развилка 3", callback_data="branch:3")]
    ]
    await query.message.reply_text(
        f"🚪 *{floor_name(floor)} · дверь {door + 1}/{max_doors}*\n"
        "Перед тобой три развилки. Куда пойдёшь?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def branch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = player_row(uid)
    m = current_miracle(uid)
    if not m or p[10]:
        await query.message.reply_text("Сейчас нельзя выбрать развилку.")
        return
    floor, door = p[3], p[4]
    if floor == 0:
        set_player(uid, door=door + 1)
        await query.message.reply_text(
            f"🚪 Дверь {door + 1}/5 открыта. В ЗАДВЕРЬЕ безопасно.",
            reply_markup=main_keyboard(uid),
        )
        return
    if floor == 26:
        await query.message.reply_text("S-000 — безопасная зона.")
        return

    # Лупа показывает только примерный исход выбранной развилки и не запускает её.
    if context.user_data.pop("peek_next", False):
        safe = random.random() < safe_chance(floor, m[6]) / 100
        await query.message.reply_text(
            "🔍 За этой развилкой, по показанию лупы: " +
            ("похоже, безопасно." if safe else "обнаружены признаки холлоу.") +
            "\nТеперь нажми «Исследовать» и выбери развилку заново."
        )
        return

    items = item_dict(p[6])
    if p[5]:
        is_hollow = False
        set_player(uid, next_safe=0)
    else:
        is_hollow = random.random() >= safe_chance(floor, m[6]) / 100

    if is_hollow:
        hhp, hdmg = hollow_stats(floor)
        set_player(uid, door=door + 1, battle_hp=p[2], battle_hollow_hp=hhp,
                   battle_damage=hdmg, battle_active=1)
        await query.message.reply_text(
            f"👁️ *ХОЛЛОУ!* Он преграждает путь.\n"
            f"❤️ Здоровье холлоу: {hhp}\n"
            f"⚔️ Урон холлоу: {hdmg}\n"
            f"Твоё здоровье: {p[2]}/{m[3]}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚔️ Атаковать", callback_data="fight:attack"),
                 InlineKeyboardButton("🏃 Отступить", callback_data="fight:run")]
            ]),
        )
        return

    # Предмет выпадает только если холлоу не появился. Общая вероятность 10%.
    roll = random.random() * 100
    if roll < 10:
        if roll < 5:
            kind, label = "plaster", "🩹 ПЛАСТЫРЬ"
        elif roll < 8.5:
            kind, label = "magnifier", "🔍 ЛУПА"
        else:
            kind, label = "helper", "📟 ХЕЛПЕР"
        items[kind] = items.get(kind, 0) + (3 if kind == "magnifier" else 1)
        set_player(uid, items=items_string(items), door=door + 1)
        await query.message.reply_text(
            f"🎁 Ты нашёл предмет: *{label}*!\nРедкость: " +
            {"plaster": "Обычный", "magnifier": "Редкий", "helper": "Легендарный"}[kind],
            parse_mode="Markdown",
            reply_markup=main_keyboard(uid),
        )
    else:
        set_player(uid, door=door + 1)
        await query.message.reply_text(
            "✅ Развилка безопасна. За дверью ничего не обнаружено.",
            reply_markup=main_keyboard(uid),
        )

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
    if action == "run":
        # Отступление гарантированно сохраняет здоровье, но дверь уже потрачена.
        set_player(uid, battle_active=0, battle_hp=0, battle_hollow_hp=0)
        await query.message.reply_text("🏃 Ты отступил. Дверь считается пройденной, холлоу остался позади.",
                                       reply_markup=main_keyboard(uid))
        return
    player_hp, hollow_hp, hollow_damage = p[7], p[8], p[9]
    hollow_hp -= m[4]
    if hollow_hp <= 0:
        set_player(uid, hp=player_hp, battle_active=0, battle_hp=0, battle_hollow_hp=0)
        await query.message.reply_text("✨ Победа! Холлоу исчез, ты прошёл дверь.",
                                       reply_markup=main_keyboard(uid))
        return
    player_hp -= hollow_damage
    if player_hp <= 0:
        # Мягкий проигрыш: возвращение в ЗАДВЕРЬЕ, миракл и инвентарь сохраняются.
        set_player(uid, hp=m[3], floor=0, door=0, battle_active=0,
                   battle_hp=0, battle_hollow_hp=0)
        await query.message.reply_text(
            "💫 Миракл больше не может продолжать. Ты возвращаешься в ЗАДВЕРЬЕ; "
            "здоровье восстановлено, прогресс этажа сброшен.",
            reply_markup=main_keyboard(uid),
        )
        return
    set_player(uid, battle_hp=player_hp, battle_hollow_hp=hollow_hp)
    await query.message.reply_text(
        f"⚔️ Ты нанёс {m[4]} урона.\n"
        f"👁️ Здоровье холлоу: {hollow_hp}\n"
        f"♥️ Твоё здоровье: {player_hp}/{m[3]}\n"
        f"Холлоу атакует и наносит {hollow_damage} урона.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚔️ Атаковать снова", callback_data="fight:attack"),
             InlineKeyboardButton("🏃 Отступить", callback_data="fight:run")]
        ]),
    )

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
    await query.message.reply_text(f"🛗 Двери лифта закрываются. Следующий этаж через {BREAK_SECONDS} секунд…")
    async def arrive():
        context.user_data["lift_waiting"] = False
        new_floor = 1 if floor == 0 else (26 if floor >= MAX_FLOOR else floor + 1)
        set_player(uid, floor=new_floor, door=0)
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"🛗 Лифт прибыл: *{floor_name(new_floor)}*.",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid),
            )
        except Exception:
            log.exception("Не удалось сообщить о прибытии лифта")
    context.job_queue.run_once(lambda job: None, 0) if False else None
    # JobQueue может отсутствовать в установке python-telegram-bot; используем asyncio.
    import asyncio
    async def delayed():
        await asyncio.sleep(BREAK_SECONDS)
        await arrive()
    context.application.create_task(delayed())

# ---- Админка: /addmiracle → имя → описание → статы → фото/пропуск ----
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
    await update.message.reply_text("Здоровье (1–750), только число:")
    return HP

async def add_hp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = parse_stat(update.message.text, 1, 750)
    if value is None:
        await update.message.reply_text("Нужно число от 1 до 750. Попробуй ещё раз:")
        return HP
    context.user_data["new_miracle"]["hp"] = value
    await update.message.reply_text("Урон (1–75), только число:")
    return DAMAGE

async def add_damage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = parse_stat(update.message.text, 1, 75)
    if value is None:
        await update.message.reply_text("Нужно число от 1 до 75. Попробуй ещё раз:")
        return DAMAGE
    context.user_data["new_miracle"]["damage"] = value
    await update.message.reply_text("Выносливость (1–300), только число:")
    return STAMINA

async def add_stamina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = parse_stat(update.message.text, 1, 300)
    if value is None:
        await update.message.reply_text("Нужно число от 1 до 300. Попробуй ещё раз:")
        return STAMINA
    context.user_data["new_miracle"]["stamina"] = value
    await update.message.reply_text("Везучесть (1–300), только число:")
    return LUCK

async def add_luck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = parse_stat(update.message.text, 1, 300)
    if value is None:
        await update.message.reply_text("Нужно число от 1 до 300. Попробуй ещё раз:")
        return LUCK
    context.user_data["new_miracle"]["luck"] = value
    await update.message.reply_text(
        "Отправь фотографию миракла или /skip, чтобы сохранить без фото."
    )
    return PHOTO

async def add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = update.message.photo[-1].file_id
    await save_new_miracle(update, context, photo_id)
    return ConversationHandler.END

async def add_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_new_miracle(update, context, None)
    return ConversationHandler.END

async def save_new_miracle(update, context, photo_id):
    data = context.user_data.pop("new_miracle", {})
    if not data:
        await update.message.reply_text("Создание отменено.")
        return
    save_miracle(
        data["name"], data["description"], data["hp"], data["damage"],
        data["stamina"], data["luck"], photo_id,
    )
    await update.message.reply_text(
        f"✅ Миракл «{data['name']}» сохранён в базе данных!",
        reply_markup=main_keyboard(update.effective_user.id),
    )

def parse_stat(text, low, high):
    try:
        value = int(text.strip())
        return value if low <= value <= high else None
    except (ValueError, AttributeError):
        return None

async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_miracle", None)
    await update.message.reply_text("Создание миракла отменено.")
    return ConversationHandler.END

async def list_miracles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    miracles = get_miracles()
    if not miracles:
        await update.message.reply_text("Мираклов пока нет.")
        return
    await update.message.reply_text(
        "✨ *Мираклы WONDERWORLD:*\n" + "\n\n".join(
            f"• {m[1]} · ♥️ {stat_bar(m[3],750)} · ⚔️ {stat_bar(m[4],75)} · "
            f"⚡️ {stat_bar(m[5],300)} · 🍀 {stat_bar(m[6],300)}"
            for m in miracles
        ),
        parse_mode="Markdown",
    )

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не понял команду. Напиши /start.")

def main():
    if not TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Добавь переменную в FadeHost → Environment."
        )
    init_db()
    app = Application.builder().token(TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("addmiracle", add_miracle_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_description)],
            HP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_hp)],
            DAMAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_damage)],
            STAMINA: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stamina)],
            LUCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_luck)],
            PHOTO: [
                MessageHandler(filters.PHOTO, add_photo),
                CommandHandler("skip", add_skip_photo),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
        allow_reentry=True,
    )

    app.add_handler(add_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("miracles", list_miracles))
    app.add_handler(CallbackQueryHandler(choose_miracle, pattern="^choose_miracle$"))
    app.add_handler(CallbackQueryHandler(pick_miracle, pattern="^pick:"))
    app.add_handler(CallbackQueryHandler(my_miracle, pattern="^my_miracle$"))
    app.add_handler(CallbackQueryHandler(inventory, pattern="^inventory$"))
    app.add_handler(CallbackQueryHandler(use_item, pattern="^use:"))
    app.add_handler(CallbackQueryHandler(explore, pattern="^explore$"))
    app.add_handler(CallbackQueryHandler(branch, pattern="^branch:"))
    app.add_handler(CallbackQueryHandler(fight, pattern="^fight:"))
    app.add_handler(CallbackQueryHandler(lift, pattern="^lift$"))
    app.add_handler(CallbackQueryHandler(rules, pattern="^rules$"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    log.info("WONDERWORLD bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
