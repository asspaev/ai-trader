"""Конфигурация приложения.

Разбита на несколько групп ``BaseSettings`` с отдельными префиксами в ENV.
Группы композируются в единый объект ``Settings`` (доступен как
``settings``). При добавлении новых групп — синхронно обновлять
``.env.example``.

В фазе 0 определены только группы ``Database`` и ``Logging``; остальные
группы (Binance, OpenRouter, AgentModels, CryptoPanic, Telegram,
Scheduler, Trading) добавляются в последующих фазах.
"""

from __future__ import annotations

from functools import cached_property

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_ENV_FILE = ".env"
_ENV_ENCODING = "utf-8"


class DatabaseSettings(BaseSettings):
    """Параметры подключения к PostgreSQL (с расширением pgvector)."""

    model_config = SettingsConfigDict(
        env_prefix="DB_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    host: str = "db"
    port: int = 5432
    name: str = Field(..., description="Имя базы данных")
    user: str = Field(..., description="Пользователь БД")
    password: str = Field(..., description="Пароль БД")

    @cached_property
    def url(self) -> str:
        """Async DSN для SQLAlchemy (asyncpg-драйвер)."""
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @cached_property
    def sync_url(self) -> str:
        """Sync DSN — нужен для Alembic offline-режима и утилит."""
        return (
            f"postgresql+psycopg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class LoggingSettings(BaseSettings):
    """Параметры логирования (loguru)."""

    model_config = SettingsConfigDict(
        env_prefix="LOG_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    level: str = "INFO"
    dir: str = "./logs"


class AgentModelsSettings(BaseSettings):
    """Имена LLM-моделей для агентов и эмбеддинга.

    Размерность эмбеддинга ``embedding_dim`` фиксируется в схеме БД
    (колонка ``news.embedding``), поэтому смена эмбеддинг-модели на ту,
    что выдаёт другой dim, потребует миграции.
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    price_model: str = "deepseek/deepseek-chat"
    news_model: str = "deepseek/deepseek-chat"
    trader_model: str = "deepseek/deepseek-chat"
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dim: int = 1536


class Settings:
    """Композитный контейнер всех групп настроек."""

    def __init__(self) -> None:
        self.db = DatabaseSettings()
        self.logging = LoggingSettings()
        self.agent = AgentModelsSettings()


settings = Settings()
