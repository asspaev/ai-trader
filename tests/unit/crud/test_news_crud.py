"""CRUD-тесты для :mod:`app.crud.news` (включая RAG поверх pgvector)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.crud import news as news_crud


pytestmark = pytest.mark.asyncio(loop_scope="session")


_DIM = settings.agent.embedding_dim


def _vec(seed: float) -> list[float]:
    """Простой детерминированный вектор размерности ``embedding_dim``.

    Первое значение задаёт ось — используется в тестах ближайшего соседа.
    """
    vector = [0.0] * _DIM
    vector[0] = seed
    if _DIM > 1:
        vector[1] = 1.0 - abs(seed)
    return vector


async def test_create_and_get_by_external_id(session):
    published_at = datetime(2026, 5, 30, 12, tzinfo=timezone.utc)

    created = await news_crud.create(
        session,
        asset="BTC",
        external_id="cp-1",
        url="https://example.com/1",
        title="ETF approved",
        source="CryptoPanic",
        published_at=published_at,
        raw_text="raw",
        summary_text="summary",
        embedding=_vec(0.5),
    )

    fetched = await news_crud.get_by_external_id(
        session, asset="BTC", external_id="cp-1"
    )

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.title == "ETF approved"
    assert fetched.embedding is not None
    assert len(fetched.embedding) == _DIM


async def test_exists_external_ids_subset(session):
    base_time = datetime(2026, 5, 30, tzinfo=timezone.utc)
    for idx in (1, 2):
        await news_crud.create(
            session,
            asset="ETH",
            external_id=f"id-{idx}",
            url=f"https://example.com/eth-{idx}",
            title=f"news-{idx}",
            source="src",
            published_at=base_time,
        )

    found = await news_crud.exists_external_ids(
        session, asset="ETH", external_ids=["id-1", "id-2", "id-3"]
    )

    assert found == {"id-1", "id-2"}


async def test_list_recent_for_asset_filters_by_time(session):
    now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)

    await news_crud.create(
        session,
        asset="BTC",
        external_id="old",
        url="https://example.com/old",
        title="old",
        source="src",
        published_at=now - timedelta(days=3),
    )
    fresh = await news_crud.create(
        session,
        asset="BTC",
        external_id="fresh",
        url="https://example.com/fresh",
        title="fresh",
        source="src",
        published_at=now - timedelta(hours=1),
    )

    recent = await news_crud.list_recent_for_asset(
        session, asset="BTC", since=now - timedelta(hours=24)
    )

    assert [n.id for n in recent] == [fresh.id]


async def test_search_similar_excludes_last_24h_and_orders_by_similarity(
    session
):
    now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)

    # «Старые» новости, которые могут быть кандидатами в RAG.
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

    # Свежая новость с очень близким эмбеддингом — должна быть исключена.
    await news_crud.create(
        session,
        asset="BTC",
        external_id="fresh",
        url="https://example.com/fresh",
        title="fresh",
        source="src",
        published_at=now - timedelta(hours=2),
        embedding=_vec(1.0),
    )

    # Чужой актив — не попадает даже при близости.
    await news_crud.create(
        session,
        asset="ETH",
        external_id="other-asset",
        url="https://example.com/other-asset",
        title="other asset",
        source="src",
        published_at=now - timedelta(days=5),
        embedding=_vec(1.0),
    )

    query = _vec(1.0)
    result = await news_crud.search_similar(
        session,
        asset="BTC",
        embedding=query,
        top_k=5,
        exclude_last_hours=24,
        now=now,
    )

    ids = [n.id for n in result]
    assert far_match.id in ids
    assert far_other.id in ids
    # ближайший (по cosine) идёт первым
    assert ids[0] == far_match.id
