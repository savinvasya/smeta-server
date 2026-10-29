from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot
from aiogram.types import LabeledPrice
import hashlib
import hmac
import json
import sqlite3
from urllib.parse import parse_qsl

BOT_TOKEN = "8393778033:AAHRsfhdJ0_6knfmuhuApehcmbJHZZqk9bU"
bot = Bot(token=BOT_TOKEN)

app = FastAPI()

# Разрешаем запросы с любых доменов (чтобы ваш HTML мог обращаться к серверу)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Инициализация базы данных (SQLite)
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            sub_end_date INTEGER,
            username TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def check_telegram_auth(init_data: str) -> dict:
    """Проверяем, что данные пришли именно от Telegram"""
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data:
            raise ValueError("No hash")
        
        hash_from_tg = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash != hash_from_tg:
            raise ValueError("Invalid hash")
        
        user_data = json.loads(parsed_data["user"])
        return user_data
    except Exception as e:
        raise HTTPException(status_code=403, detail="Invalid initData")

@app.post("/api/check_subscription")
async def check_subscription(request: Request):
    """Проверка подписки при загрузке приложения"""
    body = await request.json()
    init_data = body.get("initData")
    
    if not init_data:
        raise HTTPException(status_code=400, detail="No initData")
    
    user = check_telegram_auth(init_data)
    user_id = user['id']
    
    # Ищем пользователя в базе
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("SELECT sub_end_date FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    import time
    current_time = int(time.time())
    
    # По умолчанию даем 14 дней бесплатно (TRIAL)
    if not result:
        trial_end = current_time + (14 * 24 * 60 * 60) # 14 дней
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (user_id, sub_end_date, username) VALUES (?, ?, ?)", 
                       (user_id, trial_end, user.get('username', '')))
        conn.commit()
        conn.close()
        return {"status": "ok", "has_access": True, "is_trial": True}
    
    sub_end_date = result[0]
    has_access = current_time < sub_end_date
    
    return {"status": "ok", "has_access": has_access, "end_date": sub_end_date}

@app.post("/api/buy_subscription")
async def buy_subscription(request: Request):
    """Создание счета на оплату (Telegram Stars)"""
    body = await request.json()
    init_data = body.get("initData")
    plan = body.get("plan") # "month" или "year"
    
    user = check_telegram_auth(init_data)
    
    # Цены в Telegram Stars (XTR). 1 звезда ≈ 1.3-1.5 рубля
    # 990 руб ≈ 700 звезд. 8900 руб ≈ 6300 звезд.
    if plan == "month":
        price = 700 
        title = "Подписка на 1 месяц"
        duration = 30 * 24 * 60 * 60
    elif plan == "year":
        price = 6300
        title = "Подписка на 1 год"
        duration = 365 * 24 * 60 * 60
    else:
        raise HTTPException(status_code=400, detail="Unknown plan")

    # Генерируем ссылку на оплату через Telegram
    invoice_link = await bot.create_invoice_link(
        title=title,
        description="Доступ к профессиональному калькулятору смет",
        payload=f"sub_{plan}_{user['id']}_{duration}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=price)]
    )
    
    return {"status": "ok", "invoice_link": invoice_link}

@app.get("/")
def read_root():
    return {"status": "Server is running"}