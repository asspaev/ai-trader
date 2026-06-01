"""RAG-выборка исторически релевантных новостей по cosine-сходству.

Используется NEWS-агентом на шаге ``news_final_score``: по агрегации
свежих 24h-новостей строим query-текст, эмбеддим, и достаём ``top_k``
наиболее близких новостей в прошлом (`>24h`). Это позволяет LLM
сослаться на «исторический прецедент» с известным исходом.

Логика разнесена в этот модуль, чтобы не «толстеть» NEWS-агенту:
здесь нет промптов или парсинга — только пайплайн «текст → вектор →
SELECT через pgvector».
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crud import news as news_crud
from app.models import News
from app.services.llm.embeddings import create_embedding
from app.services.llm.openrouter import OpenRouterClient


async def fetch_relevant_history(
    session: AsyncSession,
    llm_client: OpenRouterClient,
    *,
    asset: str,
    query_text: str,
    top_k: int | None = None,
    exclude_last_hours: int | None = None,
    now: datetime | None = None,
    pipeline_run_id: uuid.UUID | None = None,
) -> list[News]:
    """Получить top-K исторических новостей, похожих на ``query_text``.

    Args:
        session: Активная async-сессия БД (read-only — не пишем).
        llm_client: Открытый :class:`OpenRouterClient` для эмбеддинга
            запроса. Запись в ``llm_calls`` обеспечивает он.
        asset: Тикер актива (фильтр на стороне SQL).
        query_text: Текст, который эмбеддим. Обычно — слитая
            ``news_agenda`` за 24h. Не должен быть пустым.
        top_k: Сколько кандидатов вернуть. По умолчанию — из
            ``TRADING_RAG_TOP_K``.
        exclude_last_hours: Окно «свежих» новостей, исключаемое из
            выборки. По умолчанию — ``TRADING_RAG_EXCLUDE_LAST_HOURS``.
        now: Опорное «сейчас» (для детерминированных тестов). В
            production — ``None``, тогда CRUD сам берёт ``utcnow``.
        pipeline_run_id: Идентификатор pipeline-тика для трекинга
            embedding-вызова в ``llm_calls``.

    Returns:
        Список :class:`News`, отсортированный по cosine-расстоянию
        возрастанию (ближайший — первый).
    """
    if not query_text or not query_text.strip():
        return []

    k = top_k if top_k is not None else settings.trading.rag_top_k
    exclude_hours = (
        exclude_last_hours
        if exclude_last_hours is not None
        else settings.trading.rag_exclude_last_hours
    )

    embedding = await create_embedding(
        llm_client,
        text=query_text,
        pipeline_run_id=pipeline_run_id,
    )

    return await search_by_embedding(
        session,
        asset=asset,
        embedding=embedding,
        top_k=k,
        exclude_last_hours=exclude_hours,
        now=now,
    )


async def search_by_embedding(
    session: AsyncSession,
    *,
    asset: str,
    embedding: Sequence[float],
    top_k: int | None = None,
    exclude_last_hours: int | None = None,
    now: datetime | None = None,
) -> list[News]:
    """Вариант для случаев, когда вектор уже есть (например, в тестах).

    Делает тот же SELECT, но без вызова LLM-эмбеддинга.
    """
    k = top_k if top_k is not None else settings.trading.rag_top_k
    exclude_hours = (
        exclude_last_hours
        if exclude_last_hours is not None
        else settings.trading.rag_exclude_last_hours
    )
    return await news_crud.search_similar(
        session,
        asset=asset,
        embedding=list(embedding),
        top_k=k,
        exclude_last_hours=exclude_hours,
        now=now,
    )


__all__ = ["fetch_relevant_history", "search_by_embedding"]
