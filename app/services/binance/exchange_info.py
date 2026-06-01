"""Загрузка и кэширование биржевых фильтров Binance.

При старте приложения для каждой торговой пары один раз вытаскиваем
``LOT_SIZE``/``PRICE_FILTER``/``NOTIONAL`` из ``GET /api/v3/exchangeInfo``
и складываем результат в :class:`ExchangeInfoCache`. Все mock-сделки
дальше сверяются с этим кэшем — никаких повторных запросов.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from loguru import logger

from app.services.binance.client import BinanceClient


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    """Минимальный набор фильтров биржи для одного символа.

    Attributes:
        symbol: Тикер пары (``BTCUSDT`` …).
        base_asset: Базовый актив (``BTC``).
        quote_asset: Котировочный актив (``USDT``).
        step_size: Шаг количества базового актива (``LOT_SIZE.stepSize``).
        min_qty: Минимальное количество (``LOT_SIZE.minQty``).
        tick_size: Шаг цены (``PRICE_FILTER.tickSize``).
        min_notional: Минимальная сумма сделки в quote-валюте
            (фильтр ``NOTIONAL`` или ``MIN_NOTIONAL`` — что найдено).
    """

    symbol: str
    base_asset: str
    quote_asset: str
    step_size: Decimal
    min_qty: Decimal
    tick_size: Decimal
    min_notional: Decimal

    def quantize_amount(self, amount: Decimal) -> Decimal:
        """Округлить количество базового актива вниз до ``step_size``.

        Возвращает ``Decimal('0')``, если результат < ``min_qty`` —
        вызывающий сам решает, что делать (поднять ``LOT_SIZE`` reason).
        """
        if self.step_size <= 0:
            return amount
        steps = (amount / self.step_size).to_integral_value(rounding="ROUND_FLOOR")
        quantized = steps * self.step_size
        if quantized < self.min_qty:
            return Decimal("0")
        return quantized


class ExchangeInfoCache:
    """In-memory кэш фильтров по символам."""

    def __init__(self, filters: Iterable[SymbolFilters]) -> None:
        self._by_symbol: dict[str, SymbolFilters] = {f.symbol: f for f in filters}

    def get(self, symbol: str) -> SymbolFilters:
        try:
            return self._by_symbol[symbol]
        except KeyError as exc:
            raise KeyError(f"Symbol {symbol!r} not in exchange info cache") from exc

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._by_symbol

    def symbols(self) -> list[str]:
        return list(self._by_symbol.keys())


async def load_exchange_info(
    client: BinanceClient,
    symbols: Iterable[str],
) -> ExchangeInfoCache:
    """Запросить ``exchangeInfo`` сразу по всем символам и собрать кэш."""
    symbols_list = [s.upper() for s in symbols]
    if not symbols_list:
        return ExchangeInfoCache([])

    # Binance ожидает JSON-массив в виде строки: ["BTCUSDT","ETHUSDT"]
    params = {"symbols": _serialize_symbols(symbols_list)}
    payload = await client.get_json("/api/v3/exchangeInfo", params=params)

    raw_symbols = payload.get("symbols", []) if isinstance(payload, Mapping) else []
    filters = [_parse_symbol(item) for item in raw_symbols]
    cache = ExchangeInfoCache(filters)

    logger.bind(component="binance.exchange_info").info(
        "Loaded exchange info for symbols: {symbols}",
        symbols=cache.symbols(),
    )
    return cache


def _serialize_symbols(symbols: list[str]) -> str:
    """Сериализовать список символов в формат, который ждёт Binance."""
    quoted = ",".join(f'"{s}"' for s in symbols)
    return f"[{quoted}]"


def _parse_symbol(raw: Mapping[str, Any]) -> SymbolFilters:
    """Из сырого JSON одного символа достать только нужные фильтры."""
    filters_by_type: dict[str, Mapping[str, Any]] = {
        f["filterType"]: f for f in raw.get("filters", [])
    }

    lot = filters_by_type.get("LOT_SIZE", {})
    price = filters_by_type.get("PRICE_FILTER", {})
    notional = filters_by_type.get("NOTIONAL") or filters_by_type.get("MIN_NOTIONAL") or {}

    min_notional = (
        notional.get("minNotional")
        or notional.get("notional")
        or "0"
    )

    return SymbolFilters(
        symbol=raw["symbol"],
        base_asset=raw["baseAsset"],
        quote_asset=raw["quoteAsset"],
        step_size=Decimal(str(lot.get("stepSize", "0"))),
        min_qty=Decimal(str(lot.get("minQty", "0"))),
        tick_size=Decimal(str(price.get("tickSize", "0"))),
        min_notional=Decimal(str(min_notional)),
    )


__all__ = [
    "ExchangeInfoCache",
    "SymbolFilters",
    "load_exchange_info",
]
