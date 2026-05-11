import os
import time
import requests
import feedparser
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TOPIC_ID = os.environ.get("TELEGRAM_TOPIC_ID")  # может быть пустым

# === МИРОВАЯ ЭКОНОМИКА ===
# Один агрегатор главных новостей (Россия + мир).
# Сюда подставь RSS-URL агрегатора "главные новости".
# Примеры мест, где можно взять такой URL:
# - каталог "Главные новости России и мира" на news-rss.ru [web:137]
# - раздел RSS на ru.investing.com с лентой главных новостей рынков [web:123][web:136]
WORLD_RSS_AGGREGATOR = "https://ru.investing.com/rss/news_462.rss"

# === КРИПТА ===
CRYPTO_RSS_LIST = [
    "https://forklog.com/feed/",        # новости и аналитика по крипте [web:124][web:138]
    "https://ru.beincrypto.com/feed/",  # русская крипто-лента BeInCrypto [web:126]
]

WORLD_LIMIT = 5
CRYPTO_LIMIT = 5


def clean_title(title: str) -> str:
    """
    Убираем технический мусор из заголовков.
    Например, если источник ставит дату в квадратных скобках в начале: "[12.05.2026] Текст".
    """
    t = title.strip()
    if t.startswith("[") and "]" in t:
        t = t.split("]", 1)[1].strip()
    return t


def get_rss_items(url: str, limit: int):
    """
    Простой случай: один RSS-агрегатор (для мировых/главных новостей).
    """
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries:
        title = clean_title(entry.title)
        link = entry.link
        published = getattr(entry, "published_parsed", None)
        ts = time.mktime(published) if published else 0
        items.append({
            "title": title,
            "link": link,
            "ts": ts,
        })

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def get_rss_items_from_list(urls, limit: int):
    """
    Несколько RSS-лент (для крипты): склеиваем, сортируем по времени, берём топ-N.
    """
    items = []
    for url in urls:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = clean_title(entry.title)
            link = entry.link
            published = getattr(entry, "published_parsed", None)
            ts = time.mktime(published) if published else 0
            items.append({
                "title": title,
                "link": link,
                "ts": ts,
            })

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if TOPIC_ID:
        payload["message_thread_id"] = int(TOPIC_ID)

    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def build_and_send_digest():
    now = datetime.utcnow()
    date_str = now.strftime("%d.%m.%y")

    # Мировые/главные новости из агрегатора
    world_news = get_rss_items(WORLD_RSS_AGGREGATOR, WORLD_LIMIT)
    # Крипта из нескольких профильных русскоязычных медиа
    crypto_news = get_rss_items_from_list(CRYPTO_RSS_LIST, CRYPTO_LIMIT)

    world_block = "\n".join(
        [f"• [{n['title']}]({n['link']})" for n in world_news]
    )
    crypto_block = "\n".join(
        [f"• [{n['title']}]({n['link']})" for n in crypto_news]
    )

    text = f"""🗞 Дайджест на утро {date_str}
Коротко: главное по миру и крипте, чтобы открыть терминал не вслепую.

🌍 Мировая экономика
{world_block}

💰 Криптовалюта
{crypto_block}

📊 Аналитика Unbias
• BTC: данные пока не подключены.

😶‍🌫️ Страх/жадность
• Индекс: данные пока не подключены.

🧺 ETF за сутки
• BTC‑ETF: данные пока не подключены.

🤖 Что думает ИИ
Рынок: (ИИ временно отключён, дайджест без комментария).
Действие: работать по системе, без фомы.

📅 Событие на сегодня
• данные по ключевым макро/политическим событиям пока не подключены.
"""

    send_telegram_message(text)


if __name__ == "__main__":
    build_and_send_digest()
