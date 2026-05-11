import os
import requests
import feedparser
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TOPIC_ID = os.environ.get("TELEGRAM_TOPIC_ID")  # может быть пустым
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

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
        summary = getattr(entry, "summary", "")
        items.append({"title": title, "link": link, "summary": summary})
    return items


def ai_summarize_news_list(news_list):
    if not news_list:
        return []

    prompt_parts = []
    for i, item in enumerate(news_list, start=1):
        prompt_parts.append(
            f"{i}) Заголовок: {item['title']}\nОписание: {item['summary']}"
        )
    prompt_text = "\n\n".join(prompt_parts)

    system_prompt = (
        "Ты помогаешь трейдеру делать краткий новостной дайджест. "
        "Для каждого пункта сделай краткое резюме в 2–3 слова, без точки, только сами слова. "
        "Ответь строго в формате:\n"
        "1) три слова\n2) два слова\n3) ...\n"
        "Без лишнего текста."
    )

    data = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.2,
    }

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json=data,
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    summaries = []
    for line in lines:
        if ")" in line:
            line = line.split(")", 1)[1].strip()
        summaries.append(line)

    result = []
    for i, item in enumerate(news_list):
        short = summaries[i] if i < len(summaries) else ""
        result.append({**item, "short_summary": short})
    return result


def ai_market_comment(world_block_text, crypto_block_text):
    system_prompt = (
        "Ты — трейдер Иван Аверьянов. Пишешь коротко, без воды, без обещаний иксов, "
        "с акцентом на системность и риск-менеджмент."
    )
    user_prompt = f"""
Новости по миру:
{world_block_text}

Новости по крипте:
{crypto_block_text}

Сделай две строки:
1) "Рынок: ..." — одно короткое предложение до 10 слов, общий фон дня.
2) "Действие: ..." — одно короткое предложение до 10 слов, как системному трейдеру относиться к этому дню (например: "работать по системе, без фомы").

Не используй эмодзи. Не давай уровней и сделок.
"""

    data = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
    }

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json=data,
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if len(lines) < 2:
        return (
            "Рынок: нейтральный фон, без явных перекосов.",
            "Действие: работать по системе, без фомы.",
        )
    return lines[0], lines[1]


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

    world_news_summarized = ai_summarize_news_list(world_news)
    crypto_news_summarized = ai_summarize_news_list(crypto_news)

    world_block_for_ai = "\n".join(
        [f"{i+1}) {n['title']} — {n['short_summary']}" for i, n in enumerate(world_news_summarized)]
    )
    crypto_block_for_ai = "\n".join(
        [f"{i+1}) {n['title']} — {n['short_summary']}" for i, n in enumerate(crypto_news_summarized)]
    )

    ai_line1, ai_line2 = ai_market_comment(world_block_for_ai, crypto_block_for_ai)

    world_block = "\n".join(
        [f"• [{n['title']}]({n['link']}) — {n['short_summary']}" for n in world_news_summarized]
    )
    crypto_block = "\n".join(
        [f"• [{n['title']}]({n['link']}) — {n['short_summary']}" for n in crypto_news_summarized]
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
{ai_line1}
{ai_line2}
"""

    send_telegram_message(text)


if __name__ == "__main__":
    build_and_send_digest()
