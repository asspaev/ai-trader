"""Тесты CoinDesk Data News-клиента: парсинг ответа и ретраи."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.config import CoinDeskNewsSettings
from app.services.news.coindesk import (
    CoinDeskNewsClient,
    CoinDeskNewsError,
    parse_articles,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"


def _load_articles_fixture() -> dict:
    with (_FIXTURES_DIR / "coindesk_news_articles.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def _settings(max_retries: int = 3) -> CoinDeskNewsSettings:
    return CoinDeskNewsSettings(
        api_key="test-key",
        base_url="https://coindesk.test",
        language="EN",
        news_limit_per_crypto=20,
        timeout_seconds=1.0,
        max_retries=max_retries,
        retry_backoff_base=0.0,
    )


def _client_with_handler(handler, max_retries: int = 3) -> CoinDeskNewsClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="https://coindesk.test",
        transport=transport,
        headers={"Authorization": "Apikey test-key"},
    )
    return CoinDeskNewsClient(_settings(max_retries=max_retries), client=http)


async def test_parse_articles_drops_broken_entries_and_normalizes_fields() -> None:
    fixture = _load_articles_fixture()

    posts = parse_articles(fixture["Data"], asset="BTC")

    # Одну запись с битым PUBLISHED_ON выкидываем, остальные — берём
    # (включая дубликат по ID — это работа дедупликатора, не парсера).
    assert [p.external_id for p in posts] == [
        "1000001",
        "1000002",
        "1000001",
    ]

    first = posts[0]
    assert first.asset == "BTC"
    assert first.title.startswith("Spot Bitcoin ETF inflows")
    assert first.url == (
        "https://www.coindesk.com/markets/spot-bitcoin-etf-inflows-record-high"
    )
    assert first.source == "CoinDesk"
    assert first.published_at == datetime(2026, 5, 31, 14, 20, tzinfo=timezone.utc)
    assert first.raw_text is not None and first.raw_text.startswith("Spot bitcoin")

    # Второй пост — без BODY (null) → raw_text None, source = "The Block".
    second = posts[1]
    assert second.raw_text is None
    assert second.source == "The Block"


async def test_fetch_recent_returns_parsed_posts() -> None:
    fixture = _load_articles_fixture()
    seen_params: dict = {}
    seen_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/news/v1/article/list"
        seen_params.update(dict(request.url.params))
        seen_headers.update({k: v for k, v in request.headers.items()})
        return httpx.Response(200, json=fixture)

    async with _client_with_handler(handler) as client:
        posts = await client.fetch_recent("btc")

    # Параметры запроса собраны корректно.
    assert seen_params["lang"] == "EN"
    assert seen_params["categories"] == "BTC"
    assert seen_params["limit"] == "20"
    # Ключ передан в HTTP-заголовке.
    assert seen_headers.get("authorization") == "Apikey test-key"

    # В фикстуре 4 записи, одна — с битой датой.
    assert len(posts) == 3
    assert {p.external_id for p in posts} == {"1000001", "1000002"}


async def test_fetch_recent_respects_limit() -> None:
    fixture = _load_articles_fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture)

    async with _client_with_handler(handler) as client:
        posts = await client.fetch_recent("BTC", limit=1)

    assert len(posts) == 1


async def test_fetch_recent_retries_on_429_then_succeeds() -> None:
    fixture = _load_articles_fixture()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"Err": {"type": "rate_limit"}})
        return httpx.Response(200, json=fixture)

    async with _client_with_handler(handler) as client:
        posts = await client.fetch_recent("BTC")

    assert calls["n"] == 3
    assert posts  # хоть что-то распарсилось


async def test_fetch_recent_raises_on_non_retryable_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"Err": {"type": "unauthorized"}})

    async with _client_with_handler(handler) as client:
        with pytest.raises(CoinDeskNewsError) as exc_info:
            await client.fetch_recent("BTC")

    assert exc_info.value.status_code == 401


async def test_fetch_recent_returns_empty_when_data_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Err": {}})

    async with _client_with_handler(handler) as client:
        posts = await client.fetch_recent("BTC")

    assert posts == []
