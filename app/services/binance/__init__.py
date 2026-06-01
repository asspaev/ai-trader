"""Клиент и утилиты для Binance public API.

Поверх `httpx.AsyncClient` тонкий слой с ретраями и единым тайм-аутом.
Никаких ключей не требуется — используем только public endpoints
``/api/v3/exchangeInfo``, ``/api/v3/klines`` и
``/api/v3/ticker/bookTicker``.
"""

from app.services.binance.client import BinanceClient, BinanceAPIError
from app.services.binance.exchange_info import (
    ExchangeInfoCache,
    SymbolFilters,
    load_exchange_info,
)
from app.services.binance.prices import (
    BookTicker,
    PriceMetrics,
    TIMEFRAMES,
    aggregate_price_metrics,
    fetch_book_ticker,
    fetch_price_metrics,
)


__all__ = [
    "BinanceClient",
    "BinanceAPIError",
    "ExchangeInfoCache",
    "SymbolFilters",
    "load_exchange_info",
    "BookTicker",
    "PriceMetrics",
    "TIMEFRAMES",
    "aggregate_price_metrics",
    "fetch_book_ticker",
    "fetch_price_metrics",
]
