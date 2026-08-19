import asyncio
import html
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import aiohttp
import asyncpg
from aiohttp import web
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, WebhookRequest

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

# Для Render обов'язково потрібен PORT
PORT = int(os.getenv("PORT", "10000"))
# URL твого сервісу на Render (наприклад, https://autoria-bot.onrender.com)
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Host": "auto.ria.com",
    "Referer": "https://auto.ria.com/uk/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0"
}

db_pool: Optional[asyncpg.Pool] = None
http_session: Optional[aiohttp.ClientSession] = None


def normalize_autoria_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url)
        params = parse_qs(parsed.query)
        params["order"] = ["7"]
        params["page"] = ["0"]
        return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
    except Exception as e:
        logging.error(f"Помилка нормалізації URL: {e}")
        return raw_url


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY,
                subscription_expires TIMESTAMP
            );
        """)
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


async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    async with db_pool.acquire() as conn:
        expires = await conn.fetchval("SELECT subscription_expires FROM bot_users WHERE user_id = $1", user_id)
    if not expires:
        return False
    return expires > datetime.now()


async def safe_send_message(user_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.send_message(user_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return True
    except Exception as e:
        logging.error(f"Помилка відправки для {user_id}: {e}")
        return False


async def parse_autoria(session: aiohttp.ClientSession, url: str) -> list:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
            html_text = await resp.text()
            if resp.status != 200:
                return []
            soup = BeautifulSoup(html_text, "html.parser")
            sections = soup.find_all("section", class_="ticket-item")
            if not sections:
                sections = soup.select('div.ticket-item, div[data-ftid="item"], div.search-result-item')

            cars = []
            for section in sections:
                try:
                    car_id = section.get("data-id") or section.get("data-good-id")
                    if not car_id:
                        link_elem = section.find("a", class_="address") or section.find("a", href=True)
                        if link_elem and "auto.ria.com" in link_elem.get("href", ""):
                            href = link_elem.get("href")
                            parts = href.split("_")
                            if parts:
                                car_id = ''.join(filter(str.isdigit, parts[-1]))
                    if not car_id:
                        continue
                    title_elem = section.find("a", class_="address") or section.find("a", class_="m-link")
                    price_elem = section.find("span", class_="size22") or section.find("span", class_="price-ticket") or section.find("strong", class_="bold")

                    title = title_elem.text.strip() if title_elem else "Автомобіль"
                    price = price_elem.text.strip() if price_elem else "Ціну не вказано"
                    link = title_elem.get("href") if title_elem else ""
                    if link and not link.startswith("http"):
                        link = "https://auto.ria.com" + link

                    cars.append({"car_id": str(car_id), "title": title, "price": price, "link": link})
                except Exception:
                    continue
            return cars
    except Exception as e:
        logging.error(f"Помилка запиту до Auto.ria: {e}")
        return []


def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📁 Мої фільтри"), KeyboardButton(text="➕ Додати посилання")],
            [KeyboardButton(text="💎 Статус підписки"), KeyboardButton(text="📞 Зв'язатися з адміном")]
        ],
        resize_keyboard=True
    )


@dp.message(Command("start"))
async def start(msg: types.Message):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO bot_users (user_id, subscription_expires) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING",
            msg.from_user.id, datetime.now() + timedelta(days=1)
        )
    await msg.answer(
        "👋 **Вітаю! Я бот для швидкого моніторингу Auto.ria.**\n\n"
        "Надішли мені посилання на пошук з Auto.ria, і я сповіщатиму тебе про нові оголошення!",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text == "💎 Статус підписки")
async def subscription_status(msg: types.Message):
    user_id = msg.from_user.id
    if user_id == ADMIN_ID:
        await msg.answer("👑 **Статус:** Адміністратор", parse_mode=ParseMode.MARKDOWN)
        return
    async with db_pool.acquire() as conn:
        expires = await conn.fetchval("SELECT subscription_expires FROM bot_users WHERE user_id = $1", user_id)
    if expires and expires > datetime.now():
        await msg.answer(f"✅ **Підписка активна!** До: `{expires.strftime('%Y-%m-%d %H:%M')}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await msg.answer("❌ **Підписка закінчилася або відсутня.**", parse_mode=ParseMode.MARKDOWN)


@dp.message(F.text == "📞 Зв'язатися з адміном")
async def contact_admin(msg: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Написати адміну", url=f"https://t.me/{ADMIN_USERNAME}")]])
    await msg.answer("💬 Звертайтеся до адміністратора:", reply_markup=kb)


@dp.message(F.text == "📁 Мої фільтри")
async def show_filters(msg: types.Message):
    if not await check_subscription(msg.from_user.id):
        await msg.answer("❌ Необхідна активна підписка!")
        return
    async with db_pool.acquire() as conn:
        filters = await conn.fetch("SELECT id, url FROM user_filters WHERE user_id = $1", msg.from_user.id)
    if not filters:
        await msg.answer("У вас немає збережених фільтрів.")
        return
    for f in filters:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Видалити", callback_data=f"del_{f['id']}")]])
        await msg.answer(f"🔗 `{html.escape(f['url'])}`", reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("del_"))
async def delete_filter(call: types.CallbackQuery):
    filter_id = int(call.data.split("_")[1])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM user_filters WHERE id = $1 AND user_id = $2", filter_id, call.from_user.id)
    await call.answer("Фільтр видалено!", show_alert=True)
    await call.message.delete()


@dp.message(F.text == "➕ Додати посилання")
async def add_prompt(msg: types.Message):
    if not await check_subscription(msg.from_user.id):
        await msg.answer("❌ Необхідна активна підписка!")
        return
    await msg.answer("Надішліть посилання з результатами пошуку Auto.ria сюди у чат.")


@dp.message(F.text.regexp(r"https?://[^\s]+") | F.caption.regexp(r"https?://[^\s]+"))
async def add_filter(msg: types.Message):
    if not await check_subscription(msg.from_user.id):
        await msg.answer("❌ Необхідна активна підписка!")
        return
    text_content = msg.text or msg.caption or ""
    if "auto.ria.com" not in text_content:
        await msg.answer("❌ Посилання має бути з сайту auto.ria.com!")
        return

    raw_url = next((w for w in text_content.split() if w.startswith("http")), "")
    if not raw_url:
        return

    url = normalize_autoria_url(raw_url)
    user_id = msg.from_user.id
    processing_msg = await msg.answer("⏳ Обробляю посилання...")

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO user_filters (user_id, url) VALUES ($1, $2) ON CONFLICT DO NOTHING", user_id, url)

    current_cars = await parse_autoria(http_session, url)
    if current_cars:
        records = [(user_id, c["car_id"]) for c in current_cars]
        async with db_pool.acquire() as conn:
            await conn.executemany("INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", records)

    await processing_msg.edit_text("✅ **Фільтр успішно додано!** Нові оголошення почнуть надходити.", parse_mode=ParseMode.HTML)


async def monitor():
    while True:
        try:
            async with db_pool.acquire() as conn:
                filters = await conn.fetch("SELECT user_id, url FROM user_filters")

            for f in filters:
                user_id = f["user_id"]
                if not await check_subscription(user_id):
                    continue

                cars = await parse_autoria(http_session, f["url"])
                for car in cars:
                    async with db_pool.acquire() as conn:
                        already_sent = await conn.fetchval("SELECT 1 FROM sent_cars WHERE user_id = $1 AND car_id = $2", user_id, car["car_id"])

                    if not already_sent:
                        msg_text = (
                            f"🚗 <b>Нове оголошення!</b>\n\n"
                            f"📌 <b>{html.escape(car['title'])}</b>\n"
                            f"💰 <b>Ціна:</b> {html.escape(car['price'])}\n\n"
                            f'🔗 <a href="{html.escape(car["link"], quote=True)}">Переглянути на Auto.ria</a>'
                        )
                        sent_ok = await safe_send_message(user_id, msg_text)
                        if sent_ok:
                            async with db_pool.acquire() as conn:
                                await conn.execute("INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", user_id, car["car_id"])
                        await asyncio.sleep(0.5)
        except Exception as e:
            logging.error(f"Помилка моніторингу: {e}")
        await asyncio.sleep(60)


async def handle_webhook(request: web.Request) -> web.Response:
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return web.Response(text="OK")


async def main():
    global http_session
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    await init_db()
    http_session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())

    # Налаштування вебхука для Render
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
        await bot.set_webhook(webhook_url)
        logging.info(ф"Вебхук встановлено на: {webhook_url}")
    else:
        logging.warning("RENDER_EXTERNAL_URL не вказано! Вебхук може не працювати автоматично.")

    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)

    asyncio.create_task(monitor())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер запущено на порту {PORT}")

    # Тримаємо додаток живим
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if http_session:
            asyncio.run(http_session.close())
