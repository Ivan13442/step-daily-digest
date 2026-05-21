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

# === ИСТОЧНИКИ: МИРОВАЯ ЭКОНОМИКА ===
# Ведомости — рубрика "Мировая экономика". [web:380]
WORLD_RSS_SOURCES = [
    "https://www.vedomosti.ru/rss/rubric/economics/global",
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
    """
    ETF-потоки по BTC и ETH через CoinGlass v4 ETF Flows History.
    Если запрос не удался — честная надпись про недоступность данных.
    """
    if not COINGLASS_API_KEY:
        logging.warning("COINGLASS_API_KEY не задан, ETF-потоки недоступны.")
        return [
            "BTC ETF: данные временно недоступны (нет API-ключа)",
            "ETH ETF: данные временно недоступны (нет API-ключа)",
        ]

    btc_data = _coinglass_get(
        "/api/etf/bitcoin/flows-history",
        params={"interval": "1d", "limit": 1},
    )
    eth_data = _coinglass_get(
        "/api/etf/ethereum/flows-history",
        params={"interval": "1d", "limit": 1},
    )

    lines: List[str] = []

    def _parse_flow(data: Optional[Dict], asset_label: str) -> str:
        if not data:
            return f"{asset_label} ETF: данные временно недоступны (ошибка API)"
        items = data.get("data") or data.get("list") or data
        if isinstance(items, dict):
            items = items.get("history") or items.get("items") or []
        if not isinstance(items, list) or not items:
            return f"{asset_label} ETF: данные временно недоступны (нет данных)"
        latest = items[-1]
        flow = (
            latest.get("netInflowUsd")
            or latest.get("net_inflow_usd")
            or latest.get("netInflow")
            or latest.get("net_inflow")
        )
        if flow is None:
            return f"{asset_label} ETF: данные временно недоступны (нет поля netInflow)"
        try:
            flow = float(flow)
        except Exception:
            return f"{asset_label} ETF: данные временно недоступны (некорректный формат netInflow)"
        mln = flow / 1_000_000.0
        if mln > 0:
            return f"{asset_label} ETF: наблюдается чистый приток (+{mln:.2f}M$)"
        elif mln < 0:
            return f"{asset_label} ETF: наблюдается чистый отток ({mln:.2f}M$)"
        else:
            return f"{asset_label} ETF: нейтрально (0.00M$)"

    lines.append(_parse_flow(btc_data, "BTC"))
    lines.append(_parse_flow(eth_data, "ETH"))

    return lines


def fetch_events_today() -> str:
    """
    Простой календарь: берем несколько событий из RSS.
    """
    try:
        parsed = feedparser.parse("https://ru.investing.com/rss/news_28.rss")
        lines = []
        for entry in parsed.entries[:5]:
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
        "Фокус: мировая экономика и глобальные рынки (США, Европа, Азия, мировые индексы, сырьевые рынки, крупные корпорации). "
        "Источники новостей по экономике взяты из раздела 'Мировая экономика' делового СМИ, "
        "твоя задача — выбрать из них ключевые глобальные сюжеты. "
        "Криптовалютные темы должны появляться только в блоке '₿ Криптовалюты', "
        "и никогда не должны попадать в блок '🌍 Мировая экономика'. "
        "Всегда отвечай на русском языке. Пиши кратко, структурированно и профессионально."
    )

    user_prompt = f"""
ДАТА: {date_str}

СЫРЫЕ ДАННЫЕ ДЛЯ ДАЙДЖЕСТА
===========================

1) Мировая экономика (сырые заголовки, максимум 10):

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

Ты должен СФОРМИРОВАТЬ ГОТОВЫЙ ТЕКСТ СТРОГО по такому шаблону (весь текст — на русском языке):

📣 Дайджест на утро {date_str}

🌍 Мировая экономика
(РОВНО 5 пунктов; это ДОЛЖНЫ быть новости ИМЕННО МИРОВОЙ ЭКОНОМИКИ и глобальных рынков:
США, Европа, Азия, мировые индексы, сырьевые рынки, крупные корпорации.
ЗАПРЕЩЕНО включать сюда любые криптовалютные новости.
Исключи заголовки, где есть слова: биткоин, bitcoin, BTC, эфириум, ethereum, ETH, крипто, crypto, токен, стейблкоин, Binance, Coinbase и т.п.)
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
(подставь фактическое значение и русское описание по данным индекса выше)

🧺 ETF потоки
• BTC ETF: ... (используй фактический BTC ETF поток, если он есть)
• ETH ETF: ... (используй фактический ETH ETF поток, если он есть)
Если вместо чисел в исходных данных текст про недоступность данных, аккуратно переформулируй это.

🔓 Важные разблокировки:
(оставь пустым, только этот заголовок — я заполняю сам)

🧱 Важные уровни ликвидаций:
(оставь пустым, только этот заголовок — я заполняю сам)

🤖 Что думает ИИ
• Рынок: краткий комментарий по рынку (1 строка, 1–2 предложения)
• Фокус дня: основная идея/нарратив дня (1 строка)
• Действие: рекомендуемое действие для трейдера (1 строка)
(опирайся на макро, крипто, индекс страха/жадности и ETF-потоки)

🧠 Мои выводы:
(оставь пустым, только этот заголовок — я заполняю сам)

🟠 BTC:
(оставь пустым, только этот заголовок — я заполняю сам)

🟣 ETH:
(оставь пустым, только этот заголовок — я заполняю сам)

📅 События на сегодня
• [Сегодня] ...
(используй данные из блока событий, минимум 1 строка)

ОГРАНИЧЕНИЯ И ФОРМАТИРОВАНИЕ
============================

- СТРОГО сохрани порядок и заголовки блоков как в шаблоне.
- В блоке Мировая экономика сделай ровно 5 пунктов БЕЗ криптовалютных тем.
- Все криптовалютные новости размещай только в блоке Криптовалюты.
- Весь текст дайджеста должен быть на русском языке.
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

    logger.info("Загружаем новости по мировой экономике (Ведомости)...")
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
