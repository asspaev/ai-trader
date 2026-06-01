"""Тесты CryptoPanic-клиента: парсинг ответа и ретраи."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.config import CryptoPanicSettings
from app.services.news.cryptopanic import (
    CryptoPanicClient,
    CryptoPanicError,
    parse_posts,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"


def _load_posts_fixture() -> dict:
    with (_FIXTURES_DIR / "cryptopanic_posts.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def _settings(max_retries: int = 3) -> CryptoPanicSettings:
    return CryptoPanicSettings(
        api_key="test-key",
        base_url="https://cryptopanic.test/api/v1",
        news_limit_per_crypto=20,
        timeout_seconds=1.0,
        max_retries=max_retries,
        retry_backoff_base=0.0,
    )


def _client_with_handler(handler, max_retries: int = 3) -> CryptoPanicClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="https://cryptopanic.test/api/v1", transport=transport
    )
    return CryptoPanicClient(_settings(max_retries=max_retries), client=http)


async def test_parse_posts_drops_broken_entries_and_normalizes_fields() -> None:
    fixture = _load_posts_fixture()

    posts = parse_posts(fixture["results"], asset="BTC")

    # Один пост с битой датой выкидываем, остальные — берём.
    assert [p.external_id for p in posts] == [
        "1000001",
        "1000002",
        "1000001",
    ]

    first = posts[0]
    assert first.asset == "BTC"
    assert first.title.startswith("Spot Bitcoin ETF inflows")
    assert first.url == (
        "https://cointelegraph.com/news/spot-bitcoin-etf-inflows-record-high"
    )
    assert first.source == "Cointelegraph"
    assert first.published_at == datetime(2026, 5, 31, 14, 20, tzinfo=timezone.utc)
    assert first.raw_text is None

    no_source = posts[-1]
    assert no_source.source == "CoinDesk"


async def test_fetch_recent_returns_parsed_posts() -> None:
    fixture = _load_posts_fixture()
    seen_params: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/posts/")
        seen_params.update(dict(request.url.params))
        return httpx.Response(200, json=fixture)

    async with _client_with_handler(handler) as client:
        posts = await client.fetch_recent("btc")

    # Параметры запроса собраны корректно.
    assert seen_params["auth_token"] == "test-key"
    assert seen_params["currencies"] == "BTC"
    assert seen_params["public"] == "true"
    assert seen_params["filter"] == "hot"
    assert seen_params["kind"] == "news"

    # В фикстуре 4 записи, одна — невалидная.
    assert len(posts) == 3
    assert {p.external_id for p in posts} == {"1000001", "1000002"}


async def test_fetch_recent_respects_limit() -> None:
    fixture = _load_posts_fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture)

    async with _client_with_handler(handler) as client:
        posts = await client.fetch_recent("BTC", limit=1)

    assert len(posts) == 1


async def test_fetch_recent_retries_on_429_then_succeeds() -> None:
    fixture = _load_posts_fixture()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"detail": "rate limit"})
        return httpx.Response(200, json=fixture)

    async with _client_with_handler(handler) as client:
        posts = await client.fetch_recent("BTC")

    assert calls["n"] == 3
    assert posts  # хоть что-то распарсилось


async def test_fetch_recent_raises_on_non_retryable_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid token"})

    async with _client_with_handler(handler) as client:
        with pytest.raises(CryptoPanicError) as exc_info:
            await client.fetch_recent("BTC")

    assert exc_info.value.status_code == 401


async def test_fetch_recent_returns_empty_when_results_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0})

    async with _client_with_handler(handler) as client:
        posts = await client.fetch_recent("BTC")

    assert posts == []
