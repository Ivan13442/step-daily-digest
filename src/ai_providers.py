import os
import time
import logging
import asyncio
from typing import List, Dict, Any
import requests


class AIProvider:
    def __init__(
        self,
        logger: logging.Logger,
        groq_api_key: str,
        api_timeout: int = 60,
    ):
        self.logger = logger
        self.groq_api_key = groq_api_key
        self.api_timeout = api_timeout

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 512,
    ) -> str:
        """
        Groq API с автоматическим retry при 429 Too Many Requests.
        При 429 ждём столько секунд, сколько указано в retry-after заголовке
        (или 60 секунд по умолчанию), затем повторяем — до 5 попыток.
        """
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        loop = asyncio.get_event_loop()
        max_attempts = 5

        for attempt in range(1, max_attempts + 1):
            def _do_request():
                resp = requests.post(
                    url,
                    json=payload,
                    timeout=self.api_timeout,
                    headers=headers,
                )
                return resp

            resp = await loop.run_in_executor(None, _do_request)

            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()

            elif resp.status_code == 429:
                # Читаем retry-after из заголовков (Groq возвращает его)
                retry_after = int(resp.headers.get("retry-after", 60))
                # Не ждём больше 120 секунд
                wait = min(retry_after + 5, 120)
                self.logger.warning(
                    "Groq 429 (попытка %d/%d) — ждём %d сек...",
                    attempt,
                    max_attempts,
                    wait,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(wait)
                    continue
                else:
                    self.logger.error("Groq 429: все попытки исчерпаны")
                    return "Комментарий временно недоступен (лимит Groq)."

            else:
                # Любая другая ошибка — сразу падаем
                resp.raise_for_status()

        return "Комментарий временно недоступен."


def create_provider(
    provider_name: str,
    logger: logging.Logger,
    openai_api_key: str,
    anthropic_api_key: str | None = None,
    ollama_base_url: str | None = None,
    api_timeout: int = 60,
) -> "AIProvider":
    """
    Всегда используем Groq. Ключ из переменной окружения GROQ_API_KEY.
    """
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY is not set in environment/secrets")
    return AIProvider(
        logger=logger,
        groq_api_key=groq_key,
        api_timeout=api_timeout,
    )
