"""Тесты :mod:`app.services.news.storage` (сохранение новости + эмбеддинг)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services.news.cryptopanic import NewsPost
from app.services.news.storage import (
    build_embedding_text,
    save_news_with_embedding,
)

from tests.unit.services.llm._helpers import FakeOpenRouterClient


pytestmark = pytest.mark.asyncio(loop_scope="session")


_DIM = settings.agent.embedding_dim


def _embedding_response(seed: float = 0.42) -> dict:
    """Сформировать /embeddings-ответ нужной размерности."""
    vector = [0.0] * _DIM
    vector[0] = seed
    if _DIM > 1:
        vector[1] = 1.0 - abs(seed)
    return {
        "data": [{"embedding": vector}],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


def _post() -> NewsPost:
    return NewsPost(
        external_id="cp-1",
        asset="BTC",
        title="ETF approved",
        url="https://example.com/etf-approved",
        source="CryptoPanic",
        published_at=datetime(2026, 5, 31, 12, tzinfo=timezone.utc),
        raw_text=None,
    )


async def test_build_embedding_text_with_summary() -> None:
    assert (
        build_embedding_text(title="ETF approved", summary_text="Bullish for BTC")
        == "ETF approved Bullish for BTC"
    )


async def test_build_embedding_text_without_summary_falls_back_to_title() -> None:
    assert build_embedding_text(title="ETF approved", summary_text=None) == "ETF approved"
    assert build_embedding_text(title="ETF approved", summary_text="   ") == "ETF approved"


async def test_save_news_with_embedding_persists_vector(session) -> None:
    fake = FakeOpenRouterClient(embedding_responses=[_embedding_response(0.5)])

    saved = await save_news_with_embedding(
        session,
        fake,  # type: ignore[arg-type]
        post=_post(),
        summary_text="Bullish for BTC after Fed pause",
    )
    await session.flush()

    assert saved.id is not None
    assert saved.summary_text == "Bullish for BTC after Fed pause"
    assert saved.embedding is not None
    assert len(saved.embedding) == _DIM

    # Эмбеддингу скормили объединённый текст.
    assert len(fake.embedding_calls) == 1
    assert fake.embedding_calls[0]["inputs"] == (
        "ETF approved Bullish for BTC after Fed pause"
    )


async def test_save_news_with_embedding_falls_back_to_title_only(session) -> None:
    fake = FakeOpenRouterClient(embedding_responses=[_embedding_response(0.1)])

    saved = await save_news_with_embedding(
        session,
        fake,  # type: ignore[arg-type]
        post=_post(),
        summary_text=None,
    )

    assert saved.summary_text is None
    assert fake.embedding_calls[0]["inputs"] == "ETF approved"


async def test_save_news_with_embedding_rejects_naive_datetime(session) -> None:
    fake = FakeOpenRouterClient(embedding_responses=[_embedding_response(0.1)])
    naive_post = NewsPost(
        external_id="cp-naive",
        asset="BTC",
        title="x",
        url="https://example.com/x",
        source=None,
        published_at=datetime(2026, 5, 31, 12),  # без tz
        raw_text=None,
    )

    with pytest.raises(ValueError):
        await save_news_with_embedding(
            session,
            fake,  # type: ignore[arg-type]
            post=naive_post,
            summary_text="x",
        )
