import asyncio
import logging
import sqlite3
import requests
import os
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# === ОСНОВНІ НАЛАШТУВАННЯ ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "8929265743:AAEJlKtxA_ObKyZKcdMWAhgf83oMfGcaxx8")
ADMIN_ID = 5482150373  # Твій Telegram ID
ADMIN_USERNAME = "Primeza777"  # Твій юзернейм в TG

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

# === РОБОТА З БАЗОЮ ДАНИХ ===
def init_db():
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    
    # Таблиця користувачів
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            join_date TEXT
        )
    """)
    
    # Таблиця фільтрів/посилань
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            created_at TEXT
        )
    """)
    
    # Таблиця вже відправлених оголошень
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_cars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            car_id TEXT,
            UNIQUE(user_id, car_id)
        )
    """)
    
    conn.commit()
    conn.close()

def add_user(user_id, username, full_name):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO users (user_id, username, full_name, join_date)
        VALUES (?, ?, ?, ?)
    """, (user_id, username, full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, full_name, join_date FROM users")
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_filter(user_id, url):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_filters (user_id, url, created_at)
        VALUES (?, ?, ?)
    """, (user_id, url, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_user_filters(user_id):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, url FROM user_filters WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_filter_by_id(filter_id, user_id):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_filters WHERE id = ? AND user_id = ?", (filter_id, user_id))
    conn.commit()
    conn.close()

def is_car_sent(user_id, car_id):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_cars WHERE user_id = ? AND car_id = ?", (user_id, car_id))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_car_sent(user_id, car_id):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO sent_cars (user_id, car_id) VALUES (?, ?)", (user_id, car_id))
    conn.commit()
    conn.close()

# === КЛАВІАТУРИ ===
def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📁 Мої фільтри"), KeyboardButton(text="➕ Додати посилання")],
            [KeyboardButton(text="ℹ️ Інформація"), KeyboardButton(text="📞 Підтримка")]
        ],
        resize_keyboard=True
    )
    return keyboard

# === ПАРСИНГ AUTO.RIA ===
def parse_autoria(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code != 200:
            return []
        
        soup = BeautifulSoup(response.text, "html.parser")
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
    except Exception as e:
        logging.error(f"Помилка парсингу {url}: {e}")
        return []

# === ОБРОБНИКИ КОМАНД ТА ПОВІДОМЛЕНЬ ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    add_user(user.id, user.username, user.full_name)
    
    welcome_text = (
        f"Вітаю, {user.first_name}! 👋\n\n"
        "Радий бачити вас у нашому боті для моніторингу оголошень Auto.ria.\n\n"
        "З моєю допомогою ви зможете отримувати найновіші пропозиції за вашими збереженими фільтрами одразу після їх появи.\n\n"
        "Оберіть потрібну дію в меню нижче:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.message(F.text == "📁 Мої фільтри")
async def show_filters(message: types.Message):
    filters = get_user_filters(message.from_user.id)
    if not filters:
        await message.answer("У вас поки немає збережених фільтрів. Натисніть «➕ Додати посилання», щоб додати новий.")
        return
    
    await message.answer("📋 **Ваші збережені фільтри:**")
    for f_id, url in filters:
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Видалити", callback_data=f"del_{f_id}")]
            ]
        )
        await message.answer(f"🔗 {url}", reply_markup=inline_kb)

@dp.callback_query(F.data.startswith("del_"))
async def process_delete_filter(callback: types.CallbackQuery):
    filter_id = int(callback.data.split("_")[1])
    delete_filter_by_id(filter_id, callback.from_user.id)
    await callback.answer("Фільтр успішно видалено!", show_alert=True)
    await callback.message.delete()

@dp.message(F.text == "➕ Додати посилання")
async def add_filter_prompt(message: types.Message):
    await message.answer("Будь ласка, надішліть посилання на відфільтрований пошук Auto.ria у наступному повідомленні.")

@dp.message(F.text.startswith("http://") | F.text.startswith("https://"))
async def save_user_url(message: types.Message):
    url = message.text.strip()
    if "auto.ria.com" not in url:
        await message.answer("Будь ласка, надішліть коректне посилання з сайту Auto.ria.")
        return
    
    add_filter(message.from_user.id, url)
    await message.answer("✅ Посилання успішно збережено! Тепер я відстежуватиму нові оголошення за цим фільтром.", reply_markup=get_main_keyboard())

@dp.message(F.text == "ℹ️ Інформація")
async def info_cmd(message: types.Message):
    info_text = (
        "ℹ️ **Інформація про бота**\n\n"
        "Цей бот автоматично перевіряє нові оголошення за вашими посиланнями з Auto.ria і миттєво сповіщає про них.\n\n"
        "Щоб розпочати роботу, додайте посилання через кнопку «➕ Додати посилання»."
    )
    await message.answer(info_text)

@dp.message(F.text == "📞 Підтримка")
async def support_cmd(message: types.Message):
    await message.answer(f"З усіх питань та пропозицій звертайтеся до адміністратора: @{ADMIN_USERNAME}")

# === АДМІН-КОМАНДИ ===
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    users = get_all_users()
    text = f"👑 **Панель адміністратора**\n\nВсього користувачів у базі: {len(users)}\n\n"
    for u_id, uname, fname, jdate in users[:20]:
        text += f"• ID: `{u_id}` | @{uname} | {fname} ({jdate})\n"
    
    await message.answer(text)

# === ТАСК МОНІТОРИНГУ ===
async def check_updates_loop():
    while True:
        try:
            conn = sqlite3.connect("bot_users.db")
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT user_id, url FROM user_filters")
            filters = cursor.fetchall()
            conn.close()
            
            for user_id, url in filters:
                cars = parse_autoria(url)
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
