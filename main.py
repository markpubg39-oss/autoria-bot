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
    
    # Таблиця користувачів
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            expire_date TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    
    # Таблиця фільтрів (мульти-посилання)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS filters (
            filter_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            search_url TEXT,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    """)
    
    # Таблиця відправлених авто
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_cars (
            user_id INTEGER,
            car_link TEXT,
            PRIMARY KEY (user_id, car_link)
        )
    """)
    
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, expire_date, is_active FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res

def add_or_update_user(user_id, username, days=3):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    user = get_user(user_id)
    
    if user:
        current_expire = datetime.strptime(user[2], "%Y-%m-%d %H:%M:%S") if user[2] else datetime.now()
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

# === ФУНКЦІЇ МУЛЬТИ-ФІЛЬТРІВ ===
def add_user_filter(user_id, url):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO filters (user_id, search_url) VALUES (?, ?)", (user_id, url))
    conn.commit()
    conn.close()

def get_user_filters(user_id):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT filter_id, search_url FROM filters WHERE user_id = ?", (user_id,))
    res = cursor.fetchall()
    conn.close()
    return res

def delete_user_filter(filter_id, user_id):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM filters WHERE filter_id = ? AND user_id = ?", (filter_id, user_id))
    conn.commit()
    conn.close()

def get_all_active_filters():
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT f.user_id, f.search_url 
        FROM filters f
        JOIN users u ON f.user_id = u.user_id
        WHERE u.is_active = 1
    """)
    filters_data = cursor.fetchall()
    conn.close()
    
    active = []
    now = datetime.now()
    for uid, url in filters_data:
        user = get_user(uid)
        if user and user[2]:
            try:
                exp_date = datetime.strptime(user[2], "%Y-%m-%d %H:%M:%S")
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
            [KeyboardButton(text="⚙️ Додати фільтр з Auto.ria")],
            [KeyboardButton(text="📁 Мої фільтри"), KeyboardButton(text="📊 Мій статус / Підписка")],
            [KeyboardButton(text="💳 Придбати підписку / Адміністратор"), KeyboardButton(text="🛑 Призупинити пошук")]
        ],
        resize_keyboard=True
    )

def buy_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💬 Зв'язатися з адміністратором", url=f"https://t.me/{ADMIN_USERNAME}")]]
    )

def check_access(user_id):
    user = get_user(user_id)
    if not user or user[3] == 0:
        return False, "❌ **Ваш доступ вимкнено або термін дії підписки закінчився.**\nНатисніть кнопку нижче, щоб зв'язатися з адміністратором для поновлення доступу."
    
    if user[2]:
        exp_date = datetime.strptime(user[2], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > exp_date:
            return False, f"⚠️ **Термін дії вашого тестового періоду / підписки закінчився.**\n\nДля продовження доступу, будь ласка, зверніться до адміністратора."
    
    return True, user[2]

# === ОБРОБКА КОМАНД ===
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    user = get_user(user_id)
    if not user:
        new_exp = add_or_update_user(user_id, username, days=3)
        msg_text = f"Вітаємо! Вам надано **3 дні безкоштовного тестового доступу**.\n\nТермін дії підписки до: `{new_exp.strftime('%d.%m.%Y %H:%M')}`\n\n"
        
        if user_id != ADMIN_ID:
            admin_msg = (
                f"🚨 **Новий користувач у боті**\n\n"
                f"👤 **Ім'я:** {message.from_user.first_name}\n"
                f"🏷 **Юзернейм:** @{message.from_user.username if message.from_user.username else 'відсутній'}\n"
                f"🆔 **ID:** `{user_id}`\n"
                f"⏳ **Тестовий доступ до:** `{new_exp.strftime('%d.%m.%Y %H:%M')}`"
            )
            admin_buttons = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ Надати 30 днів", callback_data=f"give_30_{user_id}"),
                    InlineKeyboardButton(text="🚫 Заблокувати", callback_data=f"ban_{user_id}")
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
        msg_text = f"Вітаємо! Ваш доступ до системи активний.\n\n"

    msg_text += (
        "📋 **Інструкція з користування:**\n"
        "1. Перейдіть на сайт Auto.ria та налаштуйте необхідні параметри пошуку (марка, ціна, рік випуску тощо).\n"
        "2. Скопіюйте посилання з адресного рядка браузера.\n"
        "3. Натисніть кнопку **«⚙️ Додати фільтр з Auto.ria»** та надішліть посилання в чат.\n\n"
        "💡 Ви можете додавати декілька фільтрів одночасно. Переглянути збережені посилання можна у розділі **«📁 Мої фільтри»**."
    )
    await message.answer(msg_text, parse_mode="Markdown", reply_markup=main_keyboard())

@dp.message(F.text.in_({"⚙️ Додати фільтр з Auto.ria", "⚙️ Вставити посилання з фільтром Auto.ria"}))
async def ask_url(message: types.Message):
    has_access, exp_info = check_access(message.from_user.id)
    if not has_access:
        await message.answer(exp_info, parse_mode="Markdown", reply_markup=buy_inline_keyboard())
        return
    await message.answer("Будь ласка, надішліть повне посилання з сайту Auto.ria у відповідь на це повідомлення.", parse_mode="Markdown")

@dp.message(F.text.startswith("https://auto.ria.com"))
async def process_url(message: types.Message):
    has_access, exp_info = check_access(message.from_user.id)
    if not has_access:
        await message.answer(exp_info, parse_mode="Markdown", reply_markup=buy_inline_keyboard())
        return
    
    add_user_filter(message.from_user.id, message.text.strip())
    await message.answer("✅ **Ваш фільтр успішно збережено.**\n\nСистема розпочала моніторинг оголошень за вказаними параметрами. Керувати збереженими фільтрами можна у розділі **«📁 Мої фільтри»**.", parse_mode="Markdown")

# === ОБРОБКА «МОЇ ФІЛЬТРИ» ТА ВИДАЛЕННЯ ===
@dp.message(F.text == "📁 Мої фільтри")
async def show_my_filters(message: types.Message):
    user_id = message.from_user.id
    user_filters = get_user_filters(user_id)
    
    if not user_filters:
        await message.answer("📂 **У вас немає активних фільтрів.**\n\nЩоб додати новий фільтр, скористайтесь кнопкою **«⚙️ Додати фільтр з Auto.ria»**.", parse_mode="Markdown")
        return

    await message.answer("📂 **Ваші збережені фільтри:**\nЩоб видалити потрібний фільтр, натисніть кнопку ❌ нижче відповідного посилання.")
    
    for filter_id, url in user_filters:
        short_url = url[:45] + "..." if len(url) > 45 else url
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔗 Відкрити посилання", url=url),
                InlineKeyboardButton(text="❌ Видалити", callback_data=f"del_filter_{filter_id}")
            ]
        ])
        await message.answer(f"📌 **Фільтр:**\n`{short_url}`", parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("del_filter_"))
async def delete_filter_callback(callback: types.CallbackQuery):
    filter_id = int(callback.data.split("_")[2])
    delete_user_filter(filter_id, callback.from_user.id)
    await callback.message.edit_text("🗑 **Вказаний фільтр успішно видалено.**", parse_mode="Markdown")
    await callback.answer("Видалено")

@dp.message(F.text.in_({"💳 Придбати підписку / Адміністратор", "💳 Купити підписку / Адмін"}))
async def buy_sub_button(message: types.Message):
    await message.answer(
        "💳 **Оформлення підписки та зворотний зв'язок**\n\n"
        "Для продовження терміну дії підписки або з будь-яких інших питань скористайтесь кнопкою нижче:",
        parse_mode="Markdown",
        reply_markup=buy_inline_keyboard()
    )

@dp.callback_query(F.data.startswith("give_30_") | F.data.startswith("ban_"))
async def process_admin_callbacks(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    
    data = callback.data
    if data.startswith("give_30_"):
        uid = int(data.split("_")[2])
        new_exp = add_or_update_user(uid, "user", days=30)
        await callback.message.edit_text(
            f"{callback.message.text}\n\n✅ **Оновлено:** Надано +30 днів доступу (до `{new_exp.strftime('%d.%m.%Y %H:%M')}`)",
            parse_mode="Markdown"
        )
        try:
            await bot.send_message(chat_id=uid, text=f"🎉 **Вашу підписку успішно продовжено на 30 днів.**\nТермін дії до: `{new_exp.strftime('%d.%m.%Y %H:%M')}`", parse_mode="Markdown")
        except:
            pass

    elif data.startswith("ban_"):
        uid = int(data.split("_")[1])
        block_user(uid)
        await callback.message.edit_text(f"{callback.message.text}\n\n🚫 **Оновлено:** Користувача заблоковано.", parse_mode="Markdown")
        try:
            await bot.send_message(chat_id=uid, text="❌ **Ваш доступ до бота було припинено.**", parse_mode="Markdown")
        except:
            pass

@dp.message(F.text == "📊 Мій статус / Підписка")
async def check_status(message: types.Message):
    user = get_user(message.from_user.id)
    filters = get_user_filters(message.from_user.id)
    if user:
        exp_str = user[2]
        status = "❌ Неактивна / Завершилась"
        if exp_str:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
            if datetime.now() < exp_date and user[3] == 1:
                status = "✅ Активна"
        await message.answer(
            f"📊 **Інформація про підписку:**\n\n"
            f"• Статус: {status}\n"
            f"• Діє до: `{user[2] if user[2] else 'Не обмежено'}`\n"
            f"• Кількість активних фільтрів: `{len(filters)}`",
            parse_mode="Markdown",
            reply_markup=buy_inline_keyboard()
        )

@dp.message(F.text.in_({"🛑 Призупинити пошук", "🛑 Зупинити пошук"}))
async def stop_search(message: types.Message):
    block_user(message.from_user.id)
    await message.answer("⏸ Пошук призупинено. Ваш акаунт переведено в неактивний режим.")

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

    txt = "👑 **Панель адміністратора**\n\n**Список користувачів:**\n"
    for uid, un, exp, act in users:
        exp_val = exp[:10] if exp else "Немає"
        txt += f"• ID: `{uid}` | @{un} | До: `{exp_val}` | Стан: {'✅' if act else '❌'}\n"

    txt += (
        "\n**Команди управління:**\n"
        "• `/give ID ДНІ` — надати або продовжити доступ користувачу\n"
        "• `/ban ID` — заблокувати доступ користувачу"
    )
    await message.answer(txt, parse_mode="Markdown")

@dp.message(Command("give"))
async def give_access_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("⚠️ Формат команди: `/give USER_ID ДНІ`", parse_mode="Markdown")
        return
    
    uid = int(args[1])
    days = int(args[2])
    new_exp = add_or_update_user(uid, "user", days=days)
    
    await message.answer(f"✅ Користувачу `{uid}` надано доступ на {days} днів (до `{new_exp.strftime('%d.%m.%Y %H:%M')}`).", parse_mode="Markdown")
    try:
        await bot.send_message(chat_id=uid, text=f"🎉 **Вашу підписку продовжено на {days} днів.**\nТермін дії до: `{new_exp.strftime('%d.%m.%Y %H:%M')}`", parse_mode="Markdown")
    except:
        pass

@dp.message(Command("ban"))
async def ban_user_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Формат команди: `/ban USER_ID`", parse_mode="Markdown")
        return
    
    uid = int(args[1])
    block_user(uid)
    await message.answer(f"🚫 Користувача `{uid}` заблоковано.", parse_mode="Markdown")
    try:
        await bot.send_message(chat_id=uid, text="❌ **Ваш доступ до бота було скасовано адміністратором.**", parse_mode="Markdown")
    except:
        pass

# === СКАУТ-СКАНЕР ===
async def auto_scanner():
    print("🚀 Авто-сканер активних підписок запущено!")
    while True:
        subscribers_filters = get_all_active_filters()
        for user_id, search_url in subscribers_filters:
            cars = fetch_cars_from_url(search_url)
            for title, price, location, link in reversed(cars):
                if not is_car_sent(user_id, link):
                    inline_kb = InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="🔥 Відкрити на Auto.ria", url=link)]]
                    )
                    text = (
                        f"🏎 **ЗНАЙДЕНО НОВЕ ОГОЛОШЕННЯ!**\n\n"
                        f"📌 **{title}**\n"
                        f"💵 **Ціна:** {price}\n"
                        f"📍 **Локація:** {location}"
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
