"""Получение и агрегация ценовых данных Binance.

Логика разделена на чистые функции (агрегация / расчёт метрик) и
сетевые (запросы к Binance). Чистые функции принимают сырые klines и
не требуют ``httpx`` — это упрощает юнит-тесты на фикстурах.

Архитектурное соглашение: PRICE-агент получает не сырые свечи, а
агрегированные числа по нескольким периодам (см. :data:`TIMEFRAMES`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from loguru import logger

from app.services.binance.client import BinanceClient


@dataclass(frozen=True, slots=True)
class Kline:
    """Одна свеча Binance (значимая часть).

    Поля volume/close_time оставлены для будущих метрик (объём, время).
    """

    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int


@dataclass(frozen=True, slots=True)
class PriceMetrics:
    """Числовая сводка по одному таймфрейму.

    Attributes:
        timeframe: Код таймфрейма из :data:`TIMEFRAMES`.
        candles_used: Сколько (агрегированных) свечей реально учтено.
        close_now: Цена закрытия последней свечи.
        change_pct: % изменения цены закрытия от первой свечи к последней.
        min_price: Минимум ``low`` среди учтённых свечей.
        max_price: Максимум ``high`` среди учтённых свечей.
        volatility_pct: Стандартное отклонение % приращений close
            (выборочное, ddof=1), выражено в процентах. ``None``, если
            окно слишком короткое (< 2 свечей).
    """

    timeframe: str
    candles_used: int
    close_now: Decimal
    change_pct: Decimal
    min_price: Decimal
    max_price: Decimal
    volatility_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class TimeframeSpec:
    """Описание одного таймфрейма для PRICE-агента.

    Attributes:
        code: Метка таймфрейма для агента (``"1m"``, ``"3h"``,
            ``"5Y"``…).
        native_interval: Интервал свечей, который реально просим у Binance.
        agg_factor: Сколько native-свечей склеиваем в одну
            «отображаемую» (``1`` = без агрегации).
        candle_count: Сколько агрегированных свечей нужно для метрик.
    """

    code: str
    native_interval: str
    agg_factor: int
    candle_count: int

    @property
    def required_native(self) -> int:
        return self.candle_count * self.agg_factor


@dataclass(frozen=True, slots=True)
class BookTicker:
    """``bookTicker``-цена: лучший bid и ask."""

    symbol: str
    bid_price: Decimal
    ask_price: Decimal


TIMEFRAMES: tuple[TimeframeSpec, ...] = (
    TimeframeSpec("1m",  "1m",  1,  30),
    TimeframeSpec("30m", "30m", 1,  20),
    TimeframeSpec("1h",  "1h",  1,  24),
    TimeframeSpec("3h",  "1h",  3,  16),
    TimeframeSpec("6h",  "6h",  1,  20),
    TimeframeSpec("12h", "12h", 1,  14),
    TimeframeSpec("1d",  "1d",  1,  14),
    TimeframeSpec("3d",  "3d",  1,  10),
    TimeframeSpec("7d",  "1w",  1,   8),
    TimeframeSpec("1M",  "1M",  1,  12),
    TimeframeSpec("3M",  "1M",  3,  12),
    TimeframeSpec("6M",  "1M",  6,  10),
    TimeframeSpec("1Y",  "1M",  12, 10),
    TimeframeSpec("3Y",  "1M",  36,  5),
    TimeframeSpec("5Y",  "1M",  60,  5),
)


# ---------- pure functions (тестируются без httpx) ----------


def parse_klines(raw: Iterable[Sequence[Any]]) -> list[Kline]:
    """Парсинг ответа ``/api/v3/klines`` в список :class:`Kline`."""
    return [
        Kline(
            open_time=int(item[0]),
            open=Decimal(str(item[1])),
            high=Decimal(str(item[2])),
            low=Decimal(str(item[3])),
            close=Decimal(str(item[4])),
            volume=Decimal(str(item[5])),
            close_time=int(item[6]),
        )
        for item in raw
    ]


def aggregate_klines(klines: Sequence[Kline], factor: int) -> list[Kline]:
    """Склеить каждые ``factor`` подряд идущих свечей в одну.

    Хвост, в котором не набирается ``factor`` свечей, отбрасывается —
    «частично закрытые» периоды смешивают сравнения.
    """
    if factor <= 1:
        return list(klines)
    if not klines:
        return []

    full_groups = len(klines) // factor
    result: list[Kline] = []
    for idx in range(full_groups):
        group = klines[idx * factor : (idx + 1) * factor]
        first, last = group[0], group[-1]
        result.append(
            Kline(
                open_time=first.open_time,
                open=first.open,
                high=max(k.high for k in group),
                low=min(k.low for k in group),
                close=last.close,
                volume=sum((k.volume for k in group), Decimal("0")),
                close_time=last.close_time,
            )
        )
    return result


def compute_metrics(
    timeframe: str, klines: Sequence[Kline], candle_count: int
) -> PriceMetrics | None:
    """Посчитать :class:`PriceMetrics` по последним ``candle_count`` свечам.

    Возвращает ``None``, если входной список пуст. Если свечей меньше,
    чем запрошено, используем что есть (исторических данных по
    криптоактиву может не быть на длинном горизонте).
    """
    if not klines:
        return None

    window = list(klines[-candle_count:])
    first_close = window[0].close
    last_close = window[-1].close

    change_pct = (
        ((last_close - first_close) / first_close) * Decimal("100")
        if first_close != 0
        else Decimal("0")
    )

    min_price = min(k.low for k in window)
    max_price = max(k.high for k in window)
    volatility = _stddev_of_returns_pct(window)

    return PriceMetrics(
        timeframe=timeframe,
        candles_used=len(window),
        close_now=last_close,
        change_pct=change_pct,
        min_price=min_price,
        max_price=max_price,
        volatility_pct=volatility,
    )


def aggregate_price_metrics(
    raw_klines_by_interval: dict[str, list[Sequence[Any]]],
) -> dict[str, PriceMetrics]:
    """Сводная функция: сырой ответ Binance → метрики по всем таймфреймам.

    Принимает словарь ``{native_interval: raw_klines}``. Возвращает
    словарь ``{timeframe_code: PriceMetrics}`` для всех таймфреймов,
    по которым удалось посчитать метрики.
    """
    parsed_by_interval: dict[str, list[Kline]] = {
        interval: parse_klines(raw)
        for interval, raw in raw_klines_by_interval.items()
    }

    result: dict[str, PriceMetrics] = {}
    for spec in TIMEFRAMES:
        native = parsed_by_interval.get(spec.native_interval)
        if not native:
            continue
        aggregated = aggregate_klines(native, spec.agg_factor)
        metrics = compute_metrics(spec.code, aggregated, spec.candle_count)
        if metrics is not None:
            result[spec.code] = metrics
    return result


# ---------- network ----------


async def fetch_price_metrics(
    client: BinanceClient, symbol: str
) -> dict[str, PriceMetrics]:
    """Скачать все нужные klines для одного символа и собрать метрики.

    Для каждого native-интервала делается один запрос на максимум
    свечей, которые нужны под все ассоциированные с ним таймфреймы.
    """
    required_by_interval: dict[str, int] = {}
    for spec in TIMEFRAMES:
        prev = required_by_interval.get(spec.native_interval, 0)
        required_by_interval[spec.native_interval] = max(prev, spec.required_native)

    bound = logger.bind(component="binance.prices", symbol=symbol)
    raw_by_interval: dict[str, list[Sequence[Any]]] = {}
    for interval, limit in required_by_interval.items():
        capped = min(limit, 1000)
        raw = await client.get_json(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": capped},
        )
        raw_by_interval[interval] = raw or []
        bound.debug(
            "Fetched klines {interval}: requested={limit}, returned={n}",
            interval=interval,
            limit=capped,
            n=len(raw_by_interval[interval]),
        )

    return aggregate_price_metrics(raw_by_interval)


async def fetch_book_ticker(client: BinanceClient, symbol: str) -> BookTicker:
    """``GET /api/v3/ticker/bookTicker`` — bid/ask на момент сделки."""
    payload = await client.get_json(
        "/api/v3/ticker/bookTicker", params={"symbol": symbol}
    )
    return BookTicker(
        symbol=payload["symbol"],
        bid_price=Decimal(str(payload["bidPrice"])),
        ask_price=Decimal(str(payload["askPrice"])),
    )


# ---------- internals ----------


def _stddev_of_returns_pct(klines: Sequence[Kline]) -> Decimal | None:
    """Выборочное стандартное отклонение % приращений close (ddof=1)."""
    if len(klines) < 2:
        return None

    returns: list[float] = []
    for prev, curr in zip(klines, klines[1:]):
        prev_close = float(prev.close)
        if prev_close == 0:
            continue
        returns.append((float(curr.close) - prev_close) / prev_close * 100.0)

    if len(returns) < 2:
        return None

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return Decimal(str(math.sqrt(variance)))


__all__ = [
    "Kline",
    "PriceMetrics",
    "TimeframeSpec",
    "BookTicker",
    "TIMEFRAMES",
    "parse_klines",
    "aggregate_klines",
    "compute_metrics",
    "aggregate_price_metrics",
    "fetch_price_metrics",
    "fetch_book_ticker",
]
