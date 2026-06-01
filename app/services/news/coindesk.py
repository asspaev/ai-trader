"""Async-клиент CoinDesk Data News API (источник новостей).

Используется REST: ``GET /news/v1/article/list`` с фильтром по языку
(``lang=EN``), по тикеру актива (``categories=BTC``) и лимитом
``limit=N``. Ключ передаётся в HTTP-заголовке
``Authorization: Apikey <KEY>``.

Возвращаем нормализованный список :class:`NewsPost` — далее им оперируют
дедупликатор, embedding-сервис и CRUD. Контракт ``NewsPost`` совпадает
с тем, который раньше отдавал CryptoPanic-клиент (мигрировали после
закрытия CryptoPanic free-плана 2026-04-01).

Логика ретраев скопирована из :mod:`app.services.binance.client`
(ретрай 408/425/429/5xx + ``httpx.TimeoutException`` /
``httpx.TransportError`` с экспоненциальным backoff), потому что
CoinDesk Data — такой же внешний публичный API без долгих сессий.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from app.config import CoinDeskNewsSettings, settings


_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


class CoinDeskNewsError(Exception):
    """Ошибка ответа CoinDesk Data News API, не подлежащая ретраю."""

    def __init__(self, status_code: int, payload: Any) -> None:
        super().__init__(f"CoinDesk news error {status_code}: {payload!r}")
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True, slots=True)
class NewsPost:
    """Одна новость в нормализованном виде, source-agnostic.

    Соответствует подмножеству полей, которые сохраняются в таблицу
    ``news`` (см. :class:`app.models.news.News`). Источник может быть
    любой — конкретный клиент (сейчас :class:`CoinDeskNewsClient`)
    парсит сырой ответ в эту структуру.

    Attributes:
        external_id: Идентификатор поста у источника (хранится строкой,
            чтобы быть устойчивым к смене типа у разных провайдеров).
        asset: Тикер актива, по которому пришла новость (``BTC``…).
        title: Заголовок поста.
        url: URL первоисточника.
        source: Название издания (например, ``CoinDesk``), либо ``None``.
        published_at: Время публикации в UTC (tz-aware).
        raw_text: Сырое тело новости, если источник его отдал.
    """

    external_id: str
    asset: str
    title: str
    url: str
    source: str | None
    published_at: datetime
    raw_text: str | None


class CoinDeskNewsClient:
    """Async-обёртка над CoinDesk Data News API.

    Используется как контекстный менеджер::

        async with CoinDeskNewsClient() as cd:
            posts = await cd.fetch_recent("BTC")

    На каждую монету в pipeline-тике делаем один HTTP-запрос — это
    укладывается в free-план CoinDesk Data (~3000 calls/месяц).
    """

    def __init__(
        self,
        config: CoinDeskNewsSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or settings.coindesk_news
        self._owns_client = client is None
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Apikey {self._config.api_key}"
        self._client = client or httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=httpx.Timeout(self._config.timeout_seconds),
            headers=headers,
        )

    async def __aenter__(self) -> "CoinDeskNewsClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_recent(
        self,
        asset: str,
        *,
        limit: int | None = None,
    ) -> list[NewsPost]:
        """Запросить top-N статей CoinDesk Data по одному активу.

        Args:
            asset: Тикер актива (``BTC``, ``ETH``, ``TON``). Передаётся
                в параметр ``categories`` API без изменений (только
                upper-case).
            limit: Максимум статей. По умолчанию — из
                ``COINDESK_NEWS_LIMIT_PER_CRYPTO``. CoinDesk допускает
                ``limit`` до 100.

        Returns:
            Список :class:`NewsPost` в порядке, отданном API
            (обычно — свежие сверху).
        """
        max_items = limit or self._config.news_limit_per_crypto
        params: dict[str, Any] = {
            "lang": self._config.language,
            "categories": asset.upper(),
            "limit": max_items,
        }
        payload = await self._get_json("/news/v1/article/list", params=params)

        data = payload.get("Data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            logger.bind(component="coindesk_news", asset=asset).warning(
                "CoinDesk response has no 'Data' array: {payload}",
                payload=payload,
            )
            return []

        posts = [
            post
            for post in (_parse_article(item, asset=asset) for item in data)
            if post is not None
        ]
        return posts[:max_items]

    # ---------- internals ----------

    async def _get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """GET с теми же ретрай-правилами, что у Binance-клиента."""
        attempts = max(1, self._config.max_retries)
        backoff_base = float(self._config.retry_backoff_base)
        last_exc: Exception | None = None
        bound = logger.bind(component="coindesk_news", path=path)

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                bound.warning(
                    "CoinDesk transport error on attempt {attempt}/{attempts}: {exc}",
                    attempt=attempt,
                    attempts=attempts,
                    exc=type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    return _safe_json(response)

                if response.status_code in _RETRYABLE_STATUS:
                    last_exc = CoinDeskNewsError(
                        response.status_code, _safe_json(response)
                    )
                    bound.warning(
                        "CoinDesk retryable status {status} on attempt {attempt}/{attempts}",
                        status=response.status_code,
                        attempt=attempt,
                        attempts=attempts,
                    )
                else:
                    raise CoinDeskNewsError(
                        response.status_code, _safe_json(response)
                    )

            if attempt < attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))

        assert last_exc is not None  # для type-checker
        raise last_exc


# ---------- module-level helpers ----------


def _safe_json(response: httpx.Response) -> Any:
    """Распарсить JSON, либо вернуть текст ответа."""
    try:
        return response.json()
    except ValueError:
        return response.text


def _parse_article(raw: Any, *, asset: str) -> NewsPost | None:
    """Превратить одну статью CoinDesk в :class:`NewsPost`.

    Возвращает ``None``, если запись повреждена (нет id/title/времени
    публикации/URL) — такие пропускаем без падения всего тика.

    Поля CoinDesk Data News API:

    * ``ID`` — целочисленный id статьи (приводим к str).
    * ``GUID`` — fallback на случай отсутствия ``ID``.
    * ``TITLE``, ``URL``, ``BODY`` — заголовок/ссылка/тело.
    * ``PUBLISHED_ON`` — Unix-timestamp (секунды) в UTC.
    * ``SOURCE_DATA.NAME`` — название издания.
    """
    if not isinstance(raw, Mapping):
        return None

    external_id = _coerce_external_id(raw.get("ID") or raw.get("GUID"))
    title = raw.get("TITLE")
    url = raw.get("URL")
    published_at = _parse_unix_timestamp(raw.get("PUBLISHED_ON"))

    if not external_id or not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str) or not url:
        return None
    if published_at is None:
        return None

    source = _extract_source(raw.get("SOURCE_DATA"))
    raw_text = _coerce_optional_text(raw.get("BODY"))

    return NewsPost(
        external_id=external_id,
        asset=asset.upper(),
        title=title.strip(),
        url=url,
        source=source,
        published_at=published_at,
        raw_text=raw_text,
    )


def _coerce_external_id(value: Any) -> str | None:
    """CoinDesk отдаёт ``ID`` как int — приводим к строке."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)):
        text = str(value).strip()
        return text or None
    return None


def _parse_unix_timestamp(value: Any) -> datetime | None:
    """``PUBLISHED_ON`` приходит как Unix-timestamp в секундах."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        except ValueError:
            return None
    return None


def _extract_source(raw: Any) -> str | None:
    """Из вложенного ``SOURCE_DATA`` достать читабельное имя."""
    if isinstance(raw, Mapping):
        for key in ("NAME", "SOURCE_KEY"):
            candidate = raw.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[:128]
    return None


def _coerce_optional_text(value: Any) -> str | None:
    """Привести опциональное текстовое поле к ``str`` или ``None``."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def parse_articles(raw_articles: Iterable[Any], *, asset: str) -> list[NewsPost]:
    """Публичный вход для парсинга массива ``Data`` из ответа.

    Удобно в тестах и при работе с уже сохранённым JSON-снимком.
    """
    return [
        post
        for post in (_parse_article(item, asset=asset) for item in raw_articles)
        if post is not None
    ]


__all__ = [
    "CoinDeskNewsClient",
    "CoinDeskNewsError",
    "NewsPost",
    "parse_articles",
]
