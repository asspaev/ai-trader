"""Async-клиент CryptoPanic (источник новостей).

Используется только публичный REST: ``GET /v1/posts/`` с фильтром
``filter=hot&public=true`` и параметром ``currencies={ASSET}``. Возвращаем
нормализованный список :class:`NewsPost` — далее им оперируют
дедупликатор, embedding-сервис и CRUD.

Логика ретраев скопирована из :mod:`app.services.binance.client`
(ретрай 408/425/429/5xx + ``httpx.TimeoutException`` /
``httpx.TransportError`` с экспоненциальным backoff), потому что
CryptoPanic — такой же внешний публичный API без долгих сессий.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from loguru import logger

from app.config import CryptoPanicSettings, settings


_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


class CryptoPanicError(Exception):
    """Ошибка ответа CryptoPanic, не подлежащая ретраю."""

    def __init__(self, status_code: int, payload: Any) -> None:
        super().__init__(f"CryptoPanic error {status_code}: {payload!r}")
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True, slots=True)
class NewsPost:
    """Одна новость CryptoPanic в нормализованном виде.

    Соответствует подмножеству полей CryptoPanic, которые сохраняются
    в таблицу ``news`` (см. :class:`app.models.news.News`).

    Attributes:
        external_id: ``id`` поста в CryptoPanic (строкой — у API он
            числовой, но в БД храним строкой как универсальный ключ).
        asset: Тикер актива, по которому пришла новость (``BTC``…).
        title: Заголовок поста.
        url: URL первоисточника (``original_url`` если есть, иначе ``url``).
        source: Название источника, либо ``None``.
        published_at: Время публикации в UTC (tz-aware).
        raw_text: Сырое тело новости, если CryptoPanic его отдал
            (на бесплатном плане часто отсутствует — тогда ``None``).
    """

    external_id: str
    asset: str
    title: str
    url: str
    source: str | None
    published_at: datetime
    raw_text: str | None


class CryptoPanicClient:
    """Async-обёртка над публичным CryptoPanic API.

    Используется как контекстный менеджер::

        async with CryptoPanicClient() as cp:
            posts = await cp.fetch_recent("BTC")

    На каждый монет-тик делаем один HTTP-запрос — это укладывается
    в лимиты бесплатного плана (5 req/sec, 1000 req/day).
    """

    def __init__(
        self,
        config: CryptoPanicSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or settings.cryptopanic
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=httpx.Timeout(self._config.timeout_seconds),
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> "CryptoPanicClient":
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
        kind: str = "news",
        filter_: str = "hot",
    ) -> list[NewsPost]:
        """Запросить top-N постов CryptoPanic по одному активу.

        Args:
            asset: Тикер актива в формате CryptoPanic (``BTC``, ``ETH``…).
            limit: Максимум новостей. По умолчанию — из
                ``CRYPTOPANIC_NEWS_LIMIT_PER_CRYPTO``.
            kind: Параметр ``kind`` API CryptoPanic — ``news`` /
                ``media``. По умолчанию ``news``.
            filter_: Параметр ``filter`` — ``hot``, ``rising``,
                ``bullish``, ``bearish``, ``important``, ``saved`` или
                ``lol``. По умолчанию ``hot`` (соответствует
                ``architecture.md`` §7.2).

        Returns:
            Список :class:`NewsPost`, отсортированный по времени
            публикации убывающе. Ровно так, как пришло от API.
        """
        max_items = limit or self._config.news_limit_per_crypto
        params: dict[str, Any] = {
            "auth_token": self._config.api_key,
            "currencies": asset.upper(),
            "public": "true",
            "kind": kind,
            "filter": filter_,
        }
        payload = await self._get_json("/posts/", params=params)

        results = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(results, list):
            logger.bind(component="cryptopanic", asset=asset).warning(
                "CryptoPanic response has no 'results' array: {payload}",
                payload=payload,
            )
            return []

        posts = [
            post
            for post in (_parse_post(item, asset=asset) for item in results)
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
        bound = logger.bind(component="cryptopanic", path=path)

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                bound.warning(
                    "CryptoPanic transport error on attempt {attempt}/{attempts}: {exc}",
                    attempt=attempt,
                    attempts=attempts,
                    exc=type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    return _safe_json(response)

                if response.status_code in _RETRYABLE_STATUS:
                    last_exc = CryptoPanicError(
                        response.status_code, _safe_json(response)
                    )
                    bound.warning(
                        "CryptoPanic retryable status {status} on attempt {attempt}/{attempts}",
                        status=response.status_code,
                        attempt=attempt,
                        attempts=attempts,
                    )
                else:
                    raise CryptoPanicError(
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


def _parse_post(raw: Any, *, asset: str) -> NewsPost | None:
    """Превратить один post из CryptoPanic в :class:`NewsPost`.

    Возвращает ``None``, если запись повреждена (нет id/title/времени
    публикации) — такие пропускаем без падения всего тика.
    """
    if not isinstance(raw, Mapping):
        return None

    external_id = _coerce_external_id(raw.get("id"))
    title = raw.get("title")
    published_raw = raw.get("published_at") or raw.get("created_at")
    url = raw.get("original_url") or raw.get("url")

    if not external_id or not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str) or not url:
        return None
    if not isinstance(published_raw, str):
        return None

    published_at = _parse_iso8601(published_raw)
    if published_at is None:
        return None

    source = _extract_source(raw.get("source"))
    raw_text = _coerce_optional_text(raw.get("body") or raw.get("description"))

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
    """CryptoPanic отдаёт ``id`` как int — приводим к строке."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)):
        text = str(value).strip()
        return text or None
    return None


def _parse_iso8601(value: str) -> datetime | None:
    """Разобрать строку ISO-8601 (с ``Z`` или ``+00:00``)."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_source(raw: Any) -> str | None:
    """Из вложенного объекта ``source`` достать читабельный title/domain."""
    if isinstance(raw, Mapping):
        for key in ("title", "domain"):
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


def parse_posts(raw_results: Iterable[Any], *, asset: str) -> list[NewsPost]:
    """Публичный вход для парсинга массива ``results`` из ответа.

    Удобно в тестах и при работе с уже сохранённым JSON-снимком.
    """
    return [
        post
        for post in (_parse_post(item, asset=asset) for item in raw_results)
        if post is not None
    ]


__all__ = [
    "CryptoPanicClient",
    "CryptoPanicError",
    "NewsPost",
    "parse_posts",
]
