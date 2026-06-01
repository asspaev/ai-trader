"""ORM-модель новости с вектором эмбеддинга (pgvector)."""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.models.base import Base


class News(Base):
    """Новость из CoinDesk Data + её LLM-summary + эмбеддинг.

    Эмбеддинг считается над текстом ``title + " " + summary_text``.
    Размерность задаётся через настройку ``AGENT_EMBEDDING_DIM`` (по
    умолчанию 1536 — ``openai/text-embedding-3-small``).
    """

    __tablename__ = "news"
    __table_args__ = (
        # URL глобально НЕ уникален: CoinDesk Data отдаёт одну и ту же
        # статью при запросе по разным categories=BTC|ETH|TON, и мы
        # храним её отдельной строкой на каждый затронутый asset
        # (per-asset summary + embedding, RAG-поиск тоже per-asset).
        UniqueConstraint("asset", "external_id", name="uq_news_asset_external_id"),
        Index("ix_news_asset_published_at", "asset", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sentiment, который NEWS-агент присвоил при суммаризации.
    # Кэшируем, чтобы при появлении этой же статьи под другим активом
    # переиспользовать готовый summary без повторного LLM-вызова.
    # Значения соответствуют ``app.services.agents.base.Sentiment``
    # (``bullish`` / ``bearish`` / ``neutral``); ``NULL`` — для строк,
    # созданных до миграции 0005 или сохранённых через CRUD без sentiment.
    summary_sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.agent.embedding_dim), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = ["News"]
