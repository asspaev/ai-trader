"""Высокоуровневый сервис эмбеддингов поверх OpenRouter.

Эмбеддим строку ``title + " " + summary_text`` (формирует вызывающая
сторона) через модель ``settings.agent.embedding_model``. Полная
запись в ``llm_calls`` уже обеспечивается :class:`OpenRouterClient`,
здесь только парсинг ответа и валидация размерности — она зашита в
схеме БД (``news.embedding vector(1536)``), поэтому несовпадение
с конфигом ловим явно.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.config import settings
from app.services.llm.openrouter import OpenRouterClient


class EmbeddingError(RuntimeError):
    """Ответ ``/embeddings`` не содержит валидного вектора нужной размерности."""


async def create_embedding(
    client: OpenRouterClient,
    *,
    text: str,
    agent_name: str = "embedding",
    pipeline_run_id: uuid.UUID | None = None,
    model: str | None = None,
    expected_dim: int | None = None,
) -> list[float]:
    """Получить один эмбеддинг для строки ``text``.

    Args:
        client: Активный :class:`OpenRouterClient`.
        text: Сырая строка для эмбеддинга. Пустая строка — ошибка.
        agent_name: Имя «агента» в ``llm_calls.agent_name``. По
            архитектуре — ``"embedding"``; параметр оставлен на случай,
            если придётся различать источники.
        pipeline_run_id: Опциональный идентификатор pipeline-тика.
        model: Имя эмбеддинг-модели. По умолчанию — из
            ``settings.agent.embedding_model``.
        expected_dim: Ожидаемая размерность вектора. По умолчанию —
            ``settings.agent.embedding_dim``. Несовпадение → ошибка.

    Returns:
        Список ``float`` длины ``expected_dim``.

    Raises:
        EmbeddingError: Пустой ввод, пустой ``data`` в ответе или
            несовпадение размерности.
    """
    if not text or not text.strip():
        raise EmbeddingError("Input text for embedding is empty")

    model_name = model or settings.agent.embedding_model
    dim = expected_dim or settings.agent.embedding_dim

    response = await client.embeddings(
        agent_name=agent_name,
        model=model_name,
        inputs=text,
        pipeline_run_id=pipeline_run_id,
    )

    return _extract_first_vector(response, expected_dim=dim)


def _extract_first_vector(response: dict[str, Any], *, expected_dim: int) -> list[float]:
    """Достать первый вектор из ответа OpenAI-совместимого ``/embeddings``.

    Структура ответа: ``{"data": [{"embedding": [..]}, ...], "usage": {..}}``.
    """
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, list) or not data:
        raise EmbeddingError(
            f"Embedding response has empty or missing 'data': {response!r}"
        )

    first = data[0]
    if not isinstance(first, dict):
        raise EmbeddingError(f"Embedding 'data[0]' is not an object: {first!r}")

    embedding = first.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise EmbeddingError(
            f"Embedding 'data[0].embedding' is missing or empty: {first!r}"
        )

    if len(embedding) != expected_dim:
        raise EmbeddingError(
            f"Embedding dim mismatch: got {len(embedding)}, expected {expected_dim}"
        )

    try:
        return [float(value) for value in embedding]
    except (TypeError, ValueError) as exc:
        raise EmbeddingError(
            f"Embedding contains non-numeric values: {embedding[:5]!r}..."
        ) from exc


__all__ = ["EmbeddingError", "create_embedding"]
