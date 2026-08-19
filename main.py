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

# === ОСНОВНІ НАЛАШТУВАННЯ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Змінна середовища BOT_TOKEN не встановлена!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "5482150373"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Primeza777")
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("Змінна DATABASE_URL обов'язкова для використання asyncpg!")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SEND_DELAY_SECONDS = float(os.getenv("SEND_DELAY_SECONDS", "0.4"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Повні реалістичні заголовки для обходу блокувань Auto.ria
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

db_pool: Optional[asyncpg.Pool] = None
PARALLEL_SCRAPE_LIMITER = asyncio.Semaphore(5)


# === АВТОМАТИЧНА НОРМАЛІЗАЦІЯ ПОСИЛАННЯ (СОРТУВАННЯ ЗА ДАТОЮ) ===
def normalize_autoria_url(raw_url: str) -> str:
    """
    Примусово встановлює сортування за датою подачі (order=7)
    та скидає пагінацію на першу сторінку (page=0).
    """
    try:
        parsed = urlparse(raw_url)
        params = parse_qs(parsed.query)

        # order=7 — сортування за датою додавання
        params["order"] = ["7"]

        if "page" in params:
            params["page"] = ["0"]

        new_query = urlencode(params, doseq=True)
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment
        ))
    except Exception as e:
        logging.error(f"Помилка нормалізації URL {raw_url}: {e}")
        return raw_url


# === РОБОТА З БАЗОЮ ДАНИХ (ASYNC) ===
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=10)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                join_date TEXT
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_filters (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                url TEXT,
                created_at TEXT,
                UNIQUE(user_id, url)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_cars (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                car_id TEXT,
                UNIQUE(user_id, car_id)
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_filters_user ON user_filters(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sent_user_car ON sent_cars(user_id, car_id);")


async def close_db():
    if db_pool is not None:
        await db_pool.close()


async def add_user(user_id: int, username: str, full_name: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, full_name, join_date)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id, username, full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


async def get_all_users():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, username, full_name, join_date FROM users")


async def add_filter(user_id: int, url: str) -> bool:
    async with db_pool.acquire() as conn:
        result = await conn.execute("""
            INSERT INTO user_filters (user_id, url, created_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, url) DO NOTHING
        """, user_id, url, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return result.endswith(" 1")


async def get_user_filters(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT id, url FROM user_filters WHERE user_id = $1 ORDER BY id", user_id)


async def delete_filter_by_id(filter_id: int, user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM user_filters WHERE id = $1 AND user_id = $2", filter_id, user_id)


async def get_sent_car_ids_set(user_id: int) -> set:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT car_id FROM sent_cars WHERE user_id = $1", user_id)
        return {row["car_id"] for row in rows}


async def mark_cars_sent_batch(user_id: int, car_ids: list):
    if not car_ids:
        return
    async with db_pool.acquire() as conn:
        records = [(user_id, car_id) for car_id in car_ids]
        await conn.executemany("""
            INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2)
            ON CONFLICT (user_id, car_id) DO NOTHING
        """, records)


# === КЛАВІАТУРИ ===
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📁 Мої фільтри"), KeyboardButton(text="➕ Додати посилання")],
            [KeyboardButton(text="⚙️ Налаштування"), KeyboardButton(text="ℹ️ Інформація")],
            [KeyboardButton(text="📞 Підтримка")]
        ],
        resize_keyboard=True
    )


# === ПАРСИНГ AUTO.RIA ===
async def fetch_html(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status != 200:
                logging.warning(f"auto.ria повернув статус {response.status} для {url}")
                return None
            return await response.text()
    except Exception as e:
        logging.error(f"Помилка запиту до {url}: {e}")
        return None


def parse_html(page_html: str) -> list:
    soup = BeautifulSoup(page_html, "html.parser")
    cars = []

    sections = soup.find_all("section", class_="ticket-item")
    if not sections:
        logging.info("Парсер не знайшов жодного 'ticket-item' — перевір, чи не змінилась верстка сайту")

    for section in sections:
        car_id = section.get("data-id") or section.get("data-good-id")
        if not car_id:
            continue

        title_elem = section.find("a", class_="address")
        title = title_elem.text.strip() if title_elem else "Автомобіль"
        link = title_elem.get("href") if title_elem else ""

        price_elem = section.find("span", class_="size22") or section.find("span", class_="price-ticket")
        price = price_elem.text.strip() if price_elem else "Ціну не вказано"

        cars.append({
            "car_id": str(car_id),
            "title": title,
            "price": price,
            "link": link
        })

    return cars


async def parse_autoria(session: aiohttp.ClientSession, url: str) -> list:
    async with PARALLEL_SCRAPE_LIMITER:
        page_html = await fetch_html(session, url)
        if page_html is None:
            return []
        return await asyncio.to_thread(parse_html, page_html)


# === НАДІЙНА ВІДПРАВКА ПОВІДОМЛЕНЬ ===
async def safe_send_message(user_id: int, text: str, parse_mode: Optional[str] = "HTML", retries: int = 3) -> Optional[bool]:
    for attempt in range(retries):
        try:
            await bot.send_message(user_id, text, parse_mode=parse_mode)
            return True
        except TelegramRetryAfter as e:
            logging.warning(f"Flood control: чекаю {e.retry_after}с перед повтором для {user_id}")
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            logging.info(f"Користувач {user_id} заблокував бота, пропускаю")
            return False
        except TelegramBadRequest as e:
            logging.error(f"Некоректне повідомлення для {user_id}: {e}")
            return False
        except Exception as e:
            logging.error(f"Тимчасова помилка відправки користувачу {user_id}: {e}")
            return None
    return None


# === ОБРОБНИКИ КОМАНД ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    await add_user(user.id, user.username or "", user.full_name or "")

    welcome_text = (
        f"Вітаю, {user.first_name}! 👋\n\n"
        "⚡ <b>Бот для автоматичного моніторингу оголошень Auto.ria</b>\n\n"
        "Я допомагаю першим дізнаватися про нові вигідні пропозиції авто на ринку.\n\n"
        "📌 <b>Як розпочати роботу:</b>\n"
        "1️⃣ Зайдіть на сайт <b>Auto.ria</b>.\n"
        "2️⃣ Вкажіть потрібні параметри (марку, ціну, рік тощо).\n"
        "3️⃣ <b>Обов'язково натисніть кнопку «Пошук»</b>, щоб сформувати список авто.\n"
        "4️⃣ Скопіюйте посилання прямо з адресної стрічки браузера зі сторінки зі списком результатів.\n"
        "5️⃣ Натисніть кнопку <b>«➕ Додати посилання»</b> та надішліть його сюди.\n\n"
        "Оберіть потрібну дію в меню нижче:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML")


@dp.message(F.text == "📁 Мої фільтри")
async def show_filters(message: types.Message):
    filters = await get_user_filters(message.from_user.id)
    if not filters:
        await message.answer("У вас поки немає збережених фільтрів.\n\nНатисніть кнопку «➕ Додати посилання», щоб відстежувати нові оголошення.")
        return

    await message.answer("📋 <b>Ваші активні відстеження:</b>", parse_mode="HTML")
    for row in filters:
        f_id = row["id"]
        url = row["url"]
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Видалити фільтр", callback_data=f"del_{f_id}")]
            ]
        )
        await message.answer(f"🔗 <code>{html.escape(url)}</code>", reply_markup=inline_kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("del_"))
async def process_delete_filter(callback: types.CallbackQuery):
    try:
        filter_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("Не вдалося розпізнати фільтр.", show_alert=True)
        return

    await delete_filter_by_id(filter_id, callback.from_user.id)
    await callback.answer("Фільтр успішно видалено!", show_alert=True)
    await callback.message.delete()


@dp.message(F.text == "➕ Додати посилання")
async def add_filter_prompt(message: types.Message):
    prompt_text = (
        "🔗 <b>Детальна інструкція, як додати відстеження:</b>\n\n"
        "1️⃣ Зайдіть на сайт або в додаток <b>Auto.ria</b>.\n"
        "2️⃣ Виберіть усі потрібні параметри авто (марку, модель, рік, ціну, регіон).\n"
        "3️⃣ <b>Обов'язково натисніть кнопку «Пошук»</b>, щоб перед вами відкрився список знайдених машин.\n"
        "4️⃣ Скопіюйте посилання з адресного рядка зверху.\n"
        "5️⃣ <b>Надішліть скопійоване посилання сюди у відповідь на це повідомлення.</b>\n\n"
        "💡 <i>Приклад правильного посилання:</i>\n<code>https://auto.ria.com/uk/search/?categories.main.id=1&price.USD.lte=7000</code>"
    )
    await message.answer(prompt_text, parse_mode="HTML")


@dp.message(F.text == "⚙️ Налаштування")
async def settings_cmd(message: types.Message):
    settings_text = (
        "⚙️ <b>Налаштування моніторингу</b>\n\n"
        f"• <b>Частота перевірки:</b> раз на {CHECK_INTERVAL_SECONDS} сек ⏱️\n"
        "• <b>Сповіщення:</b> Миттєві 🔔\n"
        "• <b>Статус сканера:</b> Працює 24/7 ✅\n\n"
        "Щоб переглянути або видалити ваші збережені пошуки, скористайтеся кнопкою <b>«📁 Мої фільтри»</b>."
    )
    await message.answer(settings_text, parse_mode="HTML")


@dp.message(F.text.startswith("http://") | F.text.startswith("https://"))
async def save_user_url(message: types.Message):
    raw_url = message.text.strip()

    if len(raw_url) > 2000:
        await message.answer("❌ <b>Помилка!</b> Посилання занадто довге.", parse_mode="HTML")
        return

    if "auto.ria.com" not in raw_url:
        await message.answer("❌ <b>Помилка!</b> Посилання має бути саме з сайту <code>auto.ria.com</code>.\nСпробуйте ще раз.", parse_mode="HTML")
        return

    # Автоматично оптимізуємо посилання під моніторинг по даті!
    url = normalize_autoria_url(raw_url)

    user_id = message.from_user.id
    was_added = await add_filter(user_id, url)

    if not was_added:
        await message.answer("ℹ️ Такий фільтр у вас вже є — нічого додавати не потрібно.", reply_markup=get_main_keyboard())
        return

    status_msg = await message.answer("⏳ Додаю фільтр і перевіряю поточні оголошення...")

    try:
        async with aiohttp.ClientSession() as session:
            cars = await parse_autoria(session, url)
        if cars:
            await mark_cars_sent_batch(user_id, [c["car_id"] for c in cars])
    except Exception as e:
        logging.error(f"Не вдалося прогріти фільтр для {user_id}, url={url}: {e}")

    await status_msg.delete()
    await message.answer(
        "✅ <b>Посилання успішно додано!</b>\n\n"
        "Тепер бот перевірятиме появу нових авто за цим фільтром і миттєво надсилатиме їх сюди. "
        "Оголошення, що вже є на сторінці зараз, не надсилаються — тільки нові.",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


@dp.message(F.text == "ℹ️ Інформація")
async def info_cmd(message: types.Message):
    info_text = (
        "ℹ️ <b>Про сервіс</b>\n\n"
        "Бот працює у фоновому режимі без перерв і автоматично відстежує нові оголошення за вашими фільтрами.\n\n"
        "Ви дізнаєтеся про нові машини раніше за переважну більшість покупців на сайті!"
    )
    await message.answer(info_text, parse_mode="HTML")


@dp.message(F.text == "📞 Підтримка")
async def support_cmd(message: types.Message):
    await message.answer(f"З усіх питань, пропозицій чи багів звертайтеся до адміністратора: @{ADMIN_USERNAME}")


# === АДМІН-КОМАНДИ ===
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    users = await get_all_users()
    text = f"👑 <b>Панель адміністратора</b>\n\nВсього користувачів у базі: {len(users)}\n\n"
    for row in users[:20]:
        uname = html.escape(row["username"] or "")
        fname = html.escape(row["full_name"] or "")
        text += f"• ID: <code>{row['user_id']}</code> | @{uname} | {fname} ({row['join_date']})\n"

    await message.answer(text, parse_mode="HTML")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return

    text_to_send = command.args
    if not text_to_send:
        await message.answer("⚠️ Вкажіть текст розсилки! Наприклад:\n<code>/broadcast Текст повідомлення...</code>", parse_mode="HTML")
        return

    users = await get_all_users()
    await message.answer(f"🚀 Починаю розсилку для {len(users)} користувачів...")

    success = 0
    failed = 0

    for row in users:
        u_id = row["user_id"]
        res = await safe_send_message(u_id, text_to_send, parse_mode=None)
        if res is True:
            success += 1
        else:
            failed += 1
        await asyncio.sleep(SEND_DELAY_SECONDS)

    await message.answer(f"✅ <b>Розсилку завершено!</b>\n\nУспішно: {success}\nНе доставлено: {failed}", parse_mode="HTML")


# === ІЗОЛЬОВАНА ОБРОБКА ОДНОГО ФІЛЬТРУ ===
async def process_single_filter(session: aiohttp.ClientSession, user_id: int, url: str):
    try:
        cars = await parse_autoria(session, url)
        if not cars:
            return

        sent_ids = await get_sent_car_ids_set(user_id)
        new_cars_to_send = [c for c in cars if c["car_id"] not in sent_ids]

        confirmed_ids = []

        for car in new_cars_to_send:
            safe_title = html.escape(car["title"])
            safe_price = html.escape(car["price"])
            safe_link = html.escape(car["link"], quote=True)
            msg = (
                f"🚗 <b>Нове оголошення!</b>\n\n"
                f"📌 <b>{safe_title}</b>\n"
                f"💰 <b>Ціна:</b> {safe_price}\n\n"
                f'🔗 <a href="{safe_link}">Переглянути оголошення</a>'
            )
            delivered = await safe_send_message(user_id, msg)
            if delivered is not None:
                confirmed_ids.append(car["car_id"])
            await asyncio.sleep(SEND_DELAY_SECONDS)

        if confirmed_ids:
            await mark_cars_sent_batch(user_id, confirmed_ids)

    except Exception as e:
        logging.error(f"Помилка обробки фільтра user_id={user_id}, url={url}: {e}")


# === ТАСК МОНІТОРИНГУ ===
async def check_updates_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with db_pool.acquire() as conn:
                    filters = await conn.fetch("SELECT DISTINCT user_id, url FROM user_filters")

                if filters:
                    tasks = [process_single_filter(session, row["user_id"], row["url"]) for row in filters]
                    await asyncio.gather(*tasks)

            except Exception as e:
                logging.error(f"Помилка у глобальному циклі перевірки: {e}")

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    await init_db()
    monitoring_task = asyncio.create_task(check_updates_loop())
    print("🤖 Бот готовий до роботи (Order=7 + Full Headers)!")

    try:
        await dp.start_polling(bot)
    finally:
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
