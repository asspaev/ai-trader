"""Тесты TRADER-агента: парсинг JSON, валидация buy_fraction, ретрай при битом JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import Decision
from app.models.enums import DecisionAction
from app.services.agents.base import AgentJSONParseError, Sentiment
from app.services.agents.news_agent import NewsFinalScore
from app.services.agents.price_agent import PriceSummary
from app.services.agents.trader_agent import (
    TraderAgent,
    TraderDecision,
    WalletSnapshot,
    format_decisions_block,
    parse_trader_decision,
)

from tests.unit.services.agents._helpers import chat_response
from tests.unit.services.llm._helpers import FakeOpenRouterClient


# ``asyncio_mode = "auto"`` в pyproject.toml сам подцепит async-тесты.
# Pytest-маркер ``asyncio`` для модуля не ставим — иначе он навешивается
# и на синхронные парсеры (PytestWarning).


def _wallet() -> WalletSnapshot:
    return WalletSnapshot(
        free_usdt=Decimal("1000"),
        asset_balance=Decimal("0.005"),
        asset_price_usdt=Decimal("67000"),
    )


def _price() -> PriceSummary:
    return PriceSummary(summary="Тренд вверх.", sentiment=Sentiment.BULLISH)


def _news() -> NewsFinalScore:
    return NewsFinalScore(score="Позитивные ETF-новости.", sentiment=Sentiment.BULLISH)


def _decision(idx: int = 1, action: DecisionAction = DecisionAction.HOLD) -> Decision:
    return Decision(
        id=idx,
        user_id=1,
        pipeline_run_id=None,  # type: ignore[arg-type]
        asset="BTC",
        action=action,
        buy_fraction=Decimal("0.25") if action is DecisionAction.BUY else None,
        executed=True,
        not_executed_reason=None,
        price_summary=None,
        news_score=None,
        reasoning=f"Reason {idx}",
        created_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )


# ---------- parse_trader_decision ----------


def test_parse_trader_buy_with_fraction() -> None:
    content = json.dumps(
        {
            "action": "BUY",
            "buy_fraction": 0.25,
            "reasoning": "Сильный bullish сигнал.",
        }
    )
    result = parse_trader_decision(content)
    assert result.action is DecisionAction.BUY
    assert result.buy_fraction == Decimal("0.25")
    assert result.reasoning == "Сильный bullish сигнал."


def test_parse_trader_sell_has_null_fraction() -> None:
    content = '{"action": "SELL", "buy_fraction": null, "reasoning": "Фиксируем."}'
    result = parse_trader_decision(content)
    assert result.action is DecisionAction.SELL
    assert result.buy_fraction is None


def test_parse_trader_hold_has_null_fraction() -> None:
    content = '{"action": "HOLD", "buy_fraction": null, "reasoning": "Ждём."}'
    result = parse_trader_decision(content)
    assert result.action is DecisionAction.HOLD
    assert result.buy_fraction is None


def test_parse_trader_hold_with_zero_fraction_accepted() -> None:
    """``0`` на HOLD/SELL допускаем (часть моделей пишет 0 вместо null)."""
    content = '{"action": "HOLD", "buy_fraction": 0, "reasoning": "Ждём."}'
    result = parse_trader_decision(content)
    assert result.buy_fraction is None


def test_parse_trader_rejects_buy_without_fraction() -> None:
    content = '{"action": "BUY", "buy_fraction": null, "reasoning": "?"}'
    with pytest.raises(AgentJSONParseError) as exc_info:
        parse_trader_decision(content)
    assert "buy_fraction" in str(exc_info.value)


def test_parse_trader_rejects_buy_fraction_out_of_range() -> None:
    content = '{"action": "BUY", "buy_fraction": 1.5, "reasoning": "?"}'
    with pytest.raises(AgentJSONParseError):
        parse_trader_decision(content)


def test_parse_trader_rejects_buy_fraction_zero() -> None:
    """``0`` на BUY — бессмыслица: не покупаем = HOLD."""
    content = '{"action": "BUY", "buy_fraction": 0, "reasoning": "?"}'
    with pytest.raises(AgentJSONParseError):
        parse_trader_decision(content)


def test_parse_trader_rejects_unknown_action() -> None:
    content = '{"action": "SHORT", "buy_fraction": null, "reasoning": "?"}'
    with pytest.raises(AgentJSONParseError) as exc_info:
        parse_trader_decision(content)
    assert "action" in str(exc_info.value)


def test_parse_trader_rejects_garbage_json() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_trader_decision("not a json")


def test_parse_trader_rejects_empty_reasoning() -> None:
    content = '{"action": "HOLD", "buy_fraction": null, "reasoning": "   "}'
    with pytest.raises(AgentJSONParseError):
        parse_trader_decision(content)


def test_parse_trader_accepts_action_lowercase() -> None:
    """LLM может прислать ``buy`` строчными — нормализуем."""
    content = '{"action": "buy", "buy_fraction": 0.1, "reasoning": "ok."}'
    result = parse_trader_decision(content)
    assert result.action is DecisionAction.BUY


def test_parse_trader_strips_code_fence() -> None:
    content = (
        "```json\n"
        '{"action": "HOLD", "buy_fraction": null, "reasoning": "Боковик."}'
        "\n```"
    )
    result = parse_trader_decision(content)
    assert result.action is DecisionAction.HOLD


# ---------- format_decisions_block ----------


def test_format_decisions_block_empty_history() -> None:
    assert "Истории решений" in format_decisions_block([], 12)


def test_format_decisions_block_truncates_to_limit() -> None:
    history = [_decision(i) for i in range(20)]
    block = format_decisions_block(history, limit=5)
    assert block.count("\n") == 4  # 5 строк → 4 переноса


def test_format_decisions_block_renders_action_executed_fraction() -> None:
    history = [_decision(1, action=DecisionAction.BUY)]
    block = format_decisions_block(history, limit=12)
    assert "BUY" in block
    assert "executed=true" in block
    assert "fraction=0.25" in block


# ---------- TraderAgent.decide ----------


async def test_trader_agent_decide_happy_path() -> None:
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response(
                {
                    "action": "BUY",
                    "buy_fraction": 0.3,
                    "reasoning": "Сильный bullish сигнал и позитивные новости.",
                }
            )
        ]
    )
    agent = TraderAgent(fake, model="test-model", history_limit=12)

    result = await agent.decide(
        asset="BTC",
        wallet=_wallet(),
        price=_price(),
        news=_news(),
        history=[],
    )

    assert result.action is DecisionAction.BUY
    assert result.buy_fraction == Decimal("0.3")
    assert len(fake.chat_calls) == 1
    assert fake.chat_calls[0]["agent_name"] == "trader"

    # В промпт должны попасть оба summary и состояние кошелька.
    user_msg = fake.chat_calls[0]["messages"][-1]["content"]
    assert "Тренд вверх" in user_msg
    assert "Позитивные ETF" in user_msg
    assert "1000" in user_msg  # free_usdt


async def test_trader_agent_decide_retries_on_bad_json_then_succeeds() -> None:
    """Первый ответ битый → повтор с reminder → распарсили."""
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response("это вообще не JSON"),
            chat_response(
                {
                    "action": "HOLD",
                    "buy_fraction": None,
                    "reasoning": "Ждём подтверждения.",
                }
            ),
        ]
    )
    agent = TraderAgent(fake, model="test-model")

    result = await agent.decide(
        asset="BTC",
        wallet=_wallet(),
        price=_price(),
        news=_news(),
        history=[],
    )

    assert result.action is DecisionAction.HOLD
    assert len(fake.chat_calls) == 2

    # Второй вызов должен содержать reminder про невалидный JSON.
    second_msgs = fake.chat_calls[1]["messages"]
    reminder = second_msgs[-1]["content"]
    assert "не удалось распарсить" in reminder.lower() or "распарсить" in reminder


async def test_trader_agent_decide_raises_after_two_failed_parses() -> None:
    """Два битых ответа подряд → AgentJSONParseError наверх."""
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response("мусор-1"),
            chat_response("мусор-2"),
        ]
    )
    agent = TraderAgent(fake, model="test-model")

    with pytest.raises(AgentJSONParseError):
        await agent.decide(
            asset="BTC",
            wallet=_wallet(),
            price=_price(),
            news=_news(),
            history=[],
        )

    assert len(fake.chat_calls) == 2


async def test_trader_agent_includes_history_in_prompt() -> None:
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response(
                {
                    "action": "HOLD",
                    "buy_fraction": None,
                    "reasoning": "Свежая история без явного сигнала.",
                }
            )
        ]
    )
    agent = TraderAgent(fake, model="test-model")

    history = [_decision(1, action=DecisionAction.BUY)]
    await agent.decide(
        asset="BTC",
        wallet=_wallet(),
        price=_price(),
        news=_news(),
        history=history,
    )

    user_msg = fake.chat_calls[0]["messages"][-1]["content"]
    assert "BUY executed=true fraction=0.25" in user_msg
