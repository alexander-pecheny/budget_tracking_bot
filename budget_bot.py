import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import urllib.parse
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml
from aiohttp import ClientSession, web
from pydantic import BaseModel, ConfigDict, Field, ValidationError
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
CATEGORIES_NO_COMMENT: list[str] = config.get("categories_no_comment", [])
TIMEOUT_SECONDS: int = config["timeout_seconds"]
EXCHANGE_RATES: dict[str, float] = config["exchange_rates"]
WEBAPP_API_URL: str = config.get("webapp_api_url", "")
WEBAPP_API_PORT: int = config.get("webapp_api_port", 0)
USER_NAMES: dict[int, str] = {int(k): v for k, v in config.get("user_names", {}).items()}
OPENROUTER_API_KEY: str = config.get("openrouter_api_key") or os.getenv(
    "OPENROUTER_API_KEY", ""
)
OPENROUTER_API_URL: str = config.get(
    "openrouter_api_url", "https://openrouter.ai/api/v1/chat/completions"
)
OPENROUTER_MODEL: str = config.get("openrouter_model", "google/gemini-2.5-flash-lite")
OPENROUTER_APP_NAME: str = config.get("openrouter_app_name", "Budget Tracking Bot")
OPENROUTER_SITE_URL: str = config.get("openrouter_site_url", "")
FFMPEG_PATH: str = config.get("ffmpeg_path") or os.getenv("FFMPEG_PATH", "")


def make_config_enum(name: str, values: list[str]) -> type[Enum]:
    return Enum(name, {f"VALUE_{i}": value for i, value in enumerate(values)})


CurrencyEnum = make_config_enum("CurrencyEnum", CURRENCIES)
CategoryEnum = make_config_enum("CategoryEnum", CATEGORIES)


class VoiceTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    amount: float = Field(
        gt=0,
        description="Positive spending amount exactly as spoken, without currency conversion.",
    )
    currency: CurrencyEnum = Field(
        description=(
            "Currency enum value. The speech is in Russian; map Russian mentions "
            "of Serbian dinars to RSD."
        )
    )
    category: CategoryEnum = Field(
        description="Best matching spending category enum value."
    )
    comment: str = Field(
        min_length=1,
        description=(
            "Full transcript of the user's speech as detected from the audio, "
            "in the original language."
        ),
    )


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

    def get_all_recent_transactions(self, limit: int = 50) -> list[dict]:
        conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor: sqlite3.Cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, amount, currency, category, comment, timestamp
            FROM transactions
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        """,
            (limit,),
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


def get_ffmpeg_path() -> str:
    candidates = [
        FFMPEG_PATH,
        shutil.which("ffmpeg") or "",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("ffmpeg is required to transcode voice messages.")


def get_openrouter_headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = OPENROUTER_SITE_URL
    if OPENROUTER_APP_NAME:
        headers["X-Title"] = OPENROUTER_APP_NAME
    return headers


def get_voice_transaction_schema() -> dict[str, Any]:
    schema = VoiceTransaction.model_json_schema()
    schema.pop("$defs", None)
    schema["additionalProperties"] = False
    schema["properties"]["currency"] = {
        "type": "string",
        "enum": CURRENCIES,
        "description": "Currency enum value. Map Serbian dinar/dinars/dinara to RSD.",
    }
    schema["properties"]["category"] = {
        "type": "string",
        "enum": CATEGORIES,
        "description": "Best matching spending category enum value.",
    }
    return schema


def get_voice_extraction_prompt() -> str:
    categories = ", ".join(CATEGORIES)
    currencies = ", ".join(CURRENCIES)
    return (
        "The attached voice message is in Russian. "
        "Extract one spending transaction from it. "
        "Return only the structured JSON requested by the schema. "
        f"Allowed currencies: {currencies}. "
        f"Allowed categories: {categories}. "
        "Use RSD for Russian mentions of Serbian dinars, including "
        "'динар', 'динара', 'динаров', and 'сербских динаров'. "
        "The comment field must be the full transcript of the user's speech as "
        "you detected it, in Russian, not a summary or translation."
    )


async def parse_voice_transaction(audio_base64: str) -> VoiceTransaction:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OpenRouter API key is not configured.")

    payload: dict[str, Any] = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You parse personal expense voice messages into a single "
                    "validated spending transaction."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": get_voice_extraction_prompt()},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_base64,
                            "format": "ogg",
                        },
                    },
                ],
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "voice_transaction",
                "strict": True,
                "schema": get_voice_transaction_schema(),
            },
        },
        "provider": {"require_parameters": True},
        "temperature": 0,
        "max_tokens": 300,
    }

    async with ClientSession() as session:
        async with session.post(
            OPENROUTER_API_URL,
            headers=get_openrouter_headers(),
            json=payload,
            timeout=60,
        ) as response:
            response_text = await response.text()
            if response.status >= 400:
                logging.error("OpenRouter error %s: %s", response.status, response_text)
                raise RuntimeError("OpenRouter request failed.")

    try:
        response_data = json.loads(response_text)
        content = response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        logging.error("Unexpected OpenRouter response: %s", response_text)
        raise RuntimeError("OpenRouter returned an unexpected response.") from e

    if not isinstance(content, str):
        raise RuntimeError("OpenRouter response did not include JSON content.")

    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError as e:
        logging.error("OpenRouter returned invalid JSON: %s", content)
        raise RuntimeError("OpenRouter returned invalid JSON.") from e

    return VoiceTransaction.model_validate(parsed_content)


async def transcode_to_ogg_vorbis(input_path: Path, output_path: Path) -> None:
    ffmpeg_path = get_ffmpeg_path()
    errors: list[str] = []
    for encoder in ("libvorbis", "vorbis"):
        strict_args = ["-strict", "-2"] if encoder == "vorbis" else []
        channels = "2" if encoder == "vorbis" else "1"
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path,
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            channels,
            "-ar",
            "16000",
            "-codec:a",
            encoder,
            "-q:a",
            "3",
            *strict_args,
            str(output_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode == 0:
            return
        errors.append(
            f"{encoder}: {stderr.decode('utf-8', errors='replace')}"
        )

    logging.error("ffmpeg failed: %s", "\n".join(errors))
    raise RuntimeError("Failed to transcode voice message.")


async def download_voice_audio_base64(update: Update) -> str:
    if not update.message or not update.message.voice:
        raise RuntimeError("No voice message found.")

    voice_file = await update.message.voice.get_file()
    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = Path(tmpdir) / "voice-opus.ogg"
        vorbis_path = Path(tmpdir) / "voice-vorbis.ogg"
        await voice_file.download_to_drive(custom_path=source_path)
        await transcode_to_ogg_vorbis(source_path, vorbis_path)
        return base64.b64encode(vorbis_path.read_bytes()).decode("ascii")


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


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    if not OPENROUTER_API_KEY:
        await update.message.reply_text(
            "❌ OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
            "or openrouter_api_key in config.yaml."
        )
        return

    await update.message.reply_text("Обрабатываю голосовое сообщение...")

    try:
        audio_base64 = await download_voice_audio_base64(update)
        transaction = await parse_voice_transaction(audio_base64)
    except ValidationError as e:
        logging.exception("Voice transaction validation failed: %s", e)
        await update.message.reply_text(
            "❌ Не удалось распознать расход в голосовом сообщении."
        )
        return
    except Exception as e:
        logging.exception("Voice transaction parsing failed: %s", e)
        await update.message.reply_text(
            f"❌ Ошибка при обработке голосового сообщения: {type(e).__name__}"
        )
        return

    amount = float(transaction.amount)
    currency = str(transaction.currency)
    category = str(transaction.category)
    comment = transaction.comment.strip()

    if currency not in CURRENCIES or category not in CATEGORIES:
        await update.message.reply_text(
            "❌ Модель вернула некорректную валюту или категорию."
        )
        return

    db.add_transaction(
        user_id=update.effective_user.id,
        amount=amount,
        currency=currency,
        category=category,
        comment=comment,
    )

    message = (
        f"✅ Транзакция добавлена!\n"
        f"Сумма: {amount:g} {currency}\n"
        f"Категория: {category}\n"
        f"Комментарий: {comment}"
    )
    await update.message.reply_text(
        message, reply_markup=get_webapp_keyboard(update.effective_user.id)
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    await update.message.reply_text(
        "Привет! Нажмите кнопку ниже, отправьте сумму числом или голосовое сообщение.\n"
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
    transactions = db.get_all_recent_transactions()
    for tx in transactions:
        tx["user_name"] = USER_NAMES.get(tx["user_id"], str(tx["user_id"]))
        tx["is_own"] = tx["user_id"] == user_id
        del tx["user_id"]
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
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
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
