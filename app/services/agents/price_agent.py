"""PRICE-агент: ценовая сводка по криптоактиву.

Принимает на вход агрегированные :class:`PriceMetrics` (см.
:mod:`app.services.binance.prices`), форматирует их в текстовый блок,
вызывает chat-модель и парсит JSON-ответ в :class:`PriceSummary`.

Самих свечей в промпт не отдаём — только числа. Это решение фиксировано
в `architecture.md` §17.8.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Protocol

from loguru import logger

from app.config import settings
from app.services.agents.base import (
    AgentJSONParseError,
    Sentiment,
    extract_assistant_content,
    parse_strict_json,
    render_prompt,
)
from app.services.binance.prices import TIMEFRAMES, PriceMetrics


class _ChatLLM(Protocol):
    """Минимальный публичный контракт LLM-клиента для агентов.

    Эта же сигнатура реализована :class:`OpenRouterClient` и
    :class:`FakeOpenRouterClient` из тестов.
    """

    async def chat_completion(
        self,
        *,
        agent_name: str,
        model: str,
        messages: list[dict],
        pipeline_run_id: uuid.UUID | None = ...,
        **extra,
    ) -> dict:
        ...  # pragma: no cover — structural Protocol


@dataclass(frozen=True, slots=True)
class PriceSummary:
    """Результат работы PRICE-агента.

    Attributes:
        summary: Текст 4–8 предложений на русском.
        sentiment: Общая тональность по активу.
    """

    summary: str
    sentiment: Sentiment


class PriceAgent:
    """Тонкая обёртка над LLM для генерации :class:`PriceSummary`.

    Args:
        llm_client: Любой объект с методом ``chat_completion`` (см.
            :class:`_ChatLLM`). В production — :class:`OpenRouterClient`.
        model: Имя модели для OpenRouter; по умолчанию —
            ``settings.agent.price_model``.
    """

    AGENT_NAME = "price"
    PROMPT = "price_summary"

    def __init__(self, llm_client: _ChatLLM, *, model: str | None = None) -> None:
        self._llm = llm_client
        self._model = model or settings.agent.price_model

    async def run(
        self,
        *,
        asset: str,
        metrics: Mapping[str, PriceMetrics],
        pipeline_run_id: uuid.UUID | None = None,
    ) -> PriceSummary:
        """Сгенерировать сводку по одному активу.

        Args:
            asset: Тикер актива (``"BTC"``…).
            metrics: Словарь ``{timeframe_code: PriceMetrics}``. Если
                пуст — поднимаем :class:`ValueError`: в pipeline это
                может означать, что Binance не отдал данные.
            pipeline_run_id: Идентификатор тика — для трекинга
                LLM-вызова.

        Returns:
            :class:`PriceSummary` с текстом и :class:`Sentiment`.
        """
        if not metrics:
            raise ValueError(f"PriceAgent.run requires non-empty metrics for {asset}")

        metrics_block = format_metrics_block(metrics)
        prompt = render_prompt(
            self.PROMPT,
            asset=asset.upper(),
            metrics_block=metrics_block,
        )

        response = await self._llm.chat_completion(
            agent_name=self.AGENT_NAME,
            model=self._model,
            messages=_build_messages(prompt),
            pipeline_run_id=pipeline_run_id,
        )

        content = extract_assistant_content(response)
        return parse_price_summary(content)


# ---------- pure helpers (выделены для юнит-тестов) ----------


def format_metrics_block(metrics: Mapping[str, PriceMetrics]) -> str:
    """Превратить словарь метрик в plain-text блок для промпта.

    Сохраняем порядок таймфреймов из :data:`TIMEFRAMES`, чтобы у LLM
    всегда был один и тот же визуальный ряд (короткие сверху, длинные
    снизу). Незаполненные таймфреймы пропускаем.
    """
    lines: list[str] = []
    ordered_codes = [spec.code for spec in TIMEFRAMES]
    seen: set[str] = set()
    for code in ordered_codes:
        item = metrics.get(code)
        if item is None:
            continue
        seen.add(code)
        lines.append(_format_metrics_line(item))
    # На случай если вызывающий передал нестандартный код таймфрейма.
    for code, item in metrics.items():
        if code in seen:
            continue
        lines.append(_format_metrics_line(item))
    return "\n".join(lines)


def parse_price_summary(content: str) -> PriceSummary:
    """Распарсить JSON-ответ PRICE-агента в :class:`PriceSummary`."""
    data = parse_strict_json(content)

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AgentJSONParseError(
            "PriceAgent: 'summary' must be a non-empty string",
            raw_content=content,
        )

    raw_sentiment = data.get("sentiment")
    try:
        sentiment = Sentiment.parse(raw_sentiment)
    except ValueError as exc:
        raise AgentJSONParseError(
            f"PriceAgent: invalid 'sentiment': {exc}",
            raw_content=content,
        ) from exc

    return PriceSummary(summary=summary.strip(), sentiment=sentiment)


def _format_metrics_line(item: PriceMetrics) -> str:
    """Одна строка плоского блока метрик в детерминированном формате."""
    return (
        f"[{item.timeframe}] candles={item.candles_used} "
        f"close_now={_fmt_decimal(item.close_now)} "
        f"change_pct={_fmt_decimal(item.change_pct, places=4)} "
        f"min={_fmt_decimal(item.min_price)} "
        f"max={_fmt_decimal(item.max_price)} "
        f"volatility_pct={_fmt_optional(item.volatility_pct, places=4)}"
    )


def _fmt_decimal(value: Decimal, *, places: int = 8) -> str:
    """Стабильное строковое представление :class:`Decimal`.

    LLM проще читать обычные числа, поэтому отдаём не научную форму,
    а «обычную» — с фиксированным числом знаков после запятой и без
    хвостовых нулей.
    """
    quantized = value.quantize(Decimal(10) ** -places)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return text


def _fmt_optional(value: Decimal | None, *, places: int = 8) -> str:
    return _fmt_decimal(value, places=places) if value is not None else "n/a"


def _build_messages(prompt: str) -> list[dict]:
    """Системный месседж + user — общий для всех агентов."""
    return [
        {
            "role": "system",
            "content": (
                "Ты — финансовый аналитик. Без воды, без markdown в JSON, "
                "без advisor-дисклеймеров."
            ),
        },
        {"role": "user", "content": prompt},
    ]


def metrics_as_iterable(metrics: Mapping[str, PriceMetrics]) -> Iterable[PriceMetrics]:
    """Утилита для тестов — пробежка по метрикам в порядке :data:`TIMEFRAMES`."""
    for spec in TIMEFRAMES:
        if spec.code in metrics:
            yield metrics[spec.code]


__all__ = [
    "PriceAgent",
    "PriceSummary",
    "format_metrics_block",
    "metrics_as_iterable",
    "parse_price_summary",
]
