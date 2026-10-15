import os
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== НАСТРОЙКИ PIXELVERSE ==================
# FadeHost Secrets: TELEGRAM_BOY_TOKEN = токен от @BotFather
BOT_TOKEN = os.getenv("TELEGRAM_BOY_TOKEN", "").strip()
ADMIN_ID = 7783222972

ASK_MINECRAFT_NICK, ASK_TELEGRAM_NICK = range(2)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pixelverse_bot")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[
        InlineKeyboardButton("📝 Подать заявку", callback_data="apply")
    ]]
    await update.effective_message.reply_text(
        "🎮 Добро пожаловать в PIXELVERSE!\n\n"
        "Здесь можно подать заявку на предварительный осмотр "
        "перед открытием сервера.\n\n"
        "Нажми кнопку ниже, чтобы заполнить анкету.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def begin_application(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.message.reply_text(
        "📝 Предварительный осмотр PIXELVERSE\n\n"
        "Напиши свой Minecraft-ник:"
    )
    return ASK_MINECRAFT_NICK


async def receive_minecraft_nick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    nickname = (update.effective_message.text or "").strip()

    if not nickname or len(nickname) > 32:
        await update.effective_message.reply_text(
            "Укажи Minecraft-ник длиной от 1 до 32 символов:"
        )
        return ASK_MINECRAFT_NICK

    context.user_data["minecraft_nick"] = nickname
    await update.effective_message.reply_text(
        "Отлично! Теперь напиши свой Telegram-ник.\n"
        "Например: @username\n"
        "Если у тебя нет ника, напиши «нет»."
    )
    return ASK_TELEGRAM_NICK


async def receive_telegram_nick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    telegram_nick = (update.effective_message.text or "").strip()

    if not telegram_nick or len(telegram_nick) > 64:
        await update.effective_message.reply_text(
            "Укажи Telegram-ник длиной до 64 символов:"
        )
        return ASK_TELEGRAM_NICK

    user = update.effective_user
    minecraft_nick = context.user_data.get("minecraft_nick", "Не указан")
    profile_nick = f"@{user.username}" if user and user.username else "не указан"
    user_id = user.id if user else "неизвестен"

    application_text = (
        "📩 НОВАЯ ЗАЯВКА | PIXELVERSE\n\n"
        f"⛏ Minecraft-ник: {minecraft_nick}\n"
        f"✈️ Telegram-ник из анкеты: {telegram_nick}\n\n"
        f"👤 Telegram-профиль отправителя: {profile_nick}\n"
        f"🆔 Telegram ID отправителя: {user_id}"
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=application_text,
        )
    except Exception:
        logger.exception("Не удалось отправить заявку администратору")
        await update.effective_message.reply_text(
            "❌ Не удалось отправить заявку.\n"
            "Попробуй позже или сообщи администратору."
        )
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "✅ Заявка на предварительный осмотр отправлена!\n\n"
        "Спасибо. Ожидай дальнейших новостей от PIXELVERSE."
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Заявка отменена. Чтобы начать заново, отправь /start."
    )
    return ConversationHandler.END


async def unknown_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.effective_message.reply_text(
        "Чтобы открыть меню PIXELVERSE, отправь /start."
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден токен бота! Добавь в FadeHost Secrets переменную "
            "TELEGRAM_BOY_TOKEN и укажи в ней токен от @BotFather."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    application_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(begin_application, pattern=r"^apply$")
        ],
        states={
            ASK_MINECRAFT_NICK: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_minecraft_nick,
                )
            ],
            ASK_TELEGRAM_NICK: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_telegram_nick,
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(application_conversation)
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    logger.info("PIXELVERSE bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
