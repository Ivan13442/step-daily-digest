import os
import time
import asyncio
import requests
import feedparser
import logging
import html
import json
import re
import schedule
import threading
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from typing import Dict, List, Optional

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

# ========= НАСТРОЙКИ TELEGRAM =========

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TOPIC_ID = os.environ.get("TELEGRAM_TOPIC_ID")

# ========= ЧАСОВОЙ ПОЯС САМАРЫ (UTC+4) =========

SAMARA_TZ = timezone(timedelta(hours=4))
DIGEST_TIME_LOCAL = "10:00"  # Самарское время

# ========= ИСТОЧНИКИ НОВОСТЕЙ (RSS) =========

# Несколько резервных источников для мировых новостей
WORLD_RSS_SOURCES = [
    "https://news-rss.ru/top.rss",
    "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",          # РБК
    "https://lenta.ru/rss/articles",                               # Лента.ру
    "https://www.kommersant.ru/RSS/news.xml",                      # Коммерсант
]

CRYPTO_RSS_LIST = [
    "https://forklog.com/feed/",
    "https://ru.beincrypto.com/feed/",
]

WORLD_LIMIT = 10
CRYPTO_LIMIT = 10

# ========= ИМПОРТЫ ИЗ ПРОЕКТА =========
from src.ai_providers import AIProvider, create_provider
from src.config_loader import Config, DigestGroupConfig, load_config
from src.ui_strings import get_ui_strings
from src.xml_escape import escape_xml_delimiters


# ========= УТИЛИТЫ ДЛЯ RSS =========

def clean_title(title: str) -> str:
    t = title.strip()
    if t.startswith("[") and "]" in t:
        t = t.split("]", 1)[1].strip()
    return t


def get_rss_items(urls, limit: int) -> List[Dict]:
    """
    Принимает один URL (str) или список URL.
    Перебирает по очереди пока не наберёт нужное количество новостей.
    Возвращает список {title, link, ts}.
    """
    if isinstance(urls, str):
        urls = [urls]

    items = []
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
            if len(items) >= limit:
                break
        except Exception as e:
            logging.warning("Ошибка RSS %s: %s", url, e)
            continue

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def get_rss_items_from_list(urls: List[str], limit: int) -> List[Dict]:
    """Собирает новости из нескольких RSS-лент, сортирует по дате."""
    items = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = clean_title(entry.get("title", "Без заголовка"))
                link = entry.get("link", "")
                published = getattr(entry, "published_parsed", None)
                ts = time.mktime(published) if published else 0
                items.append({"title": title, "link": link, "ts": ts})
        except Exception as e:
            logging.warning("Ошибка RSS %s: %s", url, e)

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


# ========= ОТПРАВКА В TELEGRAM =========

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if TOPIC_ID:
        payload["message_thread_id"] = int(TOPIC_ID)

    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ========= КЛАССЫ DIGEST GROUPER (без изменений логики) =========

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


_QG_DROP_PATTERNS: tuple = (
    re.compile(
        r"новый участник|joined the chat|появил(?:ся|ась|ось|ись).{0,30}участник",
        re.IGNORECASE,
    ),
    re.compile(
        r"без\s+(?:дополнительных\s+)?(?:деталей|подробностей)"
        r"|без\s+пояснени(?:й|я)"
        r"|no\s+details?"
        r"|just\s+a\s+poll",
        re.IGNORECASE,
    ),
)
_QG_HEDGE_RE = re.compile(
    r"\b(?:probably|maybe|likely|possibly|похоже|вероятно|возможно|кажется|выглядит\s+как)\b",
    re.IGNORECASE,
)
_QG_ENTITY_DIGIT_RE = re.compile(r"\d")
_QG_ENTITY_AT_RE = re.compile(r"@\w")
_QG_ENTITY_URL_RE = re.compile(r"https?://|t\.me/")
_QG_ENTITY_PROPER_RE = re.compile(r"\b(?:[A-ZА-ЯЁ][\w''-]{1,}\b.*?){2,}|[A-ZА-Я]{3,}")


def _qg_has_concrete_entity(point: str) -> bool:
    return bool(
        _QG_ENTITY_DIGIT_RE.search(point)
        or _QG_ENTITY_AT_RE.search(point)
        or _QG_ENTITY_URL_RE.search(point)
        or _QG_ENTITY_PROPER_RE.search(point)
    )


def _quality_gate_filter(bullets):
    survivors = []
    for b in bullets:
        text = b.point.strip()
        if not text:
            continue
        if any(p.search(text) for p in _QG_DROP_PATTERNS):
            continue
        has_entity = _qg_has_concrete_entity(text)
        if len(text) < 30 and not has_entity:
            continue
        if _QG_HEDGE_RE.search(text) and not has_entity:
            continue
        survivors.append(b)
    return survivors


@dataclass
class ExtractedBullet:
    point: str
    source: str
    source_url: str = ""


@dataclass
class GroupedPoint:
    point: str
    source: str
    source_url: str = ""


class DigestGrouper:
    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self._ui = get_ui_strings(config.settings.output_language)
        grouper_timeout = config.settings.api_timeout * 3
        self.provider: AIProvider = create_provider(
            provider_name=config.settings.ai_provider,
            logger=logger,
            openai_api_key=config.openai_api_key,
            anthropic_api_key=config.anthropic_api_key,
            ollama_base_url=config.settings.ollama_base_url,
            api_timeout=grouper_timeout,
        )
        self.model = config.settings.ai_model
        self.temperature = config.settings.temperature
        self.max_tokens = config.settings.max_tokens_per_summary * 3

    def _build_extractor_prompt(self, channel_name: str, summary: str) -> List[Dict]:
        cleaned_summary = _strip_channel_summary_noise(summary)
        safe_name = html.escape(channel_name, quote=True)
        safe_summary = escape_xml_delimiters(cleaned_summary)

        system_prompt = (
            "You are a bullet extractor. Given a single Telegram channel summary, "
            "extract each individual bullet point as a JSON array.\n\n"
            "IMPORTANT: Preserve the original language of the bullet points. "
            "Do NOT translate them.\n\n"
            "Output ONLY a valid JSON array in this exact format:\n"
            '[{"point": "bullet text"}, {"point": "another bullet"}]\n\n'
            "Rules:\n"
            "- Each surviving input bullet becomes one output entry\n"
            "- Preserve emojis at the start of each bullet\n"
            "- Preserve the bullet text verbatim\n"
            "- Output raw JSON only — no markdown, no explanation"
        )
        user_prompt = (
            f"Extract bullets from this channel summary.\n\n"
            f'<channel_summary source="{safe_name}">\n{safe_summary}\n</channel_summary>'
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    async def _extract_bullets_from_channel(
        self, channel_name: str, summary: str, source_url: str
    ) -> List[ExtractedBullet]:
        if not _strip_channel_summary_noise(summary).strip():
            return []
        messages = self._build_extractor_prompt(channel_name, summary)
        response = await self.provider.chat_completion(
            messages=messages,
            model=self.model,
            temperature=0.1,
            max_tokens=self.config.settings.max_tokens_per_summary,
        )
        return self._parse_extracted_response(response, channel_name, source_url)

    def _parse_extracted_response(
        self, response: str, channel_name: str, source_url: str
    ) -> List[ExtractedBullet]:
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", response.strip())
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            self.logger.warning("Extractor JSON parse failed for %s: %s", channel_name, exc)
            return []
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if isinstance(item, dict) and "point" in item:
                result.append(
                    ExtractedBullet(
                        point=str(item["point"]),
                        source=channel_name,
                        source_url=source_url,
                    )
                )
        return result

    async def _extract_all_bullets(
        self,
        channel_summaries: Dict[str, str],
        channel_urls: Dict[str, str],
    ) -> List[ExtractedBullet]:
        sem = asyncio.Semaphore(_EXTRACTOR_CONCURRENCY)

        async def _run(name: str, summary: str) -> List[ExtractedBullet]:
            async with sem:
                return await self._extract_bullets_from_channel(
                    channel_name=name,
                    summary=summary,
                    source_url=channel_urls.get(name, ""),
                )

        names = list(channel_summaries.keys())
        results = await asyncio.gather(
            *(_run(name, channel_summaries[name]) for name in names),
            return_exceptions=True,
        )
        bullets = []
        for name, res in zip(names, results):
            if isinstance(res, BaseException):
                self.logger.error("Extractor failed for %s: %s", name, res)
                continue
            bullets.extend(res)
        return bullets

    async def group_summaries(
        self,
        channel_summaries: Dict[str, str],
        channel_urls: Optional[Dict[str, str]] = None,
    ) -> Dict[str, List[GroupedPoint]]:
        urls = channel_urls or {}
        extracted = await self._extract_all_bullets(channel_summaries, urls)
        before_qg = len(extracted)
        extracted = _quality_gate_filter(extracted)
        if len(extracted) < before_qg:
            self.logger.info(
                "QUALITY GATE: %d → %d bullets",
                before_qg,
                len(extracted),
            )

        groups: Dict[str, List[GroupedPoint]] = {"Macro": [], "Crypto": []}
        for b in extracted:
            if b.source == "World/Macro":
                groups["Macro"].append(
                    GroupedPoint(point=b.point, source=b.source, source_url=b.source_url)
                )
            elif b.source == "Crypto/News":
                groups["Crypto"].append(
                    GroupedPoint(point=b.point, source=b.source, source_url=b.source_url)
                )
        return groups


# ========= ВНЕШНИЕ ДАННЫЕ =========

def fetch_unbias_btc() -> str:
    """
    FIX #2: Получаем актуальный сигнал Unbias прямо сейчас.
    Пробуем несколько endpoint'ов API.
    """
    api_key = os.environ.get("UNBIAS_API_KEY", "")
    if not api_key:
        return "нет сигнала (нет API ключа Unbias)"

    # Пробуем разные endpoint'ы — выбираем тот, что работает
    endpoints = [
        "https://unbias.fyi/api/v1/consensus",
        "https://unbias.fyi/api/v1/signals/latest",
        "https://unbias.fyi/api/v1/index/current",
    ]

    for endpoint in endpoints:
        try:
            resp = requests.get(
                endpoint,
                params={"asset": "BTC"},
                headers={"X-API-Key": api_key},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()

            # Пробуем разные ключи ответа
            idx = (
                data.get("consensus_index")
                or data.get("index")
                or data.get("value")
                or data.get("signal_value")
            )
            if idx is None:
                # Может быть вложен
                if isinstance(data.get("data"), dict):
                    idx = data["data"].get("index") or data["data"].get("value")

            if idx is None:
                continue

            idx = float(idx)
            if idx >= 25:
                action = "покупка"
            elif idx <= -25:
                action = "продажа"
            else:
                action = "держать"

            return f"{action} {idx:.1f} (диапазон от -100 до +100)"

        except Exception as e:
            logging.warning("Unbias endpoint %s: %s", endpoint, e)
            continue

    return "нет сигнала (сервис недоступен)"


def fetch_fear_greed() -> str:
    """
    FIX #3: Актуальный индекс страха/жадности прямо сейчас.
    
    Стратегия:
    1. Alternative.me — бесплатный, всегда текущий
    2. CMC как запасной
    """
    # --- Вариант 1: Alternative.me (бесплатно, всегда актуально) ---
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        entry = data["data"][0]
        value = entry["value"]
        label = entry["value_classification"]
        # Переводим метки на русский
        label_ru = {
            "Extreme Fear": "Крайний страх",
            "Fear": "Страх",
            "Neutral": "Нейтрально",
            "Greed": "Жадность",
            "Extreme Greed": "Крайняя жадность",
        }.get(label, label)
        return f"{value} — {label_ru}"
    except Exception as e:
        logging.warning("Alternative.me fear/greed: %s", e)

    # --- Вариант 2: CMC ---
    api_key = os.environ.get("CMC_API_KEY", "")
    if not api_key:
        return "индекс временно недоступен"

    try:
        resp = requests.get(
            # latest — текущий индекс, не historical
            "https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest",
            headers={"X-CMC_PRO_API_KEY": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        entry = data["data"]
        value = entry["value"]
        label = entry["value_classification"]
        return f"{value} — {label}"
    except Exception as e:
        logging.warning("CMC fear/greed: %s", e)
        return "индекс временно недоступен"


def fetch_etf_flow_brief() -> List[str]:
    """
    FIX #4: ETF потоки за предыдущие сутки.
    
    Стратегия:
    1. CoinGlass v4 — с исправленным парсингом ответа
    2. CMC ETF listings как fallback
    """
    api_key = os.environ.get("COINGLASS_API_KEY", "")

    if api_key:
        result = _fetch_etf_coinglass(api_key)
        if result:
            return result

    return _fetch_etf_cmc()


def _fetch_etf_coinglass(api_key: str) -> Optional[List[str]]:
    """CoinGlass v4 с полным перебором структуры ответа."""
    base_url = "https://open-api-v4.coinglass.com"
    headers = {
        "CG-API-KEY": api_key,
        "Accept": "application/json",
    }

    assets = [
        ("BTC", "/api/bitcoin/etf/flow-history"),
        ("ETH", "/api/ethereum/etf/flow-history"),
    ]

    lines = []
    any_success = False

    for ticker, endpoint in assets:
        try:
            resp = requests.get(
                base_url + endpoint,
                headers=headers,
                params={"interval": "1d", "limit": 2},
                timeout=15,
            )
            if resp.status_code != 200:
                logging.warning("CoinGlass %s HTTP %d", ticker, resp.status_code)
                lines.append(f"{ticker} ETF: данные временно недоступны")
                continue

            data = resp.json()

            # CoinGlass может возвращать {code, data: [...]} или просто [...]
            if isinstance(data, dict):
                payload = data.get("data") or data.get("result") or []
            elif isinstance(data, list):
                payload = data
            else:
                payload = []

            if not payload:
                lines.append(f"{ticker} ETF: данные временно недоступны")
                continue

            # Берём последнюю запись (вчерашний день)
            latest = payload[-1] if isinstance(payload, list) else payload

            # Перебираем возможные ключи net flow
            flow = None
            for key in [
                "netInflowUsd", "net_inflow_usd", "netInflow", "net_inflow",
                "totalNetInflow", "total_net_inflow", "flow", "netFlow",
            ]:
                val = latest.get(key)
                if val is not None:
                    flow = float(val)
                    break

            if flow is None:
                logging.warning("CoinGlass %s: не найден ключ flow в %s", ticker, list(latest.keys()))
                lines.append(f"{ticker} ETF: данные временно недоступны")
                continue

            any_success = True
            mln = flow / 1_000_000.0
            if mln > 0:
                lines.append(f"{ticker} ETF: приток +{mln:.1f} млн $")
            elif mln < 0:
                lines.append(f"{ticker} ETF: отток {mln:.1f} млн $")
            else:
                lines.append(f"{ticker} ETF: нейтрально 0.0 млн $")

        except Exception as e:
            logging.warning("CoinGlass %s: %s", ticker, e)
            lines.append(f"{ticker} ETF: данные временно недоступны")

    return lines if any_success else None


def _fetch_etf_cmc() -> List[str]:
    """Fallback через CMC ETF listings."""
    api_key = os.environ.get("CMC_API_KEY", "")
    if not api_key:
        return ["данные по ETF временно недоступны (нет API ключей)"]

    try:
        resp = requests.get(
            "https://pro-api.coinmarketcap.com/v1/etf/listings/latest",
            params={"limit": 5},
            headers={"X-CMC_PRO_API_KEY": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", [])[:5]
        bullets = []
        for it in items:
            name = it.get("name") or it.get("symbol") or "ETF"
            change = it.get("quote", {}).get("USD", {}).get("percent_change_24h")
            if change is not None:
                bullets.append(f"{name}: {change:+.2f}% за сутки")
            else:
                bullets.append(name)
        return bullets or ["данные по ETF временно недоступны"]
    except Exception as e:
        logging.warning("CMC ETF: %s", e)
        return ["данные по ETF временно недоступны"]


# ========= ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ =========

def fetch_events_today() -> str:
    """
    FIX #5: Экономический календарь на сегодня.
    
    Стратегия (в порядке приоритета):
    1. TradingEconomics (если есть TE_API_KEY)
    2. Finnhub (если есть FINNHUB_API_KEY) — бесплатный tier
    3. Investing.com RSS (совсем бесплатно)
    """
    # --- Вариант 1: TradingEconomics ---
    te_key = os.environ.get("TE_API_KEY", "")
    if te_key:
        result = _calendar_tradingeconomics(te_key)
        if result:
            return result

    # --- Вариант 2: Finnhub (бесплатно, нужна регистрация) ---
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
    if finnhub_key:
        result = _calendar_finnhub(finnhub_key)
        if result:
            return result

    # --- Вариант 3: RSS Investing.com (совсем без ключа) ---
    result = _calendar_investing_rss()
    if result:
        return result

    return "• Экономический календарь временно недоступен."


def _calendar_tradingeconomics(api_key: str) -> Optional[str]:
    today = date.today()
    start_str = today.strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            "https://api.tradingeconomics.com/calendar",
            params={"d1": start_str, "d2": start_str, "c": api_key, "lang": "en"},
            timeout=30,
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            return "• На сегодня нет важных событий в календаре."

        def _rank(imp):
            imp = (imp or "").lower()
            if "3" in imp or "high" in imp:
                return 0
            if "2" in imp or "medium" in imp:
                return 1
            return 2

        sorted_items = sorted(
            items,
            key=lambda x: (
                _rank(str(x.get("Importance", ""))),
                x.get("Date", "") or x.get("DateUtc", ""),
            ),
        )
        top = sorted_items[:5]
        lines = []
        for it in top:
            country = it.get("Country") or ""
            event = it.get("Event") or it.get("Category") or "Событие"
            importance = str(it.get("Importance", ""))
            dt_str = it.get("DateUtc") or it.get("Date")
            time_str = "??:??"
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    dt_samara = dt.astimezone(SAMARA_TZ)
                    time_str = dt_samara.strftime("%H:%M")
                except Exception:
                    pass

            imp_lower = importance.lower()
            if "3" in imp_lower or "high" in imp_lower:
                imp_ru = "высокая"
            elif "2" in imp_lower or "medium" in imp_lower:
                imp_ru = "средняя"
            else:
                imp_ru = ""

            parts = [p for p in [country, event] if p]
            main = ": ".join(parts)
            line = f"• {time_str} — {main}" + (f" ({imp_ru})" if imp_ru else "")
            lines.append(line)

        return "\n".join(lines) if lines else "• На сегодня нет важных событий."
    except Exception as e:
        logging.warning("TradingEconomics calendar: %s", e)
        return None


def _calendar_finnhub(api_key: str) -> Optional[str]:
    """Finnhub economic calendar — бесплатный tier даёт данные на текущий день."""
    today = date.today()
    start_str = today.strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"from": start_str, "to": start_str, "token": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        events = data.get("economicCalendar") or data.get("data") or []
        if not events:
            return "• На сегодня нет важных событий в календаре."

        # Сортируем по важности (impact: high > medium > low)
        def _rank(e):
            imp = (e.get("impact") or "").lower()
            if "high" in imp:
                return 0
            if "medium" in imp or "med" in imp:
                return 1
            return 2

        events_sorted = sorted(events, key=_rank)[:5]
        lines = []
        for ev in events_sorted:
            country = ev.get("country") or ""
            event_name = ev.get("event") or ev.get("name") or "Событие"
            time_str = "??:??"
            dt_str = ev.get("time") or ev.get("datetime")
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    dt_samara = dt.astimezone(SAMARA_TZ)
                    time_str = dt_samara.strftime("%H:%M")
                except Exception:
                    pass

            impact = (ev.get("impact") or "").lower()
            if "high" in impact:
                imp_ru = "высокая"
            elif "medium" in impact or "med" in impact:
                imp_ru = "средняя"
            else:
                imp_ru = ""

            parts = [p for p in [country, event_name] if p]
            main = ": ".join(parts)
            line = f"• {time_str} — {main}" + (f" ({imp_ru})" if imp_ru else "")
            lines.append(line)

        return "\n".join(lines) if lines else "• На сегодня нет важных событий."
    except Exception as e:
        logging.warning("Finnhub calendar: %s", e)
        return None


def _calendar_investing_rss() -> Optional[str]:
    """
    Бесплатный вариант: RSS Investing.com с экономическими событиями.
    Не требует ключа, но менее структурирован.
    """
    rss_urls = [
        "https://ru.investing.com/rss/economic_indicators.rss",
        "https://www.forexfactory.com/ffcal_week_this.xml",
    ]
    today_str = date.today().strftime("%Y-%m-%d")
    lines = []

    for url in rss_urls:
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                continue
            for entry in feed.entries[:5]:
                title = entry.get("title", "")
                # Фильтруем по дате если есть
                pub = entry.get("published", "")
                if today_str not in pub and pub:
                    continue
                lines.append(f"• {html.escape(title)}")

            if lines:
                return "\n".join(lines[:5])
        except Exception as e:
            logging.warning("Investing RSS %s: %s", url, e)
            continue

    return None


# ========= ШАБЛОН ДАЙДЖЕСТА =========

def build_digest_text_by_groups(
    groups_dict: Dict[str, List[GroupedPoint]],
    unbias_btc: str,
    fear_greed: str,
    etf_lines: List[str],
    ai_market_comment: str,
    ai_action_comment: str,
    ai_events: str,
    world_news: List[Dict],
) -> str:
    """
    FIX #1: Мировые новости теперь правильно формируют секцию Macro
    с реальными ссылками на статьи (не на RSS-ленту).
    """
    now = datetime.now(SAMARA_TZ)
    date_str = now.strftime("%d.%m.%y")

    # Мировая экономика: напрямую из RSS с реальными ссылками
    macro_points: List[GroupedPoint] = []
    for it in world_news:
        # Используем реальную ссылку на статью, а не URL RSS-ленты
        macro_points.append(
            GroupedPoint(
                point=it["title"],
                source="World/Macro",
                source_url=it["link"],  # это уже ссылка на статью
            )
        )
    groups_dict["Macro"] = macro_points

    display_names = {
        "Macro": "🌍 Мировая экономика",
        "Crypto": "₿ Криптовалюты",
    }

    sections = []
    for grp_name in ["Macro", "Crypto"]:
        points = groups_dict.get(grp_name, [])
        if not points:
            continue

        bullets_lines = []
        for p in points:
            # Чистим от лишних маркеров вроде "• " в начале
            clean_point = p.point.strip().lstrip("•").strip()
            title_escaped = html.escape(clean_point)
            if p.source_url:
                url_escaped = html.escape(p.source_url)
                bullets_lines.append(f'• <a href="{url_escaped}">{title_escaped}</a>')
            else:
                bullets_lines.append(f"• {title_escaped}")

        bullets = "\n".join(bullets_lines)
        title = display_names.get(grp_name, grp_name)
        sections.append(f"{title}\n{bullets}")

    grouped_block = "\n\n".join(sections) if sections else "Нет свежих новостей."
    etf_block = "\n".join(f"• {line}" for line in etf_lines)

    text = (
        f"📣 Дайджест на утро {date_str}\n\n"
        f"{grouped_block}\n\n"
        f"📊 Unbias\n• {unbias_btc}\n\n"
        f"😶‍🌫️ Страх/жадность\n• Индекс: {fear_greed}\n\n"
        f"🧺 ETF за сутки\n{etf_block}\n\n"
        f"🤖 Что думает ИИ\n{ai_market_comment}\n{ai_action_comment}\n\n"
        f"📅 События на сегодня\n{ai_events}"
    )
    return text


# ========= AI СУММАРИЗАЦИЯ =========

async def ai_summarize_channel(
    provider: AIProvider,
    model: str,
    channel_name: str,
    items: List[Dict],
    max_tokens: int,
) -> str:
    if not items:
        return ""

    joined = "\n".join([f"- {it['title']} ({it['link']})" for it in items])
    system_prompt = (
        "Ты делаешь краткое русскоязычное резюме новостей по одному источнику. "
        "Выдели 5-10 ключевых пунктов, каждый с новой строки, формата '• ...'. "
        "Не выдумывай факты, опирайся только на список новостей."
    )
    user_prompt = (
        f"Источник: {channel_name}\n"
        f"Вот список свежих новостей:\n{joined}\n\n"
        "Сделай краткое резюме в виде маркеров."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    response = await provider.chat_completion(
        messages=messages,
        model=model,
        temperature=0.3,
        max_tokens=max_tokens,
    )
    return response


async def ai_build_market_comment(
    provider: AIProvider,
    model: str,
    world_summary: str,
    crypto_summary: str,
    fear_greed: str,
) -> tuple:
    system_prompt = (
        "Ты опытный трейдер и аналитик крипторынка. "
        "Сделай два коротких текста на русском:\n"
        "1) Комментарий по рынку (1-2 предложения, без Markdown).\n"
        "2) Рекомендуемое действие (1 предложение, без Markdown).\n"
        "Не используй списки, звёздочки, жирный шрифт."
    )
    user_prompt = (
        f"Резюме по миру:\n{world_summary}\n\n"
        f"Резюме по крипте:\n{crypto_summary}\n\n"
        f"Индекс страха и жадности: {fear_greed}\n\n"
        "Сформулируй комментарий и действие."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    raw = await provider.chat_completion(
        messages=messages,
        model=model,
        temperature=0.4,
        max_tokens=300,
    )
    parts = [p.strip() for p in raw.split("\n") if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    if parts:
        return parts[0], "Работать по системе, без фомы."
    return "Комментарий временно недоступен.", "Работать по системе, без фомы."


# ========= ГЛАВНАЯ ЛОГИКА =========

async def build_and_send_digest():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("digest")

    config: Config = load_config("config.yaml")

    ai_provider = create_provider(
        provider_name=config.settings.ai_provider,
        logger=logger,
        openai_api_key=config.openai_api_key,
        anthropic_api_key=config.anthropic_api_key,
        ollama_base_url=config.settings.ollama_base_url,
        api_timeout=config.settings.api_timeout,
    )

    # 1. RSS новости (FIX #1: несколько резервных источников для мировых)
    logger.info("Загружаем мировые новости из RSS...")
    world_news = get_rss_items(WORLD_RSS_SOURCES, WORLD_LIMIT)
    logger.info("Мировые новости: %d статей", len(world_news))

    logger.info("Загружаем крипто новости из RSS...")
    crypto_news = get_rss_items_from_list(CRYPTO_RSS_LIST, CRYPTO_LIMIT)
    logger.info("Крипто новости: %d статей", len(crypto_news))

    # 2. AI суммаризация
    channel_summaries: Dict[str, str] = {}
    channel_urls: Dict[str, str] = {}

    world_summary = await ai_summarize_channel(
        provider=ai_provider,
        model=config.settings.ai_model,
        channel_name="World/Macro",
        items=world_news,
        max_tokens=config.settings.max_tokens_per_summary,
    )
    channel_summaries["World/Macro"] = world_summary
    channel_urls["World/Macro"] = WORLD_RSS_SOURCES[0]

    crypto_summary = await ai_summarize_channel(
        provider=ai_provider,
        model=config.settings.ai_model,
        channel_name="Crypto/News",
        items=crypto_news,
        max_tokens=config.settings.max_tokens_per_summary,
    )
    channel_summaries["Crypto/News"] = crypto_summary
    channel_urls["Crypto/News"] = CRYPTO_RSS_LIST[0]

    # 3. Группировка через DigestGrouper
    grouper = DigestGrouper(config=config, logger=logger)
    groups = await grouper.group_summaries(channel_summaries, channel_urls)

    # 4. Доп. данные (FIX #2, #3, #4)
    logger.info("Получаем Unbias, Fear/Greed, ETF...")
    unbias_btc = fetch_unbias_btc()
    fear_greed = fetch_fear_greed()
    etf_lines = fetch_etf_flow_brief()
    logger.info("Unbias: %s | F/G: %s | ETF: %s", unbias_btc, fear_greed, etf_lines)

    # 5. AI комментарий
    ai_market_comment, ai_action_comment = await ai_build_market_comment(
        provider=ai_provider,
        model=config.settings.ai_model,
        world_summary=world_summary,
        crypto_summary=crypto_summary,
        fear_greed=fear_greed,
    )

    # 6. Экономический календарь (FIX #5)
    logger.info("Получаем экономический календарь...")
    ai_events = fetch_events_today()

    # 7. Собираем текст
    text = build_digest_text_by_groups(
        groups_dict=groups,
        unbias_btc=unbias_btc,
        fear_greed=fear_greed,
        etf_lines=etf_lines,
        ai_market_comment=ai_market_comment,
        ai_action_comment=ai_action_comment,
        ai_events=ai_events,
        world_news=world_news,
    )

    # 8. Отправляем в Telegram
    logger.info("Отправляем дайджест в Telegram...")
    send_telegram_message(text)
    logger.info("Дайджест отправлен!")


def run_digest_job():
    """Синхронная обёртка для запуска asyncio из потока schedule."""
    logging.info("Запуск дайджеста по расписанию...")
    try:
        asyncio.run(build_and_send_digest())
    except Exception as e:
        logging.error("Ошибка при формировании дайджеста: %s", e, exc_info=True)


# ========= FIX #6: РАСПИСАНИЕ 10:00 САМАРА (UTC+4) =========

def start_scheduler():
    """
    Запускает планировщик с учётом часового пояса Самары (UTC+4).
    schedule работает в системном времени, поэтому пересчитываем:
    10:00 Самара = 06:00 UTC
    """
    samara_hour, samara_minute = map(int, DIGEST_TIME_LOCAL.split(":"))

    # Пересчёт в UTC
    utc_hour = (samara_hour - 4) % 24
    utc_time = f"{utc_hour:02d}:{samara_minute:02d}"

    logging.info(
        "Расписание дайджеста: %s по Самаре (%s UTC)",
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

    parser = argparse.ArgumentParser(description="Digest Bot")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Отправить дайджест прямо сейчас (без расписания)",
    )
    args = parser.parse_args()

    if args.now:
        # Запуск немедленно (для тестов и ручного запуска)
        asyncio.run(build_and_send_digest())
    else:
        # Запуск по расписанию каждый день в 10:00 Самара
        print(f"Бот запущен. Дайджест будет отправляться в {DIGEST_TIME_LOCAL} по Самаре.")
        start_scheduler()
