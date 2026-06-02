"""Конфигурация приложения.

Разбита на несколько групп ``BaseSettings`` с отдельными префиксами в ENV.
Группы композируются в единый объект ``Settings`` (доступен как
``settings``). При добавлении новых групп — синхронно обновлять
``.env.example``.

На текущей фазе подключены группы: Database, Logging, AgentModels,
Binance, OpenRouter, CoinDeskNews, Trading, Scheduler, Telegram.
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


class CoinDeskNewsSettings(BaseSettings):
    """Параметры доступа к CoinDesk Data News API (источник новостей).

    Используем REST: ``GET /news/v1/article/list?lang=EN&categories=BTC``.
    Ключ передаётся в HTTP-заголовке ``Authorization: Apikey <KEY>``.
    ``news_limit_per_crypto`` ограничивает выборку одной монеты за
    один pipeline-тик (CoinDesk допускает до ``limit=100``; берём 20
    по умолчанию — этого достаточно для NEWS-агента).

    Историческая справка: до 2026-04-01 источником был CryptoPanic
    (free Developer API). После его закрытия мигрировали на CoinDesk
    Data API (бывший CryptoCompare).
    """

    model_config = SettingsConfigDict(
        env_prefix="COINDESK_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    api_key: str = Field(default="", description="CoinDesk Data API key")
    base_url: str = "https://data-api.coindesk.com"
    language: str = "EN"
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

    * ``mode='cron'`` — тики выполняются в каждое время из CSV
      ``cron_times`` (UTC). Формат каждого элемента — ``HH:MM``.
      Дефолт ``"00:00,06:00,12:00,18:00"`` — 4 раза в сутки.
    * ``mode='interval'`` — тик каждые ``interval_minutes`` минут.
      При ``run_on_startup=true`` дополнительно делается один тик
      сразу после старта сервиса (полезно в dev/MVP-прогонах).

    Внутренние гарантии (см. ``services/pipeline/scheduler.py``):
    ``max_instances=1``, ``coalesce=True`` — следующий тик не
    запустится, пока предыдущий не завершился, и пропущенные
    срабатывания склеиваются в один. Флаг паузы между ``/stop`` и
    ``/resume`` живёт в БД (таблица ``scheduler_state``), а не здесь
    — он не относится к статической конфигурации.

    Расписание можно перечитать на лету командой ``/reload_schedule``
    (см. :meth:`PipelineScheduler.reload`) — это пересоздаёт
    ``SchedulerSettings`` напрямую из файла ``.env``, минуя
    ``os.environ`` (см. docstring у ``reload``).
    """

    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_",
        env_file=_ENV_FILE,
        env_file_encoding=_ENV_ENCODING,
        extra="ignore",
    )

    mode: str = "cron"
    cron_times: str = "00:00,06:00,12:00,18:00"
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

    @field_validator("cron_times")
    @classmethod
    def _validate_cron_times(cls, value: str) -> str:
        """Проверить, что CSV состоит из элементов вида ``HH:MM``.

        Час должен быть 0..23, минута 0..59. Дубли и порядок не трогаем
        — APScheduler корректно отрабатывает повторы (OrTrigger просто
        фильтрует одинаковые next_run-моменты). Нормализуем формат к
        ``HH:MM`` с ведущим нулём, чтобы лог расписания был предсказуемым.
        """
        cleaned: list[str] = []
        for part in value.split(","):
            item = part.strip()
            if not item:
                continue
            if ":" not in item:
                raise ValueError(
                    "SCHEDULER_CRON_TIMES item must be in 'HH:MM' format, "
                    f"got {item!r}"
                )
            hour_str, minute_str = item.split(":", 1)
            try:
                hour = int(hour_str)
                minute = int(minute_str)
            except ValueError as exc:
                raise ValueError(
                    "SCHEDULER_CRON_TIMES item must be in 'HH:MM' format with "
                    f"integer parts, got {item!r}"
                ) from exc
            if not 0 <= hour <= 23:
                raise ValueError(
                    f"SCHEDULER_CRON_TIMES hour out of range 0..23: {hour}"
                )
            if not 0 <= minute <= 59:
                raise ValueError(
                    f"SCHEDULER_CRON_TIMES minute out of range 0..59: {minute}"
                )
            cleaned.append(f"{hour:02d}:{minute:02d}")
        if not cleaned:
            raise ValueError(
                "SCHEDULER_CRON_TIMES must contain at least one 'HH:MM' entry"
            )
        return ",".join(cleaned)

    @field_validator("interval_minutes")
    @classmethod
    def _validate_interval_minutes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                f"SCHEDULER_INTERVAL_MINUTES must be positive, got {value}"
            )
        return value

    def cron_pairs(self) -> tuple[tuple[int, int], ...]:
        """Распарсить ``cron_times`` в кортеж пар ``(hour, minute)``.

        Используется ``PipelineScheduler._build_trigger`` для сборки
        :class:`OrTrigger` из набора :class:`CronTrigger`.
        """
        pairs: list[tuple[int, int]] = []
        for item in self.cron_times.split(","):
            item = item.strip()
            if not item:
                continue
            hour_str, minute_str = item.split(":", 1)
            pairs.append((int(hour_str), int(minute_str)))
        return tuple(pairs)


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
        self.coindesk_news = CoinDeskNewsSettings()
        self.trading = TradingSettings()
        self.scheduler = SchedulerSettings()
        self.telegram = TelegramSettings()


settings = Settings()
