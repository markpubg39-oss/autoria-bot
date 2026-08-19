import asyncio
import html
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import aiohttp
import asyncpg
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
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

# Заголовки для обходу захисту Auto.ria
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


# === БАЗА ДАНИХ ТА ІНІЦІАЛІЗАЦІЯ ===
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
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=reply_markup)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await safe_send_message(user_id, text, reply_markup)
    except TelegramForbiddenError:
        logging.info(f"Користувач {user_id} заблокував бота")
        return False
    except Exception as e:
        logging.error(f"Помилка відправки для {user_id}: {e}")
        return False


# === ПАРСИНГ AUTO.RIA ===
async def parse_autoria(session: aiohttp.ClientSession, url: str) -> list:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True) as resp:
            html_text = await resp.text()

            if resp.status != 200:
                logging.warning(f"Auto.ria повернув статус {resp.status} для {url}. HTML: {html_text[:200]!r}")
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

                    cars.append({
                        "car_id": str(car_id),
                        "title": title,
                        "price": price,
                        "link": link
                    })
                except Exception as e:
                    logging.error(f"Помилка розбору картки: {e}")
                    continue

            return cars
    except Exception as e:
        logging.error(f"Помилка запиту до Auto.ria для {url}: {e}")
        return []


# === КЛАВІАТУРИ ===
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📁 Мої фільтри"), KeyboardButton(text="➕ Додати посилання")],
            [KeyboardButton(text="💎 Статус підписки"), KeyboardButton(text="📞 Зв'язатися з адміном")]
        ],
        resize_keyboard=True
    )


# === ОБРОБНИКИ ТЕЛЕГРАМ ===
@dp.message(Command("start"))
async def start(msg: types.Message):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO bot_users (user_id, subscription_expires) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING",
            msg.from_user.id, datetime.now() + timedelta(days=1)
        )
    
    await msg.answer(
        "👋 **Вітаю! Я бот для швидкого моніторингу Auto.ria.**\n\n"
        "Надішли мені посилання на пошук з Auto.ria, і я миттєво сповіщатиму тебе про нові оголошення!",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


@dp.message(F.text == "💎 Статус підписки")
async def subscription_status(msg: types.Message):
    user_id = msg.from_user.id
    if user_id == ADMIN_ID:
        await msg.answer("👑 **Статус:** Адміністратор (повний доступ без обмежень)", parse_mode="Markdown")
        return

    async with db_pool.acquire() as conn:
        expires = await conn.fetchval("SELECT subscription_expires FROM bot_users WHERE user_id = $1", user_id)

    if expires and expires > datetime.now():
        date_str = expires.strftime("%Y-%m-%d %H:%M")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Продовжити підписку", callback_data="buy_sub")]
        ])
        await msg.answer(f"✅ **Підписка активна!**\nДіє до: `{date_str}`", reply_markup=kb, parse_mode="Markdown")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купити підписку", callback_data="buy_sub")]
        ])
        await msg.answer("❌ **Підписка закінчилася або відсутня.**\nДля роботи бота придбайте доступ.", reply_markup=kb, parse_mode="Markdown")


@dp.message(F.text == "📞 Зв'язатися з адміном")
async def contact_admin(msg: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написати адміну", url=f"https://t.me/{ADMIN_USERNAME}")]
    ])
    await msg.answer("💬 Потрібна допомога, хочете купити підписку чи є питання? Звертайтеся напряму до адміністратора:", reply_markup=kb)


@dp.callback_query(F.data == "buy_sub")
async def buy_subscription_callback(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написати адміну для оплати", url=f"https://t.me/{ADMIN_USERNAME}")]
    ])
    await call.message.answer("💎 **Оплата підписки:**\n\nЗв'яжіться з адміністратором для отримання реквізитів та активації доступу.", reply_markup=kb, parse_mode="Markdown")
    await call.answer()


@dp.message(F.text == "📁 Мої фільтри")
async def show_filters(msg: types.Message):
    if not await check_subscription(msg.from_user.id):
        await msg.answer("❌ Необхідна активна підписка для перегляду фільтрів! Натисніть «💎 Статус підписки».")
        return

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
    if not await check_subscription(msg.from_user.id):
        await msg.answer("❌ Необхідна активна підписка для додавання фільтрів! Натисніть «💎 Статус підписки».")
        return
    await msg.answer("Надішліть скопійоване посилання з результатами пошуку Auto.ria сюди у чат.")


@dp.message(F.text.regexp(r"https?://[^\s]+") | F.caption.regexp(r"https?://[^\s]+"))
async def add_filter(msg: types.Message):
    if not await check_subscription(msg.from_user.id):
        await msg.answer("❌ Необхідна активна підписка! Оформіть її через меню «💎 Статус підписки».")
        return

    text_content = msg.text or msg.caption or ""
    if "auto.ria.com" not in text_content:
        await msg.answer("❌ Посилання має бути саме з сайту `auto.ria.com`!", parse_mode="Markdown")
        return

    raw_url = ""
    for word in text_content.split():
        if word.startswith("http"):
            raw_url = word
            break

    if not raw_url:
        await msg.answer("❌ Не вдалося знайти посилання в повідомленні.")
        return

    url = normalize_autoria_url(raw_url)
    user_id = msg.from_user.id

    processing_msg = await msg.answer("⏳ Обробляю посилання та налаштовую фільтр...")

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_filters (user_id, url) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, url
            )

        current_cars = await parse_autoria(http_session, url)
        if current_cars:
            records = [(user_id, c["car_id"]) for c in current_cars]
            async with db_pool.acquire() as conn:
                await conn.executemany(
                    "INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    records
                )

        if current_cars:
            await processing_msg.edit_text(
                "✅ **Фільтр успішно додано!**\n"
                f"Зафіксовано поточних оголошень: <b>{len(current_cars)}</b> шт.\n"
                "Сповіщення прийдуть тільки на <b>нові</b> оголошення.",
                parse_mode="HTML"
            )
        else:
            await processing_msg.edit_text(
                "⚠️ **Посилання збережено, але парсер не зміг знайти картки авто.**\n"
                "Спробуйте перевірити його командою `/debug <посилання>`.",
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"Помилка додавання фільтра: {e}")
        await processing_msg.edit_text("❌ Сталася помилка при збереженні посилання.")


# === АДМІНСЬКІ КОМАНДИ ===
@dp.message(Command("grant"))
async def grant_subscription(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    
    args = msg.text.split()
    if len(args) < 3:
        await msg.answer("Формат: `/grant user_id days`", parse_mode="Markdown")
        return
    
    target_user_id = int(args[1])
    days = int(args[2])
    new_expire = datetime.now() + timedelta(days=days)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bot_users (user_id, subscription_expires) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET subscription_expires = $2
        """, target_user_id, new_expire)

    await msg.answer(f"✅ Успішно видано підписку користувачу `{target_user_id}` на {days} днів!", parse_mode="Markdown")
    await safe_send_message(target_user_id, f"🎉 Вам активовано/продовжено підписку на {days} днів!")


@dp.message(Command("debug"))
async def debug_parse(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return

    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().startswith("http"):
        await msg.answer("Формат: `/debug <посилання>`", parse_mode="Markdown")
        return

    raw_url = parts[1].strip()
    url = normalize_autoria_url(raw_url)

    wait_msg = await msg.answer(f"⏳ Тестую парсинг:\n`{html.escape(url)}`", parse_mode="Markdown")
    cars = await parse_autoria(http_session, url)

    if cars:
        preview = "\n".join(f"• {c['title']} — {c['price']}" for c in cars[:5])
        await wait_msg.edit_text(
            f"✅ Успішно! Знайдено оголошень: <b>{len(cars)}</b>\n\nПерші кілька:\n{html.escape(preview)}",
            parse_mode="HTML"
        )
    else:
        await wait_msg.edit_text(
            "⚠️ Знайдено 0 оголошень. Сайт міг віддати порожню сторінку через захист хмарного хостингу."
        )


# === ЦИКЛ МОНІТОРИНГУ ===
async def monitor():
    while True:
        try:
            async with db_pool.acquire() as conn:
                filters = await conn.fetch("SELECT user_id, url FROM user_filters")

            for f in filters:
                user_id = f["user_id"]
                
                if not await check_subscription(user_id):
                    continue

                url = f["url"]
                cars = await parse_autoria(http_session, url)

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
    global http_session
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    await init_db()
    
    # Примусово скидаємо старі вебхуки та сесії, щоб уникнути конфліктів
    await bot.delete_webhook(drop_pending_updates=True)
    
    http_session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    try:
        asyncio.create_task(monitor())
        logging.info("Бот повністю запущений і працює!")
        await dp.start_polling(bot, handle_signals=True)
    finally:
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
