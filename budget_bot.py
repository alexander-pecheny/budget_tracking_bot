import logging
import re
import sqlite3
import yaml
from datetime import datetime, timedelta

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

BOT_TOKEN = config["bot_token"]
AUTHORIZED_USERS = config["authorized_users"]
DATABASE_PATH = config["database_path"]
CURRENCIES = config["currencies"]
CATEGORIES = config["categories"]
TIMEOUT_SECONDS = config["timeout_seconds"]
EXCHANGE_RATES = config["exchange_rates"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

ASKING_CURRENCY, ASKING_CATEGORY, ASKING_COMMENT_CHOICE, ASKING_COMMENT = range(4)


class BudgetDatabase:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                category TEXT NOT NULL,
                comment TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def add_transaction(
        self,
        user_id: int,
        amount: float,
        currency: str,
        category: str,
        comment: str = None,
    ):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO transactions (user_id, amount, currency, category, comment)
            VALUES (?, ?, ?, ?, ?)
        """,
            (user_id, amount, currency, category, comment),
        )
        conn.commit()
        conn.close()

    def get_stats(
        self,
        start_date: datetime = None,
        end_date: datetime = None,
        days: int = None,
    ):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        if days and not start_date:
            start_date = datetime.now() - timedelta(days=days)
        elif not start_date:
            start_date = datetime.now() - timedelta(days=30)
        assert start_date

        if days and start_date and not end_date:
            end_date = start_date + timedelta(days=days)
        elif not end_date:
            end_date = datetime.now()
        assert end_date

        cursor.execute(
            """
            SELECT category, currency, SUM(amount) as total
            FROM transactions 
            WHERE timestamp BETWEEN ? AND ?
            GROUP BY category, currency
            ORDER BY total DESC
        """,
            (start_date.isoformat(), end_date.isoformat()),
        )

        results = cursor.fetchall()
        conn.close()
        return results


db = BudgetDatabase()


def convert_to_rsd(amount: float, currency: str) -> float:
    rate = EXCHANGE_RATES.get(currency, 1.0)
    return round(amount * rate, 1)


def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    await update.message.reply_text(
        "Привет! Отправь сумму числом для добавления транзакции или используй /stats для просмотра статистики."
    )


def try_float(text: str) -> float:
    text = text.replace(",", ".")
    try:
        return float(text.strip())
    except ValueError:
        return None


async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text.strip()

    amount = try_float(text)
    if amount:
        context.user_data["amount"] = amount

        keyboard = [[currency] for currency in CURRENCIES]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)

        await update.message.reply_text(
            f"Сумма: {amount}\nВыберите валюту:", reply_markup=reply_markup
        )
        return ASKING_CURRENCY

    return ConversationHandler.END


async def handle_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END

    currency = update.message.text
    if currency not in CURRENCIES:
        await update.message.reply_text(
            "Пожалуйста, выберите валюту из предложенных вариантов."
        )
        return ASKING_CURRENCY

    context.user_data["currency"] = currency

    keyboard = []
    for i in range(0, len(CATEGORIES), 2):
        row = [CATEGORIES[i]]
        if i + 1 < len(CATEGORIES):
            row.append(CATEGORIES[i + 1])
        keyboard.append(row)

    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)

    await update.message.reply_text(
        f"Сумма: {context.user_data['amount']} {currency}\nВыберите категорию:",
        reply_markup=reply_markup,
    )
    return ASKING_CATEGORY


async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END

    category = update.message.text
    if category not in CATEGORIES:
        await update.message.reply_text(
            "Пожалуйста, выберите категорию из предложенных вариантов."
        )
        return ASKING_CATEGORY

    context.user_data["category"] = category

    keyboard = [["Да", "Нет"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)

    await update.message.reply_text(
        f"Сумма: {context.user_data['amount']} {context.user_data['currency']}\n"
        f"Категория: {category}\n"
        f"Хотите добавить комментарий?",
        reply_markup=reply_markup,
    )
    return ASKING_COMMENT_CHOICE


async def handle_comment_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END

    choice = update.message.text
    if choice == "Нет":
        await save_transaction(update, context, None)
        return ConversationHandler.END
    elif choice == "Да":
        await update.message.reply_text(
            "Введите комментарий:", reply_markup=ReplyKeyboardRemove()
        )
        return ASKING_COMMENT
    else:
        await update.message.reply_text("Пожалуйста, выберите 'Да' или 'Нет'.")
        return ASKING_COMMENT_CHOICE


async def handle_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END

    comment = update.message.text
    await save_transaction(update, context, comment)
    return ConversationHandler.END


async def save_transaction(
    update: Update, context: ContextTypes.DEFAULT_TYPE, comment: str
):
    user_id = update.effective_user.id
    amount = context.user_data["amount"]
    currency = context.user_data["currency"]
    category = context.user_data["category"]

    db.add_transaction(user_id, amount, currency, category, comment)

    message = (
        f"✅ Транзакция добавлена!\nСумма: {amount} {currency}\nКатегория: {category}"
    )
    if comment:
        message += f"\nКомментарий: {comment}"

    await update.message.reply_text(message, reply_markup=ReplyKeyboardRemove())

    context.user_data.clear()


async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Транзакция не была добавлена так как вы не ответили в течение 5 минут.",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    args = context.args

    start_date = None
    end_date = None
    days = None

    if not args:
        days = 30
    elif len(args) == 1:
        arg = args[0]
        if re.match(r"^\d+$", arg):
            days = int(arg)
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", arg):
            start_date = datetime.strptime(arg, "%Y-%m-%d")
    elif len(args) == 2:
        arg1, arg2 = args
        if re.match(r"^\d{4}-\d{2}-\d{2}$", arg1):
            start_date = datetime.strptime(arg1, "%Y-%m-%d")
            if re.match(r"^\d+$", arg2):
                days = int(arg2)
            elif re.match(r"^\d{4}-\d{2}-\d{2}$", arg2):
                end_date = datetime.strptime(arg2, "%Y-%m-%d")

    try:
        results = db.get_stats(start_date, end_date, days)

        if not results:
            await update.message.reply_text("Нет транзакций за указанный период.")
            return

        category_totals = {}
        grand_total_rsd = 0

        for category, currency, amount in results:
            amount_rsd = convert_to_rsd(amount, currency)
            if category not in category_totals:
                category_totals[category] = 0
            category_totals[category] += amount_rsd
            grand_total_rsd += amount_rsd

        message = f"📊 Статистика расходов с {start_date} по {end_date}:\n\n"

        for category, total_rsd in sorted(category_totals.items(), key=lambda x: x[1], reverse=True):
            percentage = (total_rsd / grand_total_rsd) * 100 if grand_total_rsd > 0 else 0
            message += f"{category}: {total_rsd:.1f} RSD ({percentage:.1f}%)\n"

        message += f"\n💰 Общий расход: {grand_total_rsd:.1f} RSD"

        await update.message.reply_text(message)

    except Exception as e:
        await update.message.reply_text(
            f"Ошибка при получении статистики {type(e)} {e}. Проверьте формат команды."
        )


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)],
        states={
            ASKING_CURRENCY: [MessageHandler(filters.TEXT, handle_currency)],
            ASKING_CATEGORY: [MessageHandler(filters.TEXT, handle_category)],
            ASKING_COMMENT_CHOICE: [
                MessageHandler(filters.TEXT, handle_comment_choice)
            ],
            ASKING_COMMENT: [MessageHandler(filters.TEXT, handle_comment)],
        },
        fallbacks=[],
        conversation_timeout=TIMEOUT_SECONDS,
        per_user=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(conv_handler)

    print("Bot starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
