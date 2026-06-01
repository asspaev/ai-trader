"""Сохранение новости вместе с её эмбеддингом.

Эмбеддим строку ``title + " " + summary_text`` (NEWS-summary к этому
моменту уже сгенерирован LLM-агентом — см. фаза 6). Если summary
по какой-то причине пусто — эмбеддим один title, чтобы новость всё
равно попала в RAG-индекс.

Сохранение делается одной транзакцией: либо успешно записаны и поля,
и вектор, либо ничего. Это важно, чтобы из БД нельзя было «вытащить»
новость без эмбеддинга и потом сломать RAG-поиск (запрос фильтрует
``embedding IS NOT NULL``, но всё равно лишняя строка-сирота — мусор).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import news as news_crud
from app.models import News
from app.services.llm.embeddings import create_embedding
from app.services.llm.openrouter import OpenRouterClient
from app.services.news.cryptopanic import NewsPost


def build_embedding_text(*, title: str, summary_text: str | None) -> str:
    """Собрать строку, которую отдаём в эмбеддинг-модель.

    Если ``summary_text`` пуст / ``None`` — берём только title.
    Соглашение зафиксировано в ``architecture.md`` §8.3.
    """
    title_clean = (title or "").strip()
    summary_clean = (summary_text or "").strip()
    if not summary_clean:
        return title_clean
    return f"{title_clean} {summary_clean}".strip()


async def save_news_with_embedding(
    session: AsyncSession,
    llm_client: OpenRouterClient,
    *,
    post: NewsPost,
    summary_text: str | None,
    pipeline_run_id: uuid.UUID | None = None,
) -> News:
    """Получить эмбеддинг и записать новость в БД одной транзакцией.

    Args:
        session: Активная async-сессия БД. Коммит — на вызывающем
            (мы делаем только ``flush``).
        llm_client: Уже открытый :class:`OpenRouterClient` для вызова
            ``/embeddings`` (запись в ``llm_calls`` обеспечивает он).
        post: Распарсенный :class:`NewsPost` из CryptoPanic.
        summary_text: NEWS-summary, сгенерированный LLM. Может быть
            ``None``/пустой — тогда эмбеддим только заголовок и
            ``summary_text`` в БД останется ``None``.
        pipeline_run_id: Идентификатор pipeline-тика; пробрасывается
            в ``llm_calls.pipeline_run_id``.

    Returns:
        Созданная сущность :class:`News` (уже flushed, есть id).
    """
    text = build_embedding_text(title=post.title, summary_text=summary_text)
    embedding = await create_embedding(
        llm_client,
        text=text,
        pipeline_run_id=pipeline_run_id,
    )

    return await news_crud.create(
        session,
        asset=post.asset,
        external_id=post.external_id,
        url=post.url,
        title=post.title,
        source=post.source,
        published_at=_ensure_aware(post.published_at),
        raw_text=post.raw_text,
        summary_text=(summary_text.strip() if summary_text else None),
        embedding=embedding,
    )


def _ensure_aware(value: datetime) -> datetime:
    """Гарантия tz-aware: схема БД требует ``timestamptz``.

    CryptoPanic отдаёт уже tz-aware значения, но если в тестах кто-то
    подсунет naive — лучше упасть тут с явной ошибкой, чем получить
    SQL-исключение из глубины драйвера.
    """
    if value.tzinfo is None:
        raise ValueError(
            "NewsPost.published_at must be timezone-aware (got naive datetime)"
        )
    return value


__all__ = ["build_embedding_text", "save_news_with_embedding"]
