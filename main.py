import logging
import os
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Message, Update
from fastapi import FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv

load_dotenv()

# Читаем только самые необходимые переменные
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "👋 Добро пожаловать в <b>Калькулятор работ</b>!\n\n"
        "Это профессиональный инструмент для мастеров и строительных бригад. "
        "Забудьте про сложные расчеты в Excel — приложение автоматически посчитает стоимость работ.\n\n"
        "📦 <b>Что входит в приложение:</b>\n"
        "• Укладка плитки (расчет по зонам, резка, затирка)\n"
        "• Электромонтаж (по точкам и по метражу кабеля)\n"
        "• Монтаж гипсокартона (каркасы, листы, подвесы)\n"
        "• Сохранение смет, экспорт в PNG, гибкий прайс-лист.\n\n"
        "Приложение абсолютно бесплатное. Приятной работы! 👇"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🚀 Открыть калькулятор", 
            web_app=WebAppInfo(url="https://savinvasya.github.io/Tile-calculator1/")
        )]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.set_webhook(
        f"{PUBLIC_BASE_URL}/telegram/webhook", 
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message"]
    )
    try:
        yield
    finally:
        await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not x_telegram_bot_api_secret_token or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
    except Exception as exc:
        raise HTTPException(400, "Invalid update") from exc
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
def root():
    return {"status": "Bot is running without limits"}