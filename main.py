import asyncio
import logging
import sqlite3
import os
from datetime import datetime

import aiohttp
import psycopg2
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# === ОСНОВНІ НАЛАШТУВАННЯ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Змінна середовища BOT_TOKEN не встановлена!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "5482150373"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Primeza777")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

# === РОБОТА З БАЗОЮ ДАНИХ ===
def get_db_connection():
    if DATABASE_URL:
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url)
    else:
        return sqlite3.connect("bot_users.db")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            join_date TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_filters (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            url TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_cars (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            car_id TEXT,
            UNIQUE(user_id, car_id)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_filters_user ON user_filters(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sent_user_car ON sent_cars(user_id, car_id)")

    conn.commit()
    conn.close()

def add_user(user_id, username, full_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    if DATABASE_URL:
        cursor.execute("""
            INSERT INTO users (user_id, username, full_name, join_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
        """, (user_id, username, full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    else:
        cursor.execute("""
            INSERT OR IGNORE INTO users (user_id, username, full_name, join_date)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, full_name, join_date FROM users")
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_filter(user_id, url):
    conn = get_db_connection()
    cursor = conn.cursor()
    param = "%s" if DATABASE_URL else "?"
    cursor.execute(f"""
        INSERT INTO user_filters (user_id, url, created_at)
        VALUES ({param}, {param}, {param})
    """, (user_id, url, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_user_filters(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    param = "%s" if DATABASE_URL else "?"
    cursor.execute(f"SELECT id, url FROM user_filters WHERE user_id = {param}", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_filter_by_id(filter_id, user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    param = "%s" if DATABASE_URL else "?"
    cursor.execute(f"DELETE FROM user_filters WHERE id = {param} AND user_id = {param}", (filter_id, user_id))
    conn.commit()
    conn.close()

def is_car_sent(user_id, car_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    param = "%s" if DATABASE_URL else "?"
    cursor.execute(f"SELECT 1 FROM sent_cars WHERE user_id = {param} AND car_id = {param}", (user_id, str(car_id)))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_car_sent(user_id, car_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    if DATABASE_URL:
        cursor.execute("""
            INSERT INTO sent_cars (user_id, car_id) VALUES (%s, %s)
            ON CONFLICT (user_id, car_id) DO NOTHING
        """, (user_id, str(car_id)))
    else:
        cursor.execute("INSERT OR IGNORE INTO sent_cars (user_id, car_id) VALUES (?, ?)", (user_id, str(car_id)))
    conn.commit()
    conn.close()

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
async def fetch_html(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status != 200:
                logging.warning(f"auto.ria повернув статус {response.status} для {url}")
                return None
            return await response.text()
    except Exception as e:
        logging.error(f"Помилка запиту до {url}: {e}")
        return None

def parse_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cars = []

    sections = soup.find_all("section", class_="ticket-item")
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

async def parse_autoria(session: aiohttp.ClientSession, url: str) -> list[dict]:
    html = await fetch_html(session, url)
    if html is None:
        return []
    return await asyncio.to_thread(parse_html, html)

# === ОБРОБНИКИ КОМАНД ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)

    welcome_text = (
        f"Вітаю, {user.first_name}! 👋\n\n"
        "⚡ **Бот для автоматичного моніторингу оголошень Auto.ria**\n\n"
        "Я допомагаю першим дізнаватися про нові вигідні пропозиції авто на ринку.\n\n"
        "📌 **Як розпочати роботу:**\n"
        "1️⃣ Зайдіть на сайт **Auto.ria**.\n"
        "2️⃣ Вкажіть потрібні параметри (марку, ціну, рік тощо).\n"
        "3️⃣ **Обов'язково натисніть кнопку «Пошук»**, щоб сформувати список авто.\n"
        "4️⃣ **НЕ заходьте в окремі оголошення!** Скопіюйте посилання прямо з адресної стрічки браузера зі сторінки зі списком результатів.\n"
        "5️⃣ Натисніть кнопку **«➕ Додати посилання»** та надішліть його сюди.\n\n"
        "Оберіть потрібну дію в меню нижче:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "📁 Мої фільтри")
async def show_filters(message: types.Message):
    filters = get_user_filters(message.from_user.id)
    if not filters:
        await message.answer("У вас поки немає збережених фільтрів.\n\nНатисніть кнопку «➕ Додати посилання», щоб відстежувати нові оголошення.")
        return

    await message.answer("📋 **Ваші активні відстеження:**")
    for f_id, url in filters:
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Видалити фільтр", callback_data=f"del_{f_id}")]
            ]
        )
        await message.answer(f"🔗 `{url}`", reply_markup=inline_kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("del_"))
async def process_delete_filter(callback: types.CallbackQuery):
    try:
        filter_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("Не вдалося розпізнати фільтр.", show_alert=True)
        return

    delete_filter_by_id(filter_id, callback.from_user.id)
    await callback.answer("Фільтр успішно видалено!", show_alert=True)
    await callback.message.delete()

@dp.message(F.text == "➕ Додати посилання")
async def add_filter_prompt(message: types.Message):
    prompt_text = (
        "🔗 **Детальна інструкція, як додати відстеження:**\n\n"
        "1️⃣ Зайдіть на сайт або в додаток **Auto.ria**.\n"
        "2️⃣ Виберіть усі потрібні параметри авто (марку, модель, рік, ціну, регіон).\n"
        "3️⃣ **Обов'язково натисніть кнопку «Пошук»**, щоб перед вами відкрився список знайдених машин.\n"
        "4️⃣ **УВАГА: НЕ переходьте на сторінку конкретного оголошення!** Нам потрібне посилання саме на загальний результат пошуку.\n"
        "5️⃣ Скопіюйте посилання з адресного рядка зверху.\n"
        "6️⃣ **Надішліть скопійоване посилання сюди у відповідь на це повідомлення.**\n\n"
        "💡 *Приклад правильного посилання:*\n`https://auto.ria.com/uk/search/?indexName=auto,s_app,g_app&categories.main.id=1&price.USD.lte=7000`"
    )
    await message.answer(prompt_text, parse_mode="Markdown")

@dp.message(F.text == "⚙️ Налаштування")
async def settings_cmd(message: types.Message):
    settings_text = (
        "⚙️ **Налаштування моніторингу**\n\n"
        "• **Частота перевірки:** Щохвилини ⏱️\n"
        "• **Сповіщення:** Миттєві 🔔\n"
        "• **Статус сканера:** Працює 24/7 ✅\n\n"
        "Щоб переглянути або видалити ваші збережені пошуки, скористайтеся кнопкою **«📁 Мої фільтри»**."
    )
    await message.answer(settings_text, parse_mode="Markdown")

@dp.message(F.text.startswith("http://") | F.text.startswith("https://"))
async def save_user_url(message: types.Message):
    url = message.text.strip()
    if "auto.ria.com" not in url:
        await message.answer("❌ **Помилка!** Посилання має бути саме з сайту `auto.ria.com`.\nСпробуйте ще раз.", parse_mode="Markdown")
        return

    add_filter(message.from_user.id, url)
    await message.answer("✅ **Посилання успішно додано!**\n\nТепер бот перевірятиме появу нових авто за цим фільтром щохвилини й миттєво надсилатиме їх сюди.", reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "ℹ️ Інформація")
async def info_cmd(message: types.Message):
    info_text = (
        "ℹ️ **Про сервіс**\n\n"
        "Бот працює у фоновому режимі без перерв і автоматично відстежує нові оголошення за вашими фільтрами.\n\n"
        "Ви дізнаєтеся про нові машини раніше за переважну більшість покупців на сайті!"
    )
    await message.answer(info_text, parse_mode="Markdown")

@dp.message(F.text == "📞 Підтримка")
async def support_cmd(message: types.Message):
    await message.answer(f"З усіх питань, пропозицій чи багів звертайтеся до адміністратора: @{ADMIN_USERNAME}")

# === АДМІН-КОМАНДИ ===
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    users = get_all_users()
    text = f"👑 **Панель адміністратора**\n\nВсього користувачів у базі: {len(users)}\n\n"
    for u_id, uname, fname, jdate in users[:20]:
        text += f"• ID: `{u_id}` | @{uname} | {fname} ({jdate})\n"

    await message.answer(text, parse_mode="Markdown")

# === ТАСК МОНІТОРИНГУ ===
async def check_updates_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT user_id, url FROM user_filters")
                filters = cursor.fetchall()
                conn.close()

                for user_id, url in filters:
                    cars = await parse_autoria(session, url)
                    for car in cars:
                        if not is_car_sent(user_id, car["car_id"]):
                            msg = (
                                f"🚗 **Нове оголошення!**\n\n"
                                f"📌 **{car['title']}**\n"
                                f"💰 **Ціна:** {car['price']}\n\n"
                                f"🔗 [Переглянути оголошення]({car['link']})"
                            )
                            try:
                                await bot.send_message(user_id, msg, parse_mode="Markdown")
                                mark_car_sent(user_id, car["car_id"])
                            except Exception as e:
                                logging.error(f"Не вдалося відправити повідомлення користувачу {user_id}: {e}")
                    await asyncio.sleep(2)
            except Exception as e:
                logging.error(f"Помилка у циклі перевірки: {e}")

            await asyncio.sleep(60)

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    asyncio.create_task(check_updates_loop())
    print("🤖 Бот готовий до роботи!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
