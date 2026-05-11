import os
import time
import asyncio
import requests
import feedparser
import logging
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

# ========= ТВОИ НАСТРОЙКИ TELEGRAM =========

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TOPIC_ID = os.environ.get("TELEGRAM_TOPIC_ID")  # может быть пустым

# ========= ИСТОЧНИКИ НОВОСТЕЙ (RSS) =========

WORLD_RSS_AGGREGATOR = "https://news-rss.ru/top.rss"  # главные новости России и мира [web:1]
CRYPTO_RSS_LIST = [
    "https://forklog.com/feed/",
    "https://ru.beincrypto.com/feed/",
]

WORLD_LIMIT = 10  # берём побольше, а потом суммаризируем
CRYPTO_LIMIT = 10

# ========= ИМПОРТЫ ИЗ ТВОЕГО ПРОЕКТА (НУЖНО ПОДОГНАТЬ ПУТИ) =========
from src.ai_providers import AIProvider, create_provider  # подстрой путь под свой проект
from src.config_loader import Config, DigestGroupConfig   # подстрой путь под свой проект
from src.ui_strings import get_ui_strings                 # подстрой путь под свой проект
from src.xml_escape import escape_xml_delimiters          # подстрой путь под свой проект

# ========= УТИЛИТЫ ДЛЯ RSS =========

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
    feed = feedparser.parse(url)  # [web:2]
    items = []
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
            }
        )

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def get_rss_items_from_list(urls, limit: int):
    """
    Несколько RSS-лент (для крипты): склеиваем, сортируем по времени, берём топ-N.
    """
    items = []
    for url in urls:
        feed = feedparser.parse(url)  # [web:2]
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
                }
            )

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


# ========= ОТПРАВКА В TELEGRAM =========

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


# ========= КЛАССЫ И ЛОГИКА DIGEST GROUPER (ИЗ 2-ГО КОДА, ЧУТЬ УРЕЗАНО) =========

_EXTRACTOR_CONCURRENCY = 10

_LEADING_ROCKET_HEADER_RE = re.compile(r"^🚀[^\n]*\n?")
_SECTION_TWO_SPLIT_RE = re.compile(r"📎\s*(?:Also|Также)\s*:")
_DEDUP_NORMALIZE_RE = re.compile(r"\s+")
_KEY_POINTS_HEADER_RE = re.compile(
    r"^\s*📌\s*(?:Key points|Ключевые моменты|Puntos clave|Schlüsselpunkte|Points clés)\s*:\s*\n?",
    re.IGNORECASE | re.MULTILINE,
)
_NUMBERED_EMOJI_PREFIX_RE = re.compile(r"(?<!\S)[1-9]️?⃣\s*")
_TEMPLATE_TOKEN_RE = re.compile(
    r"\[(?:emoji|brief\s+(?:fact|subject)|brief|fact|subject|link)\]\s*",
    re.IGNORECASE,
)


def _strip_channel_summary_noise(summary: str) -> str:
    cleaned = _LEADING_ROCKET_HEADER_RE.sub("", summary, count=1)
    cleaned = _SECTION_TWO_SPLIT_RE.split(cleaned, maxsplit=1)[0]
    cleaned = _KEY_POINTS_HEADER_RE.sub("", cleaned)
    cleaned = _NUMBERED_EMOJI_PREFIX_RE.sub("", cleaned)
    cleaned = _TEMPLATE_TOKEN_RE.sub("", cleaned)
    return cleaned.rstrip()


def _normalize_point(point: str) -> str:
    return _DEDUP_NORMALIZE_RE.sub(" ", point).strip().lower()


_QG_DROP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"новый участник|joined 
