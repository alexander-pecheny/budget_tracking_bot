import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml
from aiohttp import web
from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

with open("config.yaml", "r", encoding="utf-8") as f:
    config: dict = yaml.safe_load(f)

BOT_TOKEN: str = config["bot_token"]
AUTHORIZED_USERS: list[int] = config["authorized_users"]
DATABASE_PATH: str = config["database_path"]
CURRENCIES: list[str] = config["currencies"]
CATEGORIES: list[str] = config["categories"]
CATEGORIES_NO_COMMENT: list[str] = config["categories_no_comment"]
TIMEOUT_SECONDS: int = config["timeout_seconds"]
EXCHANGE_RATES: dict[str, float] = config["exchange_rates"]
WEBAPP_API_URL: str = config.get("webapp_api_url", "")
WEBAPP_API_PORT: int = config.get("webapp_api_port", 0)

def make_api_token(user_id: int) -> str:
    return hashlib.sha256(f"webapp-{BOT_TOKEN}-{user_id}".encode()).hexdigest()[:32]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

ASKING_CURRENCY: int
ASKING_CATEGORY: int
ASKING_COMMENT: int
ASKING_CURRENCY, ASKING_CATEGORY, ASKING_COMMENT = range(3)


class BudgetDatabase:
    def __init__(self, db_path: str = DATABASE_PATH) -> None:
        self.db_path: str = db_path
        self.init_database()

    def init_database(self) -> None:
        conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        cursor: sqlite3.Cursor = conn.cursor()
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
        comment: Optional[str] = None,
    ) -> None:
        conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        cursor: sqlite3.Cursor = conn.cursor()
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
        start_date: datetime,
        end_date: datetime,
        user_id: Optional[int] = None,
    ) -> list[tuple[str, str, float]]:
        conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        cursor: sqlite3.Cursor = conn.cursor()

        if user_id is not None:
            cursor.execute(
                """
                SELECT category, currency, SUM(amount) as total
                FROM transactions
                WHERE user_id = ? AND timestamp BETWEEN ? AND ?
                GROUP BY category, currency
                ORDER BY total DESC
            """,
                (user_id, start_date.isoformat(), end_date.isoformat()),
            )
        else:
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

        results: list[tuple[str, str, float]] = cursor.fetchall()
        conn.close()
        return results

    def delete_last_transaction(self, user_id: int) -> bool:
        conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        cursor: sqlite3.Cursor = conn.cursor()

        cursor.execute(
            """
            SELECT id FROM transactions
            WHERE user_id = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
        """,
            (user_id,),
        )

        result: Optional[tuple[int]] = cursor.fetchone()
        if not result:
            conn.close()
            return False

        transaction_id: int = result[0]
        cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
        conn.commit()
        conn.close()
        return True

    def get_recent_transactions(self, user_id: int, limit: int = 30) -> list[dict]:
        conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor: sqlite3.Cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, amount, currency, category, comment, timestamp
            FROM transactions
            WHERE user_id = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        """,
            (user_id, limit),
        )
        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return results

    def update_transaction(
        self,
        transaction_id: int,
        user_id: int,
        amount: float,
        currency: str,
        category: str,
        comment: Optional[str] = None,
    ) -> bool:
        conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        cursor: sqlite3.Cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE transactions
            SET amount = ?, currency = ?, category = ?, comment = ?
            WHERE id = ? AND user_id = ?
        """,
            (amount, currency, category, comment, transaction_id, user_id),
        )
        success: bool = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def delete_transaction(self, transaction_id: int, user_id: int) -> bool:
        conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        cursor: sqlite3.Cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?",
            (transaction_id, user_id),
        )
        success: bool = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success


db: BudgetDatabase = BudgetDatabase()


def convert_to_rsd(amount: float, currency: str) -> float:
    rate: float = EXCHANGE_RATES.get(currency, 1.0)
    return round(amount * rate, 1)


def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS


# --- Web App keyboard ---


def get_webapp_keyboard(user_id: int = 0) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if not WEBAPP_API_URL:
        return ReplyKeyboardRemove()
    token = make_api_token(user_id)
    url = f"{WEBAPP_API_URL}/app/{token}/{user_id}"
    button = KeyboardButton(text="\U0001f4dd Добавить расход", web_app=WebAppInfo(url=url))
    return ReplyKeyboardMarkup([[button]], resize_keyboard=True)


# --- Bot handlers ---


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    try:
        data = json.loads(update.effective_message.web_app_data.data)
    except (json.JSONDecodeError, AttributeError):
        await update.effective_message.reply_text("\u274c Ошибка данных.")
        return

    amount = data.get("amount")
    currency = data.get("currency")
    category = data.get("category")
    comment = data.get("comment")

    if not amount or not isinstance(amount, (int, float)) or amount <= 0:
        await update.effective_message.reply_text("\u274c Некорректная сумма.")
        return
    if currency not in CURRENCIES:
        await update.effective_message.reply_text("\u274c Некорректная валюта.")
        return
    if category not in CATEGORIES:
        await update.effective_message.reply_text("\u274c Некорректная категория.")
        return

    db.add_transaction(
        user_id=update.effective_user.id,
        amount=float(amount),
        currency=currency,
        category=category,
        comment=comment if comment else None,
    )

    message: str = (
        f"\u2705 Транзакция добавлена!\nСумма: {amount} {currency}\nКатегория: {category}"
    )
    if comment:
        message += f"\nКомментарий: {comment}"

    await update.effective_message.reply_text(
        message, reply_markup=get_webapp_keyboard(update.effective_user.id)
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    await update.message.reply_text(
        "Привет! Нажмите кнопку ниже или отправьте сумму числом.\n"
        "/stats — статистика, /delete_last — удалить последнюю.",
        reply_markup=get_webapp_keyboard(update.effective_user.id),
    )


def try_float(text: str) -> Optional[float]:
    text = text.replace(",", ".")
    try:
        return float(text.strip())
    except ValueError:
        return None


async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END

    text: str = update.message.text.strip()

    amount: Optional[float] = try_float(text)
    if amount:
        context.user_data["amount"] = amount

        keyboard: list[list[str]] = [[currency] for currency in CURRENCIES]
        reply_markup: ReplyKeyboardMarkup = ReplyKeyboardMarkup(
            keyboard, one_time_keyboard=True
        )

        await update.message.reply_text(
            f"Сумма: {amount}\nВыберите валюту:", reply_markup=reply_markup
        )
        return ASKING_CURRENCY

    return ConversationHandler.END


async def handle_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END

    currency: str = update.message.text
    if currency not in CURRENCIES:
        await update.message.reply_text(
            "Пожалуйста, выберите валюту из предложенных вариантов."
        )
        return ASKING_CURRENCY

    context.user_data["currency"] = currency

    keyboard: list[list[str]] = []
    for i in range(0, len(CATEGORIES), 2):
        row: list[str] = [CATEGORIES[i]]
        if i + 1 < len(CATEGORIES):
            row.append(CATEGORIES[i + 1])
        keyboard.append(row)

    reply_markup: ReplyKeyboardMarkup = ReplyKeyboardMarkup(
        keyboard, one_time_keyboard=True
    )

    await update.message.reply_text(
        f"Сумма: {context.user_data['amount']} {currency}\nВыберите категорию:",
        reply_markup=reply_markup,
    )
    return ASKING_CATEGORY


async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END

    category: str = update.message.text
    if category not in CATEGORIES:
        await update.message.reply_text(
            "Пожалуйста, выберите категорию из предложенных вариантов."
        )
        return ASKING_CATEGORY

    context.user_data["category"] = category

    amount: float = context.user_data["amount"]
    currency: str = context.user_data["currency"]
    amount_rsd: float = convert_to_rsd(amount, currency)

    if category in CATEGORIES_NO_COMMENT or amount_rsd < 1000:
        await save_transaction(update, context, None)
        return ConversationHandler.END

    await update.message.reply_text(
        f"Сумма: {context.user_data['amount']} {context.user_data['currency']}\n"
        f"Категория: {category}\n"
        f"Введите комментарий:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASKING_COMMENT


async def handle_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update.effective_user.id):
        return ConversationHandler.END

    comment: str = update.message.text
    await save_transaction(update, context, comment)
    return ConversationHandler.END


async def save_transaction(
    update: Update, context: ContextTypes.DEFAULT_TYPE, comment: Optional[str]
) -> None:
    user_id: int = update.effective_user.id
    amount: float = context.user_data["amount"]
    currency: str = context.user_data["currency"]
    category: str = context.user_data["category"]

    db.add_transaction(user_id, amount, currency, category, comment)

    message: str = (
        f"\u2705 Транзакция добавлена!\nСумма: {amount} {currency}\nКатегория: {category}"
    )
    if comment:
        message += f"\nКомментарий: {comment}"

    await update.message.reply_text(message, reply_markup=get_webapp_keyboard(user_id))

    context.user_data.clear()


async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Транзакция не была добавлена так как вы не ответили в течение 5 минут.",
        reply_markup=get_webapp_keyboard(update.effective_user.id),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    args: list[str] = context.args

    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None

    if not args:
        start_date = (datetime.now() - timedelta(days=30)).replace(
            hour=0, minute=0, second=0
        )
        end_date = datetime.now().replace(hour=23, minute=59, second=59)
    elif len(args) == 1:
        arg: str = args[0]
        if try_float(arg):
            start_date = (datetime.now() - timedelta(days=int(arg))).replace(
                hour=0, minute=0, second=0
            )
            end_date = datetime.now().replace(hour=23, minute=59, second=59)
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", arg):
            start_date = datetime.strptime(arg, "%Y-%m-%d")
            end_date = datetime.now().replace(hour=23, minute=59, second=59)
        else:
            await update.message.reply_text("Не тот формат.")
            return
    elif len(args) == 2:
        arg1: str
        arg2: str
        arg1, arg2 = args
        if re.match(r"^\d{4}-\d{2}-\d{2}$", arg1):
            start_date = datetime.strptime(arg1, "%Y-%m-%d")
            if re.match(r"^\d+$", arg2):
                end_date = (start_date + timedelta(days=int(arg2))).replace(
                    hour=23, minute=59, second=59
                )
            elif re.match(r"^\d{4}-\d{2}-\d{2}$", arg2):
                end_date = datetime.strptime(arg2, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59
                )
        else:
            await update.message.reply_text("Не тот формат.")
            return
    else:
        await update.message.reply_text("Не тот формат.")
        return

    assert start_date
    assert end_date

    try:
        results: list[tuple[str, str, float]] = db.get_stats(start_date, end_date)

    except Exception as e:
        await update.message.reply_text(
            f"Ошибка при получении статистики {type(e)} {e}. Проверьте формат команды."
        )

    if not results:
        await update.message.reply_text(
            f"Нет транзакций за указанный период {start_date.date()} — {end_date.date()}."
        )
        return

    category_totals: dict[str, float] = {}
    grand_total_rsd: float = 0

    for category, currency, amount in results:
        amount_rsd: float = convert_to_rsd(amount, currency)
        if category not in category_totals:
            category_totals[category] = 0
        category_totals[category] += amount_rsd
        grand_total_rsd += amount_rsd

    message: str = (
        f"\U0001f4ca Статистика расходов с {start_date.date()} по {end_date.date()}:\n\n"
    )

    for category, total_rsd in sorted(
        category_totals.items(), key=lambda x: x[1], reverse=True
    ):
        percentage: float = (
            (total_rsd / grand_total_rsd) * 100 if grand_total_rsd > 0 else 0
        )
        message += f"{category}: {total_rsd:.1f} RSD ({percentage:.1f}%)\n"

    message += f"\n\U0001f4b0 Общий расход: {grand_total_rsd:.1f} RSD"

    await update.message.reply_text(message)


async def delete_last_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_authorized(update.effective_user.id):
        return

    user_id: int = update.effective_user.id

    try:
        success: bool = db.delete_last_transaction(user_id)
        if success:
            await update.message.reply_text("\u2705 Последняя транзакция удалена.")
        else:
            await update.message.reply_text("\u274c Нет транзакций для удаления.")
    except Exception as e:
        await update.message.reply_text(
            f"Ошибка при удалении транзакции: {type(e)} {e}"
        )


async def stats_me_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    user_id: int = update.effective_user.id
    args: list[str] = context.args

    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None

    if not args:
        start_date = (datetime.now() - timedelta(days=30)).replace(
            hour=0, minute=0, second=0
        )
        end_date = datetime.now().replace(hour=23, minute=59, second=59)
    elif len(args) == 1:
        arg: str = args[0]
        if try_float(arg):
            start_date = (datetime.now() - timedelta(days=int(arg))).replace(
                hour=0, minute=0, second=0
            )
            end_date = datetime.now().replace(hour=23, minute=59, second=59)
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", arg):
            start_date = datetime.strptime(arg, "%Y-%m-%d")
            end_date = datetime.now().replace(hour=23, minute=59, second=59)
        else:
            await update.message.reply_text("Не тот формат.")
            return
    elif len(args) == 2:
        arg1: str
        arg2: str
        arg1, arg2 = args
        if re.match(r"^\d{4}-\d{2}-\d{2}$", arg1):
            start_date = datetime.strptime(arg1, "%Y-%m-%d")
            if re.match(r"^\d+$", arg2):
                end_date = (start_date + timedelta(days=int(arg2))).replace(
                    hour=23, minute=59, second=59
                )
            elif re.match(r"^\d{4}-\d{2}-\d{2}$", arg2):
                end_date = datetime.strptime(arg2, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59
                )
        else:
            await update.message.reply_text("Не тот формат.")
            return
    else:
        await update.message.reply_text("Не тот формат.")
        return

    assert start_date
    assert end_date

    try:
        results: list[tuple[str, str, float]] = db.get_stats(
            start_date, end_date, user_id
        )
    except Exception as e:
        await update.message.reply_text(
            f"Ошибка при получении статистики {type(e)} {e}. Проверьте формат команды."
        )
        return

    if not results:
        await update.message.reply_text(
            f"Нет ваших транзакций за указанный период {start_date.date()} — {end_date.date()}."
        )
        return

    category_totals: dict[str, float] = {}
    grand_total_rsd: float = 0

    for category, currency, amount in results:
        amount_rsd: float = convert_to_rsd(amount, currency)
        if category not in category_totals:
            category_totals[category] = 0
        category_totals[category] += amount_rsd
        grand_total_rsd += amount_rsd

    message: str = (
        f"\U0001f4ca Ваша статистика расходов с {start_date.date()} по {end_date.date()}:\n\n"
    )

    for category, total_rsd in sorted(
        category_totals.items(), key=lambda x: x[1], reverse=True
    ):
        percentage: float = (
            (total_rsd / grand_total_rsd) * 100 if grand_total_rsd > 0 else 0
        )
        message += f"{category}: {total_rsd:.1f} RSD ({percentage:.1f}%)\n"

    message += f"\n\U0001f4b0 Ваш общий расход: {grand_total_rsd:.1f} RSD"

    await update.message.reply_text(message)


# --- API server ---


def validate_api_token(request: web.Request) -> Optional[int]:
    token = request.headers.get("X-Api-Token", "")
    try:
        user_id = int(request.query.get("user_id", "0"))
    except ValueError:
        return None
    if not is_authorized(user_id):
        return None
    if token != make_api_token(user_id):
        return None
    return user_id


@web.middleware
async def cors_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except web.HTTPException as e:
            response = e
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, PUT, DELETE, OPTIONS"
    return response


APP_HTML: str = (Path(__file__).parent / "app.html").read_text(encoding="utf-8")


async def api_serve_app(request: web.Request) -> web.Response:
    return web.Response(text=APP_HTML, content_type="text/html")


async def api_get_config(request: web.Request) -> web.Response:
    user_id = validate_api_token(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"categories": CATEGORIES, "currencies": CURRENCIES})


async def api_get_transactions(request: web.Request) -> web.Response:
    user_id = validate_api_token(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    transactions = db.get_recent_transactions(user_id)
    return web.json_response(transactions)


async def api_update_transaction(request: web.Request) -> web.Response:
    user_id = validate_api_token(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    tid = int(request.match_info["tid"])
    body = await request.json()
    amount = body.get("amount")
    currency = body.get("currency")
    category = body.get("category")
    comment = body.get("comment")
    if not amount or currency not in CURRENCIES or category not in CATEGORIES:
        return web.json_response({"error": "invalid data"}, status=400)
    success = db.update_transaction(
        tid, user_id, float(amount), currency, category, comment or None
    )
    if not success:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})


async def api_delete_transaction(request: web.Request) -> web.Response:
    user_id = validate_api_token(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    tid = int(request.match_info["tid"])
    success = db.delete_transaction(tid, user_id)
    if not success:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": True})


# --- Main ---


async def run() -> None:
    # Start API server if configured
    runner: Optional[web.AppRunner] = None
    if WEBAPP_API_PORT:
        api_app = web.Application(middlewares=[cors_middleware])
        api_app.router.add_get("/app/{token}/{uid}", api_serve_app)
        api_app.router.add_get("/config", api_get_config)
        api_app.router.add_get("/transactions", api_get_transactions)
        api_app.router.add_put("/transactions/{tid}", api_update_transaction)
        api_app.router.add_delete("/transactions/{tid}", api_delete_transaction)
        runner = web.AppRunner(api_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBAPP_API_PORT)
        await site.start()
        print(f"API server running on port {WEBAPP_API_PORT}")

    # Build bot application
    application: Application = Application.builder().token(BOT_TOKEN).build()

    conv_handler: ConversationHandler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)],
        states={
            ASKING_CURRENCY: [MessageHandler(filters.TEXT, handle_currency)],
            ASKING_CATEGORY: [MessageHandler(filters.TEXT, handle_category)],
            ASKING_COMMENT: [MessageHandler(filters.TEXT, handle_comment)],
        },
        fallbacks=[],
        conversation_timeout=TIMEOUT_SECONDS,
        per_user=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("delete_last", delete_last_command))
    application.add_handler(CommandHandler("stats_me", stats_me_command))
    application.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data)
    )
    application.add_handler(conv_handler)

    # Start bot
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    print("Bot starting...")

    # Wait for shutdown signal
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    # Cleanup
    print("Shutting down...")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    if runner:
        await runner.cleanup()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
