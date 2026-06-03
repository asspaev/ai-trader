"""Тесты PRICE-агента: рендер промпта, парсинг ответа, end-to-end через fake LLM."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.agents.base import AgentJSONParseError, Sentiment
from app.services.agents.price_agent import (
    PriceAgent,
    format_metrics_block,
    parse_price_summary,
)
from app.services.binance.prices import PriceMetrics

from tests.unit.services.agents._helpers import chat_response
from tests.unit.services.llm._helpers import FakeOpenRouterClient


# ``asyncio_mode = "auto"`` в pyproject.toml сам подцепит async-тесты.
# Pytest-маркер ``asyncio`` для модуля не ставим — иначе он навешивается
# и на синхронные парсеры (PytestWarning).


def _metrics(timeframe: str = "1h", **overrides) -> PriceMetrics:
    defaults = dict(
        timeframe=timeframe,
        candles_used=24,
        close_now=Decimal("67432.12000000"),
        change_pct=Decimal("2.5000"),
        min_price=Decimal("65000"),
        max_price=Decimal("68000"),
        volatility_pct=Decimal("1.2345"),
    )
    defaults.update(overrides)
    return PriceMetrics(**defaults)


# ---------- format_metrics_block ----------


def test_format_metrics_block_orders_by_timeframes() -> None:
    """Порядок строк должен следовать ``TIMEFRAMES`` (короткие сверху)."""
    metrics = {
        "1d": _metrics("1d"),
        "1m": _metrics("1m"),
        "1h": _metrics("1h"),
    }
    block = format_metrics_block(metrics)
    lines = block.splitlines()
    codes_in_order = [line.split("]")[0].lstrip("[") for line in lines]
    assert codes_in_order == ["1m", "1h", "1d"]


def test_format_metrics_block_renders_optional_volatility() -> None:
    metrics = {"1m": _metrics("1m", volatility_pct=None)}
    block = format_metrics_block(metrics)
    assert "volatility_pct=n/a" in block


def test_format_metrics_block_appends_unknown_codes() -> None:
    """Если кто-то передал нестандартный код — он не теряется."""
    metrics = {"weird": _metrics("weird")}
    block = format_metrics_block(metrics)
    assert "[weird]" in block


# ---------- parse_price_summary ----------


def test_parse_price_summary_happy_path() -> None:
    content = (
        '{"summary": "Краткий боковик с расширением волатильности.", '
        '"sentiment": "neutral"}'
    )
    result = parse_price_summary(content)
    assert result.summary.startswith("Краткий боковик")
    assert result.sentiment is Sentiment.NEUTRAL


def test_parse_price_summary_strips_code_fence() -> None:
    content = '```json\n{"summary": "ok", "sentiment": "bullish"}\n```'
    result = parse_price_summary(content)
    assert result.sentiment is Sentiment.BULLISH


def test_parse_price_summary_rejects_empty_summary() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_price_summary('{"summary": "  ", "sentiment": "bullish"}')


def test_parse_price_summary_rejects_unknown_sentiment() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_price_summary('{"summary": "ok", "sentiment": "explosive"}')


def test_parse_price_summary_rejects_array() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_price_summary("[]")


# ---------- PriceAgent.run (end-to-end через FakeOpenRouterClient) ----------


async def test_price_agent_run_calls_llm_and_parses_response() -> None:
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response(
                {
                    "summary": "Краткосрочно — восходящий импульс.",
                    "sentiment": "bullish",
                }
            )
        ]
    )
    agent = PriceAgent(fake, model="test-model")

    result = await agent.run(asset="BTC", metrics={"1h": _metrics("1h")})

    assert result.sentiment is Sentiment.BULLISH
    assert "восходящий" in result.summary

    # Проверяем что запрос был построен с нужными параметрами.
    assert len(fake.chat_calls) == 1
    call = fake.chat_calls[0]
    assert call["agent_name"] == "price"
    assert call["model"] == "test-model"
    user_msg = call["messages"][-1]["content"]
    assert "BTC" in user_msg
    assert "[1h]" in user_msg  # метрики попали в промпт


async def test_price_agent_run_requires_metrics() -> None:
    fake = FakeOpenRouterClient(chat_responses=[])
    agent = PriceAgent(fake, model="test-model")
    with pytest.raises(ValueError):
        await agent.run(asset="BTC", metrics={})
    # LLM не должен быть вызван
    assert fake.chat_calls == []


async def test_price_agent_run_retries_on_missing_sentiment_then_succeeds() -> None:
    """Первый ответ без ``sentiment`` → повтор с reminder → распарсили."""
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response({"summary": "Только сводка, без sentiment."}),
            chat_response(
                {
                    "summary": "Полный ответ во второй попытке.",
                    "sentiment": "neutral",
                }
            ),
        ]
    )
    agent = PriceAgent(fake, model="test-model")

    result = await agent.run(asset="BTC", metrics={"1h": _metrics("1h")})

    assert result.sentiment is Sentiment.NEUTRAL
    assert "Полный ответ" in result.summary
    assert len(fake.chat_calls) == 2

    # Второй вызов должен содержать reminder про невалидный JSON.
    second_msgs = fake.chat_calls[1]["messages"]
    reminder = second_msgs[-1]["content"]
    assert "распарсить" in reminder.lower()


async def test_price_agent_run_raises_after_two_failed_parses() -> None:
    """Два битых ответа подряд → AgentJSONParseError наверх."""
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response({"summary": "no sentiment 1"}),
            chat_response({"summary": "no sentiment 2"}),
        ]
    )
    agent = PriceAgent(fake, model="test-model")

    with pytest.raises(AgentJSONParseError):
        await agent.run(asset="BTC", metrics={"1h": _metrics("1h")})

    assert len(fake.chat_calls) == 2
