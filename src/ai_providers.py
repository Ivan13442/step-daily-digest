import os
import logging
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
        Минимальный провайдер под Groq API с OpenAI-подобным интерфейсом.
        Документация Groq: формат совместим с /v1/chat/completions.
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

        import asyncio
        loop = asyncio.get_event_loop()

        def _do_request():
            resp = requests.post(url, json=payload, timeout=self.api_timeout, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()

        return await loop.run_in_executor(None, _do_request)


def create_provider(
    provider_name: str,
    logger: logging.Logger,
    openai_api_key: str,
    anthropic_api_key: str | None = None,
    ollama_base_url: str | None = None,
    api_timeout: int = 60,
) -> AIProvider:
    """
    Игнорируем provider_name и openai_api_key — всегда используем Groq.
    Ключ берём из переменной окружения GROQ_API_KEY.
    """
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        raise RuntimeError("GROQ_API_KEY is not set in environment/secrets")

    return AIProvider(
        logger=logger,
        groq_api_key=groq_key,
        api_timeout=api_timeout,
    )
