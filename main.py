import asyncio
import html
import logging
import os
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import aiohttp
import asyncpg
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# === НАЛАШТУВАННЯ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5482150373"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Primeza777")
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# РЕАЛІСТИЧНІ ЗАГОЛОВКИ (обхід Cloudflare)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1"
}

db_pool: Optional[asyncpg.Pool] = None

# === ФУНКЦІЇ ===
def normalize_autoria_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url)
        params = parse_qs(parsed.query)
        params["order"] = ["7"]  # Сортування за датою
        params["page"] = ["0"]   # Перша сторінка
        return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
    except:
        return raw_url

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    await db_pool.execute("CREATE TABLE IF NOT EXISTS sent_cars (user_id BIGINT, car_id TEXT, UNIQUE(user_id, car_id));")
    await db_pool.execute("CREATE TABLE IF NOT EXISTS user_filters (id SERIAL PRIMARY KEY, user_id BIGINT, url TEXT);")

async def parse_autoria(session: aiohttp.ClientSession, url: str) -> list:
    try:
        async with session.get(url, headers=HEADERS, timeout=15) as resp:
            html_text = await resp.text()
            soup = BeautifulSoup(html_text, "html.parser")
            cars = []
            for section in soup.find_all("section", class_="ticket-item"):
                car_id = section.get("data-id")
                if not car_id: continue
                title = section.find("a", class_="address")
                cars.append({"car_id": car_id, "title": title.text.strip() if title else "Авто", "link": title.get("href") if title else ""})
            return cars
    except Exception as e:
        logging.error(f"Помилка парсингу: {e}")
        return []

# === ОБРОБНИКИ ===
@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer("Бот готовий! Надсилай посилання з Auto.ria.", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📁 Мої фільтри"), KeyboardButton(text="➕ Додати посилання")]], resize_keyboard=True))

@dp.message(F.text.startswith("http"))
async def add_filter(msg: types.Message):
    url = normalize_autoria_url(msg.text)
    await db_pool.execute("INSERT INTO user_filters (user_id, url) VALUES ($1, $2)", msg.from_user.id, url)
    await msg.answer("✅ Посилання додано. Тепер чекаємо на нові авто!")

# === ГОЛОВНИЙ ЦИКЛ ===
async def monitor():
    while True:
        async with aiohttp.ClientSession() as session:
            filters = await db_pool.fetch("SELECT * FROM user_filters")
            for f in filters:
                cars = await parse_autoria(session, f['url'])
                for car in cars:
                    res = await db_pool.fetchval("SELECT 1 FROM sent_cars WHERE user_id=$1 AND car_id=$2", f['user_id'], car['car_id'])
                    if not res:
                        await bot.send_message(f['user_id'], f"🚗 Нове авто: {car['title']}\n{car['link']}")
                        await db_pool.execute("INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2)", f['user_id'], car['car_id'])
        await asyncio.sleep(60)

async def main():
    await init_db()
    asyncio.create_task(monitor())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
