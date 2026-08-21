"""
Auto.ria Monitor Bot — Production Ready (Direct Parsing Fix)
"""

import asyncio
import html
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, urlunparse, unquote

import aiohttp
import asyncpg
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# ============================================================
#                       НАЛАШТУВАННЯ
# ============================================================

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

PROXY_URL = os.getenv("PROXY_URL") or None

PARSE_RETRIES = int(os.getenv("PARSE_RETRIES", "3"))
PARSE_TIMEOUT = int(os.getenv("PARSE_TIMEOUT", "25"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
PARSE_CONCURRENCY = int(os.getenv("PARSE_CONCURRENCY", "3"))
MAX_FILTERS_PER_USER = int(os.getenv("MAX_FILTERS_PER_USER", "5"))
REQUEST_DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "1.5"))
REQUEST_DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "3.5"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
]

_CHALLENGE_MARKERS = (
    "captcha",
    "cloudflare",
    "attention required",
    "just a moment",
    "cf-browser-verification",
    "cf-chl",
    "перевірка браузера",
    "проверка браузера",
    "checking your browser",
)


def build_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://auto.ria.com/uk/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


db_pool: Optional[asyncpg.Pool] = None
http_session: Optional[aiohttp.ClientSession] = None


def normalize_autoria_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url)
        pairs = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not re.match(r"^(order|page|sort(\[\d*\])?(\.\w+)?)$", k, re.IGNORECASE)
        ]
        pairs.append(("order", "7"))
        pairs.append(("page", "0"))
        pairs.append(("sort[0].order", "dates.created.desc"))
        new_query = urlencode(pairs, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    except Exception as e:
        logging.error(f"Помилка нормалізації URL: {e}")
        return raw_url


# ============================================================
#               УНІВЕРСАЛЬНИЙ ПАРСЕР ТА БУДІВНИК URL AUTO.RIA
# ============================================================

_COMPOUND_ALIASES = {
    "marka_id": ("brand_id", None, "multi"),
    "marka": ("brand_id", None, "multi"),
    "brandid": ("brand_id", None, "multi"),
    "model_id": ("model_id", None, "multi"),
    "modelid": ("model_id", None, "multi"),
    "s_yers": ("year", "min", "range"),
    "po_yers": ("year", "max", "range"),
    "year_from": ("year", "min", "range"),
    "year_to": ("year", "max", "range"),
    "price_ot": ("price", "min", "range"),
    "price_do": ("price", "max", "range"),
    "price_from": ("price", "min", "range"),
    "price_to": ("price", "max", "range"),
    "raceint": ("mileage", None, "range"),
    "custom": ("customs", None, "multi"),
    "customs": ("customs", None, "multi"),
}

_RANGE_FIELD_ALIASES = {
    "price": "price",
    "year": "year",
    "yers": "year",
    "mileage": "mileage",
    "race": "mileage",
    "probig": "mileage",
    "probeg": "mileage",
}

_MULTI_FIELD_ALIASES = {
    "brand": "brand_id",
    "model": "model_id",
    "body": "body_type",
    "bodystyle": "body_type",
    "kuzov": "body_type",
    "gearbox": "gearbox",
    "gear": "gearbox",
    "korobka": "gearbox",
    "fuel": "fuel_type",
    "fuelid": "fuel_type",
    "type": "fuel_type",
    "state": "region",
    "region": "region",
    "oblast": "region",
    "city": "city",
}

_HINT_MIN = {"gte", "ot", "from"}
_HINT_MAX = {"lte", "do", "to"}
_MODIFIER_TOKENS = {"id"}
_IGNORED_TOP_LEVEL = {"order", "page", "sort", "countpage", "size", "lang_id", "lang_ud"}

_TOKEN_RE = re.compile(r'([^\[\].]+)(?:\[(\d*)\])?')

_CURRENCY_MAP = {1: "USD", 2: "UAH", 3: "EUR"}
_CURRENCY_TO_CODE = {"USD": "1", "UAH": "2", "EUR": "3"}


def _deep_unquote(s: str) -> str:
    prev = s
    for _ in range(3):
        cur = unquote(prev)
        if cur == prev:
            break
        prev = cur
    return prev


def _tokenize_key(key: str):
    tokens = []
    for name, idx in _TOKEN_RE.findall(key):
        name = name.strip()
        if not name:
            continue
        tokens.append((name, int(idx) if idx.isdigit() else None))
    return tokens


def _to_number(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value


def parse_autoria_url(url: str) -> dict:
    result = {
        "brand_id": [],
        "model_id": [],
        "price_min": None,
        "price_max": None,
        "price_currency": None,
        "year_min": None,
        "year_max": None,
        "mileage_min": None,
        "mileage_max": None,
        "body_type": [],
        "gearbox": [],
        "fuel_type": [],
        "region": [],
        "city": [],
        "customs": [],
        "extra": {},
    }

    if not url:
        return result

    try:
        clean_url = _deep_unquote(url)
        parsed = urlparse(clean_url)
        pairs = parse_qsl(parsed.query, keep_blank_values=False)
    except Exception as e:
        logging.error(f"parse_autoria_url: не вдалось розпарсити URL '{url}': {e}")
        return result

    for raw_key, raw_value in pairs:
        try:
            key = _deep_unquote(raw_key)
            value = _deep_unquote(raw_value).strip()
            if not value:
                continue

            tokens = _tokenize_key(key)
            if not tokens:
                continue

            top_name = tokens[0][0].lower()
            if top_name in _IGNORED_TOP_LEVEL:
                continue

            for name, _ in tokens:
                if name.upper() in ("USD", "UAH", "EUR") and result["price_currency"] is None:
                    result["price_currency"] = name.upper()

            field = None
            bound = None
            category = None
            field_index = None
            field_pos = None

            for pos, (name, idx) in enumerate(tokens):
                alias = _COMPOUND_ALIASES.get(name.lower())
                if alias:
                    field, bound, category = alias
                    field_index = idx
                    field_pos = pos
                    break

            if field is None:
                for pos, (name, idx) in enumerate(tokens):
                    lname = name.lower()
                    if lname in _MODIFIER_TOKENS:
                        continue
                    if lname in _RANGE_FIELD_ALIASES:
                        field = _RANGE_FIELD_ALIASES[lname]
                        category = "range"
                        field_index = idx
                        field_pos = pos
                        break
                    if lname in _MULTI_FIELD_ALIASES:
                        field = _MULTI_FIELD_ALIASES[lname]
                        category = "multi"
                        field_index = idx
                        field_pos = pos
                        break

            if field is None:
                path = re.sub(r'\[\d*\]', '', key)
                result["extra"].setdefault(path, []).append(value)
                continue

            if category == "multi":
                bucket = result[field]
                item = _to_number(value) if value.isdigit() else value
                if item not in bucket:
                    bucket.append(item)
                continue

            if field == "price" and field_index is not None:
                num_val = _to_number(value)
                if field_index == 0:
                    if isinstance(num_val, int) and num_val in _CURRENCY_MAP:
                        result["price_currency"] = _CURRENCY_MAP[num_val]
                    continue
                elif field_index == 1:
                    result["price_min"] = num_val
                    continue
                elif field_index == 2:
                    result["price_max"] = num_val
                    continue

            if bound is None:
                for name, _ in tokens:
                    lname = name.lower()
                    if lname in _HINT_MIN:
                        bound = "min"
                        break
                    if lname in _HINT_MAX:
                        bound = "max"
                        break

            if bound is None and field_index is not None:
                bound = "min" if field_index == 0 else "max"

            number_value = _to_number(value)

            if bound is None:
                if field_pos is not None and field_pos == len(tokens) - 1:
                    bound = "min" if result.get(f"{field}_min") is None else "max"
                else:
                    path = re.sub(r'\[\d*\]', '', key)
                    result["extra"].setdefault(path, []).append(value)
                    continue

            result[f"{field}_{bound}"] = number_value

        except Exception as e:
            logging.warning(f"parse_autoria_url: пропущено параметр '{raw_key}'={raw_value!r}: {e}")
            continue

    return result


_FIELD_LABELS_UK = {
    "brand_id": "🚗 Марка (ID)",
    "model_id": "🚘 Модель (ID)",
    "body_type": "🚙 Тип кузова",
    "gearbox": "⚙️ Коробка передач",
    "fuel_type": "⛽ Паливо",
    "region": "📍 Область",
    "city": "🏙️ Місто",
    "customs": "🛃 Розмитнення",
}


def format_filters_summary(parsed: dict) -> str:
    lines = []
    for field, label in _FIELD_LABELS_UK.items():
        values = parsed.get(field) or []
        if values:
            lines.append(f"{label}: {', '.join(str(v) for v in values)}")

    price_min, price_max = parsed.get("price_min"), parsed.get("price_max")
    if price_min is not None or price_max is not None:
        cur = parsed.get("price_currency") or "USD"
        lines.append(f"💰 Ціна: {price_min if price_min is not None else '—'} — {price_max if price_max is not None else '—'} {cur}")

    year_min, year_max = parsed.get("year_min"), parsed.get("year_max")
    if year_min is not None or year_max is not None:
        lines.append(f"📅 Рік: {year_min if year_min is not None else '—'} — {year_max if year_max is not None else '—'}")

    mileage_min, mileage_max = parsed.get("mileage_min"), parsed.get("mileage_max")
    if mileage_min is not None or mileage_max is not None:
        lines.append(f"🛣️ Пробіг: {mileage_min if mileage_min is not None else '—'} — {mileage_max if mileage_max is not None else '—'} тис. км")

    if not lines:
        return "ℹ️ Не вдалося розпізнати конкретні фільтри в посиланні — воно оброблятиметься як є."

    return "\n".join(lines)


# ============================================================
#                           БАЗА ДАНИХ
# ============================================================

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
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
        await conn.execute("""
            ALTER TABLE user_filters ADD COLUMN IF NOT EXISTS filters_json JSONB;
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sent_cars_user ON sent_cars(user_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_filters_user ON user_filters(user_id);
        """)


async def check_subscription(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    async with db_pool.acquire() as conn:
        expires = await conn.fetchval("SELECT subscription_expires FROM bot_users WHERE user_id = $1", user_id)
    if not expires:
        return False
    return expires > datetime.now()


# ============================================================
#                    ВІДПРАВКА ПОВІДОМЛЕНЬ
# ============================================================

def format_car_notification(car: dict) -> str:
    lines = [
        "🚗 <b>Нове оголошення!</b>",
        "",
        f"📌 <b>{html.escape(car.get('title') or 'Автомобіль')}</b>",
        f"💰 <b>Ціна:</b> {html.escape(car.get('price') or 'не вказано')}",
    ]
    if car.get("link"):
        lines.append("")
        lines.append(f'🔗 <a href="{html.escape(car["link"], quote=True)}">Переглянути на Auto.ria</a>')
    return "\n".join(lines)


async def send_car_notification(user_id: int, car: dict) -> bool:
    caption = format_car_notification(car)
    image_url = car.get("image")

    for _ in range(3):
        try:
            if image_url:
                await bot.send_photo(user_id, photo=image_url, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(user_id, caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
            return True
        except TelegramRetryAfter as e:
            logging.warning(f"Флуд-контроль Telegram: чекаємо {e.retry_after}с (user {user_id})")
            await asyncio.sleep(e.retry_after + 0.5)
            continue
        except TelegramForbiddenError:
            logging.info(f"Користувач {user_id} заблокував бота — пропускаємо.")
            return False
        except TelegramBadRequest:
            if image_url:
                image_url = None
                continue
            logging.error(f"TelegramBadRequest при відправці для {user_id}")
            return False
        except Exception as e:
            logging.error(f"Помилка відправки для {user_id}: {type(e).__name__}: {e}")
            return False
    return False


# ============================================================
#                           ПАРСИНГ
# ============================================================

def _extract_cars_from_html(html_text: str) -> list:
    soup = BeautifulSoup(html_text, "html.parser")
    sections = soup.select('section.ticket-item, div.ticket-item, div[data-good-id], div.search-result-item, section.item-ticket')

    cars = []
    for section in sections:
        try:
            car_id = section.get("data-id") or section.get("data-good-id") or section.get("data-item-id")
            
            title_elem = (
                section.find("a", class_="address") 
                or section.find("a", class_="m-link") 
                or section.select_one('a[href*="/auto_"]')
                or section.find("a", href=True)
            )

            if not car_id and title_elem:
                href = title_elem.get("href", "")
                if "/auto_" in href or "auto.ria.com" in href:
                    match = re.search(r'_(\d+)\.html', href)
                    if match:
                        car_id = match.group(1)
                    else:
                        parts = href.split("_")
                        if parts:
                            car_id = ''.join(filter(str.isdigit, parts[-1]))

            if not car_id:
                continue

            price_elem = (
                section.find("span", class_="size22")
                or section.find("span", class_="price-ticket")
                or section.find("strong", class_="bold")
                or section.select_one('.price-color')
                or section.select_one('[data-currency]')
            )

            title = " ".join((title_elem.text if title_elem else "Автомобіль").split())
            price = " ".join((price_elem.text if price_elem else "Ціну не вказано").split())

            link = title_elem.get("href") if title_elem else ""
            if link and not link.startswith("http"):
                link = "https://auto.ria.com" + link

            image = None
            img_elem = section.find("img")
            if img_elem:
                image = img_elem.get("src") or img_elem.get("data-src") or img_elem.get("data-lazy")
                if image and image.startswith("//"):
                    image = "https:" + image

            cars.append({
                "car_id": str(car_id),
                "title": title,
                "price": price,
                "link": link,
                "image": image,
            })
        except Exception:
            continue
    return cars


async def parse_autoria(session: aiohttp.ClientSession, url: str) -> list:
    last_error: Optional[Exception] = None

    for attempt in range(1, PARSE_RETRIES + 1):
        try:
            request_kwargs = dict(
                headers=build_headers(),
                timeout=aiohttp.ClientTimeout(total=PARSE_TIMEOUT),
                allow_redirects=True,
            )
            if PROXY_URL:
                request_kwargs["proxy"] = PROXY_URL

            async with session.get(url, **request_kwargs) as resp:
                status = resp.status

                if status in (403, 429):
                    retry_after_header = resp.headers.get("Retry-After")
                    wait = float(retry_after_header) if retry_after_header and retry_after_header.isdigit() else min(8 * attempt, 40)
                    logging.warning(f"Auto.ria: статус {status} для {url} (спроба {attempt}/{PARSE_RETRIES}), чекаємо {wait:.0f}с")
                    last_error = RuntimeError(f"HTTP {status}")
                    await asyncio.sleep(wait)
                    continue

                if status != 200:
                    logging.warning(f"Auto.ria відповіла статусом {status} для {url} (спроба {attempt}/{PARSE_RETRIES})")
                    last_error = RuntimeError(f"HTTP {status}")
                    await asyncio.sleep(min(5 * attempt, 20))
                    continue

                html_text = await resp.text()
                lowered = html_text.lower()
                if any(marker in lowered for marker in _CHALLENGE_MARKERS):
                    logging.warning(f"Схоже на CAPTCHA/Cloudflare-сторінку для {url} (спроба {attempt}/{PARSE_RETRIES})")
                    last_error = RuntimeError("Captcha/challenge page")
                    await asyncio.sleep(min(6 * attempt, 30))
                    continue

                return _extract_cars_from_html(html_text)

        except asyncio.TimeoutError as e:
            last_error = e
            logging.error(f"Таймаут запиту до Auto.ria (спроба {attempt}/{PARSE_RETRIES}): {url}")
        except aiohttp.ClientError as e:
            last_error = e
            logging.error(f"Мережева помилка запиту до Auto.ria (спроба {attempt}/{PARSE_RETRIES}): {type(e).__name__}: {e}")
        except Exception as e:
            last_error = e
            logging.error(f"Неочікувана помилка парсингу (спроба {attempt}/{PARSE_RETRIES}): {type(e).__name__}: {e}")

        await asyncio.sleep(1.5 * attempt + random.uniform(0, 1.5))

    logging.error(f"Усі спроби запиту до {url} провалились. Остання помилка: {last_error}")
    return []


async def parse_autoria_smart(session: aiohttp.ClientSession, raw_url: str) -> list:
    normalized = normalize_autoria_url(raw_url)
    cars = await parse_autoria(session, normalized)
    if cars:
        return cars
    return await parse_autoria(session, raw_url)


# ============================================================
#                        UI / КЛАВІАТУРИ
# ============================================================

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
        "Надішли мені посилання на пошук з Auto.ria (з телефону чи з компʼютера, "
        "будь-яке — я сам приведу його до потрібного вигляду), і я сповіщатиму тебе "
        "про нові оголошення!",
        reply_markup=get_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(Command("help"))
async def help_cmd(msg: types.Message):
    await msg.answer(
        "ℹ️ <b>Як користуватись:</b>\n\n"
        "1️⃣ Знайди потрібні авто на auto.ria.com з фільтрами (марка, ціна, рік тощо)\n"
        "2️⃣ Скопіюй посилання і надішли його сюди\n"
        "3️⃣ Бот сам почне моніторити нові оголошення за цим фільтром\n\n"
        f"Можна додати до {MAX_FILTERS_PER_USER} фільтрів одночасно.\n"
        "Керувати фільтрами: «📁 Мої фільтри».",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard(),
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
        filters = await conn.fetch("SELECT id, url, filters_json FROM user_filters WHERE user_id = $1", msg.from_user.id)
    if not filters:
        await msg.answer("У вас немає збережених фільтрів.")
        return
    for f in filters:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Видалити", callback_data=f"del_{f['id']}")]])
        text = f"🔗 `{html.escape(f['url'])}`"
        if f["filters_json"]:
            try:
                parsed = json.loads(f["filters_json"]) if isinstance(f["filters_json"], str) else f["filters_json"]
                summary = format_filters_summary(parsed)
                text += f"\n\n{html.escape(summary)}"
            except Exception:
                pass
        await msg.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


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


async def _add_single_filter(msg: types.Message, raw_url: str, user_id: int) -> None:
    async with db_pool.acquire() as conn:
        already_exists = await conn.fetchval(
            "SELECT 1 FROM user_filters WHERE user_id = $1 AND url = $2",
            user_id, normalize_autoria_url(raw_url)
        )
        if not already_exists:
            count = await conn.fetchval("SELECT COUNT(*) FROM user_filters WHERE user_id = $1", user_id)
            if count >= MAX_FILTERS_PER_USER and user_id != ADMIN_ID:
                await msg.answer(
                    f"⚠️ Досягнуто ліміт фільтрів ({MAX_FILTERS_PER_USER}). "
                    f"Видаліть старий фільтр у «📁 Мої фільтри», щоб додати новий."
                )
                return

    url = normalize_autoria_url(raw_url)
    processing_msg = await msg.answer("⏳ Обробляю посилання...")

    parsed_filters = parse_autoria_url(url)
    filters_summary = format_filters_summary(parsed_filters)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_filters (user_id, url, filters_json)
            VALUES ($1, $2, $3::jsonb)
            ON CONFLICT (user_id, url) DO UPDATE SET filters_json = EXCLUDED.filters_json
            """,
            user_id, url, json.dumps(parsed_filters, ensure_ascii=False)
        )

    current_cars = await parse_autoria_smart(http_session, url)
    if current_cars:
        records = [(user_id, c["car_id"]) for c in current_cars]
        async with db_pool.acquire() as conn:
            await conn.executemany("INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", records)
        await processing_msg.edit_text(
            f"✅ <b>Фільтр успішно додано!</b>\n\n{html.escape(filters_summary)}\n\n"
            f"Знайдено {len(current_cars)} авто. Нові оголошення почнуть надходити.",
            parse_mode=ParseMode.HTML
        )
    else:
        await processing_msg.edit_text(
            f"⚠️ <b>Фільтр додано, але зараз не вдалося отримати оголошення з Auto.ria.</b>\n\n"
            f"{html.escape(filters_summary)}\n\n"
            f"Спробуємо ще раз під час наступного циклу моніторингу.",
            parse_mode=ParseMode.HTML
        )


@dp.message(F.text.regexp(r"https?://\S+") | F.caption.regexp(r"https?://\S+"))
async def add_filter(msg: types.Message):
    if not await check_subscription(msg.from_user.id):
        await msg.answer("❌ Необхідна активна підписка!")
        return

    text_content = msg.text or msg.caption or ""
    raw_urls = re.findall(r"https?://\S+", text_content)
    autoria_urls = [u for u in raw_urls if "auto.ria.com" in u]

    if not autoria_urls:
        await msg.answer("❌ Посилання має бути з сайту auto.ria.com!")
        return

    user_id = msg.from_user.id
    for raw_url in autoria_urls:
        try:
            await _add_single_filter(msg, raw_url, user_id)
        except Exception as e:
            logging.error(f"Помилка додавання фільтра для {user_id}: {type(e).__name__}: {e}")
            await msg.answer("❌ Сталася помилка при додаванні цього посилання. Спробуйте ще раз пізніше.")


# ============================================================
#                     ФОНОВИЙ МОНІТОРИНГ
# ============================================================

async def _process_filter(sem: asyncio.Semaphore, record) -> None:
    user_id = record["user_id"]
    url = record["url"]

    async with sem:
        try:
            if not await check_subscription(user_id):
                return

            cars = await parse_autoria_smart(http_session, url)
            if not cars:
                return

            car_ids = [c["car_id"] for c in cars]
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT car_id FROM sent_cars WHERE user_id = $1 AND car_id = ANY($2::text[])",
                    user_id, car_ids
                )
            already_sent = {r["car_id"] for r in rows}
            new_cars = [c for c in cars if c["car_id"] not in already_sent]
            if not new_cars:
                return

            newly_sent_ids = []
            for car in new_cars:
                ok = await send_car_notification(user_id, car)
                if ok:
                    newly_sent_ids.append(car["car_id"])
                await asyncio.sleep(0.5)

            if newly_sent_ids:
                async with db_pool.acquire() as conn:
                    await conn.executemany(
                        "INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        [(user_id, cid) for cid in newly_sent_ids]
                    )
        except Exception as e:
            logging.error(f"Помилка обробки фільтра user={user_id}: {type(e).__name__}: {e}")
        finally:
            await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


async def monitor():
    while True:
        try:
            async with db_pool.acquire() as conn:
                filters = await conn.fetch("SELECT user_id, url FROM user_filters")

            sem = asyncio.Semaphore(PARSE_CONCURRENCY)
            await asyncio.gather(*[_process_filter(sem, f) for f in filters], return_exceptions=True)
        except Exception as e:
            logging.error(f"Критична помилка циклу моніторингу: {type(e).__name__}: {e}")

        await asyncio.sleep(MONITOR_INTERVAL)


async def monitor_watchdog():
    while True:
        try:
            await monitor()
        except Exception as e:
            logging.error(f"monitor() несподівано завершився: {type(e).__name__}: {e}. Перезапуск через 10с.")
            await asyncio.sleep(10)


# ============================================================
#                             MAIN
# ============================================================

async def main():
    global http_session
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    await init_db()

    connector = aiohttp.TCPConnector(limit=10, limit_per_host=4, ttl_dns_cache=300)
    http_session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(), connector=connector)

    if PROXY_URL:
        logging.info("🌐 Проксі увімкнено для запитів до Auto.ria.")
    else:
        logging.info("🌐 Проксі не задано (PROXY_URL порожній) — запити йдуть напряму.")

    await bot.delete_webhook(drop_pending_updates=True)

    asyncio.create_task(monitor_watchdog())
    logging.info("✅ Бот повністю запущений і працює!")

    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        await http_session.close()
        if db_pool:
            await db_pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
