"""Конфигурация приложения.

Разбита на несколько групп ``BaseSettings`` с отдельными префиксами в ENV.
Группы композируются в единый объект ``Settings`` (доступен как
``settings``). При добавлении новых групп — синхронно обновлять
``.env.example``.

На текущей фазе подключены группы: Database, Logging, AgentModels,
Binance, OpenRouter, Trading. Остальные (CryptoPanic, Telegram,
Scheduler) добавляются в последующих фазах.
"""

from __future__ import annotations

from decimal import Decimal
from functools import cached_property

from pydantic import Field, field_validator
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


class BinanceSettings(BaseSettings):
    """Параметры подключения к Binance public API.

    Используется только публичный API (без ключей). ``taker_fee``
    моделирует Binance Spot taker-комиссию без BNB-скидки.
    """

    model_config = SettingsConfigDict(
        env_prefix="BINANCE_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    base_url: str = "https://api.binance.com"
    taker_fee: Decimal = Decimal("0.001")
    timeout_seconds: float = 10.0
    max_retries: int = 3
    retry_backoff_base: float = 1.0


class OpenRouterSettings(BaseSettings):
    """Параметры подключения к OpenRouter (chat + embeddings).

    Все вызовы LLM в проекте идут через OpenRouter (см.
    :mod:`app.services.llm.openrouter`). Ретрай-политика — экспоненциальный
    backoff ``base ** (attempt - 1)``: при дефолтном ``base=3`` и
    ``max_retries=4`` получаем sleep-паузы ``1s / 3s / 9s`` между четырьмя
    попытками подряд. ``timeout_seconds`` — per-request HTTP-таймаут,
    который передаётся в ``httpx.Timeout``.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENROUTER_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    api_key: str = Field(default="", description="OpenRouter API key (Bearer)")
    base_url: str = "https://openrouter.ai/api/v1"
    timeout_seconds: float = 60.0
    max_retries: int = 4
    retry_backoff_base: float = 3.0
    http_referer: str | None = Field(
        default=None,
        description="Опционально — HTTP-Referer для OpenRouter-аналитики",
    )
    app_title: str | None = Field(
        default=None,
        description="Опционально — X-Title для OpenRouter-аналитики",
    )


class TradingSettings(BaseSettings):
    """Параметры торговой стратегии (символы, стартовый капитал, лимиты)."""

    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    initial_capital_rub: Decimal = Decimal("100000")
    symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "TON"])
    quote_asset: str = "USDT"
    decisions_history_limit: int = 12
    rag_top_k: int = 5
    rag_exclude_last_hours: int = 24
    pipeline_step_timeout_seconds: int = 300

    @field_validator("symbols", mode="before")
    @classmethod
    def _split_symbols(cls, value: object) -> object:
        """Принять либо список, либо CSV-строку из ENV."""
        if isinstance(value, str):
            return [item.strip().upper() for item in value.split(",") if item.strip()]
        return value

    def pair(self, asset: str) -> str:
        """Полный тикер пары: ``BTC`` → ``BTCUSDT``."""
        return f"{asset.upper()}{self.quote_asset}"


class Settings:
    """Композитный контейнер всех групп настроек."""

    def __init__(self) -> None:
        self.db = DatabaseSettings()
        self.logging = LoggingSettings()
        self.agent = AgentModelsSettings()
        self.binance = BinanceSettings()
        self.openrouter = OpenRouterSettings()
        self.trading = TradingSettings()


settings = Settings()
