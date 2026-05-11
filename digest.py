import os
import requests
import feedparser
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TOPIC_ID = os.environ.get("TELEGRAM_TOPIC_ID")  # может быть пустым

WORLD_RSS = "https://news.google.com/rss/search?q=macro+economy&hl=en-US&gl=US&ceid=US:en"
CRYPTO_RSS = "https://news.google.com/rss/search?q=cryptocurrency&hl=en-US&gl=US&ceid=US:en"
WORLD_LIMIT = 3
CRYPTO_LIMIT = 3


def get_rss_items(url: str, limit: int):
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:limit]:
        title = entry.title
        link = entry.link
        items.append({"title": title, "link": link})
    return items


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    if TOPIC_ID:
        payload["message_thread_id"] = int(TOPIC_ID)

    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def build_and_send_digest():
    now = datetime.utcnow()
    date_str = now.strftime("%d.%m.%y")

    world_news = get_rss_items(WORLD_RSS, WORLD_LIMIT)
    crypto_news = get_rss_items(CRYPTO_RSS, CRYPTO_LIMIT)

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
• BTC: (пока заглушка, добавим позже)

😶‍🌫️ Страх/жадность
• Индекс: (пока заглушка)

🧺 ETF за сутки
• BTC‑ETF: (пока заглушка)

🤖 Что думает ИИ
Рынок: (ИИ временно отключён, дайджест без комментария).
Действие: работать по системе, без фомы.
"""

    send_telegram_message(text)


if __name__ == "__main__":
    build_and_send_digest()
