"""Тесты сервиса :mod:`app.services.fx` (курс USDT/RUB).

Сетевые походы моделируются через ``httpx.MockTransport``: для Binance
прокидываем фейковый транспорт в его ``AsyncClient``, для CoinGecko —
передаём готовый клиент в ``fetch_usdt_rub_rate``.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.config import BinanceSettings
from app.services.binance.client import BinanceClient
from app.services.fx import (
    COINGECKO_URL,
    FxRate,
    FxRateError,
    fetch_usdt_rub_rate,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


def _binance_settings() -> BinanceSettings:
    return BinanceSettings(
        base_url="https://example.test",
        taker_fee=Decimal("0.001"),
        timeout_seconds=1.0,
        max_retries=2,
        retry_backoff_base=0.0,
    )


def _binance_client(handler) -> BinanceClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="https://example.test", transport=transport)
    return BinanceClient(_binance_settings(), client=http)


def _coingecko_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_returns_binance_mid_price_on_success() -> None:
    def binance_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/ticker/bookTicker"
        assert request.url.params["symbol"] == "USDTRUB"
        return httpx.Response(
            200,
            json={
                "symbol": "USDTRUB",
                "bidPrice": "90.00",
                "askPrice": "91.00",
            },
        )

    cg_calls = {"n": 0}

    def cg_handler(request: httpx.Request) -> httpx.Response:
        cg_calls["n"] += 1
        return httpx.Response(500)

    async with _binance_client(binance_handler) as binance:
        async with _coingecko_client(cg_handler) as cg:
            fx = await fetch_usdt_rub_rate(binance, coingecko_http=cg)

    assert isinstance(fx, FxRate)
    assert fx.source == "binance"
    assert fx.rate == Decimal("90.5")
    assert cg_calls["n"] == 0


async def test_falls_back_to_coingecko_when_binance_errors() -> None:
    def binance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"msg": "boom"})

    def cg_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.coingecko.com"
        assert request.url.params["ids"] == "tether"
        assert request.url.params["vs_currencies"] == "rub"
        return httpx.Response(200, json={"tether": {"rub": 89.75}})

    async with _binance_client(binance_handler) as binance:
        async with _coingecko_client(cg_handler) as cg:
            fx = await fetch_usdt_rub_rate(binance, coingecko_http=cg)

    assert fx.source == "coingecko"
    assert fx.rate == Decimal("89.75")


async def test_falls_back_to_coingecko_when_binance_returns_zero_prices() -> None:
    def binance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"symbol": "USDTRUB", "bidPrice": "0", "askPrice": "0"},
        )

    def cg_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tether": {"rub": "95.5"}})

    async with _binance_client(binance_handler) as binance:
        async with _coingecko_client(cg_handler) as cg:
            fx = await fetch_usdt_rub_rate(binance, coingecko_http=cg)

    assert fx.source == "coingecko"
    assert fx.rate == Decimal("95.5")


async def test_raises_when_both_sources_fail() -> None:
    def binance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"msg": "binance down"})

    def cg_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="coingecko down")

    async with _binance_client(binance_handler) as binance:
        async with _coingecko_client(cg_handler) as cg:
            with pytest.raises(FxRateError):
                await fetch_usdt_rub_rate(binance, coingecko_http=cg)


async def test_raises_on_malformed_coingecko_payload() -> None:
    def binance_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    def cg_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    async with _binance_client(binance_handler) as binance:
        async with _coingecko_client(cg_handler) as cg:
            with pytest.raises(FxRateError):
                await fetch_usdt_rub_rate(binance, coingecko_http=cg)


async def test_coingecko_url_param_is_respected() -> None:
    seen_url: dict[str, str] = {}

    def binance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"msg": "down"})

    def cg_handler(request: httpx.Request) -> httpx.Response:
        seen_url["path"] = request.url.path
        seen_url["host"] = request.url.host
        return httpx.Response(200, json={"tether": {"rub": 88.0}})

    async with _binance_client(binance_handler) as binance:
        async with _coingecko_client(cg_handler) as cg:
            fx = await fetch_usdt_rub_rate(
                binance,
                coingecko_http=cg,
                coingecko_url=COINGECKO_URL,
            )

    assert fx.source == "coingecko"
    assert seen_url["host"] == "api.coingecko.com"
    assert seen_url["path"] == "/api/v3/simple/price"
