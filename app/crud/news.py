"""CRUD для модели :class:`News` (CoinDesk Data + pgvector RAG)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import News


async def create(
    session: AsyncSession,
    *,
    asset: str,
    external_id: str,
    url: str,
    title: str,
    source: str | None,
    published_at: datetime,
    raw_text: str | None = None,
    summary_text: str | None = None,
    embedding: list[float] | None = None,
) -> News:
    news = News(
        asset=asset,
        external_id=external_id,
        url=url,
        title=title,
        source=source,
        published_at=published_at,
        raw_text=raw_text,
        summary_text=summary_text,
        embedding=embedding,
    )
    session.add(news)
    await session.flush()
    return news


async def get_by_id(session: AsyncSession, news_id: int) -> News | None:
    return await session.get(News, news_id)


async def get_by_external_id(
    session: AsyncSession, *, asset: str, external_id: str
) -> News | None:
    stmt = select(News).where(
        News.asset == asset, News.external_id == external_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def exists_external_ids(
    session: AsyncSession, *, asset: str, external_ids: list[str]
) -> set[str]:
    """Вернуть подмножество ``external_ids``, уже сохранённых для актива."""
    if not external_ids:
        return set()
    stmt = select(News.external_id).where(
        News.asset == asset, News.external_id.in_(external_ids)
    )
    return {row[0] for row in (await session.execute(stmt)).all()}


async def list_recent_for_asset(
    session: AsyncSession,
    *,
    asset: str,
    since: datetime,
) -> list[News]:
    """Новости за период (``published_at >= since``)."""
    stmt = (
        select(News)
        .where(News.asset == asset, News.published_at >= since)
        .order_by(News.published_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def search_similar(
    session: AsyncSession,
    *,
    asset: str,
    embedding: list[float],
    top_k: int = 5,
    exclude_last_hours: int = 24,
    now: datetime | None = None,
) -> list[News]:
    """RAG-поиск: cosine top-K по эмбеддингу, исключая последние ``exclude_last_hours``.

    Используется оператор ``<=>`` (cosine distance) из pgvector — для
    него существует IVFFlat-индекс ``ix_news_embedding_ivfflat``.
    """
    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(hours=exclude_last_hours)
    stmt = (
        select(News)
        .where(
            News.asset == asset,
            News.published_at < cutoff,
            News.embedding.isnot(None),
        )
        .order_by(News.embedding.cosine_distance(embedding))
        .limit(top_k)
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = [
    "create",
    "get_by_id",
    "get_by_external_id",
    "exists_external_ids",
    "list_recent_for_asset",
    "search_similar",
]
