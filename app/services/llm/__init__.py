"""Сервисный слой обращения к LLM через OpenRouter.

Реэкспорт публичных имён, чтобы вызывающие модули (агенты) могли
импортировать только из ``app.services.llm`` без знания внутренней
структуры.
"""

from app.services.llm.embeddings import EmbeddingError, create_embedding
from app.services.llm.openrouter import (
    OpenRouterClient,
    OpenRouterError,
    SessionFactory,
)

__all__ = [
    "EmbeddingError",
    "OpenRouterClient",
    "OpenRouterError",
    "SessionFactory",
    "create_embedding",
]
