import os
import time
import asyncio
import logging
import html
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict

import requests
import feedparser
import schedule

# ========= НАСТРОЙКИ =========

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
# Актуальная модель Groq по умолчанию, но можно переопределить через env GROQ_MODEL
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

COINGLASS_API_KEY = os.environ.get("COINGLASS_API_KEY", "")
ALTERNATIVE_FNG_URL = "https://api.alternative.me/fng/?limit=1"

SAMARA_TZ = timezone(timedelta(hours=4))
DIGEST_TIME_LOCAL = "10:00"  # Самара

WORLD_RSS_SOURCES = [
    "https://rssexport.rbc.ru/rbcnews/economics/30/full.rss",
    "https://www.vedomosti.ru/rss/rubric/economics",
]

CRYPTO_RSS_SOURCES = [
    "https://forklog.com/feed/",
    "https://ru.beincrypto.com/feed/",
]

WORLD_LIMIT = 10
CRYPTO_LIMIT = 10

# ========= УТИЛИТЫ =========

def clean_title(title: str) -> str:
    t = title.strip()
    if t.startswith("[") and "]" in t:
        t = t.split("]", 1)[1].strip()
    # Один backslash, как в обычном regex
    t = re.sub(r'^[•★✓▶►■◆◇✨🔥🚀📌📈📉🟢🔴⚡️]\s*', '', t, count=1)
    return t.strip()


def fetch_rss_list(urls: List[str], limit: int) -> List[Dict]:
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
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


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


def fetch_etf_flows() -> List[str]:
    """
    Пока что делаем честный fallback: если CoinGlass не отдает данные
    или эндпоинты отличаются от документации, просто говорим, что данные недоступны.
    Никаких выдуманных чисел.
    """
    key = COINGLASS_API_KEY
    if not key:
        logging.warning("COINGLASS_API_KEY не задан, ETF-потоки недоступны.")
        return [
            "BTC ETF: данные временно недоступны (нет API-ключа)",
            "ETH ETF: данные временно недоступны (нет API-ключа)",
        ]

    # Здесь будет аккуратная интеграция с v4 ETF endpoints CoinGlass.
    # Сейчас намеренно возвращаем заглушки, чтобы не ловить 404 и не ломать дайджест.
    logging.warning("CoinGlass ETF endpoints пока не настроены, возвращаем заглушки.")
    return [
        "BTC ETF: данные временно недоступны (ошибка API или невалидный endpoint)",
        "ETH ETF: данные временно недоступны (ошибка API или невалидный endpoint)",
    ]


def fetch_events_today() -> str:
    """
    Простой fallback: если нет своих ключей календаря,
    берём RSS Investing и делаем 3–5 событий.
    """
    try:
        parsed = feedparser.parse("https://ru.investing.com/rss/news_28.rss")
        lines = []
        for entry in parsed.entries[:4]:
            title = html.escape(re.sub(r'<[^>]+>', '', entry.title))
            lines.append(f"• [Сегодня] {title}")
        if lines:
            return "\n".join(lines)
    except Exception as e:
        logging.warning("Events RSS error: %s", e)
    return "• [Сегодня] Важных макроэкономических публикаций не запланировано."


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
) -> str:
    """
    Здесь вся логика структуры. Код только собирает данные и вызывает эту функцию.
    """

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
    etf_raw = "\n".join(f"• {line}" for line in etf_lines)

    system_prompt = (
        "Ты профессиональный финансовый редактор. "
        "Твоя задача — взять сырые данные и собрать готовый утренний дайджест строго по заданному шаблону. "
        "Язык: русский. Пиши аккуратно и профессионально."
    )

    user_prompt = f"""
ДАТА: {date_str}

СЫРЫЕ ДАННЫЕ ДЛЯ ДАЙДЖЕСТА
===========================

1) Мировые макроэкономические новости (сырые заголовки, максимум 10):

{world_block_raw}

2) Криптовалютные новости (сырые заголовки, максимум 10):

{crypto_block_raw}

3) Индекс страха и жадности:

{fear_greed}

4) ETF-потоки:

{etf_raw}

5) События на сегодня (сырые строки):

{events_block}


ШАБЛОН ДАЙДЖЕСТА
================

Ты должен СФОРМИРОВАТЬ ГОТОВЫЙ ТЕКСТ СТРОГО по такому шаблону:

📣 Дайджест на утро {date_str}

🌍 Мировая экономика
• <a href="ссылка1">Заголовок 1</a>
• <a href="ссылка2">Заголовок 2</a>
• ...
(от 3 до 5 пунктов, только самые важные, на основе макро-новостей выше)

₿ Криптовалюты
• <a href="ссылка1">Заголовок 1</a>
• <a href="ссылка2">Заголовок 2</a>
• ...
(от 3 до 5 пунктов, на основе крипто-новостей выше)

📊 <a href="https://unbias.fyi/">Аналитика Unbias</a>

😶‍🌫️ Страх/жадность
• Индекс: X — Описание
(подставь фактическое значение и русское описание по данным индекса выше)

🧺 ETF потоки
• BTC ETF: ... (используй фактический BTC ETF поток)
• ETH ETF: ... (используй фактический ETH ETF поток)
ТЕКСТ ДОЛЖЕН ИСПОЛЬЗОВАТЬ РЕАЛЬНЫЕ ЧИСЛА ИЗ блока ETF-потоки, если они есть.
Если вместо чисел приходит текст про недоступность данных, аккуратно отрази это.

Важные разблокировки
(оставь пустым, просто эту строку без пунктов под ней)

Важные уровни ликвидации
(оставь пустым, просто эту строку без пунктов под ней)

🤖 Что думает ИИ
• Рынок: краткий комментарий по рынку (1 строка, 1–2 предложения)
• Фокус дня: основная идея/нарратив дня (1 строка)
• Действие: рекомендуемое действие для трейдера (1 строка)
(опирайся на макро, крипто, индекс страха/жадности и ETF-потоки)

Мои выводы:
(оставь пустым, только заголовок)

BTC:
(оставь пустым, только заголовок)

ETH:
(оставь пустым, только заголовок)

Что там по кошелькам- аналитика кошельков
(оставь эту строку, без дополнительных пунктов)

📅 События на сегодня
• [Сегодня] ... 
(используй данные из блока событий, минимум 1 строка)

ОГРАНИЧЕНИЯ И ФОРМАТИРОВАНИЕ
============================

- СТРОГО сохрани порядок и заголовки блоков как в шаблоне.
- Используй HTML-ссылки вида <a href="URL">Текст</a>.
- Не добавляй лишних блоков.
- Не заполняй те блоки, где явно указано «оставить пустым».
- Не используй жирный Markdown (**), только обычный текст и ссылки.
- Не используй кодовые блоки.
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

    logger.info("Загружаем экономические новости из макро-RSS...")
    world_news = fetch_rss_list(WORLD_RSS_SOURCES, WORLD_LIMIT)
    logger.info("Мировые новости: %d статей", len(world_news))

    logger.info("Загружаем крипто новости из RSS...")
    crypto_news = fetch_rss_list(CRYPTO_RSS_SOURCES, CRYPTO_LIMIT)
    logger.info("Крипто новости: %d статей", len(crypto_news))

    logger.info("Получаем Fear/Greed...")
    fg = fetch_fear_greed()

    logger.info("Получаем ETF потоки...")
    etf = fetch_etf_flows()

    logger.info("Получаем экономические события...")
    events = fetch_events_today()

    logger.info("Формируем дайджест через Groq...")
    digest_text = ai_build_full_digest(
        world_news=world_news,
        crypto_news=crypto_news,
        fear_greed=fg,
        etf_lines=etf,
        events_block=events,
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
