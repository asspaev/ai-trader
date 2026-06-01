"""Тесты NEWS-агента: парсеры, форматтеры, поведение build_agenda на пустых данных."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import News
from app.services.agents.base import AgentJSONParseError, Sentiment
from app.services.agents.news_agent import (
    AgendaTopic,
    NewsAgenda,
    NewsAgent,
    NewsSummary,
    SummarizedPost,
    format_agenda_block,
    format_history_block,
    format_summaries_block,
    parse_news_agenda,
    parse_news_final_score,
    parse_news_summary,
)
from app.services.news.coindesk import NewsPost

from tests.unit.services.agents._helpers import chat_response
from tests.unit.services.llm._helpers import FakeOpenRouterClient


# ``asyncio_mode = "auto"`` в pyproject.toml сам подцепит async-тесты.
# Pytest-маркер ``asyncio`` для модуля не ставим — иначе он навешивается
# и на синхронные парсеры (PytestWarning).


def _post(idx: int = 1) -> NewsPost:
    return NewsPost(
        external_id=f"id-{idx}",
        asset="BTC",
        title=f"Title {idx}",
        url=f"https://news.test/{idx}",
        source="src",
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        raw_text=f"body {idx}",
    )


def _news(idx: int = 1) -> News:
    return News(
        id=idx,
        asset="BTC",
        external_id=f"hist-{idx}",
        url=f"https://hist.test/{idx}",
        title=f"Historic {idx}",
        source="hist-src",
        published_at=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        raw_text=None,
        summary_text=f"Историческое содержание №{idx}",
        embedding=None,
    )


# ---------- parse_news_summary ----------


def test_parse_news_summary_happy_path() -> None:
    content = (
        '{"summary": "Новость про SEC, негатив для рынка.", '
        '"sentiment": "bearish"}'
    )
    result = parse_news_summary(content)
    assert result.summary.startswith("Новость")
    assert result.sentiment is Sentiment.BEARISH


def test_parse_news_summary_rejects_missing_sentiment() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_news_summary('{"summary": "ok"}')


def test_parse_news_summary_rejects_empty_summary() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_news_summary('{"summary": "", "sentiment": "neutral"}')


# ---------- parse_news_agenda ----------


def _agenda_json(topics: list[dict], digest: str = "Кратко.") -> str:
    import json

    return json.dumps({"topics": topics, "digest": digest}, ensure_ascii=False)


def test_parse_news_agenda_happy_path() -> None:
    content = _agenda_json(
        [
            {"title": "ETF", "description": "Одобрение ETF.", "impact": "bullish"},
            {"title": "Reg", "description": "Регулятор давит.", "impact": "bearish"},
        ],
        digest="Двойственная повестка.",
    )
    result = parse_news_agenda(content)
    assert len(result.topics) == 2
    assert result.topics[0].title == "ETF"
    assert result.topics[1].impact is Sentiment.BEARISH
    assert result.digest == "Двойственная повестка."


def test_parse_news_agenda_truncates_to_three_topics() -> None:
    """Если LLM прислал >3 тем — берём первые три."""
    topics = [
        {"title": f"T{i}", "description": f"D{i}", "impact": "neutral"}
        for i in range(5)
    ]
    result = parse_news_agenda(_agenda_json(topics))
    assert len(result.topics) == 3
    assert result.topics[2].title == "T2"


def test_parse_news_agenda_rejects_empty_topics() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_news_agenda(_agenda_json([]))


def test_parse_news_agenda_rejects_bad_impact() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_news_agenda(
            _agenda_json(
                [{"title": "T", "description": "D", "impact": "explosive"}]
            )
        )


# ---------- parse_news_final_score ----------


def test_parse_news_final_score_happy_path() -> None:
    content = (
        '{"score": "Исторически такие новости двигают цену вверх.", '
        '"sentiment": "bullish"}'
    )
    result = parse_news_final_score(content)
    assert result.sentiment is Sentiment.BULLISH


def test_parse_news_final_score_rejects_empty_score() -> None:
    with pytest.raises(AgentJSONParseError):
        parse_news_final_score('{"score": "", "sentiment": "neutral"}')


# ---------- formatters ----------


def test_format_summaries_block_numbers_lines() -> None:
    summaries = [
        SummarizedPost(
            post=_post(1),
            summary=NewsSummary(summary="A", sentiment=Sentiment.BULLISH),
        ),
        SummarizedPost(
            post=_post(2),
            summary=NewsSummary(summary="B", sentiment=Sentiment.BEARISH),
        ),
    ]
    block = format_summaries_block(summaries)
    lines = block.splitlines()
    assert lines[0].startswith("[1] (bullish)")
    assert lines[1].startswith("[2] (bearish)")


def test_format_agenda_block_includes_topics_and_digest() -> None:
    agenda = NewsAgenda(
        topics=(
            AgendaTopic(title="ETF", description="одобрен", impact=Sentiment.BULLISH),
        ),
        digest="Краткая повестка.",
    )
    block = format_agenda_block(agenda)
    assert "ETF" in block
    assert "одобрен" in block
    assert "Digest: Краткая повестка." in block


def test_format_agenda_block_handles_empty_topics() -> None:
    agenda = NewsAgenda(topics=tuple(), digest="Тихо.")
    block = format_agenda_block(agenda)
    assert "Темы за 24 часа не выделены" in block


def test_format_history_block_handles_empty() -> None:
    assert "Исторических" in format_history_block([])


def test_format_history_block_truncates_long_summary() -> None:
    news = _news()
    news.summary_text = "x" * 1000
    block = format_history_block([news])
    assert "x" * 600 in block
    assert "…" in block


# ---------- NewsAgent end-to-end через FakeOpenRouterClient ----------


async def test_news_agent_summarize_post_routes_to_summary_agent_name() -> None:
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response({"summary": "Краткое содержание.", "sentiment": "neutral"})
        ]
    )
    agent = NewsAgent(fake, model="test-model")
    result = await agent.summarize_post(_post())
    assert result.summary == "Краткое содержание."
    assert fake.chat_calls[0]["agent_name"] == "news_summary"


async def test_news_agent_build_agenda_returns_empty_without_llm_call() -> None:
    """Пустой список summary → нет вызова LLM, токены не тратятся."""
    fake = FakeOpenRouterClient(chat_responses=[])
    agent = NewsAgent(fake, model="test-model")
    agenda = await agent.build_agenda("BTC", [])
    assert agenda.topics == tuple()
    assert "BTC" in agenda.digest
    assert fake.chat_calls == []


async def test_news_agent_build_agenda_calls_llm_when_summaries_present() -> None:
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response(
                {
                    "topics": [
                        {
                            "title": "ETF",
                            "description": "одобрен",
                            "impact": "bullish",
                        }
                    ],
                    "digest": "Свежая позитивная повестка.",
                }
            )
        ]
    )
    agent = NewsAgent(fake, model="test-model")

    summaries = [
        SummarizedPost(
            post=_post(),
            summary=NewsSummary(summary="A", sentiment=Sentiment.BULLISH),
        )
    ]
    agenda = await agent.build_agenda("BTC", summaries)
    assert len(agenda.topics) == 1
    assert agenda.topics[0].title == "ETF"
    assert fake.chat_calls[0]["agent_name"] == "news_agenda"


async def test_news_agent_final_score_skips_llm_when_no_signals() -> None:
    """Если и agenda пуста, и history пуста — LLM не зовём."""
    fake = FakeOpenRouterClient(chat_responses=[])
    agent = NewsAgent(fake, model="test-model")
    agenda = NewsAgenda(topics=tuple(), digest="ничего")
    result = await agent.final_score("BTC", agenda=agenda, history=[])
    assert result.sentiment is Sentiment.NEUTRAL
    assert fake.chat_calls == []


async def test_news_agent_final_score_calls_llm_when_history_present() -> None:
    fake = FakeOpenRouterClient(
        chat_responses=[
            chat_response({"score": "Аналог из 2025-го дал рост.", "sentiment": "bullish"})
        ]
    )
    agent = NewsAgent(fake, model="test-model")

    agenda = NewsAgenda(topics=tuple(), digest="свежего нет")
    result = await agent.final_score("BTC", agenda=agenda, history=[_news()])

    assert result.sentiment is Sentiment.BULLISH
    assert fake.chat_calls[0]["agent_name"] == "news_final_score"
    # В промпт должна была попасть историческая новость.
    user_msg = fake.chat_calls[0]["messages"][-1]["content"]
    assert "Historic 1" in user_msg
