import asyncio
import logging
import sqlite3
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# === ОСНОВНІ НАЛАШТУВАННЯ ===
BOT_TOKEN = "8929265743:AAEoHONTPUt8Lniw0lnX7DcVJGfy0GjNSrU"
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            search_url TEXT,
            expire_date TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_cars (
            user_id INTEGER,
            car_link TEXT,
            PRIMARY KEY (user_id, car_link)
        )
    """)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN expire_date TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, search_url, expire_date, is_active FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res

def add_or_update_user(user_id, username, days=3):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    user = get_user(user_id)
    
    if user:
        current_expire = datetime.strptime(user[3], "%Y-%m-%d %H:%M:%S") if user[3] else datetime.now()
        start_from = max(datetime.now(), current_expire)
        new_expire = start_from + timedelta(days=days)
        cursor.execute("UPDATE users SET expire_date = ?, is_active = 1, username = ? WHERE user_id = ?", 
                       (new_expire.strftime("%Y-%m-%d %H:%M:%S"), username, user_id))
    else:
        new_expire = datetime.now() + timedelta(days=days)
        cursor.execute("INSERT INTO users (user_id, username, expire_date, is_active) VALUES (?, ?, ?, 1)",
                       (user_id, username, new_expire.strftime("%Y-%m-%d %H:%M:%S")))
    
    conn.commit()
    conn.close()
    return new_expire

def block_user(user_id):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def save_user_url(user_id, url):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET search_url = ? WHERE user_id = ?", (url, user_id))
    conn.commit()
    conn.close()

def get_active_subscribers():
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, search_url, expire_date FROM users WHERE is_active = 1 AND search_url IS NOT NULL")
    users = cursor.fetchall()
    conn.close()
    
    active = []
    now = datetime.now()
    for uid, url, exp_str in users:
        if exp_str:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
                if exp_date > now:
                    active.append((uid, url))
            except Exception:
                pass
    return active

def is_car_sent(user_id, car_link):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_cars WHERE user_id = ? AND car_link = ?", (user_id, car_link))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def mark_car_sent(user_id, car_link):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO sent_cars (user_id, car_link) VALUES (?, ?)", (user_id, car_link))
    conn.commit()
    conn.close()

# === ПАРСЕР ===
def fetch_cars_from_url(url):
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        sections = soup.find_all("section", class_="ticket-item") or soup.find_all("div", class_="proposition")
        
        cars = []
        for sec in sections:
            a_tag = sec.find("a", class_="address") or sec.find("a", class_="m-link")
            price_tag = sec.find("span", class_="green") or sec.find("span", class_="price")
            location_tag = sec.find("li", class_="view-location") or sec.find("div", class_="location")

            if a_tag:
                href = a_tag.get("href", "")
                title = a_tag.text.strip()
                price = price_tag.text.strip() if price_tag else "Ціна не вказана"
                location = location_tag.text.strip() if location_tag else "Україна"

                if "/auto_" in href and title:
                    full_link = href if href.startswith("http") else f"https://auto.ria.com{href}"
                    cars.append((title, price, location, full_link))
        return cars
    except Exception:
        return []

# === МЕНЮ ТА КНОПКИ ===
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚙️ Вставити посилання з фільтром Auto.ria")],
            [KeyboardButton(text="📊 Мій статус / Підписка"), KeyboardButton(text="💳 Купити підписку / Адмін")],
            [KeyboardButton(text="🛑 Зупинити пошук")]
        ],
        resize_keyboard=True
    )

def buy_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💬 Написати адміну щодо підписки", url=f"https://t.me/{ADMIN_USERNAME}")]]
    )

def check_access(user_id):
    user = get_user(user_id)
    if not user or user[4] == 0:
        return False, "❌ **Твій доступ вимкнено або підписка закінчилася.**\nНатисни кнопку нижче, щоб написати адміну та продовжити доступ."
    
    if user[3]:
        exp_date = datetime.strptime(user[3], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > exp_date:
            return False, f"⚠️ **Твій тестовий період / підписка закінчилася!**\n\nЩоб купити підписку — натисни кнопку нижче."
    
    return True, user[3]

# === ОБРОБКА КОМАНД ===
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    user = get_user(user_id)
    if not user:
        new_exp = add_or_update_user(user_id, username, days=3)
        msg_text = f"👋 **Здоров! Тобі автоматично надано 3 ДНІ БЕЗКОШТОВНОГО ТЕСТУ!**\n\nПідписка активна до: `{new_exp.strftime('%d.%m.%Y %H:%M')}`\n\n"
        
        if user_id != ADMIN_ID:
            admin_msg = (
                f"🚨 **НОВИЙ КОРИСТУВАЧ У БОТІ!**\n\n"
                f"👤 **Ім'я:** {message.from_user.first_name}\n"
                f"🏷 **Юзернейм:** @{message.from_user.username if message.from_user.username else 'немає'}\n"
                f"🆔 **ID:** `{user_id}`\n"
                f"⏳ **Тест до:** `{new_exp.strftime('%d.%m.%Y %H:%M')}`"
            )
            admin_buttons = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ 30 днів", callback_data=f"give_30_{user_id}"),
                    InlineKeyboardButton(text="🚫 Забанити", callback_data=f"ban_{user_id}")
                ]
            ])
            try:
                await bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="Markdown", reply_markup=admin_buttons)
            except Exception as e:
                print(f"Помилка сповіщення адміну: {e}")
    else:
        has_access, exp_info = check_access(user_id)
        if not has_access:
            await message.answer(exp_info, parse_mode="Markdown", reply_markup=buy_inline_keyboard())
            return
        msg_text = f"👋 **Здоров! Твій доступ активний.**\n\n"

    msg_text += (
        "⚡️ **Як користуватися:**\n"
        "1. Заходиш на Auto.ria, виставляєш потрібні параметри (марку, ціну, рік, місто).\n"
        "2. Копіюєш посилання з браузера.\n"
        "3. Тиснеш **«⚙️ Вставити посилання з фільтром Auto.ria»** і надсилаєш його сюди."
    )
    await message.answer(msg_text, parse_mode="Markdown", reply_markup=main_keyboard())

@dp.message(F.text == "💳 Купити підписку / Адмін")
async def buy_sub_button(message: types.Message):
    await message.answer(
        "💳 **ПРИДБАННЯ ПІДПИСКИ ТА ЗВ'ЯЗОК З АДМІНОМ**\n\n"
        "Щоб продовжити доступ до бота після тестового періоду або поставити питання — тисни кнопку нижче:",
        parse_mode="Markdown",
        reply_markup=buy_inline_keyboard()
    )

@dp.callback_query()
async def process_admin_callbacks(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    
    data = callback.data
    if data.startswith("give_30_"):
        uid = int(data.split("_")[2])
        new_exp = add_or_update_user(uid, "user", days=30)
        await callback.message.edit_text(
            f"{callback.message.text}\n\n✅ **ОНОВЛЕНО:** Надано +30 днів (до `{new_exp.strftime('%d.%m.%Y %H:%M')}`)",
            parse_mode="Markdown"
        )
        try:
            await bot.send_message(chat_id=uid, text=f"🎉 **Твою підписку продовжено на 30 днів!**\nДіє до: `{new_exp.strftime('%d.%m.%Y %H:%M')}`", parse_mode="Markdown")
        except:
            pass

    elif data.startswith("ban_"):
        uid = int(data.split("_")[1])
        block_user(uid)
        await callback.message.edit_text(f"{callback.message.text}\n\n🚫 **ОНОВЛЕНО:** Користувача заблоковано!", parse_mode="Markdown")
        try:
            await bot.send_message(chat_id=uid, text="❌ **Твій доступ до бота скасовано.**", parse_mode="Markdown")
        except:
            pass

@dp.message(F.text == "⚙️ Вставити посилання з фільтром Auto.ria")
async def ask_url(message: types.Message):
    has_access, exp_info = check_access(message.from_user.id)
    if not has_access:
        await message.answer(exp_info, parse_mode="Markdown", reply_markup=buy_inline_keyboard())
        return
    await message.answer("Надішли у відповідь повне посилання з Auto.ria.", parse_mode="Markdown")

@dp.message(F.text.startswith("https://auto.ria.com"))
async def process_url(message: types.Message):
    has_access, exp_info = check_access(message.from_user.id)
    if not has_access:
        await message.answer(exp_info, parse_mode="Markdown", reply_markup=buy_inline_keyboard())
        return
    save_user_url(message.from_user.id, message.text.strip())
    await message.answer("✅ **Фільтр збережено!** Бот вже сканує ринок під твій запит.", parse_mode="Markdown")

@dp.message(F.text == "📊 Мій статус / Підписка")
async def check_status(message: types.Message):
    user = get_user(message.from_user.id)
    if user:
        exp_str = user[3]
        status = "❌ Неактивна / Завершилась"
        if exp_str:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
            if datetime.now() < exp_date and user[4] == 1:
                status = "✅ Активна"
        await message.answer(
            f"📊 **Твій статус:** {status}\n"
            f"⏳ **Підписка діє до:** `{user[3] if user[3] else 'Не обмежено'}`\n\n"
            f"⚙️ **Твій активний URL:**\n{user[2] or 'Не вказано'}",
            parse_mode="Markdown",
            reply_markup=buy_inline_keyboard()
        )

@dp.message(F.text == "🛑 Зупинити пошук")
async def stop_search(message: types.Message):
    block_user(message.from_user.id)
    await message.answer("⏸ Пошук зупинено. Твій доступ поставлено на паузу.")

# === АДМІН-КОМАНДИ ===
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, expire_date, is_active FROM users")
    users = cursor.fetchall()
    conn.close()

    txt = "👑 **АДМІН-ПАНЕЛЬ**\n\n**Список користувачів:**\n"
    for uid, un, exp, act in users:
        exp_val = exp[:10] if exp else "Немає"
        txt += f"• ID: `{uid}` | @{un} | До: `{exp_val}` | Стан: {'✅' if act else '❌'}\n"

    txt += (
        "\n**Команди управління:**\n"
        "• `/give ID ДНІ` — дати/продовжити доступ (наприклад: `/give 5482150373 30`)\n"
        "• `/ban ID` — миттєво заблокувати юзера"
    )
    await message.answer(txt, parse_mode="Markdown")

@dp.message(Command("give"))
async def give_access_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("⚠️ Пиши так: `/give USER_ID ДНІ`", parse_mode="Markdown")
        return
    
    uid = int(args[1])
    days = int(args[2])
    new_exp = add_or_update_user(uid, "user", days=days)
    
    await message.answer(f"✅ Користувачу `{uid}` додано {days} днів! Підписка до: `{new_exp.strftime('%d.%m.%Y %H:%M')}`", parse_mode="Markdown")
    try:
        await bot.send_message(chat_id=uid, text=f"🎉 **Твою підписку продовжено на {days} днів!**\nДіє до: `{new_exp.strftime('%d.%m.%Y %H:%M')}`", parse_mode="Markdown")
    except:
        pass

@dp.message(Command("ban"))
async def ban_user_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Пиши так: `/ban USER_ID`", parse_mode="Markdown")
        return
    
    uid = int(args[1])
    block_user(uid)
    await message.answer(f"🚫 Користувача `{uid}` заблоковано!", parse_mode="Markdown")
    try:
        await bot.send_message(chat_id=uid, text="❌ **Твій доступ до бота було скасовано адміном.**", parse_mode="Markdown")
    except:
        pass

# === СКАУТ-СКАНЕР ===
async def auto_scanner():
    print("🚀 Авто-сканер активних підписок запущено!")
    while True:
        subscribers = get_active_subscribers()
        for user_id, search_url in subscribers:
            cars = fetch_cars_from_url(search_url)
            for title, price, location, link in reversed(cars):
                if not is_car_sent(user_id, link):
                    inline_kb = InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="🔥 Відкрити на Auto.ria", url=link)]]
                    )
                    text = (
                        f"🏎 **ЗНАЙДЕНО НОВИЙ ВАРІАНТ!**\n\n"
                        f"📌 **{title}**\n"
                        f"💵 **Ціна:** {price}\n"
                        f"📍 **Локація:** {location}\n\n"
                        f"⚡️ *Купуй першим, поки не перехопили!*"
                    )
                    try:
                        await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", reply_markup=inline_kb)
                        mark_car_sent(user_id, link)
                        print(f"📩 Надіслано юзеру {user_id}: {title}")
                        await asyncio.sleep(1.5)
                    except Exception:
                        pass
        await asyncio.sleep(45)

async def main():
    init_db()
    asyncio.create_task(auto_scanner())
    print("🤖 Бот готовий до роботи!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
