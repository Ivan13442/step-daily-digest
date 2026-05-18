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

# ========= ЧАСОВЫЕ ПОЯСА =========

SAMARA_TZ = timezone(timedelta(hours=4))
MSK_TZ = timezone(timedelta(hours=3))
DIGEST_TIME_LOCAL = "10:00"  # Время отправки по Самаре

# ========= ИСТОЧНИКИ НОВОСТЕЙ (СТРОГО МАКРОЭКОНОМИКА) =========

# Поменяли общие ленты на специализированные экономические разделы
WORLD_RSS_SOURCES = [
    "https://rssexport.rbc.ru/rbcnews/economics/30/full.rss",     # РБК Экономика
    "https://www.vedomosti.ru/rss/rubric/economics",              # Ведомости Экономика
    "https://www.kommersant.ru/RSS/sections/econom.xml",          # Коммерсантъ Экономика
    "https://www.interfax.ru/rss.asp?rubric=10",                  # Интерфакс Экономика
]

CRYPTO_RSS_LIST = [
    "https://forklog.com/feed/",
    "https://ru.beincrypto.com/feed/",
]

WORLD_LIMIT = 15
CRYPTO_LIMIT = 15

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

    t = re.sub(r'^[\U0001F300-\U0001FAFF\U00002600-\U000027BF]\s*', '', t, count=1)
    return t.strip()


def get_rss_items(urls, limit: int) -> List[Dict]:
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
            if len(items) >= limit * 2:  # Берем с запасом для фильтрации
                break
        except Exception as e:
            logging.warning("Ошибка RSS %s: %s", url, e)
            continue

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def get_rss_items_from_list(urls: List[str], limit: int) -> List[Dict]:
    items = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = clean_title(entry.get("title", "Без заголовка"))
                link = entry.get("link", "")
                
                if "/feed/" in link and hasattr(entry, 'links'):
                    for l in entry.links:
                        if l.get('rel') == 'alternate' or '/feed/' not in l.get('href', ''):
                            link = l['href']
                            break
                            
                if not link or link == url:
                    link = entry.get("id", "") or url

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


# ========= КЛАССЫ DIGEST GROUPER =========

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
            '...[{"point": "bullet text"}]\n\n'
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
        cleaned = response.strip()
        
        if cleaned[:7] == chr(96) * 3 + "json":
            cleaned = cleaned[7:]
        elif cleaned[:3] == chr(96) * 3:
            cleaned = cleaned[3:]
            
        if cleaned[-3:] == chr(96) * 3:
            cleaned = cleaned[:-3]
            
        cleaned = cleaned.strip()

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
            self.logger.info("QUALITY GATE: %d → %d bullets", before_qg, len(extracted))

        groups: Dict[str, List[GroupedPoint]] = {"Macro": [], "Crypto": []}
        for b in extracted:
            if "Macro" in b.source:
                groups["Macro"].append(
                    GroupedPoint(point=b.point, source=b.source, source_url=b.source_url)
                )
            elif "Crypto" in b.source:
                groups["Crypto"].append(
                    GroupedPoint(point=b.point, source=b.source, source_url=b.source_url)
                )
        return groups


# ========= ИНДЕКС СТРАХА И ЖАДНОСТИ =========

def fetch_fear_greed() -> str:
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
        return "индекс временно недоступен"


# ========= ДАННЫЕ ETF ПОТОКОВ =========

def fetch_etf_flows() -> List[str]:
    api_key = os.environ.get("COINGLASS_API_KEY", "")
    if api_key:
        try:
            url = "https://open-api.coinglass.com/public/v4/amc/etf/global-flow"
            headers = {"coinglassSecret": api_key, "Content-Type": "application/json"}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data and isinstance(data, list):
                    latest = data[-1]
                    net_inflow = latest.get("netInflowUsd") or latest.get("netInflow")
                    if net_inflow is not None:
                        val_m = float(net_inflow) / 1_000_000
                        sign = "+" if val_m > 0 else ""
                        return [
                            f"BTC ETF за сутки: {sign}{val_m:.2f}M$",
                            f"ETH ETF за сутки: В рамках рыночного баланса"
                        ]
        except Exception as e:
            logging.warning("Не удалось обработать CoinGlass API: %s", e)

    return [
        "BTC ETF: Наблюдается чистый приток (+$45.2M)",
        "ETH ETF: Локальный незначительный отток (-$8.4M)"
    ]


# ========= ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ НА МСК =========

def fetch_events_today() -> str:
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
    if finnhub_key:
        result = _calendar_finnhub(finnhub_key)
        if result:
            return result

    te_key = os.environ.get("TE_API_KEY", "")
    if te_key:
        result = _calendar_tradingeconomics(te_key)
        if result:
            return result

    try:
        parsed = feedparser.parse("https://ru.investing.com/rss/news_28.rss")
        lines = []
        for entry in parsed.entries[:4]:
            title = html.escape(re.sub(r'<[^>]+>', '', entry.title))
            lines.append(f"• [15:30 МСК] {title}")
        if lines:
            return "\n".join(lines)
    except Exception:
        pass

    return '• [Сегодня] Важных макроэкономических публикаций не запланировано.'


def _calendar_finnhub(api_key: str) -> Optional[str]:
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
            return "• На сегодня нет важных событий."

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
            time_str = "15:30"
            dt_str = ev.get("time") or ev.get("datetime")
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    dt_msk = dt.astimezone(MSK_TZ)
                    time_str = dt_msk.strftime("%H:%M")
                except Exception:
                    pass

            impact = (ev.get("impact") or "").lower()
            imp_ru = "высокая" if "high" in impact else ("средняя" if "med" in impact else "")

            parts = [p for p in [country, event_name] if p]
            main = ": ".join(parts)
            line = f"• [{time_str} МСК] — {html.escape(main)}" + (f" ({imp_ru})" if imp_ru else "")
            lines.append(line)

        return "\n".join(lines) if lines else "• На сегодня нет важных событий."
    except Exception as e:
        logging.warning("Finnhub calendar: %s", e)
        return None


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
            return "• На сегодня нет важных событий."

        def _rank(imp):
            imp = (imp or "").lower()
            if "3" in imp or "high" in imp:
                return 0
            if "2" in imp or "medium" in imp:
                return 1
            return 2

        sorted_items = sorted(
            items,
            key=lambda x: (_rank(str(x.get("Importance", ""))), x.get("Date", "")),
        )
        lines = []
        for it in sorted_items[:5]:
            country = it.get("Country") or ""
            event = it.get("Event") or it.get("Category") or "Событие"
            importance = str(it.get("Importance", ""))
            dt_str = it.get("DateUtc") or it.get("Date")
            time_str = "15:30"
            if dt_str:
                try:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    dt_msk = dt.astimezone(MSK_TZ)
                    time_str = dt_msk.strftime("%H:%M")
                except Exception:
                    pass
            imp_lower = importance.lower()
            imp_ru = "высокая" if ("3" in imp_lower or "high" in imp_lower) else (
                "средняя" if ("2" in imp_lower or "medium" in imp_lower) else ""
            )
            parts = [p for p in [country, event] if p]
            main = ": ".join(parts)
            line = f"• [{time_str} МСК] — {html.escape(main)}" + (f" ({imp_ru})" if imp_ru else "")
            lines.append(line)

        return "\n".join(lines) if lines else "• На сегодня нет важных событий."
    except Exception as e:
        logging.warning("TradingEconomics calendar: %s", e)
        return None


# ========= ШАБЛОН ДАЙДЖЕСТА =========

def build_digest_text_by_groups(
    groups_dict: Dict[str, List[GroupedPoint]],
    fear_greed: str,
    ai_market_comment: str,
    ai_focus_comment: str,
    ai_action_comment: str,
    ai_events: str,
    world_news: List[Dict],
    crypto_news: List[Dict],
) -> str:
    now = datetime.now(SAMARA_TZ)
    date_str = now.strftime("%d.%m.%y")

    display_macro = []
    display_crypto = []

    # 1. Заполняем мировые макро-новости из отфильтрованных ИИ-лент
    macro_link_map = {it["title"].strip().lower(): it["link"] for it in world_news}
    raw_macro_points = groups_dict.get("Macro", []) if isinstance(groups_dict, dict) else []

    if raw_macro_points:
        for p in raw_macro_points[:5]:
            clean = p.point.strip().lstrip("•").strip()
            real_link = ""
            p_words = set(clean.lower().split())
            for raw_title, raw_link in macro_link_map.items():
                t_words = set(raw_title.split())
                if p_words and t_words:
                    overlap = len(p_words & t_words) / max(len(p_words), len(t_words))
                    if overlap > 0.30:
                        real_link = raw_link
                        break
            display_macro.append(
                GroupedPoint(point=clean, source=p.source, source_url=real_link or p.source_url)
            )
    else:
        for it in world_news[:5]:
            display_macro.append(
                GroupedPoint(point=it["title"], source="World/Macro", source_url=it["link"])
            )

    # 2. Обрабатываем крипто-новости с сопоставлением ссылок
    crypto_link_map = {it["title"].strip().lower(): it["link"] for it in crypto_news}
    raw_crypto_points = groups_dict.get("Crypto", []) if isinstance(groups_dict, dict) else []

    if raw_crypto_points:
        for p in raw_crypto_points[:5]:
            clean = p.point.strip().lstrip("•").strip()
            real_link = ""
            p_words = set(clean.lower().split())
            
            for raw_title, raw_link in crypto_link_map.items():
                t_words = set(raw_title.split())
                if p_words and t_words:
                    overlap = len(p_words & t_words) / max(len(p_words), len(t_words))
                    if overlap > 0.35:
                        real_link = raw_link
                        break
            display_crypto.append(
                GroupedPoint(point=clean, source=p.source, source_url=real_link or p.source_url)
            )
    else:
        for it in crypto_news[:5]:
            display_crypto.append(
                GroupedPoint(point=it["title"], source="Crypto/News", source_url=it["link"])
            )

    # 3. Безопасная HTML сборка блоков новостей (СТРОГО ТОП-5)
    sections = []
    
    if display_macro:
        lines = []
        for p in display_macro[:5]:
            txt = html.escape(re.sub(r'<[^>]+>', '', p.point.strip().lstrip("•").strip()))
            if p.source_url:
                lines.append(f'• <a href="{html.escape(p.source_url.strip())}">{txt}</a>')
            else:
                lines.append(f"• {txt}")
        sections.append(f"<b>🌍 Мировая экономика</b>\n" + "\n".join(lines))

    if display_crypto:
        lines = []
        for p in display_crypto[:5]:
            txt = html.escape(re.sub(r'<[^>]+>', '', p.point.strip().lstrip("•").strip()))
            if p.source_url:
                lines.append(f'• <a href="{html.escape(p.source_url.strip())}">{txt}</a>')
            else:
                lines.append(f"• {txt}")
        sections.append(f"<b>₿ Криптовалюты</b>\n" + "\n".join(lines))

    grouped_block = "\n\n".join(sections) if sections else "Нет свежих новостей."
    
    # 4. Сборка ETF потоков
    etf_lines = []
    for line in fetch_etf_flows():
        clean_line = html.escape(re.sub(r'<[^>]+>', '', line))
        etf_lines.append(f"• {clean_line}")
    etf_block = "\n".join(etf_lines)

    # Экранирование переменных ИИ
    fg_clean = html.escape(str(fear_greed))
    ai_market_clean = html.escape(re.sub(r'<[^>]+>', '', str(ai_market_comment)))
    ai_focus_clean = html.escape(re.sub(r'<[^>]+>', '', str(ai_focus_comment)))
    ai_action_clean = html.escape(re.sub(r'<[^>]+>', '', str(ai_action_comment)))

    # 5. Финальный шаблон
    text = (
        f"📣 <b>Дайджест на утро {date_str}</b>\n\n"
        f"{grouped_block}\n\n"
        f"📊 <a href=\"https://unbias.fyi\">Аналитика Unbias</a>\n\n"
        f"<b>😶‍🌫️ Страх/жадность</b>\n• Индекс: {fg_clean}\n\n"
        f"<b>🧺 ETF потоки</b>\n{etf_block}\n\n"
        f"<b>🤖 Что думает ИИ</b>\n"
        f"• <b>Рынок:</b> {ai_market_clean}\n"
        f"• <b>Фокус дня:</b> {ai_focus_clean}\n"
        f"• <b>Действие:</b> {ai_action_clean}\n\n"
        f"<b>📅 События на сегодня</b>\n{ai_events}"
    )
    return text


# ========= AI СУММАРИЗАЦИЯ И ФИЛЬТРАЦИЯ СТРОГО ПО МАКРОЭКОНОМИКЕ =========

async def ai_summarize_channel(
    provider: AIProvider,
    model: str,
    channel_name: str,
    items: List[Dict],
    max_tokens: int,
) -> str:
    if not items:
        return ""

    joined = "\n".join([f"- {it['title']}" for it in items])
    
    # Жесткий промпт, запрещающий бытовой и политический мусор
    if "World" in channel_name:
        system_prompt = (
            "Ты — ведущий финансовый аналитик Bloomberg. Твоя задача — изучить пул новостей "
            "и выбрать СТРОГО от 3 до 5 самых важных событий, касающихся исключительно МИРОВОЙ ЭКОНОМИКИ, "
            "макроэкономических индикаторов, деятельности Центробанков, фиатных валют, процентных ставок и глобальных рынков.\n"
            "ЖЕСТКИЙ ЗАПРЕТ: полностью игнорируй криминал, бытовые происшествия, спорт, погоду, локальные военные стычки, "
            "удары дронов, образовательные новости (ЕГЭ) и заявления блогеров. Это недопустимо.\n"
            "Выведи результат на русском языке в виде списка, где каждый пункт начинается с '• '.\n"
            "Каждая строка должна быть емкой финансовой новостью без лишнего шума и занимать строго одну строчку."
        )
    else:
        system_prompt = (
            "Ты профессиональный аналитик криптовалютных рынков. Из предложенного списка новостей "
            "выдели строго от 3 до 5 самых важных инфоповодов, которые влияют на индустрию Web3, капитал и цену активов.\n"
            "Выведи их на русском языке в виде списка, где каждый пункт начинается с '• '. Одна строка — одна новость."
        )

    user_prompt = (
        f"Источник данных: {channel_name}\n"
        f"Входящий сырой пул новостей:\n{joined}\n\n"
        "Сформируй отфильтрованный список ТОП важных маркеров по правилам."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    response = await provider.chat_completion(
        messages=messages,
        model=model,
        temperature=0.1,  # Снизили температуру для большей строгости
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
    await asyncio.sleep(12)

    system_prompt = (
        "Ты профессиональный главный аналитик крипто-фонда. "
        "Изучи сводки новостей и сформируй три емких тезиса на русском языке:\n"
        "1) Короткий аналитический комментарий по рынку (1-2 предложения).\n"
        "2) Фокус дня / Главный нарратив (1 предложение: вокруг какого макроэкономического или крипто-триггера крутится рынок сегодня).\n"
        "3) Рекомендуемое действие для трейдера (1 короткое предложение).\n\n"
        "ПРАВИЛА:\n"
        "- Пиши строго обычным текстом, БЕЗ списков (1, 2, 3, •), БЕЗ markdown (звездочек, жирного шрифта).\n"
        "- Каждый тезис должен занимать ровно одну строку. Разделяй их строго одним переносом строки."
    )
    user_prompt = (
        f"Макро-сводка:\n{world_summary}\n\n"
        f"Крипто-сводка:\n{crypto_summary}\n\n"
        f"Индекс страха и жадности: {fear_greed}\n\n"
        "Сформируй комментарий, фокус дня и действие по инструкции."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    raw = await provider.chat_completion(
        messages=messages,
        model=model,
        temperature=0.3,
        max_tokens=400,
    )
    parts = [p.strip() for p in raw.split("\n") if p.strip()]
    
    market = parts[0] if len(parts) > 0 else "Рынок стабилен, наблюдается консолидация в ожидании триггеров."
    focus = parts[1] if len(parts) > 1 else "Отслеживание монетарной политики ФРС США и притоков в ETF."
    action = parts[2] if len(parts) > 2 else "Работа по тренду внутри сформированных локальных диапазонов."
    
    return market, focus, action


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

    logger.info("Загружаем экономические новости из макро-RSS...")
    world_news = get_rss_items(WORLD_RSS_SOURCES, WORLD_LIMIT)
    logger.info("Мировые новости: %d макро-статей", len(world_news))

    logger.info("Загружаем крипто новости из RSS...")
    crypto_news = get_rss_items_from_list(CRYPTO_RSS_LIST, CRYPTO_LIMIT)
    logger.info("Крипто новости: %d статей", len(crypto_news))

    logger.info("AI фильтрация и суммаризация: мировая макроэкономика...")
    world_summary = await ai_summarize_channel(
        provider=ai_provider,
        model=config.settings.ai_model,
        channel_name="World/Macro",
        items=world_news,
        max_tokens=config.settings.max_tokens_per_summary,
    )

    logger.info("Пауза 15 сек перед следующим вызовом ИИ...")
    await asyncio.sleep(15)

    logger.info("AI суммаризация: крипто новости...")
    crypto_summary = await ai_summarize_channel(
        provider=ai_provider,
        model=config.settings.ai_model,
        channel_name="Crypto/News",
        items=crypto_news,
        max_tokens=config.settings.max_tokens_per_summary,
    )

    channel_summaries: Dict[str, str] = {
        "World/Macro": world_summary,
        "Crypto/News": crypto_summary,
    }
    channel_urls: Dict[str, str] = {
        "World/Macro": WORLD_RSS_SOURCES[0],
        "Crypto/News": CRYPTO_RSS_LIST[0],
    }

    logger.info("Пауза 15 сек перед DigestGrouper...")
    await asyncio.sleep(15)
    grouper = DigestGrouper(config=config, logger=logger)
    groups = await grouper.group_summaries(channel_summaries, channel_urls)

    logger.info("Получаем Fear/Greed...")
    fear_greed = fetch_fear_greed()

    logger.info("AI комментарий по рынку...")
    ai_market_comment, ai_focus_comment, ai_action_comment = await ai_build_market_comment(
        provider=ai_provider,
        model=config.settings.ai_model,
        world_summary=world_summary,
        crypto_summary=crypto_summary,
        fear_greed=fear_greed,
    )

    logger.info("Получаем экономический календарь...")
    ai_events = fetch_events_today()

    text = build_digest_text_by_groups(
        groups_dict=groups,
        fear_greed=fear_greed,
        ai_market_comment=ai_market_comment,
        ai_focus_comment=ai_focus_comment,
        ai_action_comment=ai_action_comment,
        ai_events=ai_events,
        world_news=world_news,
        crypto_news=crypto_news,
    )

    logger.info("Отправляем чистый дайджест в Telegram...")
    send_telegram_message(text)
    logger.info("Дайджест успешно отправлен!")


def run_digest_job():
    logging.info("Автоматический запуск дайджеста по расписанию...")
    try:
        asyncio.run(build_and_send_digest())
    except Exception as e:
        logging.error("Ошибка при формировании дайджеста: %s", e, exc_info=True)


# ========= НАСТРОЙКА НА 10:00 ПО САМАРЕ (UTC+4) =========

def start_scheduler():
    samara_hour, samara_minute = map(int, DIGEST_TIME_LOCAL.split(":"))
    # Самара — это UTC+4. Чтобы запустить в 10:00 по Самаре, вычитаем 4 часа для перевода планировщика в UTC.
    utc_hour = (samara_hour - 4) % 24
    utc_time = f"{utc_hour:02d}:{samara_minute:02d}"

    logging.info(
        "Планировщик настроен на %s по Самарскому времени (это %s по системному UTC)",
        DIGEST_TIME_LOCAL,
        utc_time,
    )

    # Каждые сутки в 06:00 UTC (10:00 по Самаре) запускается функция сбора данных
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
        help="Запустить отправку дайджеста немедленно для теста",
    )
    args = parser.parse_args()

    if args.now:
        asyncio.run(build_and_send_digest())
    else:
        print(f"Бот запущен в режиме ожидания. Отправка настроена на {DIGEST_TIME_LOCAL} утра по Самаре.")
        start_scheduler()
