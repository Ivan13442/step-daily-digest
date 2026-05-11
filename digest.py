import os
import time
import requests
import feedparser
from datetime import datetime
from typing import List, Dict, Any

from digest_grouper import group_items  # наш модуль LLM-группировки

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TOPIC_ID = os.environ.get("TELEGRAM_TOPIC_ID")  # может быть пустым

# === МИРОВАЯ ЭКОНОМИКА ===
WORLD_RSS_AGGREGATOR = "https://news-rss.ru/top.rss"

# === КРИПТА ===
CRYPTO_RSS_LIST = [
    "https://forklog.com/feed/",
    "https://ru.beincrypto.com/feed/",
]

WORLD_LIMIT = 8
CRYPTO_LIMIT = 8


def clean_title(title: str) -> str:
    t = title.strip()
    if t.startswith("[") and "]" in t:
        t = t.split("]", 1)[1].strip()
    return t


def _parse_feed(url: str, limit: int) -> List[Dict[str, Any]]:
    feed = feedparser.parse(url)
    items: List[Dict[str, Any]] = []
    for entry in feed.entries:
        title = clean_title(entry.title)
        link = entry.link
        published = getattr(entry, "published_parsed", None)
        ts = time.mktime(published) if published else 0
        items.append(
            {
                "title": title,
                "link": link,
                "ts": ts,
                "source": url,
            }
        )
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def get_world_items() -> List[Dict[str, Any]]:
    return _parse_feed(WORLD_RSS_AGGREGATOR, WORLD_LIMIT)


def get_crypto_items() -> List[Dict[str, Any]]:
    all_items: List[Dict[str, Any]] = []
    for url in CRYPTO_RSS_LIST:
        all_items.extend(_parse_feed(url, CRYPTO_LIMIT))
    all_items.sort(key=lambda x: x["ts"], reverse=True)
    return all_items[:CRYPTO_LIMIT]


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: Dict[str, Any] = {
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
    date_str = now.strftime("%d.%m.%Y")

    world_news = get_world_items()
    crypto_news = get_crypto_items()

    raw_items: List[Dict[str, Any]] = []

    for item in world_news:
        raw_items.append(
            {
                "text": item["title"],
                "link": item["link"],
                "channel": "World",
            }
        )

    for item in crypto_news:
        raw_items.append(
            {
                "text": item["title"],
                "link": item["link"],
                "channel": "Crypto",
            }
        )

    groups = [
        {
            "name": "📊 Экономика",
            "description": "Макроэкономика, мировые новости, рынки, геополитика.",
        },
        {
            "name": "💰 Криптовалюта",
            "description": "Криптовалюты, DeFi, биржи, блокчейн, регуляция.",
        },
    ]

    grouped = group_items(raw_items, groups)

    econ_block_lines: List[str] = []
    crypto_block_lines: List[str] = []

    for item in grouped.get("📊 Экономика", []):
        econ_block_lines.append(f"🔹 {item['text']} [[ >>> ]]({item['link']})")

    for item in grouped.get("💰 Криптовалюта", []):
        crypto_block_lines.append(f"🔹 {item['text']} [[ >>> ]]({item['link']})")

    econ_block = "\n".join(econ_block_lines) if econ_block_lines else "• Нет подходящих новостей."
    crypto_block = "\n".join(crypto_block_lines) if crypto_block_lines else "• Нет подходящих новостей."

    text = f"""🗞 Новостной дайджест на утро {date_str}

📊 Экономика:

{econ_block}

💰 Криптовалюта:

{crypto_block}

📅 Важное по макро на сегодня:
• данные по ключевым макро/политическим событиям пока не подключены.
"""

    send_telegram_message(text)


if __name__ == "__main__":
    build_and_send_digest()
