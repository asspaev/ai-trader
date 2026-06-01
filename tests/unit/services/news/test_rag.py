"""Тесты RAG-выборки (``app.services.news.rag``).

Покрывают:

* ``fetch_relevant_history`` — эмбеддит query, исключает последние 24h,
  возвращает top-K кандидатов в порядке cosine-сходства.
* Несовпадение размерности эмбеддинга поднимает
  :class:`EmbeddingError` (валидация в :mod:`app.services.llm.embeddings`).
* Пустой query — без HTTP/LLM-запросов, пустой список.
* Возможность звать ``search_by_embedding`` без LLM, когда вектор уже есть.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.crud import news as news_crud
from app.services.llm.embeddings import EmbeddingError
from app.services.news.rag import fetch_relevant_history, search_by_embedding

from tests.unit.services.llm._helpers import FakeOpenRouterClient


pytestmark = pytest.mark.asyncio(loop_scope="session")


_DIM = settings.agent.embedding_dim


def _vec(seed: float) -> list[float]:
    """Простой детерминированный вектор размерности эмбеддинга."""
    vector = [0.0] * _DIM
    vector[0] = seed
    if _DIM > 1:
        vector[1] = 1.0 - abs(seed)
    return vector


def _embedding_response(seed: float) -> dict:
    return {
        "data": [{"embedding": _vec(seed)}],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


async def _seed_history(session, now: datetime) -> dict[str, int]:
    """Несколько новостей с разной свежестью и эмбеддингами."""
    far_match = await news_crud.create(
        session,
        asset="BTC",
        external_id="far-match",
        url="https://example.com/far-match",
        title="far match",
        source="src",
        published_at=now - timedelta(days=7),
        embedding=_vec(1.0),
    )
    far_other = await news_crud.create(
        session,
        asset="BTC",
        external_id="far-other",
        url="https://example.com/far-other",
        title="far other",
        source="src",
        published_at=now - timedelta(days=10),
        embedding=_vec(-1.0),
    )
    fresh = await news_crud.create(
        session,
        asset="BTC",
        external_id="fresh",
        url="https://example.com/fresh",
        title="fresh",
        source="src",
        published_at=now - timedelta(hours=2),
        embedding=_vec(1.0),
    )
    other_asset = await news_crud.create(
        session,
        asset="ETH",
        external_id="other-asset",
        url="https://example.com/other-asset",
        title="other asset",
        source="src",
        published_at=now - timedelta(days=5),
        embedding=_vec(1.0),
    )
    return {
        "far_match": far_match.id,
        "far_other": far_other.id,
        "fresh": fresh.id,
        "other_asset": other_asset.id,
    }


async def test_fetch_relevant_history_returns_top_k_ordered_and_excludes_fresh(
    session,
) -> None:
    now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    ids = await _seed_history(session, now)
    fake = FakeOpenRouterClient(embedding_responses=[_embedding_response(1.0)])

    result = await fetch_relevant_history(
        session,
        fake,  # type: ignore[arg-type]
        asset="BTC",
        query_text="ETF approval bullish",
        top_k=5,
        exclude_last_hours=24,
        now=now,
    )

    returned_ids = [n.id for n in result]
    # Свежая новость и чужой актив отфильтрованы.
    assert ids["fresh"] not in returned_ids
    assert ids["other_asset"] not in returned_ids
    # Ближайший по cosine идёт первым.
    assert returned_ids[0] == ids["far_match"]
    assert ids["far_other"] in returned_ids
    # Эмбеддили один раз именно query_text.
    assert len(fake.embedding_calls) == 1
    assert fake.embedding_calls[0]["inputs"] == "ETF approval bullish"


async def test_fetch_relevant_history_uses_settings_defaults(session) -> None:
    now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    await _seed_history(session, now)
    fake = FakeOpenRouterClient(embedding_responses=[_embedding_response(1.0)])

    result = await fetch_relevant_history(
        session,
        fake,  # type: ignore[arg-type]
        asset="BTC",
        query_text="ETF approval bullish",
        now=now,
    )

    assert 0 < len(result) <= settings.trading.rag_top_k


async def test_fetch_relevant_history_short_circuits_on_empty_query(session) -> None:
    fake = FakeOpenRouterClient(embedding_responses=[])

    result = await fetch_relevant_history(
        session,
        fake,  # type: ignore[arg-type]
        asset="BTC",
        query_text="   ",
    )

    assert result == []
    assert fake.embedding_calls == []  # LLM не звали


async def test_fetch_relevant_history_propagates_embedding_dim_mismatch(session) -> None:
    bad_response = {
        "data": [{"embedding": [0.1, 0.2, 0.3]}],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }
    fake = FakeOpenRouterClient(embedding_responses=[bad_response])

    with pytest.raises(EmbeddingError):
        await fetch_relevant_history(
            session,
            fake,  # type: ignore[arg-type]
            asset="BTC",
            query_text="anything",
        )


async def test_search_by_embedding_does_not_call_llm(session) -> None:
    now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    ids = await _seed_history(session, now)

    result = await search_by_embedding(
        session,
        asset="BTC",
        embedding=_vec(1.0),
        top_k=2,
        exclude_last_hours=24,
        now=now,
    )

    returned = [n.id for n in result]
    assert returned[0] == ids["far_match"]
    assert ids["fresh"] not in returned
