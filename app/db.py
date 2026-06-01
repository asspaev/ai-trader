"""Async-движок SQLAlchemy и фабрика сессий.

Используется единый ``engine`` и ``SessionLocal`` на всё приложение.
Бизнес-код получает сессию через :func:`get_session` (FastAPI/aiogram
DI или ручной ``async with``). Любые SQL-/ORM-запросы должны вызываться
только из ``app/crud/*``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


engine: AsyncEngine = create_async_engine(
    settings.db.url,
    future=True,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Контекстная фабрика сессии БД.

    Использовать как::

        async with get_session() as session:
            ...
    """
    async with SessionLocal() as session:
        yield session


async def dispose_engine() -> None:
    """Закрыть пул соединений (вызывать при graceful shutdown)."""
    await engine.dispose()


__all__ = ["engine", "SessionLocal", "get_session", "dispose_engine"]
