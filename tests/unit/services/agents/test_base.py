"""Тесты базовых утилит :mod:`app.services.agents.base`."""

from __future__ import annotations

import pytest

from app.services.agents.base import (
    AgentError,
    AgentJSONParseError,
    BaseAgent,
    Sentiment,
    extract_assistant_content,
    parse_strict_json,
    render_prompt,
)

from tests.unit.services.agents._helpers import chat_response
from tests.unit.services.llm._helpers import FakeOpenRouterClient


# Все тесты модуля синхронные — asyncio-маркер не нужен.


def test_render_prompt_substitutes_values() -> None:
    """``$asset`` → реальное значение, файл существует на диске."""
    rendered = render_prompt(
        "price_summary",
        asset="BTC",
        metrics_block="[1m] candles=5 close_now=1",
    )
    assert "BTC" in rendered
    assert "[1m] candles=5 close_now=1" in rendered


def test_render_prompt_raises_on_missing_variable() -> None:
    """``str.Template.substitute`` падает на пропущенной переменной."""
    with pytest.raises(KeyError):
        render_prompt("price_summary", asset="BTC")  # нет metrics_block


def test_sentiment_parse_case_insensitive() -> None:
    assert Sentiment.parse("Bullish") is Sentiment.BULLISH
    assert Sentiment.parse("  bearish ") is Sentiment.BEARISH
    assert Sentiment.parse(Sentiment.NEUTRAL) is Sentiment.NEUTRAL


def test_sentiment_parse_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        Sentiment.parse("optimistic")
    with pytest.raises(ValueError):
        Sentiment.parse(None)


def test_extract_assistant_content_happy_path() -> None:
    response = {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
    }
    assert extract_assistant_content(response) == "hi"


@pytest.mark.parametrize(
    "broken",
    [
        {},  # нет choices
        {"choices": []},  # пустой массив
        {"choices": [{}]},  # нет message
        {"choices": [{"message": {}}]},  # нет content
        {"choices": [{"message": {"content": 42}}]},  # не строка
    ],
)
def test_extract_assistant_content_raises_on_malformed(broken: dict) -> None:
    with pytest.raises(AgentError):
        extract_assistant_content(broken)


def test_parse_strict_json_plain() -> None:
    assert parse_strict_json('{"a": 1}') == {"a": 1}


def test_parse_strict_json_strips_code_fence() -> None:
    """LLM иногда оборачивает ответ в ```json``` — терпим, но парсим."""
    content = '```json\n{"a": 1}\n```'
    assert parse_strict_json(content) == {"a": 1}


def test_parse_strict_json_strips_plain_fence() -> None:
    """Fence без указания языка тоже считается."""
    content = "```\n{\"a\": 1}\n```"
    assert parse_strict_json(content) == {"a": 1}


def test_parse_strict_json_raises_on_array() -> None:
    """Массив на верхнем уровне — ошибка: ожидаем объект."""
    with pytest.raises(AgentJSONParseError) as exc_info:
        parse_strict_json("[1, 2, 3]")
    assert "object" in str(exc_info.value)


def test_parse_strict_json_raises_on_garbage() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_strict_json("это не JSON")


def test_parse_strict_json_tolerates_trailing_prose() -> None:
    """DeepSeek иногда дописывает текст после JSON — старая логика падала
    с ``json.JSONDecodeError: Extra data``, теперь читаем первый объект."""
    content = '{"a": 1}\n\nThis is my reasoning behind the answer.'
    assert parse_strict_json(content) == {"a": 1}


def test_parse_strict_json_tolerates_leading_prose() -> None:
    """Аналогично — текст перед JSON тоже не должен ронять парсер."""
    content = "Here is the answer:\n{\"a\": 1}"
    assert parse_strict_json(content) == {"a": 1}


def test_agent_json_parse_error_is_picklable() -> None:
    """loguru с ``enqueue=True`` сериализует исключения в фоновую очередь —
    custom ``__init__`` с keyword-only ломал unpickle."""
    import pickle

    err = AgentJSONParseError("boom", raw_content="<<<")
    restored = pickle.loads(pickle.dumps(err))
    assert isinstance(restored, AgentJSONParseError)
    assert str(restored) == str(err)
    assert restored.raw_content == "<<<"


# ---------- BaseAgent._chat_with_parse_retry ----------


def _parse_required_field(content: str) -> dict:
    """Тестовый парсер: требует ``{"value": <str>}`` иначе AgentJSONParseError."""
    data = parse_strict_json(content)
    value = data.get("value")
    if not isinstance(value, str):
        raise AgentJSONParseError(
            "value must be a string",
            raw_content=content,
        )
    return {"value": value}


def _msg_factory(prompt: str):
    """Фабрика messages: на первой попытке — голый prompt, на второй —
    дополнительная reminder-строка с текстом предыдущей ошибки."""

    def build(prior_error: AgentJSONParseError | None) -> list[dict]:
        messages: list[dict] = [{"role": "user", "content": prompt}]
        if prior_error is not None:
            messages.append({"role": "user", "content": f"retry: {prior_error}"})
        return messages

    return build


async def test_base_agent_retries_on_parse_error_then_succeeds() -> None:
    """Первый ответ не парсится → второй проходит. Reminder попадает во второй вызов."""
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response({"wrong": "shape"}),
            chat_response({"value": "ok"}),
        ]
    )
    agent = BaseAgent(fake, model="test-model")

    result = await agent._chat_with_parse_retry(
        agent_name="dummy",
        messages_factory=_msg_factory("prompt"),
        parser=_parse_required_field,
    )

    assert result == {"value": "ok"}
    assert len(fake.chat_calls) == 2
    # Во втором вызове должен быть reminder с текстом ошибки.
    second_messages = fake.chat_calls[1]["messages"]
    assert any("retry:" in m["content"] for m in second_messages)


async def test_base_agent_raises_after_all_attempts_fail() -> None:
    """Два битых ответа подряд → AgentJSONParseError наверх."""
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response({"wrong": "1"}),
            chat_response({"wrong": "2"}),
        ]
    )
    agent = BaseAgent(fake, model="test-model")

    with pytest.raises(AgentJSONParseError):
        await agent._chat_with_parse_retry(
            agent_name="dummy",
            messages_factory=_msg_factory("prompt"),
            parser=_parse_required_field,
        )

    assert len(fake.chat_calls) == BaseAgent.MAX_PARSE_ATTEMPTS


async def test_base_agent_returns_on_first_success_without_retry() -> None:
    """Успех на первой попытке — второго вызова быть не должно."""
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response({"value": "first"}),
        ]
    )
    agent = BaseAgent(fake, model="test-model")

    result = await agent._chat_with_parse_retry(
        agent_name="dummy",
        messages_factory=_msg_factory("prompt"),
        parser=_parse_required_field,
    )

    assert result == {"value": "first"}
    assert len(fake.chat_calls) == 1
    # На первой попытке reminder в messages быть не должен.
    first_messages = fake.chat_calls[0]["messages"]
    assert not any("retry:" in m["content"] for m in first_messages)
