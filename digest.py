import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

from openai import AsyncOpenAI  # pip install openai

logger = logging.getLogger("digest_grouper")


# -------------------- модели данных --------------------


@dataclass
class RawItem:
    text: str
    link: str
    channel: str


@dataclass
class GroupedItem:
    text: str
    link: str
    channel: str
    group: str


# -------------------- настройки LLM --------------------


LLM_MODEL = "gpt-4.1-mini"
LLM_TEMPERATURE = 0.1
LLM_MAX_TOKENS = 2048

client = AsyncOpenAI()  # использует OPENAI_API_KEY из окружения


# -------------------- нормализация и дедуп --------------------


_WS_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


# quality gate: выкидываем слабые или мусорные заголовки
_DROP_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"новый участник|joined the chat", re.IGNORECASE),
    re.compile(r"без подробност|без деталей|no details|just a poll", re.IGNORECASE),
]

_HEDGE_RE = re.compile(
    r"\b(probably|maybe|likely|possibly|похоже|вероятно|возможно|кажется)\b",
    re.IGNORECASE,
)

_ENTITY_RE = re.compile(
    r"\d|@\w|https?://|[A-ZА-ЯЁ]{3,}"
)


def _has_entity(text: str) -> bool:
    return bool(_ENTITY_RE.search(text))


def _quality_filter(items: List[RawItem]) -> List[RawItem]:
    result: List[RawItem] = []
    for it in items:
        t = it.text.strip()
        if not t:
            continue
        if any(p.search(t) for p in _DROP_PATTERNS):
            continue
        has_entity = _has_entity(t)
        if len(t) < 30 and not has_entity:
            continue
        if _HEDGE_RE.search(t) and not has_entity:
            continue
        result.append(it)
    return result


def _dedup(items: List[RawItem]) -> List[RawItem]:
    by_key: Dict[str, RawItem] = {}
    for it in items:
        key = _normalize_text(it.text)
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = it
        else:
            # если есть дубликат — оставляем более длинный заголовок
            if len(it.text) > len(existing.text):
                by_key[key] = it
    return list(by_key.values())


# -------------------- промпты --------------------


def _build_classifier_messages(
    items: List[RawItem],
    groups: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """
    groups: [{"name": "...", "description": "..."}]
    """
    groups_desc = "\n".join(f'- "{g["name"]}": {g["description"]}' for g in groups)
    payload = [
        {"text": it.text, "link": it.link, "channel": it.channel}
        for it in items
    ]
    bullets_json = json.dumps(payload, ensure_ascii=False)

    system_prompt = (
        "You are a news classifier. You receive JSON with short news headlines.\n\n"
        "Your task: assign each item to exactly one of the predefined topic groups.\n\n"
        "Output ONLY valid JSON object where keys are group names and values are arrays of items.\n"
        'Format:\n{"GroupName": [{"text": "...", "link": "...", "channel": "..."}]}\n\n'
        "Rules:\n"
        "- Use the group descriptions to choose the best fit.\n"
        "- If an item fits multiple groups, choose the most specific.\n"
        "- Do not change text, link or channel fields.\n"
        "- Do not add or drop items.\n"
        "- No explanations, no markdown, JSON only."
    )

    user_prompt = (
        f"Groups:\n{groups_desc}\n\n"
        f"Items to classify (JSON array):\n{bullets_json}"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


async def _classify_async(
    items: List[RawItem],
    groups: List[Dict[str, str]],
) -> Dict[str, List[GroupedItem]]:
    if not items:
        return {}

    messages = _build_classifier_messages(items, groups)

    resp = await client.chat.completions.create(
        model=LLM_MODEL,
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
        messages=messages,
    )

    text = resp.choices[0].message.content.strip()
    # на случай ```json ...
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse classifier JSON: %s", e)
        return {}

    result: Dict[str, List[GroupedItem]] = {}
    for group_name, items_list in data.items():
        if not isinstance(items_list, list):
            continue
        for it in items_list:
            try:
                gi = GroupedItem(
                    text=str(it["text"]),
                    link=str(it["link"]),
                    channel=str(it.get("channel", "")),
                    group=group_name,
                )
            except KeyError:
                continue
            result.setdefault(group_name, []).append(
                {
                    "text": gi.text,
                    "link": gi.link,
                    "channel": gi.channel,
                }
            )
    return result


def group_items(
    raw_items: List[Dict[str, Any]],
    groups: List[Dict[str, str]],
) -> Dict[str, List[Dict[str, str]]]:
    """
    Синхронная обёртка: принимает список словарей
    {"text": str, "link": str, "channel": str}
    и возвращает {group_name: [ {...}, ... ]}.
    """
    items = [
        RawItem(
            text=str(it["text"]),
            link=str(it["link"]),
            channel=str(it.get("channel", "")),
        )
        for it in raw_items
    ]

    items = _quality_filter(items)
    items = _dedup(items)

    return asyncio.run(_classify_async(items, groups))
