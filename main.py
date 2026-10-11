"""
STARHENGE GUIDE - Telegram bot
Python 3.10+
Install: pip install "python-telegram-bot>=21,<22"
Run: set TELEGRAM_BOT_TOKEN=...   (Windows PowerShell: $env:TELEGRAM_BOT_TOKEN="...")
     python main.py

Admin Telegram ID: 7783222972
Data is stored in starhenge.sqlite3.

Core features:
- /рудник (or /mine): +1 💎, with a 1% chance of +10 💎 instead; cooldown per user: 2 hours.
- Daily /ежедневно (or /daily): +10 💎 every 24 hours.
- Monsters, passive coin income, hunger, wishes, levels and bakery.
- Admin panel /admin for creating bakery items.
- Admin creation wizard asks for all requested bakery properties.
"""

import logging
import os
import random
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters, ConversationHandler,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = 7783222972
DB_PATH = os.getenv("STARHENGE_DB_PATH", "starhenge.sqlite3")

MINING_COOLDOWN = 2 * 60 * 60
DAILY_COOLDOWN = 24 * 60 * 60
MONSTER_HUNGER_MAX = 10
HUNGER_LOSS_SECONDS = 6 * 60 * 60  # -1 hunger every 6 hours
BAKE, BAKE_QTY = range(2)
ADMIN_NAME, ADMIN_COST, ADMIN_QTY, ADMIN_TIME, ADMIN_LEVEL, ADMIN_HUNGER, ADMIN_XP = range(7)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("starhenge")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            coins REAL NOT NULL DEFAULT 0,
            gems INTEGER NOT NULL DEFAULT 0,
            last_mine INTEGER NOT NULL DEFAULT 0,
            last_daily INTEGER NOT NULL DEFAULT 0,
            last_tick INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS monsters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 1,
            xp INTEGER NOT NULL DEFAULT 0,
            hunger INTEGER NOT NULL DEFAULT 10,
            base_income REAL NOT NULL,
            income_multiplier REAL NOT NULL DEFAULT 1.0,
            wish_item_id INTEGER,
            last_hunger_tick INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(wish_item_id) REFERENCES bakery_items(id)
        );
        CREATE TABLE IF NOT EXISTS bakery_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            cost REAL NOT NULL,
            quantity INTEGER NOT NULL,
            cook_seconds INTEGER NOT NULL,
            required_level INTEGER NOT NULL,
            hunger_restore INTEGER NOT NULL,
            xp_reward INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS inventory (
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, item_id),
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(item_id) REFERENCES bakery_items(id)
        );
        CREATE TABLE IF NOT EXISTS baking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            finish_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(item_id) REFERENCES bakery_items(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)


def now() -> int:
    return int(time.time())


def ensure_user(tg_user):
    t = now()
    with db() as c:
        c.execute(
            "INSERT INTO users(user_id, username, last_tick) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (tg_user.id, tg_user.username or tg_user.full_name, t),
        )
        c.execute("UPDATE users SET last_tick=? WHERE user_id=? AND last_tick=0", (t, tg_user.id))


def get_user(user_id: int):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def fmt_coins(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def level_requirement(level: int) -> int:
    """XP required to advance FROM this level to the next."""
    if level <= 1:
        return 100
    if level == 2:
        return 1500
    if level == 3:
        return 5000
    # Requirements grow by the specified rate in each band.
    req = 5000
    for current_level in range(4, level + 1):
        if current_level <= 10:
            rate = 1.25
        elif current_level <= 15:
            rate = 1.00
        elif current_level <= 20:
            rate = 0.80
        elif current_level <= 30:
            rate = 0.65
        elif current_level <= 40:
            rate = 0.40
        else:
            rate = 0.40
        req = int(round(req * (1 + rate)))
    return max(1, req)


def income_multiplier_for_level(level: int) -> float:
    """Each level gives +40% to +75%; large multipliers are less likely."""
    # Weighted bands favor the lower end.
    return random.choices(
        [1.40, 1.45, 1.50, 1.55, 1.60, 1.65, 1.70, 1.75],
        weights=[25, 22, 18, 14, 9, 6, 4, 2],
        k=1,
    )[0]


def tick_user(user_id: int):
    """Accrue passive income and hunger decay without needing a background job."""
    t = now()
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not u:
            return
        last = int(u["last_tick"] or t)
        elapsed = max(0, min(t - last, 30 * 24 * 3600))
        monsters = c.execute("SELECT * FROM monsters WHERE user_id=?", (user_id,)).fetchall()
        total_income = 0.0
        for m in monsters:
            if int(m["hunger"]) > 0:
                total_income += float(m["base_income"]) * float(m["income_multiplier"])
            last_hunger = int(m["last_hunger_tick"] or t)
            hunger_elapsed = max(0, t - last_hunger)
            loss = hunger_elapsed // HUNGER_LOSS_SECONDS
            if loss:
                c.execute(
                    "UPDATE monsters SET hunger=MAX(0,hunger-?), last_hunger_tick=? WHERE id=?",
                    (int(loss), last_hunger + int(loss) * HUNGER_LOSS_SECONDS, m["id"]),
                )
        c.execute(
            "UPDATE users SET coins=coins+?, last_tick=? WHERE user_id=?",
            (total_income * elapsed, t, user_id),
        )


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛏️ Рудник", callback_data="mine"),
         InlineKeyboardButton("🎁 Ежедневная награда", callback_data="daily")],
        [InlineKeyboardButton("👾 Мои монстры", callback_data="monsters"),
         InlineKeyboardButton("🥐 Пекарня", callback_data="bakery")],
        [InlineKeyboardButton("🎒 Инвентарь", callback_data="inventory"),
         InlineKeyboardButton("💰 Баланс", callback_data="balance")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    tick_user(update.effective_user.id)
    await update.effective_message.reply_text(
        "✦ *STARHENGE GUIDE* ✦\n\n"
        "Добро пожаловать в звёздное поселение! ⛏️ Добывай 💎, "
        "забирай ежедневную награду, заселяй монстров и выпекай для них лакомства.\n\n"
        "Команды: /рудник, /ежедневно, /баланс, /монстры, /заселить, /пекарня, /инвентарь",
        parse_mode="Markdown", reply_markup=main_keyboard()
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    tick_user(uid)
    u = get_user(uid)
    await update.effective_message.reply_text(
        f"💰 Монеты: *{fmt_coins(u['coins'])}*\n💎 Кристаллы: *{u['gems']}*",
        parse_mode="Markdown"
    )


async def do_mine(update: Update, context: ContextTypes.DEFAULT_TYPE, from_button=False):
    user = update.effective_user
    ensure_user(user)
    t = now()
    with db() as c:
        row = c.execute("SELECT gems,last_mine FROM users WHERE user_id=?", (user.id,)).fetchone()
        remaining = MINING_COOLDOWN - (t - int(row["last_mine"]))
        if remaining > 0:
            msg = f"⏳ Рудник ещё восстанавливается. Попробуй через {remaining // 3600} ч. {(remaining % 3600) // 60} мин."
        else:
            jackpot = random.random() < 0.01
            reward = 10 if jackpot else 1
            c.execute("UPDATE users SET gems=gems+?,last_mine=? WHERE user_id=?", (reward, t, user.id))
            msg = "🎉 *КУШ!* Ты нашёл жилу кристаллов: +10 💎" if jackpot else "⛏️ Ты добыл +1 💎"
    if from_button:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def mine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_mine(update, context)


async def mine_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # In groups, the user can simply type "рудник" without a slash.
    text = (update.effective_message.text or "").strip().casefold()
    if text in {"рудник", "⛏️ рудник", "mine"}:
        await do_mine(update, context)


async def do_daily(update: Update, context: ContextTypes.DEFAULT_TYPE, from_button=False):
    user = update.effective_user
    ensure_user(user)
    t = now()
    with db() as c:
        row = c.execute("SELECT last_daily FROM users WHERE user_id=?", (user.id,)).fetchone()
        remaining = DAILY_COOLDOWN - (t - int(row["last_daily"]))
        if remaining > 0:
            msg = f"🎁 Награда уже получена. Возвращайся через {remaining // 3600} ч. {(remaining % 3600) // 60} мин."
        else:
            c.execute("UPDATE users SET gems=gems+10,last_daily=? WHERE user_id=?", (t, user.id))
            msg = "🎁 Ежедневная награда: *+10 💎*"
    if from_button:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_daily(update, context)


def monster_status(m):
    wish = "❌" if not m["wish_item_id"] else None
    if m["wish_item_id"]:
        with db() as c:
            item = c.execute("SELECT name FROM bakery_items WHERE id=?", (m["wish_item_id"],)).fetchone()
        wish = item["name"] if item else "❌"
    return f"🍖 {m['hunger']}/{MONSTER_HUNGER_MAX} | {wish} | ⬆️ {m['level']}"


async def monsters(update: Update, context: ContextTypes.DEFAULT_TYPE, from_button=False):
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    tick_user(uid)
    with db() as c:
        rows = c.execute("SELECT * FROM monsters WHERE user_id=? ORDER BY id", (uid,)).fetchall()
    if not rows:
        msg = "У тебя пока нет монстров. Используй /заселить, чтобы заселить первого!"
    else:
        lines = ["👾 *Твои монстры:*"]
        for m in rows:
            wish_text = "❌"
            if m["wish_item_id"]:
                with db() as c:
                    item = c.execute("SELECT name FROM bakery_items WHERE id=?", (m["wish_item_id"],)).fetchone()
                if item:
                    wish_text = item["name"]
            lines.append(
                f"\n*{m['name']}* · ID `{m['id']}`\n"
                f"🍖 {m['hunger']}/{MONSTER_HUNGER_MAX} | {wish_text} | ⬆️ {m['level']}\n"
                f"✨ Опыт: {m['xp']}/{level_requirement(m['level'])}\n"
                f"💰 Доход: {fmt_coins(m['base_income'] * m['income_multiplier'])}/сек."
            )
        msg = "\n".join(lines)
    if from_button:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def monsters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await monsters(update, context)


async def settle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    with db() as c:
        count = c.execute("SELECT COUNT(*) n FROM monsters WHERE user_id=?", (uid,)).fetchone()["n"]
        if count >= 5:
            await update.effective_message.reply_text("🏠 В поселении может быть не больше 5 монстров.")
            return
        names = ["Астра", "Комета", "Нова", "Орион", "Люмен", "Звёздник", "Пикси", "Метеор"]
        name = random.choice(names)
        income = round(random.uniform(0.35, 0.60), 2)
        wish = c.execute("SELECT id FROM bakery_items WHERE active=1").fetchall()
        wish_id = random.choice(wish)["id"] if wish and random.random() < 0.75 else None
        c.execute(
            "INSERT INTO monsters(user_id,name,base_income,level,xp,hunger,wish_item_id,last_hunger_tick) "
            "VALUES(?,?,?,1,0,10,?,?)",
            (uid, name, income, wish_id, now())
        )
    await update.effective_message.reply_text(
        f"🌠 В поселении появился монстр *{name}*!\n"
        f"🍖 10/10 | {'❌' if not wish_id else 'есть хотелка в пекарне'} | ⬆️ 1\n"
        f"💰 Базовый доход: {fmt_coins(income)} 💰/сек.\n\n"
        "У монстров случайное имя и базовый доход. Команда доступна, пока у тебя меньше 5 монстров.",
        parse_mode="Markdown"
    )


async def bakery(update: Update, context: ContextTypes.DEFAULT_TYPE, from_button=False):
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    tick_user(uid)
    with db() as c:
        items = c.execute("SELECT * FROM bakery_items WHERE active=1 ORDER BY required_level,cost").fetchall()
        mons = c.execute("SELECT id,level FROM monsters WHERE user_id=?", (uid,)).fetchall()
    if not items:
        msg = "🥐 Пекарня пока пуста. Админ может добавить предметы через /admin."
        markup = None
    else:
        msg = "🥐 *Пекарня STARHENGE*\nВыбери лакомство для покупки и выпечки:"
        buttons = []
        max_level = max([m["level"] for m in mons], default=1)
        for item in items:
            lock = " 🔒" if item["required_level"] > max_level else ""
            buttons.append([InlineKeyboardButton(
                f"{item['name']} · {fmt_coins(item['cost'])}💰{lock}",
                callback_data=f"bake:{item['id']}"
            )])
        markup = InlineKeyboardMarkup(buttons)
    if from_button:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.effective_message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)


async def bakery_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await bakery(update, context)


async def inventory(update: Update, context: ContextTypes.DEFAULT_TYPE, from_button=False):
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    with db() as c:
        rows = c.execute(
            "SELECT b.name,i.quantity FROM inventory i JOIN bakery_items b ON b.id=i.item_id "
            "WHERE i.user_id=? AND i.quantity>0 ORDER BY b.name", (uid,)
        ).fetchall()
        pending = c.execute(
            "SELECT b.name,k.quantity,k.finish_at FROM baking k JOIN bakery_items b ON b.id=k.item_id "
            "WHERE k.user_id=? ORDER BY k.finish_at", (uid,)
        ).fetchall()
    lines = ["🎒 *Инвентарь*"]
    if rows:
        lines += [f"• {r['name']} ×{r['quantity']}" for r in rows]
    else:
        lines.append("Пока пусто.")
    if pending:
        lines.append("\n🔥 *В духовке:*")
        for p in pending:
            left = max(0, p["finish_at"] - now())
            lines.append(f"• {p['name']} ×{p['quantity']} · осталось {left // 60} мин. {left % 60} сек.")
    msg = "\n".join(lines)
    if from_button:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def inventory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await inventory(update, context)


async def collect_ready_bakes(user_id: int):
    t = now()
    with db() as c:
        ready = c.execute("SELECT * FROM baking WHERE user_id=? AND finish_at<=?", (user_id, t)).fetchall()
        for job in ready:
            c.execute(
                "INSERT INTO inventory(user_id,item_id,quantity) VALUES(?,?,?) "
                "ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity",
                (user_id, job["item_id"], job["quantity"])
            )
            c.execute("DELETE FROM baking WHERE id=?", (job["id"],))
    return ready


async def begin_bake(update: Update, context: ContextTypes.DEFAULT_TYPE, item_id: int):
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    await collect_ready_bakes(uid)
    tick_user(uid)
    with db() as c:
        item = c.execute("SELECT * FROM bakery_items WHERE id=? AND active=1", (item_id,)).fetchone()
        u = c.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()
        monsters = c.execute("SELECT level FROM monsters WHERE user_id=?", (uid,)).fetchall()
        max_level = max([m["level"] for m in monsters], default=1)
        if not item:
            text = "Этот предмет больше недоступен."
        elif max_level < item["required_level"]:
            text = f"🔒 Нужен монстр уровня {item['required_level']} или выше."
        elif float(u["coins"]) < float(item["cost"]):
            text = f"Не хватает монет. Нужно {fmt_coins(item['cost'])} 💰."
        else:
            c.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (item["cost"], uid))
            c.execute(
                "INSERT INTO baking(user_id,item_id,quantity,finish_at) VALUES(?,?,?,?)",
                (uid, item_id, item["quantity"], now() + item["cook_seconds"])
            )
            text = f"🔥 Поставлено в духовку: *{item['name']}*.\nГотово через {item['cook_seconds']} сек. Загляни в /инвентарь."
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(text, parse_mode="Markdown")


async def feed_monster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    await collect_ready_bakes(uid)
    with db() as c:
        rows = c.execute(
            "SELECT i.item_id,i.quantity,b.name FROM inventory i JOIN bakery_items b ON b.id=i.item_id "
            "WHERE i.user_id=? AND i.quantity>0", (uid,)
        ).fetchall()
        mons = c.execute("SELECT id,name FROM monsters WHERE user_id=?", (uid,)).fetchall()
    if not mons:
        await update.effective_message.reply_text("Сначала засели монстра: /заселить")
        return
    if not rows:
        await update.effective_message.reply_text("В инвентаре нет готовых лакомств. Загляни в /пекарня.")
        return
    buttons = []
    for m in mons:
        for item in rows:
            buttons.append([InlineKeyboardButton(
                f"{m['name']} ← {item['name']} ×{item['quantity']}",
                callback_data=f"feed:{m['id']}:{item['item_id']}"
            )])
    await update.effective_message.reply_text("🍮 Выбери, кого и чем накормить:", reply_markup=InlineKeyboardMarkup(buttons))


async def do_feed(update: Update, context: ContextTypes.DEFAULT_TYPE, monster_id: int, item_id: int):
    uid = update.effective_user.id
    with db() as c:
        monster = c.execute("SELECT * FROM monsters WHERE id=? AND user_id=?", (monster_id, uid)).fetchone()
        item = c.execute("SELECT * FROM bakery_items WHERE id=? AND active=1", (item_id,)).fetchone()
        inv = c.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_id=?", (uid, item_id)).fetchone()
        if not monster or not item or not inv or inv["quantity"] < 1:
            text = "Не удалось накормить монстра. Проверь инвентарь."
        else:
            c.execute("UPDATE inventory SET quantity=quantity-1 WHERE user_id=? AND item_id=?", (uid, item_id))
            new_hunger = min(MONSTER_HUNGER_MAX, monster["hunger"] + item["hunger_restore"])
            xp = int(item["xp_reward"])
            level = int(monster["level"])
            total_xp = int(monster["xp"]) + xp
            while total_xp >= level_requirement(level) and level < 40:
                total_xp -= level_requirement(level)
                level += 1
                c.execute(
                    "UPDATE monsters SET income_multiplier=income_multiplier*? WHERE id=?",
                    (income_multiplier_for_level(level), monster_id)
                )
            wish_bonus = ""
            if monster["wish_item_id"] == item_id:
                xp += max(1, int(item["xp_reward"] * 0.5))
                total_xp += max(1, int(item["xp_reward"] * 0.5))
                wish_bonus = "\n💖 Хотелка исполнена! Бонусный опыт +50%."
                while total_xp >= level_requirement(level) and level < 40:
                    total_xp -= level_requirement(level)
                    level += 1
                    c.execute(
                        "UPDATE monsters SET income_multiplier=income_multiplier*? WHERE id=?",
                        (income_multiplier_for_level(level), monster_id)
                    )
            c.execute(
                "UPDATE monsters SET hunger=?,xp=?,level=?,wish_item_id=NULL,last_hunger_tick=? WHERE id=?",
                (new_hunger, total_xp, level, now(), monster_id)
            )
            # Assign a fresh wish, if there are active bakery items.
            wishes = c.execute("SELECT id FROM bakery_items WHERE active=1 AND id<>?", (item_id,)).fetchall()
            if wishes and random.random() < 0.75:
                c.execute("UPDATE monsters SET wish_item_id=? WHERE id=?", (random.choice(wishes)["id"], monster_id))
            text = f"😋 *{monster['name']}* съел(а) {item['name']}!\n🍖 Сытость: {new_hunger}/{MONSTER_HUNGER_MAX}\n✨ Получено опыта: {xp}\n⬆️ Уровень: {level}{wish_bonus}"
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(text, parse_mode="Markdown")


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    if data == "mine":
        await do_mine(update, context, True)
    elif data == "daily":
        await do_daily(update, context, True)
    elif data == "monsters":
        await monsters(update, context, True)
    elif data == "bakery":
        await bakery(update, context, True)
    elif data == "inventory":
        await inventory(update, context, True)
    elif data == "balance":
        await q.answer()
        uid = update.effective_user.id
        ensure_user(update.effective_user)
        tick_user(uid)
        u = get_user(uid)
        await q.message.reply_text(f"💰 Монеты: {fmt_coins(u['coins'])}\n💎 Кристаллы: {u['gems']}")
    elif data.startswith("bake:"):
        try:
            await begin_bake(update, context, int(data.split(":")[1]))
        except (ValueError, IndexError):
            await q.answer("Некорректный предмет.", show_alert=True)
    elif data.startswith("feed:"):
        try:
            _, mid, iid = data.split(":")
            await do_feed(update, context, int(mid), int(iid))
        except (ValueError, IndexError):
            await q.answer("Некорректный выбор.", show_alert=True)
    else:
        await q.answer()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Эта панель доступна только администратору.")
        return
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать предмет пекарни", callback_data="admin:create")],
        [InlineKeyboardButton("📋 Список предметов", callback_data="admin:list")],
    ])
    await update.effective_message.reply_text("⚙️ *STARHENGE ADMIN PANEL*", parse_mode="Markdown", reply_markup=markup)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа.", show_alert=True)
        return
    await q.answer()
    if q.data == "admin:create":
        context.user_data["admin_item"] = {}
        await q.message.reply_text("Введи название предмета:")
        return ADMIN_NAME
    if q.data == "admin:list":
        with db() as c:
            rows = c.execute("SELECT * FROM bakery_items ORDER BY id DESC").fetchall()
        if not rows:
            msg = "Предметов пока нет."
        else:
            msg = "\n".join(
                f"#{r['id']} {r['name']} | {fmt_coins(r['cost'])}💰 | ×{r['quantity']} | "
                f"{r['cook_seconds']} сек. | ур. {r['required_level']} | сытость +{r['hunger_restore']} | XP {r['xp_reward']}"
                for r in rows
            )
        await q.message.reply_text(msg)
    return ConversationHandler.END


async def admin_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_message.text.strip()
    if not name or len(name) > 60:
        await update.effective_message.reply_text("Название должно быть от 1 до 60 символов. Попробуй ещё раз:")
        return ADMIN_NAME
    context.user_data.setdefault("admin_item", {})["name"] = name
    await update.effective_message.reply_text("Стоимость в 💰 (число, например 12.5):")
    return ADMIN_COST


async def admin_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = float(update.effective_message.text.replace(",", "."))
        if value < 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Введи неотрицательное число, например 12.5:")
        return ADMIN_COST
    context.user_data["admin_item"]["cost"] = value
    await update.effective_message.reply_text("Количество лакомств за одну выпечку (целое число):")
    return ADMIN_QTY


async def admin_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int(update.effective_message.text)
        if value < 1 or value > 10000:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Введи целое число от 1 до 10000:")
        return ADMIN_QTY
    context.user_data["admin_item"]["quantity"] = value
    await update.effective_message.reply_text("Время готовки в секундах (0 или больше):")
    return ADMIN_TIME


async def admin_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int(update.effective_message.text)
        if value < 0 or value > 30 * 24 * 3600:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Введи время в секундах от 0 до 2592000:")
        return ADMIN_TIME
    context.user_data["admin_item"]["cook_seconds"] = value
    await update.effective_message.reply_text("Требуемый уровень монстра (от 1 до 40):")
    return ADMIN_LEVEL


async def admin_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int(update.effective_message.text)
        if value < 1 or value > 40:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Введи уровень от 1 до 40:")
        return ADMIN_LEVEL
    context.user_data["admin_item"]["required_level"] = value
    await update.effective_message.reply_text("Сколько сытости восстанавливает лакомство (1–10):")
    return ADMIN_HUNGER


async def admin_hunger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int(update.effective_message.text)
        if value < 1 or value > 10:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Введи число от 1 до 10:")
        return ADMIN_HUNGER
    context.user_data["admin_item"]["hunger_restore"] = value
    await update.effective_message.reply_text("Сколько опыта выдаётся за выпечку (целое число):")
    return ADMIN_XP


async def admin_xp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int(update.effective_message.text)
        if value < 0 or value > 10_000_000:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Введи неотрицательное целое число:")
        return ADMIN_XP
    item = context.user_data["admin_item"]
    item["xp_reward"] = value
    try:
        with db() as c:
            c.execute(
                "INSERT INTO bakery_items(name,cost,quantity,cook_seconds,required_level,hunger_restore,xp_reward) "
                "VALUES(?,?,?,?,?,?,?)",
                (item["name"], item["cost"], item["quantity"], item["cook_seconds"],
                 item["required_level"], item["hunger_restore"], item["xp_reward"])
            )
        await update.effective_message.reply_text(
            "✅ Предмет создан!\n"
            f"🥐 {item['name']}\n💰 Стоимость: {fmt_coins(item['cost'])}\n"
            f"📦 Количество за выпечку: {item['quantity']}\n⏱️ Готовка: {item['cook_seconds']} сек.\n"
            f"⬆️ Требуемый уровень: {item['required_level']}\n🍖 Сытость: +{item['hunger_restore']}\n"
            f"✨ Опыт: {item['xp_reward']}"
        )
    except sqlite3.IntegrityError:
        await update.effective_message.reply_text("❌ Предмет с таким названием уже существует. Запусти /admin и попробуй снова.")
    context.user_data.pop("admin_item", None)
    return ConversationHandler.END


async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("admin_item", None)
    await update.effective_message.reply_text("Создание предмета отменено.")
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "✦ *STARHENGE GUIDE: команды* ✦\n"
        "/рудник — добыть 💎 (раз в 2 часа)\n"
        "/ежедневно — получить +10 💎 (раз в 24 часа)\n"
        "/баланс — посмотреть валюты\n"
        "/заселить — получить случайного монстра (до 5)\n"
        "/монстры — статус монстров\n"
        "/пекарня — выбрать лакомство и поставить в духовку\n"
        "/инвентарь — готовые и выпекаемые предметы\n"
        "/накормить — накормить монстра лакомством\n"
        "/admin — панель администратора",
        parse_mode="Markdown"
    )


async def post_init(application: Application):
    init_db()
    log.info("STARHENGE GUIDE started.")


def main():
    if not TOKEN:
        raise SystemExit(
            "Не найден TELEGRAM_BOT_TOKEN. Добавь токен в переменные окружения хостинга."
        )
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["help", "помощь"], help_cmd))
    application.add_handler(CommandHandler(["mine", "рудник"], mine_cmd))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^\\s*(рудник|⛏️\\s*рудник|mine)\\s*$"),
        mine_text_handler
    ))
    application.add_handler(CommandHandler(["daily", "ежедневно"], daily_cmd))
    application.add_handler(CommandHandler(["balance", "баланс"], balance))
    application.add_handler(CommandHandler(["settle", "заселить"], settle))
    application.add_handler(CommandHandler(["monsters", "монстры"], monsters_cmd))
    application.add_handler(CommandHandler(["bakery", "пекарня"], bakery_cmd))
    application.add_handler(CommandHandler(["inventory", "инвентарь"], inventory_cmd))
    application.add_handler(CommandHandler(["feed", "накормить"], feed_monster))
    application.add_handler(CommandHandler("admin", admin_panel))

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern=r"^admin:(create|list)$")],
        states={
            ADMIN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_name)],
            ADMIN_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cost)],
            ADMIN_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_qty)],
            ADMIN_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_time)],
            ADMIN_LEVEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_level)],
            ADMIN_HUNGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_hunger)],
            ADMIN_XP: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_xp)],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin)],
        per_user=True,
        per_chat=True,
    )
    application.add_handler(admin_conv)
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
