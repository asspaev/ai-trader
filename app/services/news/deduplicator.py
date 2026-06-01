"""Дедупликация новостей по ``external_id`` через CRUD.

Используется на шаге pipeline NEWS-ветки до вызова LLM/embedding —
повторно процессить уже сохранённую новость дорого и бессмысленно.
Учитываем дубликаты как уже сохранённых в БД, так и пришедших в одной
выдаче (одна и та же новость иногда дублируется в разных категориях
news-провайдера).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import news as news_crud
from app.services.news.coindesk import NewsPost


async def filter_new_posts(
    session: AsyncSession,
    *,
    asset: str,
    posts: Iterable[NewsPost],
) -> list[NewsPost]:
    """Оставить только те ``posts``, которых ещё нет в БД для актива.

    Args:
        session: Активная async-сессия БД (read-only).
        asset: Тикер актива (фильтр на стороне CRUD по
            ``UNIQUE(asset, external_id)``).
        posts: Итерируемый набор :class:`NewsPost`, обычно прямо из
            :meth:`CoinDeskNewsClient.fetch_recent`.

    Returns:
        Список новых постов в исходном порядке. Внутри батча
        дубликаты по ``external_id`` тоже выкидываем.
    """
    posts_list = list(posts)
    if not posts_list:
        return []

    batch_seen: set[str] = set()
    unique_in_batch: list[NewsPost] = []
    for post in posts_list:
        if post.external_id in batch_seen:
            continue
        batch_seen.add(post.external_id)
        unique_in_batch.append(post)

    existing = await news_crud.exists_external_ids(
        session,
        asset=asset,
        external_ids=[p.external_id for p in unique_in_batch],
    )

    return [post for post in unique_in_batch if post.external_id not in existing]


def split_seen_unseen(
    posts: Sequence[NewsPost],
    *,
    known_external_ids: set[str],
) -> tuple[list[NewsPost], list[NewsPost]]:
    """Разделить ``posts`` на (новые, уже виденные).

    Чистая функция — её удобно использовать в местах, где список
    известных id уже в памяти (например, после одного SELECT по всем
    активам сразу). В pipeline-шаге сейчас не вызывается, но входит
    в публичный API модуля для будущих оптимизаций.
    """
    unseen: list[NewsPost] = []
    seen: list[NewsPost] = []
    batch_seen: set[str] = set()
    for post in posts:
        if post.external_id in batch_seen:
            seen.append(post)
            continue
        batch_seen.add(post.external_id)
        if post.external_id in known_external_ids:
            seen.append(post)
        else:
            unseen.append(post)
    return unseen, seen


__all__ = ["filter_new_posts", "split_seen_unseen"]
