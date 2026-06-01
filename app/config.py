"""Конфигурация приложения.

Разбита на несколько групп ``BaseSettings`` с отдельными префиксами в ENV.
Группы композируются в единый объект ``Settings`` (доступен как
``settings``). При добавлении новых групп — синхронно обновлять
``.env.example``.

На текущей фазе подключены группы: Database, Logging, AgentModels,
Binance, OpenRouter, CryptoPanic, Trading, Scheduler, Telegram.
"""

from __future__ import annotations

from decimal import Decimal
from functools import cached_property
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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


class CryptoPanicSettings(BaseSettings):
    """Параметры доступа к CryptoPanic (источник новостей).

    Используем публичный REST: ``GET /v1/posts/?auth_token=...``.
    ``news_limit_per_crypto`` ограничивает выборку одной монеты за
    один pipeline-тик (CryptoPanic отдаёт максимум ~20 на странице
    бесплатного плана; берём столько же).
    """

    model_config = SettingsConfigDict(
        env_prefix="CRYPTOPANIC_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    api_key: str = Field(default="", description="CryptoPanic auth_token")
    base_url: str = "https://cryptopanic.com/api/v1"
    news_limit_per_crypto: int = 20
    timeout_seconds: float = 15.0
    max_retries: int = 3
    retry_backoff_base: float = 1.0


class TradingSettings(BaseSettings):
    """Параметры торговой стратегии (символы, стартовый капитал, лимиты)."""

    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    initial_capital_rub: Decimal = Decimal("100000")
    # NoDecode — отключаем авто-JSON-парсинг для ENV-значения, чтобы
    # ``TRADING_SYMBOLS=BTC,ETH,TON`` (CSV) не падал на ``json.loads``.
    # CSV-парсинг выполняет валидатор ``_split_symbols`` ниже.
    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTC", "ETH", "TON"]
    )
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


class SchedulerSettings(BaseSettings):
    """Параметры APScheduler.

    * ``mode='cron'`` — тики выполняются на каждый час из CSV
      ``cron_hours`` (UTC). Дефолт ``"0,6,12,18"`` — 4 раза в сутки,
      как описано в ``architecture.md`` §10.
    * ``mode='interval'`` — тик каждые ``interval_minutes`` минут.
      При ``run_on_startup=true`` дополнительно делается один тик
      сразу после старта сервиса (полезно в dev/MVP-прогонах).

    Внутренние гарантии (см. ``services/pipeline/scheduler.py``):
    ``max_instances=1``, ``coalesce=True`` — следующий тик не
    запустится, пока предыдущий не завершился, и пропущенные
    срабатывания склеиваются в один. Флаг паузы между ``/stop`` и
    ``/resume`` живёт в БД (таблица ``scheduler_state``), а не здесь
    — он не относится к статической конфигурации.
    """

    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    mode: str = "cron"
    cron_hours: str = "0,6,12,18"
    interval_minutes: int = 30
    run_on_startup: bool = True

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> object:
        """Принять регистр в любом виде и нормализовать к lower-case."""
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized not in {"cron", "interval"}:
                raise ValueError(
                    f"SCHEDULER_MODE must be 'cron' or 'interval', got {value!r}"
                )
            return normalized
        return value

    @field_validator("cron_hours")
    @classmethod
    def _validate_cron_hours(cls, value: str) -> str:
        """Проверить, что CSV состоит из чисел 0..23."""
        cleaned: list[str] = []
        for part in value.split(","):
            item = part.strip()
            if not item:
                continue
            try:
                hour = int(item)
            except ValueError as exc:
                raise ValueError(
                    f"SCHEDULER_CRON_HOURS contains non-integer item: {item!r}"
                ) from exc
            if not 0 <= hour <= 23:
                raise ValueError(
                    f"SCHEDULER_CRON_HOURS hour out of range 0..23: {hour}"
                )
            cleaned.append(str(hour))
        if not cleaned:
            raise ValueError("SCHEDULER_CRON_HOURS must contain at least one hour")
        return ",".join(cleaned)

    @field_validator("interval_minutes")
    @classmethod
    def _validate_interval_minutes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                f"SCHEDULER_INTERVAL_MINUTES must be positive, got {value}"
            )
        return value


class TelegramSettings(BaseSettings):
    """Параметры Telegram-бота (aiogram 3).

    Используется единственный bot-token, авторизация — по
    ``telegram_id`` пользователя из таблицы ``users`` (см.
    ``app/services/telegram/handlers.py``). Лимиты ``history_limit_*``
    защищают команду ``/history N`` от запроса абсурдных N — пользователь
    может ввести любое число, но фактически берётся ``min(N, max)``.
    """

    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    bot_token: str = Field(
        default="",
        description="Telegram Bot API token (BotFather)",
    )
    history_limit_default: int = 10
    history_limit_max: int = 50

    @field_validator("history_limit_default", "history_limit_max")
    @classmethod
    def _validate_history_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                f"TELEGRAM history limits must be positive, got {value}"
            )
        return value


class Settings:
    """Композитный контейнер всех групп настроек."""

    def __init__(self) -> None:
        self.db = DatabaseSettings()
        self.logging = LoggingSettings()
        self.agent = AgentModelsSettings()
        self.binance = BinanceSettings()
        self.openrouter = OpenRouterSettings()
        self.cryptopanic = CryptoPanicSettings()
        self.trading = TradingSettings()
        self.scheduler = SchedulerSettings()
        self.telegram = TelegramSettings()


settings = Settings()
