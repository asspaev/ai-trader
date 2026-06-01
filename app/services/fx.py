"""Сервис получения курса USDT/RUB для инициализации пользователя.

Источник 1 (основной) — Binance public bookTicker для пары
``USDTRUB``: берём середину между bid и ask. Источник 2 (fallback) —
CoinGecko ``/api/v3/simple/price?ids=tether&vs_currencies=rub``.

Используется только в ``scripts/init_user.py`` (фаза 3 — одноразовая
конвертация стартового RUB-капитала в USDT). В рабочем цикле всё
исчисляется в USDT, а RUB-эквивалент в Telegram считается заново при
каждом ответе по тому же сервису.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import httpx
from loguru import logger

from app.services.binance.client import BinanceClient
from app.services.binance.prices import fetch_book_ticker


COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
"""Дефолтный CoinGecko-эндпоинт для simple price."""

FxSource = Literal["binance", "coingecko"]


class FxRateError(RuntimeError):
    """Не удалось получить курс ни из одного источника."""


@dataclass(frozen=True, slots=True)
class FxRate:
    """Курс ``RUB за 1 USDT`` и источник, откуда он получен."""

    rate: Decimal
    source: FxSource


async def fetch_usdt_rub_rate(
    binance_client: BinanceClient,
    *,
    coingecko_http: httpx.AsyncClient | None = None,
    coingecko_url: str = COINGECKO_URL,
) -> FxRate:
    """Получить курс USDT/RUB с фолбэком на CoinGecko.

    Args:
        binance_client: Готовый async-клиент Binance.
        coingecko_http: Готовый httpx-клиент для CoinGecko. Если не
            передан — создаётся внутренний на время одного запроса.
        coingecko_url: Эндпоинт CoinGecko (обычно дефолтный).

    Returns:
        :class:`FxRate` с rate в формате «RUB за 1 USDT».

    Raises:
        FxRateError: Если оба источника не ответили валидными данными.
    """
    bound = logger.bind(component="fx", asset_pair="USDTRUB")

    try:
        ticker = await fetch_book_ticker(binance_client, "USDTRUB")
    except Exception as exc:
        bound.warning(
            "Binance USDTRUB bookTicker failed, falling back to CoinGecko: {err}",
            err=f"{type(exc).__name__}: {exc}",
        )
    else:
        if ticker.bid_price > 0 and ticker.ask_price > 0:
            mid = (ticker.bid_price + ticker.ask_price) / Decimal("2")
            bound.info(
                "Got USDT/RUB from Binance: bid={bid}, ask={ask}, mid={mid}",
                bid=ticker.bid_price,
                ask=ticker.ask_price,
                mid=mid,
            )
            return FxRate(rate=mid, source="binance")
        bound.warning(
            "Binance USDTRUB bookTicker returned non-positive prices: bid={bid}, ask={ask}",
            bid=ticker.bid_price,
            ask=ticker.ask_price,
        )

    return await _fetch_from_coingecko(
        http=coingecko_http,
        url=coingecko_url,
        bound=bound,
    )


async def _fetch_from_coingecko(
    *,
    http: httpx.AsyncClient | None,
    url: str,
    bound,
) -> FxRate:
    """Запросить курс у CoinGecko (может быть вызвано как fallback)."""
    owns_client = http is None
    client = http or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(
            url, params={"ids": "tether", "vs_currencies": "rub"}
        )
        if response.status_code != 200:
            raise FxRateError(
                f"CoinGecko returned status {response.status_code}: "
                f"{response.text[:200]}"
            )
        payload = response.json()
    except FxRateError:
        raise
    except Exception as exc:
        raise FxRateError(
            f"CoinGecko request failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    try:
        raw_rate = payload["tether"]["rub"]
        rate = Decimal(str(raw_rate))
    except (KeyError, TypeError, ValueError) as exc:
        raise FxRateError(
            f"CoinGecko payload is not parseable: {payload!r}"
        ) from exc

    if rate <= 0:
        raise FxRateError(f"CoinGecko returned non-positive rate: {rate}")

    bound.info("Got USDT/RUB from CoinGecko: rate={rate}", rate=rate)
    return FxRate(rate=rate, source="coingecko")


__all__ = [
    "COINGECKO_URL",
    "FxRate",
    "FxRateError",
    "FxSource",
    "fetch_usdt_rub_rate",
]
