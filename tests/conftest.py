"""Общие фикстуры тестов.

Поднимаем реальный Postgres с расширением ``vector`` (образ
``pgvector/pgvector:pg16``) через ``testcontainers``. На session-scope
создаём контейнер и применяем все Alembic-миграции. Для каждого теста
выдаётся отдельная сессия, которая откатывается после теста.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Один контейнер ``pgvector/pgvector:pg16`` на всю тест-сессию.

    Переменные окружения ``DB_*`` выставляются ДО любых импортов
    ``app.config`` — pydantic-settings приоритизирует os.environ над
    значениями ``.env``-файла, поэтому ``settings.db`` подцепит
    параметры контейнера.
    """
    with PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="ai_trader",
        password="ai_trader",
        dbname="ai_trader_test",
        driver="asyncpg",
    ) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        os.environ["DB_HOST"] = host
        os.environ["DB_PORT"] = str(port)
        os.environ["DB_NAME"] = "ai_trader_test"
        os.environ["DB_USER"] = "ai_trader"
        os.environ["DB_PASSWORD"] = "ai_trader"
        yield container


@pytest.fixture(scope="session")
def _database_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return (
        f"postgresql+asyncpg://ai_trader:ai_trader@{host}:{port}/ai_trader_test"
    )


@pytest.fixture(scope="session")
def _alembic_upgraded(postgres_container: PostgresContainer) -> None:
    """Применяем все миграции один раз на session.

    На момент сбора pytest ``app.config.settings.db`` уже мог быть
    инициализирован значениями из ``.env`` (когда коллекция тестов
    импортировала ``app.crud``). Поэтому до запуска alembic явно
    пересоздаём ``DatabaseSettings`` — pydantic-settings прочитает
    свежие ``DB_*`` env vars, выставленные ``postgres_container``.
    """
    from alembic import command
    from alembic.config import Config

    from app.config import DatabaseSettings, settings

    settings.db = DatabaseSettings()  # type: ignore[assignment]

    config = Config("alembic.ini")
    config.set_main_option("script_location", "app/alembic")
    command.upgrade(config, "head")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(
    _database_url: str, _alembic_upgraded: None
) -> AsyncIterator[AsyncEngine]:
    """Async-движок для тестов (один на сессию)."""
    engine = create_async_engine(_database_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Чистая сессия для одного теста.

    Перед каждым тестом TRUNCATE всех таблиц — гарантирует
    детерминированную выборку. После теста — rollback всего, что
    осталось в открытой транзакции.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE TABLE llm_calls, news, transactions, decisions, "
            "wallets, users RESTART IDENTITY CASCADE"
        )

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
