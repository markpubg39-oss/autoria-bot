"""
Auto.ria Monitor Bot — Production Ready + Full Admin Control
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
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, unquote

import aiohttp
import asyncpg
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# ============================================================
#                     НАЛАШТУВАННЯ
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

PARSE_RETRIES = int(os.getenv("PARSE_RETRIES", "4"))
PARSE_TIMEOUT = int(os.getenv("PARSE_TIMEOUT", "25"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "60"))
PARSE_CONCURRENCY = int(os.getenv("PARSE_CONCURRENCY", "3"))
MAX_FILTERS_PER_USER = int(os.getenv("MAX_FILTERS_PER_USER", "5"))
REQUEST_DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "1.5"))
REQUEST_DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "3.5"))

# Підписка, яку автоматично отримує новий користувач при першому /start
NEW_USER_SUBSCRIPTION_DAYS = int(os.getenv("NEW_USER_SUBSCRIPTION_DAYS", "3"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
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
    "ddos-guard",
)


def build_headers(force_ua: Optional[str] = None) -> dict:
    return {
        "User-Agent": force_ua or random.choice(USER_AGENTS),
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
    """Бере посилання користувача як є і лише примусово додає сортування за датою + скидає page/order."""
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
#               ПАРСЕР ФІЛЬТРІВ (для гарного відображення)
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
        "brand_id": [], "model_id": [],
        "price_min": None, "price_max": None, "price_currency": None,
        "year_min": None, "year_max": None,
        "mileage_min": None, "mileage_max": None,
        "body_type": [], "gearbox": [], "fuel_type": [],
        "region": [], "city": [], "customs": [],
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
#                            БАЗА ДАНИХ
# ============================================================
# ТУТ ДОДАНО: Спроби підключення (retries) та обгортання DDL у транзакцію!

async def init_db():
    global db_pool
    for attempt in range(1, 6):
        try:
            db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, timeout=15)
            break
        except Exception as e:
            if attempt == 5:
                raise
            logging.warning(f"Спроба {attempt}/5 підключення до БД не вдалась: {e}. Повтор через 3с...")
            await asyncio.sleep(3)

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    subscription_expires TIMESTAMP
                );
            """)
            await conn.execute("""
                ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS username TEXT;
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
        "🚗 <b>Знайдено нове оголошення!</b>",
    ]
    if car.get("link"):
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
#                            ПАРСИНГ HTML
# ============================================================

URL_RE = re.compile(r"https?://\S+")

_ID_FROM_HREF_RE = re.compile(r'/auto_[^"/?]*?_(\d{6,})\.html', re.IGNORECASE)
_ID_ANY_RE = re.compile(r'(\d{6,})\.html')


def _extract_id_from_href(href: str) -> Optional[str]:
    if not href:
        return None
    m = _ID_FROM_HREF_RE.search(href)
    if m:
        return m.group(1)
    m = _ID_ANY_RE.search(href)
    if m:
        return m.group(1)
    return None


def _normalize_image(src: Optional[str]) -> Optional[str]:
    if not src:
        return None
    src = src.strip()
    if not src or src.startswith("data:"):
        return None
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return "https://auto.ria.com" + src
    return src


def _extract_via_dom(soup: BeautifulSoup) -> list:
    """Стратегія 1: DOM парсинг картки оголошень."""
    sections = soup.select(
        'section.ticket-item, div.ticket-item, section.item-ticket, '
        'div[data-good-id], div.search-result-item, '
        'div[data-marker="list-item"], article[data-id], '
        'section[data-id][data-good-id], div.content-bar, '
        'div.ticket-status'
    )

    cars = []
    for section in sections:
        try:
            car_id = (
                section.get("data-id")
                or section.get("data-good-id")
                or section.get("data-item-id")
                or section.get("data-advertisement-id")
            )

            title_elem = (
                section.select_one('a.address')
                or section.select_one('a.m-link')
                or section.select_one('a.blue.bold')
                or section.select_one('span.blue.bold')
                or section.select_one('.head-ticket a')
                or section.select_one('a[href*="/auto_"]')
                or section.find("a", href=True)
            )

            href = title_elem.get("href", "") if title_elem else ""
            if not car_id and href:
                car_id = _extract_id_from_href(href)

            if not car_id:
                any_link = section.select_one('a[href*="/auto_"], a[href*=".html"]')
                if any_link:
                    car_id = _extract_id_from_href(any_link.get("href", ""))
                    if not title_elem:
                        title_elem = any_link
                        href = any_link.get("href", "")

            if not car_id:
                continue

            price_elem = (
                section.select_one('.price-ticket[data-currency="USD"]')
                or section.select_one('.price-ticket')
                or section.select_one('span.size22.green')
                or section.select_one('span.size22')
                or section.select_one('.green.bold')
                or section.select_one('span.price')
                or section.select_one('[data-currency]')
                or section.select_one('.price-color')
            )

            title_text = title_elem.get_text(strip=True) if title_elem else ""
            price_text = price_elem.get_text(strip=True) if price_elem else ""

            title = " ".join(title_text.split()) if title_text else "Автомобіль"
            price = " ".join(price_text.split()) if price_text else "Ціну не вказано"

            link = href
            if link and not link.startswith("http"):
                link = "https://auto.ria.com" + link if link.startswith("/") else link

            image = None
            img_elem = section.find("img")
            if img_elem:
                image = _normalize_image(
                    img_elem.get("src") or img_elem.get("data-src")
                    or img_elem.get("data-lazy") or img_elem.get("data-original")
                )
            if not image:
                picture_source = section.find("source")
                if picture_source:
                    image = _normalize_image(picture_source.get("srcset") or picture_source.get("data-srcset"))

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


def _extract_via_jsonld(soup: BeautifulSoup) -> list:
    cars = []

    def _walk(node):
        if isinstance(node, dict):
            node_type = str(node.get("@type", "")).lower()
            url_val = node.get("url") or node.get("@id")
            if node_type in ("car", "product", "vehicle") or (url_val and "/auto_" in str(url_val)):
                href = url_val or ""
                car_id = _extract_id_from_href(str(href))
                if car_id:
                    name = node.get("name") or "Автомобіль"
                    offers = node.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price_val = None
                    if isinstance(offers, dict):
                        price_val = offers.get("price") or offers.get("priceSpecification", {}).get("price")
                        currency = offers.get("priceCurrency", "")
                    else:
                        currency = ""
                    price = f"{price_val} {currency}".strip() if price_val else "Ціну не вказано"
                    image = node.get("image")
                    if isinstance(image, list):
                        image = image[0] if image else None
                    link = str(href)
                    if link and not link.startswith("http"):
                        link = "https://auto.ria.com" + link
                    cars.append({
                        "car_id": str(car_id),
                        "title": " ".join(str(name).split()),
                        "price": price,
                        "link": link,
                        "image": _normalize_image(image) if image else None,
                    })
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "{}")
        except Exception:
            continue
        _walk(data)

    return cars


_JSON_ID_BLOCK_RE = re.compile(
    r'"(?:advertisementId|adId|id)"\s*:\s*(\d{6,})[^}]{0,600}?'
    r'(?:"title"\s*:\s*"([^"]{0,200})")?',
    re.IGNORECASE,
)


def _extract_via_regex_fallback(html_text: str) -> list:
    cars = {}

    for m in re.finditer(r'href="([^"]*?/auto_[^"]*?_(\d{6,})\.html[^"]*)"', html_text):
        href, car_id = m.group(1), m.group(2)
        if car_id in cars:
            continue
        link = href if href.startswith("http") else "https://auto.ria.com" + href
        cars[car_id] = {
            "car_id": car_id,
            "title": "Автомобіль",
            "price": "Ціну не вказано",
            "link": link,
            "image": None,
        }

    for m in _JSON_ID_BLOCK_RE.finditer(html_text):
        car_id, title = m.group(1), m.group(2)
        if car_id not in cars:
            cars[car_id] = {
                "car_id": car_id,
                "title": " ".join(title.split()) if title else "Автомобіль",
                "price": "Ціну не вказано",
                "link": f"https://auto.ria.com/uk/auto_{car_id}.html",
                "image": None,
            }
        elif title and cars[car_id]["title"] == "Автомобіль":
            cars[car_id]["title"] = " ".join(title.split())

    return list(cars.values())


def _extract_cars_from_html(html_text: str) -> list:
    soup = BeautifulSoup(html_text, "html.parser")
    merged: dict = {}

    for strategy in (_extract_via_dom, _extract_via_jsonld):
        try:
            for car in strategy(soup):
                cid = car["car_id"]
                if cid not in merged:
                    merged[cid] = car
                else:
                    for key in ("title", "price", "link", "image"):
                        if not merged[cid].get(key) or merged[cid][key] in ("Автомобіль", "Ціну не вказано"):
                            if car.get(key):
                                merged[cid][key] = car[key]
        except Exception as e:
            logging.warning(f"Стратегія парсингу {strategy.__name__} впала: {type(e).__name__}: {e}")

    if not merged:
        try:
            for car in _extract_via_regex_fallback(html_text):
                merged.setdefault(car["car_id"], car)
        except Exception as e:
            logging.warning(f"Regex fallback впав: {type(e).__name__}: {e}")

    return list(merged.values())


async def parse_autoria(session: aiohttp.ClientSession, url: str) -> list:
    last_error: Optional[Exception] = None
    used_agents: list = []

    for attempt in range(1, PARSE_RETRIES + 1):
        try:
            available_agents = [ua for ua in USER_AGENTS if ua not in used_agents] or USER_AGENTS
            ua = random.choice(available_agents)
            used_agents.append(ua)

            request_kwargs = dict(
                headers=build_headers(force_ua=ua),
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
                    logging.warning(f"Auto.ria: статус {status} для {url} (спроба {attempt}/{PARSE_RETRIES}), чекаємо {wait:.0f}с, новий UA")
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
                    logging.warning(f"Схоже на CAPTCHA/Cloudflare-сторінку для {url} (спроба {attempt}/{PARSE_RETRIES}), новий UA")
                    last_error = RuntimeError("Captcha/challenge page")
                    await asyncio.sleep(min(6 * attempt, 30))
                    continue

                cars = _extract_cars_from_html(html_text)
                if not cars and attempt < PARSE_RETRIES:
                    logging.info(f"0 авто на спробі {attempt}/{PARSE_RETRIES} для {url} — повтор з іншим User-Agent")
                    last_error = RuntimeError("0 cars extracted")
                    await asyncio.sleep(min(4 * attempt, 15))
                    continue

                return cars

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
    normalized_url = normalize_autoria_url(raw_url)
    return await parse_autoria(session, normalized_url)


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


# ============================================================
#              START (реєстрація юзера + атомарність)
# ============================================================
# ТУТ ДОДАНО: Атомарний INSERT без конфліктів і винесене сповіщення адміну!

@dp.message(Command("start"))
async def start(msg: types.Message):
    user_id = msg.from_user.id
    username = msg.from_user.username or "без_юзернейму"

    is_new = False
    async with db_pool.acquire() as conn:
        # Атомарна перевірка та вставка без ризику Race Condition (UniqueViolation)
        row = await conn.fetchrow(
            """
            INSERT INTO bot_users (user_id, username, subscription_expires)
            VALUES ($1, $2, NOW() + make_interval(days => $3))
            ON CONFLICT (user_id) DO UPDATE 
            SET username = EXCLUDED.username
            RETURNING (xmax = 0) AS inserted;
            """,
            user_id, username, NEW_USER_SUBSCRIPTION_DAYS
        )
        if row and row["inserted"]:
            is_new = True

    # Якщо користувач дійсно новий — надсилаємо сповіщення адміну (винесено за межі пулу з'єднань)
    if is_new:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🆕 <b>Новий користувач!</b>\n"
                f"ID: <code>{user_id}</code>\n"
                f"Username: @{html.escape(username)}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logging.warning(f"Не вдалося сповістити адміна про нового користувача: {e}")

    await msg.answer(
        "👋 **Вітаю! Я бот для швидкого моніторингу Auto.ria.**\n\n"
        "Надішли мені посилання на пошук з Auto.ria (з телефону чи з компʼютера, "
        "будь-яке — я сам приведу його до потрібного вигляду), і я сповіщатиму тебе "
        f"про нові оголошення!\n\n"
        f"🎁 Тобі активовано безкоштовну підписку на {NEW_USER_SUBSCRIPTION_DAYS} дні.",
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
        text = f"🔗 {html.escape(f['url'])}"
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
    normalized_url = normalize_autoria_url(raw_url)

    async with db_pool.acquire() as conn:
        already_exists = await conn.fetchval(
            "SELECT 1 FROM user_filters WHERE user_id = $1 AND url = $2",
            user_id, normalized_url
        )
        if not already_exists:
            count = await conn.fetchval("SELECT COUNT(*) FROM user_filters WHERE user_id = $1", user_id)
            if count >= MAX_FILTERS_PER_USER and user_id != ADMIN_ID:
                await msg.answer(
                    f"⚠️ Досягнуто ліміт фільтрів ({MAX_FILTERS_PER_USER}). "
                    f"Видаліть старий фільтр у «📁 Мої фільтри», щоб додати новий."
                )
                return

    processing_msg = await msg.answer("⏳ Обробляю посилання...")

    parsed_filters = parse_autoria_url(normalized_url)
    filters_summary = format_filters_summary(parsed_filters)

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO user_filters (user_id, url, filters_json)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (user_id, url) DO UPDATE SET filters_json = EXCLUDED.filters_json
                """,
                user_id, normalized_url, json.dumps(parsed_filters, ensure_ascii=False)
            )
    except Exception as e:
        logging.error(f"Критична помилка збереження фільтра user={user_id}: {type(e).__name__}: {e}")
        await processing_msg.edit_text("❌ Не вдалося зберегти фільтр через помилку бази даних. Спробуйте ще раз пізніше.")
        return

    current_cars = []
    try:
        current_cars = await parse_autoria_smart(http_session, normalized_url)
    except Exception as e:
        logging.error(f"Помилка парсингу оголошень для фільтра user={user_id}: {e}")
        current_cars = []

    if current_cars:
        try:
            baseline_records = [(user_id, c["car_id"]) for c in current_cars]
            async with db_pool.acquire() as conn:
                await conn.executemany(
                    "INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    baseline_records
                )
        except Exception as e:
            logging.error(f"Помилка запису baseline sent_cars: {e}")

    if current_cars:
        await processing_msg.edit_text(
            f"✅ <b>Фільтр успішно додано!</b>\n\n{html.escape(filters_summary)}\n\n"
            f"📊 Знайдено {len(current_cars)} авто за поточним фільтром — усі вони зафіксовані "
            f"як вже переглянуті (baseline).\n"
            f"Тепер бот надсилатиме виключно НОВІ оголошення.",
            parse_mode=ParseMode.HTML
        )
    else:
        await processing_msg.edit_text(
            f"✅ <b>Фільтр додано і збережено!</b>\n\n{html.escape(filters_summary)}\n\n"
            f"⚠️ Не вдалося отримати список оголошень з Auto.ria. Моніторинг запущено.",
            parse_mode=ParseMode.HTML
        )


def _message_has_url(message: types.Message) -> bool:
    text_content = message.text or message.caption or ""
    return bool(URL_RE.search(text_content))


@dp.message(_message_has_url)
async def add_filter(msg: types.Message):
    if not await check_subscription(msg.from_user.id):
        await msg.answer("❌ Необхідна активна підписка!")
        return

    text_content = msg.text or msg.caption or ""
    raw_urls = URL_RE.findall(text_content)
    autoria_urls = [u for u in raw_urls if "auto.ria.com" in u]

    if not autoria_urls:
        await msg.answer("❌ Посилання має бути з сайту auto.ria.com!")
        return

    user_id = msg.from_user.id
    for raw_url in autoria_urls:
        try:
            await _add_single_filter(msg, raw_url, user_id)
        except Exception as e:
            logging.error(f"Помилка додавання фільтра для {user_id}: {e}")
            await msg.answer("❌ Сталася помилка при додаванні цього посилання.")


# ============================================================
#                    АДМІН-КОМАНДИ
# ============================================================

@dp.message(Command("users"))
async def list_users(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT user_id, username, subscription_expires FROM bot_users ORDER BY subscription_expires DESC NULLS LAST"
        )
    if not users:
        await msg.answer("Користувачів поки немає.")
        return

    lines = ["👥 <b>Список користувачів:</b>\n"]
    for u in users:
        uname = html.escape(u["username"] or "без_юзернейму")
        expires = u["subscription_expires"]
        expires_str = expires.strftime("%Y-%m-%d %H:%M") if expires else "—"
        active = "✅" if (expires and expires > datetime.now()) else "❌"
        lines.append(f"{active} ID: <code>{u['user_id']}</code> | @{uname} | до {expires_str}")

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await msg.answer(text[i:i + 4000], parse_mode=ParseMode.HTML)


@dp.message(Command("give"))
async def give_sub(msg: types.Message, command: CommandObject):
    if msg.from_user.id != ADMIN_ID:
        return
    if not command.args:
        await msg.answer("Використання: /give <user_id> [днів] (за замовчуванням 30)")
        return

    args = command.args.split()
    try:
        target_id = int(args[0])
        days = int(args[1]) if len(args) > 1 else 30
    except (ValueError, IndexError):
        await msg.answer("❌ Невірний формат. Використання: /give <user_id> [днів]")
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_users (user_id, subscription_expires) 
            VALUES ($1, NOW() + make_interval(days => $2))
            ON CONFLICT (user_id) DO UPDATE 
            SET subscription_expires = GREATEST(COALESCE(bot_users.subscription_expires, NOW()), NOW()) + make_interval(days => $2)
            """,
            target_id, days
        )

    await msg.answer(f"✅ Користувачу {target_id} додано {days} днів підписки.")
    try:
        await bot.send_message(target_id, f"🎉 Вам додано {days} днів підписки адміністратором!")
    except Exception:
        pass


@dp.message(Command("ban"))
async def ban_user(msg: types.Message, command: CommandObject):
    if msg.from_user.id != ADMIN_ID:
        return
    if not command.args:
        await msg.answer("Використання: /ban <user_id>")
        return

    try:
        target_id = int(command.args.split()[0])
    except (ValueError, IndexError):
        await msg.answer("❌ Невірний формат. Використання: /ban <user_id>")
        return

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM bot_users WHERE user_id = $1", target_id)
        await conn.execute("DELETE FROM user_filters WHERE user_id = $1", target_id)
        await conn.execute("DELETE FROM sent_cars WHERE user_id = $1", target_id)

    await msg.answer(f"🚫 Користувача {target_id} видалено разом з усіма його фільтрами.")


# ============================================================
#                  ФОНОВИЙ МОНІТОРИНГ
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

            for car in new_cars:
                ok = await send_car_notification(user_id, car)
                if ok:
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "INSERT INTO sent_cars (user_id, car_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                            user_id, car["car_id"]
                        )
                await asyncio.sleep(0.5)
        except Exception as e:
            logging.error(f"Помилка обробки фільтра user={user_id}: {type(e).__name__}: {e}")
        finally:
            await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


MONITOR_ERROR_COOLDOWN = int(os.getenv("MONITOR_ERROR_COOLDOWN", "120"))


async def monitor():
    while True:
        try:
            async with db_pool.acquire() as conn:
                filters = await conn.fetch("SELECT user_id, url FROM user_filters")

            sem = asyncio.Semaphore(PARSE_CONCURRENCY)
            await asyncio.gather(*[_process_filter(sem, f) for f in filters], return_exceptions=True)

        except Exception as e:
            logging.error(f"Помилка в циклі моніторингу: {e}")
            await asyncio.sleep(MONITOR_ERROR_COOLDOWN)
            continue

        await asyncio.sleep(MONITOR_INTERVAL)


async def monitor_watchdog():
    while True:
        try:
            await monitor()
        except Exception as e:
            logging.error(f"monitor() несподівано завершився: {type(e).__name__}: {e}. Перезапуск через 10с.")
            await asyncio.sleep(10)


# ============================================================
#             ГЛОБАЛЬНИЙ ОБРОБНИК ПОМИЛОК
# ============================================================

@dp.errors()
async def global_error_handler(event: types.ErrorEvent) -> bool:
    update = event.update
    exc = event.exception
    logging.error(
        f"Необроблений виняток під час обробки update_id={getattr(update, 'update_id', '?')}: "
        f"{type(exc).__name__}: {exc}",
        exc_info=exc,
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ <b>Неочікувана помилка в боті</b>\n"
            f"<code>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    return True


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
        logging.info("🌐 Проксі не задано — запити йдуть напряму.")

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
@dp.message(F.video)
async def get_video_id(msg: types.Message):
    await msg.answer(f"Твій file_id:\n<code>{msg.video.file_id}</code>", parse_mode=ParseMode.HTML)
