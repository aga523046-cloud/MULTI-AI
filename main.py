import os import logging
from telegram import ( Update, InlineKeyboardButton, InlineKeyboardMarkup, ) from telegram.ext import ( Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, ContextTypes, filters, )
===== НАСТРОЙКИ =====
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_БОТА") ADMIN_ID = 7783222972
MINECRAFT_NICK, TELEGRAM_NICK = range(2)
logging.basicConfig( format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO, ) logger = logging.getLogger(name)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE): keyboard = [ [InlineKeyboardButton("📝 Подать заявку", callback_data="apply")] ] await update.message.reply_text( "🎮 Добро пожаловать в PIXELVERSE!\n\n" "Здесь можно подать заявку на предварительный осмотр " "перед открытием сервера.\n\n" "Нажми кнопку ниже, чтобы заполнить анкету.", reply_markup=InlineKeyboardMarkup(keyboard), )
async def begin_application( update: Update, context: ContextTypes.DEFAULT_TYPE ): query = update.callback_query await query.answer() await query.message.reply_text( "📝 Предварительный осмотр PIXELVERSE\n\n" "Напиши свой Minecraft-ник:", parse_mode="Markdown", ) return MINECRAFT_NICK
async def receive_minecraft_nick( update: Update, context: ContextTypes.DEFAULT_TYPE ): nickname = update.message.text.strip()
if not nickname or len(nickname) > 32:
    await update.message.reply_text(
        "Укажи корректный Minecraft-ник (до 32 символов). Попробуй ещё раз:"
    )
    return MINECRAFT_NICK

context.user_data["minecraft_nick"] = nickname

await update.message.reply_text(
    "Отлично! Теперь напиши свой Telegram-ник.\n"
    "Например: @username\n\n"
    "Если у тебя нет ника, напиши «нет»."
)
return TELEGRAM_NICK
async def receive_telegram_nick( update: Update, context: ContextTypes.DEFAULT_TYPE ): telegram_nick = update.message.text.strip()
if not telegram_nick or len(telegram_nick) > 64:
    await update.message.reply_text(
        "Укажи Telegram-ник (до 64 символов). Попробуй ещё раз:"
    )
    return TELEGRAM_NICK

minecraft_nick = context.user_data.get("minecraft_nick", "Не указан")
applicant = update.effective_user
telegram_username = (
    f"@{applicant.username}" if applicant.username else "нет"
)

message = (
    "📩 НОВАЯ ЗАЯВКА | PIXELVERSE\n\n"
    f"⛏ Minecraft-ник: {minecraft_nick}\n"
    f"✈️ Telegram-ник из анкеты: {telegram_nick}\n\n"
    f"👤 Telegram-профиль: {telegram_username}\n"
    f"🆔 Telegram ID: {applicant.id}"
)

try:
    await context.bot.send_message(chat_id=ADMIN_ID, text=message)
except Exception:
    logger.exception("Не удалось отправить заявку администратору")
    await update.message.reply_text(
        "Не получилось отправить заявку. Попробуй позже."
    )
    return ConversationHandler.END

await update.message.reply_text(
    "✅ Заявка на предварительный осмотр отправлена!\n\n"
    "Ожидай дальнейших новостей от PIXELVERSE."
)
context.user_data.clear()
return ConversationHandler.END
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE): context.user_data.clear() await update.message.reply_text( "Заявка отменена. Чтобы начать заново, отправь /start." ) return ConversationHandler.END
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text( "Чтобы открыть меню PIXELVERSE, отправь /start." )
def main(): if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН_БОТА": raise RuntimeError( "Укажи токен бота в переменной окружения TELEGRAM_BOT_TOKEN " "или в настройке BOT_TOKEN в main.py." )
app = Application.builder().token(BOT_TOKEN).build()

application_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(begin_application, pattern="^apply$")],
    states={
        MINECRAFT_NICK: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_minecraft_nick)
        ],
        TELEGRAM_NICK: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_telegram_nick)
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    allow_reentry=True,
)

app.add_handler(CommandHandler("start", start))
app.add_handler(application_conversation)
app.add_handler(CommandHandler("cancel", cancel))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu))

logger.info("PIXELVERSE bot is running")
app.run_polling()
if name == "main": main()
