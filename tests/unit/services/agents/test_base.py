"""Тесты базовых утилит :mod:`app.services.agents.base`."""

from __future__ import annotations

import pytest

from app.services.agents.base import (
    AgentError,
    AgentJSONParseError,
    Sentiment,
    extract_assistant_content,
    parse_strict_json,
    render_prompt,
)


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
