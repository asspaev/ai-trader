"""Тесты ретраев и обработки ошибок ``BinanceClient``."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.config import BinanceSettings
from app.services.binance.client import BinanceAPIError, BinanceClient


pytestmark = pytest.mark.asyncio(loop_scope="session")


def _settings(max_retries: int = 3) -> BinanceSettings:
    return BinanceSettings(
        base_url="https://example.test",
        taker_fee=Decimal("0.001"),
        timeout_seconds=1.0,
        max_retries=max_retries,
        retry_backoff_base=0.0,  # без задержек в тестах
    )


def _client_with_handler(handler, max_retries: int = 3) -> BinanceClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="https://example.test", transport=transport)
    return BinanceClient(_settings(max_retries=max_retries), client=http)


async def test_get_json_returns_parsed_payload_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with _client_with_handler(handler) as client:
        payload = await client.get_json("/api/v3/exchangeInfo")
    assert payload == {"ok": True}


async def test_get_json_retries_on_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"msg": "rate limit"})
        return httpx.Response(200, json={"ok": True})

    async with _client_with_handler(handler) as client:
        payload = await client.get_json("/api/v3/klines")
    assert payload == {"ok": True}
    assert calls["n"] == 3


async def test_get_json_raises_after_exhausting_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"msg": "boom"})

    async with _client_with_handler(handler, max_retries=2) as client:
        with pytest.raises(BinanceAPIError) as exc_info:
            await client.get_json("/api/v3/klines")
    assert exc_info.value.status_code == 503


async def test_get_json_does_not_retry_on_non_retryable_4xx() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"msg": "bad symbol"})

    async with _client_with_handler(handler) as client:
        with pytest.raises(BinanceAPIError) as exc_info:
            await client.get_json("/api/v3/klines")
    assert exc_info.value.status_code == 400
    assert calls["n"] == 1


async def test_get_json_retries_on_timeout() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectTimeout("simulated timeout", request=request)
        return httpx.Response(200, json={"ok": True})

    async with _client_with_handler(handler) as client:
        payload = await client.get_json("/api/v3/klines")
    assert payload == {"ok": True}
    assert calls["n"] == 2
