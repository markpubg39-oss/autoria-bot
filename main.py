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

PROXY_URL = os.getenv("PROXY_URL") or None

PARSE_RETRIES = int(os.getenv("PARSE_RETRIES", "3"))
PARSE_TIMEOUT = int(os.getenv("PARSE_TIMEOUT", "25"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
]


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
        params = parse_qs(parsed.query)
        params["order"] = ["7"]
        params["page"] = ["0"]
        return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
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


def build_autoria_url(parsed: dict) -> str:
    """
    Будує чистий канонічний URL Auto.ria на основі витягнутих фільтрів.
    """
    base_url = "https://auto.ria.com/uk/search/"
    query_params = [
        ("indexName", "auto,order_by,desc"),
        ("categories.main.id", "1"),
        ("country.g.id", "218"),
        ("price.currency", "1" if parsed.get("price_currency") != "UAH" else "2"),
        ("sort[0].order", "dates.created.desc"),
        ("search_type", "1"),
        ("order", "7"),
        ("page", "0")
    ]

    for i, b in enumerate(parsed.get("brand_id", [])):
        query_params.append((f"brand.id[{i}]", str(b)))

    for i, m in enumerate(parsed.get("model_id", [])):
        query_params.append((f"model.id[{i}]", str(m)))

    if parsed.get("year_min") is not None:
        query_params.append(("s_yers[0]", str(parsed["year_min"])))
        query_params.append(("year[0].gte", str(parsed["year_min"])))
    if parsed.get("year_max") is not None:
        query_params.append(("po_yers[0]", str(parsed["year_max"])))
        query_params.append(("year[0].lte", str(parsed["year_max"])))

    if parsed.get("price_min") is not None:
        query_params.append(("price_ot", str(parsed["price_min"])))
        query_params.append(("price[0]", str(parsed["price_min"])))
    if parsed.get("price_max") is not None:
        query_params.append(("price_do", str(parsed["price_max"])))
        query_params.append(("price[1]", str(parsed["price_max"])))

    if parsed.get("mileage_min") is not None:
        query_params.append(("raceFrom", str(parsed["mileage_min"])))
    if parsed.get("mileage_max") is not None:
        query_params.append(("raceTo", str(parsed["mileage_max"])))

    for i, bt in enumerate(parsed.get("body_type", [])):
        query_params.append((f"body_style[{i}]", str(bt)))
    for i, g in enumerate(parsed.get("gearbox", [])):
        query_params.append((f"gearbox[{i}]", str(g)))
    for i, ft in enumerate(parsed.get("fuel_type", [])):
        query_params.append((f"type[{i}]", str(ft)))
    for i, r in enumerate(parsed.get("region", [])):
        query_params.append((f"region.id[{i}]", str(r)))

    return f"{base_url}?{urlencode(query_params)}"


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
        await conn.execute("""
            ALTER TABLE user_filters ADD COLUMN IF NOT EXISTS filters_json JSONB;
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


def _extract_cars_from_html(html_text: str) -> list:
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
                if status != 200:
                    logging.warning(f"Auto.ria відповіла статусом {status} для {url} (спроба {attempt}/{PARSE_RETRIES})")
                    last_error = RuntimeError(f"HTTP {status}")
                    await asyncio.sleep(min(5 * attempt, 20))
                    continue

                html_text = await resp.text()
                cars = _extract_cars_from_html(html_text)

                if not cars and "captcha" in html_text.lower():
                    logging.warning(f"Схоже на CAPTCHA-сторінку для {url} (спроба {attempt}/{PARSE_RETRIES})")
                    last_error = RuntimeError("Captcha/challenge page")
                    await asyncio.sleep(min(5 * attempt, 20))
                    continue

                return cars

        except Exception as e:
            last_error = e
            logging.error(f"Помилка запиту до Auto.ria (спроба {attempt}/{PARSE_RETRIES}): {type(e).__name__}: {e}")

        await asyncio.sleep(1.5 * attempt + random.uniform(0, 1.5))

    logging.error(f"Усі спроби запиту до {url} провалились. Остання помилка: {last_error}")
    return []


async def parse_autoria_smart(session: aiohttp.ClientSession, raw_url: str) -> list:
    """
    1. Перебудовує посилання в канонічне.
    2. Якщо знаходить авто — повертає їх.
    3. Якщо канонічний URL повернув 0 авто — робить fallback на оригінальний raw_url.
    """
    parsed_filters = parse_autoria_url(raw_url)
    canonical_url = build_autoria_url(parsed_filters)

    cars = await parse_autoria(session, canonical_url)
    if cars:
        return cars

    logging.info(f"Канонічний URL повернув 0 авто, робимо fallback на оригінальне посилання.")
    return await parse_autoria(session, raw_url)


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
            f"✅ **Фільтр успішно додано!**\n\n{html.escape(filters_summary)}\n\nЗнайдено {len(current_cars)} авто. Нові оголошення почнуть надходити.",
            parse_mode=ParseMode.HTML
        )
    else:
        await processing_msg.edit_text(
            f"⚠️ **Фільтр додано, але зараз не вдалося отримати оголошення з Auto.ria.**\n\n"
            f"{html.escape(filters_summary)}\n\n"
            f"Спробуємо ще раз під час наступного циклу моніторингу.",
            parse_mode=ParseMode.HTML
        )


async def monitor():
    while True:
        try:
            async with db_pool.acquire() as conn:
                filters = await conn.fetch("SELECT user_id, url FROM user_filters")

            for f in filters:
                user_id = f["user_id"]
                if not await check_subscription(user_id):
                    continue

                cars = await parse_autoria_smart(http_session, f["url"])
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

                await asyncio.sleep(random.uniform(1.5, 3.5))
        except Exception as e:
            logging.error(f"Помилка моніторингу: {type(e).__name__}: {e}")
        await asyncio.sleep(60)


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

    asyncio.create_task(monitor())
    logging.info("✅ Бот повністю запущений і працює!")

    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        await http_session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
