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
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# === НАЛАШТУВАННЯ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Змінна середовища BOT_TOKEN не встановлена!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "5482150373"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Primeza777")
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("Змінна DATABASE_URL обов'язкова!")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Повні браузерні заголовки для обходу блокувань
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

# === ХЕЛПЕРИ ТА БАЗА ДАНИХ ===
def normalize_autoria_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url)
        params = parse_qs(parsed.query)
        params["order"] = ["7"]  # Сортування за датою
        params["page"] = ["0"]   # Перша сторінка
        return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
    except Exception as e:
        logging.error(f"Помилка нормалізації URL: {e}")
        return raw_url

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_cars (
                user_id BIGINT, 
                car_id TEXT, 
                PRIMARY KEY(user_id, car_id)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_filters (
                id SERIAL PRIMARY KEY, 
                user_id BIGINT, 
                url TEXT,
                UNIQUE(user_id, url)
            );
        """)

async def safe_send_message(user_id: int, text: str) -> bool:
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await safe_send_message(user_id, text)
    except TelegramForbiddenError:
        logging.info(f"Користувач {user_id} заблокував бота")
        return False
    except Exception as e:
        logging.error(f"Помилка відправки для {user_id}: {e}")
        return False

# === ПАРСИНГ AUTO.RIA ===
async def parse_autoria(session: aiohttp.ClientSession, url: str) -> list:
    try:
        async with session.get(url, headers=HEADERS, timeout=15) as resp:
            if resp.status != 200:
                logging.warning(f"Auto.ria повернув статус {resp.status}")
                return []
            
            html_text = await resp.text()
            soup = BeautifulSoup(html_text, "html.parser")
            cars = []
            
            sections = soup.find_all("section", class_="ticket-item")
            if not sections:
                logging.info("Парсер не знайшов жодного 'ticket-item'")

            for section in sections:
                car_id = section.get("data-id") or section.get("data-good-id")
                if not car_id: 
                    continue
                
                title_elem = section.find("a", class_="address")
                price_elem = section.find("span", class_="size22") or section.find("span", class_="price-ticket")
                
                title = title_elem.text.strip() if title_elem else "Автомобіль"
                price = price_elem.text.strip() if price_elem else "Ціну не вказано"
                link = title_elem.get("href") if title_elem else ""

                cars.append({
                    "car_id": str(car_id), 
                    "title": title, 
                    "price": price,
                    "link": link
                })
            return cars
    except Exception as e:
        logging.error(f"Помилка парсингу {url}: {e}")
        return []

# === КЛАВІАТУРИ ===
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📁 Мої фільтри"), KeyboardButton(text="➕ Додати посилання")]
        ],
        resize_keyboard=True
    )

# === ОБРОБНИКИ ТЕЛЕГРАМ ===
@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer(
        "👋 **Вітаю! Я бот для моніторингу Auto.ria.**\n\n"
        "Надішли мені посилання на пошук з Auto.ria, і я буду сповіщати про нові авто в реальному часі!",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "📁 Мої фільтри")
async def show_filters(msg: types.Message):
    async with db_pool.acquire() as conn:
        filters = await conn.fetch("SELECT id, url FROM user_filters WHERE user_id = $1", msg.from_user.id)
    
    if not filters:
        await msg.answer("У вас немає збережених фільтрів. Натисніть «➕ Додати посилання».")
        return

    await msg.answer("📋 **Ваші активні відстеження:**", parse_mode="Markdown")
    for f in filters:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Видалити", callback_data=f"del_{f['id']}")]
        ])
        await msg.answer(f"🔗 `{html.escape(f['url'])}`", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("del_"))
async def delete_filter(call: types.CallbackQuery):
    filter_id = int(call.data.split("_")[1])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM user_filters WHERE id = $1 AND user_id = $2", filter_id, call.from_user.id)
    await call.answer("Фільтр видалено!", show_alert=True)
    await call.message.delete()

@dp.message(F.text == "➕ Додати посилання")
async def add_prompt(msg: types.Message):
    await msg.answer("Надішліть скопійоване посилання з результатами пошуку Auto.ria сюди у чат.")

@dp.message(F.text.startswith("http"))
async def add_filter(msg: types.Message):
    raw_url = msg.text.strip()
    if "auto.ria.com" not in raw_url:
        await msg.answer("❌ Посилання має бути саме з сайту `auto.ria.com`!", parse_mode="Markdown")
        return

    url = normalize_autoria_url(raw_url)
    user_id = msg.from_user.id

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_filters (user_id, url) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, url
            )

        # Прогрів теми: записуємо вже існуючі авто, щоб не спамити ними
        async with aiohttp.ClientSession() as session:
            current_cars = await parse_autoria(session, url)
            if current_cars:
                records = [(user_id, c["car_id"]) for c in current_cars]
                async with db_pool.acquire() as conn:
                    await conn.executemany(
                        "INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        records
                    )

        await msg.answer("✅ **Фільтр успішно додано!**\nПоточні авто збережено. Сповіщення прийдуть тільки на **нові** оголошення.", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Помилка додавання фільтра: {e}")
        await msg.answer("❌ Сталася помилка при збереженні посилання.")

# === ЦИКЛ МОНІТОРИНГУ ===
async def monitor():
    while True:
        try:
            async with db_pool.acquire() as conn:
                filters = await conn.fetch("SELECT user_id, url FROM user_filters")

            if filters:
                async with aiohttp.ClientSession() as session:
                    for f in filters:
                        user_id = f["user_id"]
                        url = f["url"]
                        cars = await parse_autoria(session, url)

                        for car in cars:
                            async with db_pool.acquire() as conn:
                                already_sent = await conn.fetchval(
                                    "SELECT 1 FROM sent_cars WHERE user_id = $1 AND car_id = $2",
                                    user_id, car["car_id"]
                                )

                            if not already_sent:
                                safe_title = html.escape(car["title"])
                                safe_price = html.escape(car["price"])
                                safe_link = html.escape(car["link"], quote=True)
                                
                                msg_text = (
                                    f"🚗 <b>Нове оголошення!</b>\n\n"
                                    f"📌 <b>{safe_title}</b>\n"
                                    f"💰 <b>Ціна:</b> {safe_price}\n\n"
                                    f'🔗 <a href="{safe_link}">Переглянути на Auto.ria</a>'
                                )

                                sent_ok = await safe_send_message(user_id, msg_text)
                                if sent_ok:
                                    async with db_pool.acquire() as conn:
                                        await conn.execute(
                                            "INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                                            user_id, car["car_id"]
                                        )
                                await asyncio.sleep(0.5)

        except Exception as e:
            logging.error(f"Помилка у циклі моніторингу: {e}")

        await asyncio.sleep(60)

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    await init_db()
    asyncio.create_task(monitor())
    logging.info("Бот готовий до роботи!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
