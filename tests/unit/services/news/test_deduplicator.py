"""Тесты :mod:`app.services.news.deduplicator`."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.crud import news as news_crud
from app.services.news.coindesk import NewsPost
from app.services.news.deduplicator import filter_new_posts, split_seen_unseen


pytestmark = pytest.mark.asyncio(loop_scope="session")


def _post(external_id: str, *, asset: str = "BTC") -> NewsPost:
    return NewsPost(
        external_id=external_id,
        asset=asset,
        title=f"news {external_id}",
        url=f"https://example.com/{external_id}",
        source="src",
        published_at=datetime(2026, 5, 31, 12, tzinfo=timezone.utc),
        raw_text=None,
    )


async def test_filter_new_posts_returns_only_unseen(session) -> None:
    # «Старая» новость уже в БД.
    await news_crud.create(
        session,
        asset="BTC",
        external_id="old",
        url="https://example.com/old",
        title="old",
        source="src",
        published_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
    )

    posts = [_post("old"), _post("new-1"), _post("new-2")]

    fresh = await filter_new_posts(session, asset="BTC", posts=posts)

    assert [p.external_id for p in fresh] == ["new-1", "new-2"]


async def test_filter_new_posts_dedupes_within_batch(session) -> None:
    posts = [_post("a"), _post("a"), _post("b"), _post("a")]

    fresh = await filter_new_posts(session, asset="BTC", posts=posts)

    # Внутри батча тоже схлопываем дубликаты, сохраняя порядок.
    assert [p.external_id for p in fresh] == ["a", "b"]


async def test_filter_new_posts_isolates_assets(session) -> None:
    """Дубликат под другим активом не блокирует сохранение."""
    await news_crud.create(
        session,
        asset="BTC",
        external_id="shared",
        url="https://example.com/shared",
        title="shared",
        source="src",
        published_at=datetime(2026, 5, 30, tzinfo=timezone.utc),
    )

    fresh = await filter_new_posts(
        session, asset="ETH", posts=[_post("shared", asset="ETH")]
    )

    assert [p.external_id for p in fresh] == ["shared"]


async def test_filter_new_posts_empty_input(session) -> None:
    assert await filter_new_posts(session, asset="BTC", posts=[]) == []


async def test_split_seen_unseen_pure() -> None:
    posts = [_post("a"), _post("b"), _post("a"), _post("c")]

    unseen, seen = split_seen_unseen(
        posts, known_external_ids={"b"}
    )

    assert [p.external_id for p in unseen] == ["a", "c"]
    # Второе появление "a" и уже виденный "b" — оба в seen.
    assert [p.external_id for p in seen] == ["b", "a"]
