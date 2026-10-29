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
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery, Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
DB_PATH = os.getenv("DB_PATH", "users.db")
MAX_AUTH_AGE = 24 * 60 * 60
PLANS = {
    "month": {"stars": 700, "seconds": 30 * 86400, "title": "Доступ на 30 дней"},
    "year": {"stars": 6300, "seconds": 365 * 86400, "title": "Доступ на 365 дней"},
}

if len(WEBHOOK_SECRET) < 16 or not all(c.isalnum() or c in "_-" for c in WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET must contain only A-Z, a-z, 0-9, _ or -")
if not PUBLIC_BASE_URL.startswith("https://"):
    raise RuntimeError("PUBLIC_BASE_URL must start with https://")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

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
            user_id INTEGER PRIMARY KEY,
            sub_end_date INTEGER NOT NULL,
            username TEXT,
            trial_end_date INTEGER
        )""")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "trial_end_date" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN trial_end_date INTEGER")
        conn.execute("""CREATE TABLE IF NOT EXISTS invoices (
            payload TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS payments (
            telegram_charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            stars INTEGER NOT NULL,
            paid_at INTEGER NOT NULL
        )""")

def telegram_user(init_data: str | None) -> dict:
    if not init_data:
        raise HTTPException(401, "Missing Telegram initData")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError("Duplicate fields")
        signature = data.pop("hash")
        if len(signature) != 64:
            raise ValueError("Invalid hash length")
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("Signature mismatch")
        now = int(time.time())
        auth_date = int(data["auth_date"])
        if auth_date > now + 60 or now - auth_date > MAX_AUTH_AGE:
            raise ValueError("Expired initData")
        user = json.loads(data["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            raise ValueError("Invalid user")
        return user
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(403, "Invalid or expired Telegram initData") from exc

class AuthRequest(BaseModel):
    initData: str

class PurchaseRequest(AuthRequest):
    plan: str

def ensure_user(user: dict):
    now = int(time.time())
    with db() as conn:
        conn.execute("""INSERT OR IGNORE INTO users(user_id, sub_end_date, username, trial_end_date)
                        VALUES (?, ?, ?, ?)""", (user["id"], now + 14 * 86400, user.get("username", ""), now + 14 * 86400))
        conn.execute("UPDATE users SET username=? WHERE user_id=?", (user.get("username", ""), user["id"]))
        return conn.execute("SELECT sub_end_date, trial_end_date FROM users WHERE user_id=?", (user["id"],)).fetchone()

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    try:
        with db() as conn:
            invoice = conn.execute("SELECT user_id, plan FROM invoices WHERE payload=?", (query.invoice_payload,)).fetchone()
        if not invoice or invoice[0] != query.from_user.id or invoice[1] not in PLANS or query.currency != "XTR" or query.total_amount != PLANS[invoice[1]]["stars"]:
            await query.answer(ok=False, error_message="Счёт недействителен. Создайте новый счёт.")
            return
        await query.answer(ok=True)
    except Exception:
        logger.exception("Pre-checkout verification failed")
        await query.answer(ok=False, error_message="Ошибка проверки платежа. Попробуйте позже.")

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    if not payment or not message.from_user:
        return
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            invoice = conn.execute("SELECT user_id, plan FROM invoices WHERE payload=?", (payment.invoice_payload,)).fetchone()
            if not invoice or invoice[0] != message.from_user.id or invoice[1] not in PLANS:
                logger.error("Unknown or mismatched paid invoice: %s", payment.invoice_payload)
                return
            user_id, plan = invoice
            if payment.currency != "XTR" or payment.total_amount != PLANS[plan]["stars"]:
                logger.error("Paid amount mismatch for user %s", user_id)
                return
            inserted = conn.execute("""INSERT OR IGNORE INTO payments(telegram_charge_id, user_id, plan, stars, paid_at)
                VALUES (?, ?, ?, ?, ?)""", (payment.telegram_payment_charge_id, user_id, plan, payment.total_amount, int(time.time())))
            if inserted.rowcount == 0:
                return
            now = int(time.time())
            conn.execute("""INSERT OR IGNORE INTO users(user_id, sub_end_date, username, trial_end_date)
                VALUES (?, ?, ?, NULL)""", (user_id, now, message.from_user.username or ""))
            conn.execute("UPDATE users SET sub_end_date = MAX(sub_end_date, ?) + ? WHERE user_id=?",
                         (now, PLANS[plan]["seconds"], user_id))
        await message.answer("Оплата получена! Доступ продлён. Спасибо!")
    except Exception:
        logger.exception("Payment processing failed")
        raise

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/webhook", secret_token=WEBHOOK_SECRET,
                          allowed_updates=["message", "pre_checkout_query"])
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
    return {"status": "ok", "has_access": now < end,
            "is_trial": trial_end is not None and now < trial_end,
            "end_date": end}

@app.post("/api/buy_subscription")
async def buy_subscription(body: PurchaseRequest):
    user = telegram_user(body.initData)
    if body.plan not in PLANS:
        raise HTTPException(400, "Unknown plan")
    ensure_user(user)
    plan = PLANS[body.plan]
    payload = "sub_" + secrets.token_urlsafe(24)
    with db() as conn:
        conn.execute("INSERT INTO invoices(payload, user_id, plan, created_at) VALUES (?, ?, ?, ?)",
                     (payload, user["id"], body.plan, int(time.time())))
    try:
        link = await bot.create_invoice_link(
            title=plan["title"], description="Доступ к профессиональному калькулятору смет",
            payload=payload, currency="XTR", prices=[LabeledPrice(label=plan["title"], amount=plan["stars"])],
        )
    except Exception:
        logger.exception("Failed to create invoice")
        raise HTTPException(502, "Unable to create Telegram invoice")
    return {"status": "ok", "invoice_link": link}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(403, "Forbidden")
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "Invalid update") from exc
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
def root():
    return {"status": "Server is running"}
