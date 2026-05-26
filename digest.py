import os
import time
import asyncio
import logging
import html
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

import requests
import feedparser
import schedule

# ========= НАСТРОЙКИ =========

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

COINGLASS_API_KEY = os.environ.get("COINGLASS_API_KEY", "")
ALTERNATIVE_FNG_URL = "https://api.alternative.me/fng/?limit=1"

SAMARA_TZ = timezone(timedelta(hours=4))
DIGEST_TIME_LOCAL = "10:00"  # Самара

# === ИСТОЧНИКИ МИРОВОЙ ЭКОНОМИКИ (РУССКИЕ ФИДЫ) ===
WORLD_RSS_SOURCES = [
    "https://www.vedomosti.ru/rss/rubric/economics/global",
    "https://1prime.ru/export/rss2/index.xml",
]

# Крипта — русские источники
CRYPTO_RSS_SOURCES = [
    "https://forklog.com/feed/",
    "https://ru.beincrypto.com/feed/",
]

WORLD_LIMIT = 10
CRYPTO_LIMIT = 10

# Фильтры свежести
WORLD_FRESH_HOURS = 24
WORLD_MAX_AGE_HOURS = 72
CRYPTO_MAX_AGE_HOURS = 72

# ========= ВРЕМЕННЫЙ ПРОТОТИП ДЛЯ РАЗБЛОКИРОВОК =========

HARDCODED_UNLOCKS: List[Dict] = [
    {
        "ticker": "WLD",
        "name": "Worldcoin",
        "unlock_time_utc": "2026-05-25T12:00:00Z",
        "unlock_value_usd": 50_000_000,
        "unlock_pct_circ": 6.5,
        "cmc_url": "https://coinmarketcap.com/currencies/worldcoin-wld/",
    },
    {
        "ticker": "SOL",
        "name": "Solana",
        "unlock_time_utc": "2026-05-26T18:00:00Z",
        "unlock_value_usd": 20_000_000,
        "unlock_pct_circ": 4.0,
        "cmc_url": "https://coinmarketcap.com/currencies/solana/",
    },
    {
        "ticker": "ARB",
        "name": "Arbitrum",
        "unlock_time_utc": "2026-05-27T09:00:00Z",
        "unlock_value_usd": 15_000_000,
        "unlock_pct_circ": 3.2,
        "cmc_url": "https://coinmarketcap.com/currencies/arbitrum/",
    },
]


def format_unlocks_for_prompt(items: List[Dict]) -> str:
    lines = []
    for u in items:
        ticker = html.escape(u.get("ticker", "TOKEN"))
        url = html.escape(
            u.get("cmc_url", "https://coinmarketcap.com/ru/token-unlocks/"),
            quote=True,
        )

        raw_dt = u.get("unlock_time_utc")
        time_str = ""
        if raw_dt:
            try:
                dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                time_str = dt.strftime("%d.%m %H:%M UTC")
            except Exception:
                time_str = raw_dt

        pct = u.get("unlock_pct_circ")
        usd = u.get("unlock_value_usd")
        extra = ""
        if isinstance(pct, (int, float)):
            extra = f", ≈{pct:.1f}% от циркуляции"
        elif isinstance(usd, (int, float)):
            extra = f", ≈{usd / 1_000_000:.1f}M$"

        text = f"{ticker} — {time_str}{extra}"
        lines.append(f'• <a href="{url}">{text}</a>')

    if not lines:
        return "• Разблокировок, которые выделяются по объёму, в ближайшие дни нет."
    return "\n".join(lines)


# ========= УТИЛИТЫ =========

def clean_title(title: str) -> str:
    t = title.strip()
    if t.startswith("[") and "]" in t:
        t = t.split("]", 1)[1].strip()
    t = re.sub(r'^[•★✓▶►■◆◇✨🔥🚀📌📈📉🟢🔴⚡️]\s*', '', t, count=1)
    return t.strip()


def fetch_rss_raw(urls: List[str]) -> List[Dict]:
    items: List[Dict] = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                logging.warning("RSS пустой или недоступен: %s", url)
                continue
            for entry in feed.entries:
                title = clean_title(entry.get("title", "Без заголовка"))
                link = entry.get("link", "")
                published = getattr(entry, "published_parsed", None)
                ts = time.mktime(published) if published else 0
                items.append({"title": title, "link": link, "ts": ts})
            logging.info("RSS загружен (%d записей): %s", len(feed.entries), url)
        except Exception as e:
            logging.warning("Ошибка RSS %s: %s", url, e)
    return items


def filter_and_limit_by_age(
    items: List[Dict],
    limit: int,
    max_age_hours: Optional[int] = None,
) -> List[Dict]:
    now_ts = time.time()
    filtered: List[Dict] = []
    for it in items:
        ts = it.get("ts", 0) or 0
        if max_age_hours is not None and ts > 0:
            age_hours = (now_ts - ts) / 3600.0
            if age_hours > max_age_hours:
                continue
        filtered.append(it)
    filtered.sort(key=lambda x: x["ts"], reverse=True)
    return filtered[:limit]


def remove_crypto_from_world(items: List[Dict]) -> List[Dict]:
    crypto_words = [
        "биткоин", "bitcoin", "btc",
        "эфириум", "ethereum", "eth",
        "крипто", "crypto", "токен",
        "token", "стейблкоин", "stablecoin",
        "binance", "coinbase",
    ]
    res = []
    for it in items:
        title_low = it["title"].lower()
        if any(w in title_low for w in crypto_words):
            continue
        res.append(it)
    return res


def fetch_world_news_with_fallback() -> List[Dict]:
    raw_items = fetch_rss_raw(WORLD_RSS_SOURCES)
    raw_items = remove_crypto_from_world(raw_items)

    fresh = filter_and_limit_by_age(
        raw_items,
        limit=WORLD_LIMIT,
        max_age_hours=WORLD_FRESH_HOURS,
    )
    if len(fresh) >= WORLD_LIMIT:
        return fresh

    extended = filter_and_limit_by_age(
        raw_items,
        limit=WORLD_LIMIT,
        max_age_hours=WORLD_MAX_AGE_HOURS,
    )
    return extended


def fetch_crypto_news() -> List[Dict]:
    raw_items = fetch_rss_raw(CRYPTO_RSS_SOURCES)
    filtered = filter_and_limit_by_age(
        raw_items,
        limit=CRYPTO_LIMIT,
        max_age_hours=CRYPTO_MAX_AGE_HOURS,
    )
    return filtered


def fetch_fear_greed() -> str:
    try:
        resp = requests.get(ALTERNATIVE_FNG_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        entry = data["data"][0]
        value = entry.get("value")
        label = entry.get("value_classification") or ""
        if value is None:
            return "индекс временно недоступен"
        label_ru = {
            "Extreme Fear": "Крайний страх",
            "Fear": "Страх",
            "Neutral": "Нейтрально",
            "Greed": "Жадность",
            "Extreme Greed": "Крайняя жадность",
        }.get(label, label or "Без классификации")
        return f"{value} — {label_ru}"
    except Exception as e:
        logging.warning("Fear/Greed error: %s", e)
        return "индекс временно недоступен"


def _coinglass_get(path: str, params: Optional[Dict] = None) -> Optional[Dict]:
    if not COINGLASS_API_KEY:
        return None
    base_url = "https://open-api-v4.coinglass.com"
    headers = {
        "CG-API-KEY": COINGLASS_API_KEY,
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            base_url + path,
            headers=headers,
            params=params or {},
            timeout=15,
        )
        if resp.status_code != 200:
            logging.warning(
                "CoinGlass HTTP %s %s: %s",
                resp.status_code,
                path,
                resp.text[:200],
            )
            return None
        return resp.json()
    except Exception as e:
        logging.warning("CoinGlass request error %s: %s", path, e)
        return None


def fetch_etf_flows() -> List[str]:
    return []


def fetch_events_today() -> str:
    return ""


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code != 200:
        logging.error("Telegram error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


# ========= ВЫЗОВ GROQ =========

def groq_chat_completion(messages: List[Dict], model: str = GROQ_MODEL) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ========= ОДИН БОЛЬШОЙ ПРОМТ (ШАБЛОН) =========

def ai_build_full_digest(
    world_news: List[Dict],
    crypto_news: List[Dict],
    fear_greed: str,
    etf_lines: List[str],
    events_block: str,
    unlocks_block: str,
    news_sources_block: str,
) -> str:
    now = datetime.now(SAMARA_TZ)
    date_str = now.strftime("%d.%m.%y")

    def _format_news_block(items: List[Dict]) -> str:
        lines = []
        for it in items:
            title = it["title"].strip()
            link = it["link"].strip()
            safe_title = html.escape(title.replace("\n", " ").strip())
            safe_link = html.escape(link, quote=True)
            lines.append(f'• <a href="{safe_link}">{safe_title}</a>')
        return "\n".join(lines)

    world_block_raw = _format_news_block(world_news)
    crypto_block_raw = _format_news_block(crypto_news)
    etf_header = '🧺 <a href="https://coinmarketcap.com/ru/etf/">ETF потоки</a>'

    system_prompt = (
        "Ты профессиональный финансовый редактор. "
        "Фокус: мировая экономика и глобальные рынки (США, Европа, Азия, мировые индексы, сырьевые рынки, крупные корпорации). "
        "Экономические новости приходят из нескольких надёжных СМИ (Ведомости, ПРАЙМ и др.). "
        "Твоя задача — выбрать из них ключевые глобальные сюжеты. "
        "Криптовалютные темы должны появляться только в блоке '₿ Криптовалюты', "
        "и никогда не должны попадать в блок '🌍 Мировая экономика'. "
        "Всегда отвечай на русском языке. Пиши кратко, структурированно и профессионально."
    )

    user_prompt = f"""
ДАТА: {date_str}

СЫРЫЕ ДАННЫЕ ДЛЯ ДАЙДЖЕСТА
===========================

1) Мировая экономика (сырые заголовки из нескольких источников, максимум {WORLD_LIMIT};
   сначала собраны новости за последние {WORLD_FRESH_HOURS} часов, при нехватке — дополняются до {WORLD_MAX_AGE_HOURS} часов):

{world_block_raw}

2) Криптовалютные новости (сырые заголовки, максимум {CRYPTO_LIMIT}; отфильтрованы по дате — до {CRYPTO_MAX_AGE_HOURS} часов):

{crypto_block_raw}

3) Индекс страха и жадности:

{fear_greed}

4) Важные разблокировки (сырые строки):

{unlocks_block}

5) События на сегодня (сырые строки):

{events_block}


ШАБЛОН ДАЙДЖЕСТА
================

Ты должен СФОРМИРОВАТЬ ГОТОВЫЙ ТЕКСТ СТРОГО по такому шаблону (весь текст — на русском языке):

📣 Дайджест на утро {date_str}

🌍 Мировая экономика
(ДО 5 пунктов; это ДОЛЖНЫ быть новости ИМЕННО МИРОВОЙ ЭКОНОМИКИ и глобальных рынков:
США, Европа, Азия, мировые индексы, сырьевые рынки, крупные корпорации.
ЗАПРЕЩЕНО включать сюда любые криптовалютные новости.
Исключи заголовки, где есть слова: биткоин, bitcoin, BTC, эфириум, ethereum, ETH, крипто, crypto, токен, стейблкоин, Binance, Coinbase и т.п.
Если новостей меньше пяти, используй столько пунктов, сколько есть, но не вставляй фразы типа 'не удалось найти новости'.)
• <a href="ссылка1">Заголовок 1</a>
• <a href="ссылка2">Заголовок 2</a>
• <a href="ссылка3">Заголовок 3</a>
• <a href="ссылка4">Заголовок 4</a>
• <a href="ссылка5">Заголовок 5</a>

₿ Криптовалюты
(РОВНО 5 пунктов; выбери 5 самых важных новостей из блока крипто выше.
ВСЕ криптовалютные новости должны идти сюда, а не в блок 'Мировая экономика'.)
• <a href="ссылка1">Заголовок 1</a>
• <a href="ссылка2">Заголовок 2</a>
• <a href="ссылка3">Заголовок 3</a>
• <a href="ссылка4">Заголовок 4</a>
• <a href="ссылка5">Заголовок 5</a>

📊 <a href="https://unbias.fyi/">Аналитика Unbias</a>

😶‍🌫️ Страх/жадность
• Индекс: X — Описание

{etf_header}

🔓 Важные разблокировки:
{unlocks_block}

🧱 Важные уровни ликвидаций:
(оставь пустым, только этот заголовок — я заполняю сам)

🤖 Что думает ИИ
• Рынок: краткий комментарий по рынку (1 строка, 1–2 предложения)
• Фокус дня: основная идея/нарратив дня (1 строка)
• Действие: рекомендуемое действие для трейдера (1 строка)
(опирайся на макро, крипто, индекс страха/жадности и ETF-потоки)

🧠 Мои выводы:
(оставь пустым, только этот заголовок — я заполняю сам)

📅 <a href="https://tradingeconomics.com/calendar">События на сегодня</a>
(ничего не добавляй под этим заголовком — никаких пунктов, ни одной строки)

ОГРАНИЧЕНИЯ И ФОРМАТИРОВАНИЕ
============================

- СТРОГО сохрани порядок и заголовки блоков как в шаблоне.
- В блоке Мировая экономика сделай до 5 пунктов БЕЗ криптовалютных тем (если новостей меньше — делай меньше пунктов).
- Все криптовалютные новости размещай только в блоке Криптовалюты.
- Весь текст дайджеста должен быть на русском языке.
- Используй HTML-ссылки вида <a href="URL">Текст</a>.
- Не добавляй лишних блоков.
- Не заполняй те блоки, где явно указано «оставить пустым».
- В блоке «Топ новостники» используй только те два источника, которые я дал (Telegram‑канал и сайт), без выдуманных.
- Выведи ТОЛЬКО финальный текст дайджеста, без пояснений.
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    raw = groq_chat_completion(messages)
    return raw.strip()


# ========= ГЛАВНАЯ ЛОГИКА =========

async def build_and_send_digest():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("digest")

    logger.info("Загружаем новости по мировой экономике (несколько источников)...")
    world_news = fetch_world_news_with_fallback()
    logger.info("Мировые новости (после фильтра и fallback): %d статей", len(world_news))

    logger.info("Загружаем крипто новости из RSS...")
    crypto_news = fetch_crypto_news()
    logger.info("Крипто новости (фильтр по дате): %d статей", len(crypto_news))

    logger.info("Получаем Fear/Greed...")
    fg = fetch_fear_greed()

    logger.info("Получаем ETF потоки...")
    etf = fetch_etf_flows()

    logger.info("Формируем блок разблокировок...")
    now_ts = datetime.now(timezone.utc).timestamp()
    filtered_unlocks = [
        u
        for u in HARDCODED_UNLOCKS
        if u.get("unlock_time_utc")
        and datetime.fromisoformat(
            u["unlock_time_utc"].replace("Z", "+00:00")
        ).timestamp()
        > now_ts
    ]
    unlocks_block = format_unlocks_for_prompt(filtered_unlocks)

    logger.info("Формируем блок топ новостников...")
    # Твой топ‑источников; в будущем можно динамически переключать
    news_sources_block = '\n'.join([
        '• <a href="https://t.me/crypto_hd">@crypto_hd</a>',
        '• <a href="https://ru.beincrypto.com/">ru.beincrypto.com</a>',
    ])

    logger.info("Получаем экономические события...")
    events = ""

    logger.info("Формируем дайджест через Groq...")
    digest_text = ai_build_full_digest(
        world_news=world_news,
        crypto_news=crypto_news,
        fear_greed=fg,
        etf_lines=etf,
        events_block=events,
        unlocks_block=unlocks_block,
        news_sources_block=news_sources_block,
    )

    logger.info("Отправляем дайджест в Telegram...")
    send_telegram_message(digest_text)
    logger.info("Дайджест успешно отправлен!")


def run_digest_job():
    logging.info("Запуск дайджеста по расписанию...")
    try:
        asyncio.run(build_and_send_digest())
    except Exception as e:
        logging.error("Ошибка при формировании дайджеста: %s", e, exc_info=True)


def start_scheduler():
    samara_hour, samara_minute = map(int, DIGEST_TIME_LOCAL.split(":"))
    utc_hour = (samara_hour - 4) % 24
    utc_time = f"{utc_hour:02d}:{samara_minute:02d}"
    logging.info(
        "Планировщик настроен на %s по Самаре (это %s по UTC)",
        DIGEST_TIME_LOCAL,
        utc_time,
    )

    schedule.every().day.at(utc_time).do(run_digest_job)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Digest Bot (one big prompt)")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Запустить дайджест немедленно для теста",
    )
    args = parser.parse_args()

    if args.now:
        asyncio.run(build_and_send_digest())
    else:
        print(f"Бот запущен. Отправка настроена на {DIGEST_TIME_LOCAL} по Самаре.")
        start_scheduler()
