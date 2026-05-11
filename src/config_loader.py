from dataclasses import dataclass, field
from typing import List
import yaml


@dataclass
class DigestGroupConfig:
    name: str
    description: str = ""


@dataclass
class Settings:
    ai_provider: str = "openai"
    ai_model: str = "gpt-4o-mini"
    output_language: str = "ru"
    api_timeout: int = 60
    max_tokens_per_summary: int = 800
    temperature: float = 0.3
    max_tokens: int = 800
    dedup_topics: bool = True
    ollama_base_url: str | None = None
    digest_groups: List[DigestGroupConfig] = field(default_factory=list)


@dataclass
class Config:
    openai_api_key: str
    anthropic_api_key: str | None = None
    settings: Settings = field(default_factory=Settings)


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    openai_key = raw.get("openai_api_key", "")
    anthropic_key = raw.get("anthropic_api_key")

    s = raw.get("settings", {}) or {}

    groups_raw = s.get("digest_groups", []) or []
    groups = [
        DigestGroupConfig(name=g.get("name", ""), description=g.get("description", ""))
        for g in groups_raw
        if g.get("name")
    ]

    settings = Settings(
        ai_provider=s.get("ai_provider", "openai"),
        ai_model=s.get("ai_model", "gpt-4o-mini"),
        output_language=s.get("output_language", "ru"),
        api_timeout=int(s.get("api_timeout", 60)),
        max_tokens_per_summary=int(s.get("max_tokens_per_summary", 800)),
        temperature=float(s.get("temperature", 0.3)),
        max_tokens=int(s.get("max_tokens", 800)),
        dedup_topics=bool(s.get("dedup_topics", True)),
        ollama_base_url=s.get("ollama_base_url"),
        digest_groups=groups,
    )

    return Config(
        openai_api_key=openai_key,
        anthropic_api_key=anthropic_key,
        settings=settings,
    )
