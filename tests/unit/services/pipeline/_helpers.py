"""Помощники для тестов pipeline-слоя.

Содержит fake-клиенты Binance и news-провайдера с минимальной
поверхностью, которая нужна крипто-шагу, генератор синтетических klines
под любой интервал, а также фабрики «стандартных» chat/embedding ответов
для :class:`FakeOpenRouterClient`.

Зачем нужны fakes:

* :class:`FakeBinanceClient` подменяет реальный httpx-клиент — нам
  важен только метод ``get_json`` (`/api/v3/klines`,
  `/api/v3/ticker/bookTicker`), потому что :mod:`app.services.binance.prices`
  и :func:`fetch_book_ticker` идут именно через него.
* :class:`FakeNewsClient` подменяет ``fetch_recent`` (это единственный
  метод, который дёргает pipeline-ветка NEWS). Source-agnostic — реальный
  клиент сейчас CoinDesk Data, был CryptoPanic, может быть любой другой.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.config import settings
from app.services.binance.exchange_info import ExchangeInfoCache, SymbolFilters
from app.services.binance.prices import TIMEFRAMES
from app.services.news.coindesk import NewsPost


# ---------- chat / embedding response builders ----------


def chat_response(content: str | dict[str, Any]) -> dict[str, Any]:
    """OpenAI-совместимый /chat/completions ответ с одной message.

    Дублирует ``tests.unit.services.agents._helpers.chat_response`` —
    специально не импортируем оттуда, чтобы pipeline-тесты не зависели
    от модулей агентов в части импортов (циклов нет, просто гигиена).
    """
    body = json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content
    return {
        "id": "test-id",
        "object": "chat.completion",
        "model": "deepseek/deepseek-chat",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": body},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def embedding_response(seed: float = 0.42) -> dict[str, Any]:
    """/embeddings-ответ нужной размерности (берётся из настроек)."""
    dim = settings.agent.embedding_dim
    vector = [0.0] * dim
    vector[0] = seed
    if dim > 1:
        vector[1] = 1.0 - abs(seed)
    return {
        "data": [{"embedding": vector}],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


# ---------- agent payloads ----------


def price_chat(sentiment: str = "bullish", summary: str | None = None) -> dict[str, Any]:
    """Стандартный ответ PRICE-агента."""
    return chat_response(
        {
            "summary": summary or "Краткосрочный тренд вверх, среднесрочно нейтрально.",
            "sentiment": sentiment,
        }
    )


def news_summary_chat(sentiment: str = "neutral") -> dict[str, Any]:
    return chat_response(
        {"summary": "Кратко: новость затрагивает регуляторов.", "sentiment": sentiment}
    )


def news_agenda_chat() -> dict[str, Any]:
    return chat_response(
        {
            "topics": [
                {
                    "title": "Regulation",
                    "description": "Новости о регулировании.",
                    "impact": "neutral",
                }
            ],
            "digest": "Свежая повестка: регуляторика.",
        }
    )


def news_final_chat(sentiment: str = "neutral") -> dict[str, Any]:
    return chat_response(
        {
            "score": "Исторически такие новости двигают цену в обе стороны.",
            "sentiment": sentiment,
        }
    )


def trader_chat(
    action: str = "HOLD",
    *,
    buy_fraction: float | None = None,
    reasoning: str = "Принимаю решение.",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": action, "reasoning": reasoning}
    payload["buy_fraction"] = buy_fraction
    return chat_response(payload)


# ---------- klines ----------


def synthetic_klines(count: int, *, base_price: float = 100.0, step: float = 1.0) -> list[list[Any]]:
    """Сгенерировать ``count`` синтетических свечей в формате Binance.

    Все свечи делаются монотонно растущими — этого достаточно, чтобы
    :func:`compute_metrics` посчитал не-``None`` метрики. Конкретные
    значения не важны: PRICE-агент мокирован в тестах.
    """
    klines: list[list[Any]] = []
    for idx in range(count):
        open_time = 1_700_000_000_000 + idx * 3_600_000
        close_time = open_time + 3_600_000 - 1
        o = base_price + step * idx
        c = o + step / 2
        h = max(o, c) + step / 4
        low = min(o, c) - step / 4
        klines.append(
            [
                open_time,
                f"{o:.2f}",
                f"{h:.2f}",
                f"{low:.2f}",
                f"{c:.2f}",
                "10.0",
                close_time,
                "1000",
                50,
                "5.0",
                "500",
                "0",
            ]
        )
    return klines


def klines_for_all_timeframes() -> dict[str, list[list[Any]]]:
    """Заранее заполнить кэш сырых klines под все ``TIMEFRAMES``.

    Возвращаем словарь по ``native_interval`` (а не по timeframe-коду),
    потому что именно так его потребляет :func:`fetch_price_metrics`.
    """
    required_by_interval: dict[str, int] = {}
    for spec in TIMEFRAMES:
        prev = required_by_interval.get(spec.native_interval, 0)
        required_by_interval[spec.native_interval] = max(prev, spec.required_native)
    return {
        interval: synthetic_klines(limit)
        for interval, limit in required_by_interval.items()
    }


# ---------- fake clients ----------


class FakeBinanceClient:
    """Минимальный fake для ``BinanceClient.get_json``.

    Хранит таблицы klines (``interval → raw_klines``) и bookTicker
    (``symbol → {bidPrice, askPrice}``). Принимает их под конкретный
    символ; на любой неизвестный путь — :class:`AssertionError`, чтобы
    тест явно показывал, что pipeline дёрнул лишнюю ручку.
    """

    def __init__(
        self,
        *,
        klines_by_interval: Mapping[str, list[list[Any]]] | None = None,
        book_ticker_by_symbol: Mapping[str, dict[str, str]] | None = None,
    ) -> None:
        self._klines = dict(klines_by_interval or {})
        self._book = dict(book_ticker_by_symbol or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        self.calls.append((path, dict(params or {})))
        if path == "/api/v3/klines":
            interval = (params or {}).get("interval")
            return list(self._klines.get(interval, []))
        if path == "/api/v3/ticker/bookTicker":
            symbol = (params or {}).get("symbol")
            return self._book[symbol]
        raise AssertionError(f"FakeBinanceClient: unexpected path {path!r}")

    async def aclose(self) -> None:
        return None


class FakeNewsClient:
    """Возвращает заранее уложенные посты по тикеру актива.

    Source-agnostic подмена для :class:`CoinDeskNewsClient`: реализует
    только метод ``fetch_recent``, который дёргает NEWS-ветка pipeline.
    """

    def __init__(self, posts_by_asset: Mapping[str, Iterable[NewsPost]] | None = None) -> None:
        self._posts: dict[str, list[NewsPost]] = {
            asset.upper(): list(items) for asset, items in (posts_by_asset or {}).items()
        }
        self.calls: list[str] = []

    async def fetch_recent(
        self,
        asset: str,
        *,
        limit: int | None = None,
    ) -> list[NewsPost]:
        self.calls.append(asset.upper())
        return list(self._posts.get(asset.upper(), []))

    async def aclose(self) -> None:
        return None


# ---------- factories ----------


def make_news_post(
    *,
    asset: str = "BTC",
    external_id: str = "cp-1",
    title: str = "ETF news",
    url: str = "https://example.com/news",
    published_at: datetime | None = None,
    raw_text: str | None = "Body text",
) -> NewsPost:
    """Готовый :class:`NewsPost` для тестов pipeline."""
    return NewsPost(
        external_id=external_id,
        asset=asset.upper(),
        title=title,
        url=url,
        source="CoinDesk",
        published_at=published_at or datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        raw_text=raw_text,
    )


def make_book_ticker(
    *,
    bid: str = "66950.00",
    ask: str = "67050.00",
) -> dict[str, str]:
    """Сырой словарь, который Binance возвращает на ``bookTicker``.

    Заполняем минимальный набор полей, который читает
    :func:`fetch_book_ticker`.
    """
    return {"symbol": "DUMMY", "bidPrice": bid, "askPrice": ask}


def make_filters(symbol: str, asset: str = "BTC") -> SymbolFilters:
    """:class:`SymbolFilters` с «легко проходимыми» порогами для тестов."""
    return SymbolFilters(
        symbol=symbol,
        base_asset=asset,
        quote_asset="USDT",
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_notional=Decimal("10"),
    )


def make_exchange_info(symbols: Iterable[str]) -> ExchangeInfoCache:
    """Кэш с одинаковыми мягкими фильтрами под каждый символ."""
    filters: list[SymbolFilters] = []
    for symbol in symbols:
        asset = symbol.replace("USDT", "")
        filters.append(make_filters(symbol, asset=asset))
    return ExchangeInfoCache(filters)


__all__ = [
    "FakeBinanceClient",
    "FakeNewsClient",
    "chat_response",
    "embedding_response",
    "klines_for_all_timeframes",
    "make_book_ticker",
    "make_exchange_info",
    "make_filters",
    "make_news_post",
    "news_agenda_chat",
    "news_final_chat",
    "news_summary_chat",
    "price_chat",
    "synthetic_klines",
    "trader_chat",
]
