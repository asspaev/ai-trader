"""Тонкий async-клиент над публичным Binance REST API.

Назначение модуля — централизованный httpx.AsyncClient с keep-alive,
едиными таймаутами и ретраями (429 / 5xx / сетевые ошибки).

Бизнес-сервисы (``exchange_info``, ``prices``) не дергают ``httpx``
напрямую — они вызывают :class:`BinanceClient.get_json`, чтобы
повторная логика, метрики и обработка ошибок были в одном месте.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx
from loguru import logger

from app.config import BinanceSettings, settings


_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class BinanceAPIError(Exception):
    """Ошибка ответа Binance, которую не имеет смысла ретраить."""

    def __init__(self, status_code: int, payload: Any) -> None:
        super().__init__(f"Binance API error {status_code}: {payload!r}")
        self.status_code = status_code
        self.payload = payload


class BinanceClient:
    """Async-обёртка над публичным Binance API.

    Используется как контекстный менеджер::

        async with BinanceClient() as client:
            data = await client.get_json("/api/v3/exchangeInfo")

    Снаружи приложения держим один и тот же экземпляр на всё время
    жизни процесса — keep-alive переиспользует TCP-соединения.
    """

    def __init__(
        self,
        config: BinanceSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or settings.binance
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=httpx.Timeout(self._config.timeout_seconds),
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> "BinanceClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Закрыть httpx-клиент, если он создан внутри."""
        if self._owns_client:
            await self._client.aclose()

    async def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """GET с ретраями. Возвращает уже распарсенный JSON.

        Ретраит сетевые ошибки и retryable-статусы (429/5xx) с
        экспоненциальным backoff: ``base * 2**(attempt-1)``.
        4xx (кроме 408/425/429) поднимаем как :class:`BinanceAPIError`
        без ретрая — это «навсегда плохой» запрос.
        """
        attempts = max(1, self._config.max_retries)
        backoff_base = self._config.retry_backoff_base
        last_exc: Exception | None = None
        bound = logger.bind(binance_path=path)

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                bound.warning(
                    "Binance transport error on attempt {attempt}/{attempts}: {exc}",
                    attempt=attempt,
                    attempts=attempts,
                    exc=type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    return response.json()

                if response.status_code in _RETRYABLE_STATUS:
                    last_exc = BinanceAPIError(
                        response.status_code, _safe_json(response)
                    )
                    bound.warning(
                        "Binance retryable status {status} on attempt {attempt}/{attempts}",
                        status=response.status_code,
                        attempt=attempt,
                        attempts=attempts,
                    )
                else:
                    raise BinanceAPIError(response.status_code, _safe_json(response))

            if attempt < attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))

        assert last_exc is not None  # для type-checker
        raise last_exc


def _safe_json(response: httpx.Response) -> Any:
    """Попытаться распарсить JSON, иначе вернуть текст."""
    try:
        return response.json()
    except ValueError:
        return response.text


__all__ = ["BinanceClient", "BinanceAPIError"]
