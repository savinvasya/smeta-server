import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.types import Message, Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from yookassa import Configuration, Payment

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
DB_PATH = os.getenv("DB_PATH", "users.db")
ADMIN_ID = int(os.environ["ADMIN_ID"])
MAX_AUTH_AGE = 24 * 60 * 60

# --- НАСТРОЙКИ ЮKASSA ---
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "")
Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

# Планы теперь в рублях!
PLANS = {
    "month": {"amount": 499.00, "title": "Доступ на 1 месяц", "seconds": 30 * 86400},
    "year": {"amount": 4990.00, "title": "Доступ на 1 год", "seconds": 365 * 86400},
}

if len(WEBHOOK_SECRET) < 16 or not all(c.isalnum() or c in "_-" for c in WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET must contain only A-Z, a-z, 0-9, _ or -")
if not PUBLIC_BASE_URL.startswith("https://"):
    raise RuntimeError("PUBLIC_BASE_URL must start with https://")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "Добро пожаловать в <b>Калькулятор работ</b>! 👋\n\n"
        "Это профессиональный инструмент для мастеров и строительных бригад.\n\n"
        "💳 <b>Стоимость доступа:</b>\n"
        "• 1 месяц — <b>499 ₽</b>\n"
        "• 1 год — <b>4990 ₽</b>\n\n"
        "🎁 <b>Пробный период: 14 дней бесплатно!</b>\n\n"
        "Нажмите кнопку ниже, чтобы начать работу 👇"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть калькулятор", web_app=WebAppInfo(url="https://savinvasya.github.io/Tile-calculator1/"))],
        [InlineKeyboardButton(text="💳 Узнать про подписку", callback_data="show_prices")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

@dp.message(Command("give_access"))
async def give_access(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    args = (message.text or "").split()
    if len(args) != 3:
        await message.answer("❌ Использование: <code>/give_access ID_пользователя КОЛИЧЕСТВО_ДНЕЙ</code>", parse_mode="HTML")
        return
    try:
        target_id = int(args[1])
        days = int(args[2])
    except ValueError:
        await message.answer("❌ ID и дни должны быть целыми числами.")
        return
    now = int(time.time())
    end_date = now + days * 86400
    try:
        with db() as conn:
            conn.execute("""INSERT OR IGNORE INTO users(user_id, sub_end_date, username, trial_end_date) VALUES (?, ?, ?, ?)""", (target_id, end_date, "AdminGranted", now))
            conn.execute("UPDATE users SET sub_end_date = MAX(sub_end_date, ?) WHERE user_id = ?", (end_date, target_id))
    except Exception:
        logger.exception("Failed to grant access")
        await message.answer("❌ Ошибка выдачи доступа.")
        return
    await message.answer(f"✅ Пользователю <code>{target_id}</code> выдан доступ на {days} дней.", parse_mode="HTML")

@dp.callback_query(F.data == "show_prices")
async def show_prices(callback):
    text = (
        "💳 <b>Подписка на Калькулятор работ</b>\n\n"
        "Оплата принимается российскими картами и через СБП.\n\n"
        "📅 <b>Тариф «1 месяц»</b> — 499 ₽\n"
        "📅 <b>Тариф «1 год»</b> — 4990 ₽ (выгода 15%)\n\n"
        "После оплаты доступ активируется автоматически."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 499 ₽ (1 месяц)", callback_data="buy_month")],
        [InlineKeyboardButton(text="Оплатить 4990 ₽ (1 год)", callback_data="buy_year")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback):
    await callback.message.delete()
    await cmd_start(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_"))
async def buy_placeholder(callback):
    await callback.answer("Пожалуйста, откройте калькулятор и оформите оплату там (кнопка «Купить»).", show_alert=True)

@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    try:
        connection.execute("PRAGMA busy_timeout=15000")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, sub_end_date INTEGER NOT NULL, username TEXT, trial_end_date INTEGER
        )""")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "trial_end_date" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN trial_end_date INTEGER")
        
        # Таблица для платежей ЮKassa
        conn.execute("""CREATE TABLE IF NOT EXISTS yookassa_payments (
            payment_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, plan TEXT NOT NULL, amount REAL NOT NULL, paid_at INTEGER NOT NULL
        )""")

def telegram_user(init_data: str | None) -> dict:
    if not init_data: raise HTTPException(401, "Missing Telegram initData")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        signature = data.pop("hash")
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature): raise ValueError("Signature mismatch")
        now = int(time.time())
        auth_date = int(data["auth_date"])
        if auth_date > now + 60 or now - auth_date > MAX_AUTH_AGE: raise ValueError("Expired initData")
        user = json.loads(data["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0: raise ValueError("Invalid user")
        return user
    except Exception as exc:
        raise HTTPException(403, "Invalid or expired Telegram initData") from exc

class AuthRequest(BaseModel): initData: str
class PurchaseRequest(AuthRequest): plan: str

def ensure_user(user: dict):
    now = int(time.time())
    with db() as conn:
        conn.execute("""INSERT OR IGNORE INTO users(user_id, sub_end_date, username, trial_end_date)
                        VALUES (?, ?, ?, ?)""", (user["id"], now + 0 * 86400, user.get("username", ""), now + 0 * 86400))
        conn.execute("UPDATE users SET username=? WHERE user_id=?", (user.get("username", ""), user["id"]))
        return conn.execute("SELECT sub_end_date, trial_end_date FROM users WHERE user_id=?", (user["id"],)).fetchone()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/webhook", secret_token=WEBHOOK_SECRET, allowed_updates=["message", "callback_query"])
    try:
        yield
    finally:
        await bot.session.close()

app = FastAPI(lifespan=lifespan)
origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

@app.post("/api/check_subscription")
async def check_subscription(body: AuthRequest):
    user = telegram_user(body.initData)
    end, trial_end = ensure_user(user)
    now = int(time.time())
    return {"status": "ok", "has_access": now < end, "is_trial": trial_end is not None and now < trial_end, "end_date": end}

@app.post("/api/buy_subscription")
async def buy_subscription(body: PurchaseRequest):
    user = telegram_user(body.initData)
    if body.plan not in PLANS: raise HTTPException(400, "Unknown plan")
    ensure_user(user)
    plan = PLANS[body.plan]
    
    try:
        # Создаем платеж в ЮKassa
        payment = Payment.create({
            "amount": {"value": f"{plan['amount']:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://savinvasya.github.io/Tile-calculator1/"},
            "capture": True,
            "description": plan["title"],
            "metadata": {"user_id": user["id"], "plan": body.plan}
        }, secrets.token_hex(16))
        return {"status": "ok", "invoice_link": payment.confirmation.confirmation_url}
    except Exception:
        logger.exception("YooKassa payment creation failed")
        raise HTTPException(502, "Unable to create YooKassa payment")

@app.post("/api/yookassa/webhook")
async def yookassa_webhook(request: Request):
    try:
        data = await request.json()
        if data.get("event") == "payment.succeeded":
            payment_id = data["object"]["id"]
            metadata = data["object"].get("metadata", {})
            user_id = int(metadata.get("user_id", 0))
            plan_key = metadata.get("plan")
            
            if user_id and plan_key in PLANS:
                now = int(time.time())
                plan = PLANS[plan_key]
                with db() as conn:
                    inserted = conn.execute("""INSERT OR IGNORE INTO yookassa_payments(payment_id, user_id, plan, amount, paid_at)
                        VALUES (?, ?, ?, ?, ?)""", (payment_id, user_id, plan_key, plan["amount"], now))
                    if inserted.rowcount > 0:
                        conn.execute("""INSERT OR IGNORE INTO users(user_id, sub_end_date, username, trial_end_date) VALUES (?, ?, ?, NULL)""", (user_id, now, ""))
                        conn.execute("UPDATE users SET sub_end_date = MAX(sub_end_date, ?) + ? WHERE user_id=?",
                                     (now, plan["seconds"], user_id))
        return {"status": "ok"}
    except Exception:
        logger.exception("YooKassa webhook error")
        return {"status": "error"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(403, "Forbidden")
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
    except Exception as exc:
        raise HTTPException(400, "Invalid update") from exc
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
def root():
    return {"status": "Server is running"}
