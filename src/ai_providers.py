import os
import logging
from typing import List, Dict, Any

import requests


class AIProvider:
    def __init__(
        self,
        logger: logging.Logger,
        openai_api_key: str,
        api_timeout: int = 60,
    ):
        self.logger = logger
        self.openai_api_key = openai_api_key
        self.api_timeout = api_timeout

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 512,
    ) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
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
            resp = requests.post(url, json=payload, timeout=self.api_timeout)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"]["message"]["content"].strip()

        return await loop.run_in_executor(None, _do_request)


def create_provider(
    provider_name: str,
    logger: logging.Logger,
    openai_api_key: str,
    anthropic_api_key: str | None = None,
    ollama_base_url: str | None = None,
    api_timeout: int = 60,
) -> AIProvider:
    # Пока игнорируем provider_name и всегда используем OpenAI
    if not openai_api_key:
        openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    return AIProvider(
        logger=logger,
        openai_api_key=openai_api_key,
        api_timeout=api_timeout,
    )
