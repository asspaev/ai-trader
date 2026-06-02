"""NEWS-агент: три LLM-вызова вокруг новостной картины актива.

1. :meth:`NewsAgent.summarize_post` — короткий summary одной новости
   + её sentiment. Делается только для новых (не дублей) новостей.
2. :meth:`NewsAgent.build_agenda` — собирает «повестку дня» по всем
   summary за 24 часа. Возвращает 1–3 тематики + общий ``digest``,
   который потом используется как RAG-запрос.
3. :meth:`NewsAgent.final_score` — итоговая оценка после того, как
   RAG нашёл top-K исторически релевантных новостей.

Все три вызова — отдельные :class:`LLMCall` (агент-name записывается
разный, см. :data:`AGENT_NAMES`), что даёт чистую статистику по
стоимости каждого этапа.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Sequence

from loguru import logger

from app.config import settings
from app.models import News
from app.services.agents.base import (
    AgentJSONParseError,
    Sentiment,
    extract_assistant_content,
    parse_strict_json,
    render_prompt,
)
from app.services.news.coindesk import NewsPost


AGENT_NAMES = {
    "summary": "news_summary",
    "agenda": "news_agenda",
    "final": "news_final_score",
}


class _ChatLLM(Protocol):
    """Минимальный публичный контракт LLM-клиента (см. PriceAgent)."""

    async def chat_completion(
        self,
        *,
        agent_name: str,
        model: str,
        messages: list[dict],
        pipeline_run_id: uuid.UUID | None = ...,
        **extra,
    ) -> dict:
        ...  # pragma: no cover


@dataclass(frozen=True, slots=True)
class NewsSummary:
    """Результат :meth:`NewsAgent.summarize_post`."""

    summary: str
    sentiment: Sentiment


@dataclass(frozen=True, slots=True)
class AgendaTopic:
    """Одна тема дня внутри :class:`NewsAgenda`."""

    title: str
    description: str
    impact: Sentiment


@dataclass(frozen=True, slots=True)
class NewsAgenda:
    """Результат :meth:`NewsAgent.build_agenda`.

    ``digest`` — связный 2–4-предложный пересказ повестки, который
    используется как query-текст для RAG-поиска.
    """

    topics: tuple[AgendaTopic, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class NewsFinalScore:
    """Результат :meth:`NewsAgent.final_score`."""

    score: str
    sentiment: Sentiment


@dataclass(frozen=True, slots=True)
class SummarizedPost:
    """Пара «исходный post + сгенерированный summary».

    Удобно передавать как вход в :meth:`NewsAgent.build_agenda`,
    потому что внутри одного pipeline-тика мы и summary, и сам post
    держим в памяти до конца обработки монеты.
    """

    post: NewsPost
    summary: NewsSummary


class NewsAgent:
    """Триада LLM-вызовов вокруг новостей одного актива.

    Args:
        llm_client: LLM-клиент с методом ``chat_completion``.
        model: Имя модели; по умолчанию — ``settings.agent.news_model``.
    """

    def __init__(self, llm_client: _ChatLLM, *, model: str | None = None) -> None:
        self._llm = llm_client
        self._model = model or settings.agent.news_model

    # ---------- step 1: per-post summary ----------

    async def summarize_post(
        self,
        post: NewsPost,
        *,
        pipeline_run_id: uuid.UUID | None = None,
    ) -> NewsSummary:
        """Сгенерировать summary одной новости + её sentiment."""
        prompt = render_prompt(
            "news_summary",
            asset=post.asset,
            title=post.title,
            body=(post.raw_text or "").strip() or "(тело не получено от источника)",
            published_at=format_published_at(post.published_at),
        )
        response = await self._llm.chat_completion(
            agent_name=AGENT_NAMES["summary"],
            model=self._model,
            messages=_build_messages(prompt),
            pipeline_run_id=pipeline_run_id,
        )
        content = extract_assistant_content(response)
        return parse_news_summary(content)

    # ---------- step 2: 24h agenda ----------

    async def build_agenda(
        self,
        asset: str,
        summaries: Sequence[SummarizedPost],
        *,
        pipeline_run_id: uuid.UUID | None = None,
    ) -> NewsAgenda:
        """Собрать повестку дня по 24h-summary.

        Если ``summaries`` пуст — возвращаем синтетическую «пустую»
        повестку без вызова LLM, чтобы не платить токены за «нет
        данных». Эту особенность учитывает :meth:`final_score`.
        """
        if not summaries:
            return _empty_agenda(asset)

        summaries_block = format_summaries_block(summaries)
        prompt = render_prompt(
            "news_agenda",
            asset=asset.upper(),
            summaries_block=summaries_block,
        )
        response = await self._llm.chat_completion(
            agent_name=AGENT_NAMES["agenda"],
            model=self._model,
            messages=_build_messages(prompt),
            pipeline_run_id=pipeline_run_id,
        )
        content = extract_assistant_content(response)
        return parse_news_agenda(content)

    # ---------- step 3: RAG-aware final score ----------

    async def final_score(
        self,
        asset: str,
        *,
        agenda: NewsAgenda,
        history: Sequence[News],
        pipeline_run_id: uuid.UUID | None = None,
    ) -> NewsFinalScore:
        """Финальная оценка с учётом RAG-исторических новостей.

        Если и agenda пуста, и history пуста — отдаём нейтральную
        заглушку без вызова LLM (нечего скорить).
        """
        if not agenda.topics and not history:
            return NewsFinalScore(
                score=(
                    f"По активу {asset.upper()} за последние 24 часа значимых "
                    "новостей не зафиксировано, исторических аналогов также нет. "
                    "Сигналов новостного фона нет."
                ),
                sentiment=Sentiment.NEUTRAL,
            )

        prompt = render_prompt(
            "news_final_score",
            asset=asset.upper(),
            current_agenda_block=format_agenda_block(agenda),
            history_block=format_history_block(history),
        )
        response = await self._llm.chat_completion(
            agent_name=AGENT_NAMES["final"],
            model=self._model,
            messages=_build_messages(prompt),
            pipeline_run_id=pipeline_run_id,
        )
        content = extract_assistant_content(response)
        return parse_news_final_score(content)


# ---------- pure parsers ----------


def parse_news_summary(content: str) -> NewsSummary:
    """JSON ответа :meth:`NewsAgent.summarize_post` → :class:`NewsSummary`."""
    data = parse_strict_json(content)

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AgentJSONParseError(
            "NewsAgent.summary: 'summary' must be a non-empty string",
            raw_content=content,
        )

    try:
        sentiment = Sentiment.parse(data.get("sentiment"))
    except ValueError as exc:
        raise AgentJSONParseError(
            f"NewsAgent.summary: invalid 'sentiment': {exc}",
            raw_content=content,
        ) from exc

    return NewsSummary(summary=summary.strip(), sentiment=sentiment)


def parse_news_agenda(content: str) -> NewsAgenda:
    """JSON ответа :meth:`NewsAgent.build_agenda` → :class:`NewsAgenda`."""
    data = parse_strict_json(content)

    raw_topics = data.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise AgentJSONParseError(
            "NewsAgent.agenda: 'topics' must be a non-empty array",
            raw_content=content,
        )
    if len(raw_topics) > 3:
        # LLM «расширился» — отрезаем хвост, чтобы дальше не тащить шум.
        raw_topics = raw_topics[:3]

    topics: list[AgendaTopic] = []
    for idx, raw in enumerate(raw_topics):
        if not isinstance(raw, dict):
            raise AgentJSONParseError(
                f"NewsAgent.agenda: topics[{idx}] is not an object",
                raw_content=content,
            )
        title = raw.get("title")
        description = raw.get("description")
        impact_raw = raw.get("impact")
        if not isinstance(title, str) or not title.strip():
            raise AgentJSONParseError(
                f"NewsAgent.agenda: topics[{idx}].title must be a non-empty string",
                raw_content=content,
            )
        if not isinstance(description, str) or not description.strip():
            raise AgentJSONParseError(
                f"NewsAgent.agenda: topics[{idx}].description must be a non-empty string",
                raw_content=content,
            )
        try:
            impact = Sentiment.parse(impact_raw)
        except ValueError as exc:
            raise AgentJSONParseError(
                f"NewsAgent.agenda: topics[{idx}].impact invalid: {exc}",
                raw_content=content,
            ) from exc
        topics.append(
            AgendaTopic(
                title=title.strip(),
                description=description.strip(),
                impact=impact,
            )
        )

    digest = data.get("digest")
    if not isinstance(digest, str) or not digest.strip():
        raise AgentJSONParseError(
            "NewsAgent.agenda: 'digest' must be a non-empty string",
            raw_content=content,
        )

    return NewsAgenda(topics=tuple(topics), digest=digest.strip())


def parse_news_final_score(content: str) -> NewsFinalScore:
    """JSON ответа :meth:`NewsAgent.final_score` → :class:`NewsFinalScore`."""
    data = parse_strict_json(content)

    score = data.get("score")
    if not isinstance(score, str) or not score.strip():
        raise AgentJSONParseError(
            "NewsAgent.final: 'score' must be a non-empty string",
            raw_content=content,
        )

    try:
        sentiment = Sentiment.parse(data.get("sentiment"))
    except ValueError as exc:
        raise AgentJSONParseError(
            f"NewsAgent.final: invalid 'sentiment': {exc}",
            raw_content=content,
        ) from exc

    return NewsFinalScore(score=score.strip(), sentiment=sentiment)


# ---------- formatters (для промптов) ----------


def format_summaries_block(summaries: Sequence[SummarizedPost]) -> str:
    """``[1] 2026-06-02 14:35 UTC (bullish) <summary>\\n…``

    Дата публикации каждой новости включается в строку, чтобы NEWS-агент
    при построении повестки видел хронологию событий и мог расставлять
    приоритеты (свежее событие важнее вышедшего в начале 24h-окна).
    """
    lines: list[str] = []
    for idx, item in enumerate(summaries, start=1):
        published = format_published_at(item.post.published_at)
        lines.append(
            f"[{idx}] {published} ({item.summary.sentiment.value}) "
            f"{item.summary.summary.strip()}"
        )
    return "\n".join(lines)


def format_agenda_block(agenda: NewsAgenda) -> str:
    """Сжатое текстовое представление повестки для финального промпта."""
    if not agenda.topics:
        return "Темы за 24 часа не выделены.\nDigest: " + agenda.digest

    topic_lines: list[str] = []
    for idx, topic in enumerate(agenda.topics, start=1):
        topic_lines.append(
            f"{idx}. [{topic.impact.value}] {topic.title}: {topic.description}"
        )
    return "Темы:\n" + "\n".join(topic_lines) + f"\n\nDigest: {agenda.digest}"


def format_history_block(history: Sequence[News]) -> str:
    """Каждая историческая новость — одна строка с датой и текстом."""
    if not history:
        return "Исторических релевантных новостей не найдено."

    lines: list[str] = []
    for idx, item in enumerate(history, start=1):
        published = format_published_at(item.published_at)
        summary = (item.summary_text or item.title or "").strip()
        if len(summary) > 600:
            summary = summary[:600].rstrip() + "…"
        lines.append(f"[{idx}] {published} | {item.title.strip()}\n    {summary}")
    return "\n".join(lines)


def format_published_at(value: datetime) -> str:
    """Единый формат даты публикации новости для всех промптов NEWS-агента.

    Используем UTC c минутной точностью: для трейдингового анализа
    важна не секунда, а час события. Tz-naive значение трактуем как UTC
    (страховка для исторических записей до миграции 0006, если такие
    окажутся без timezone после round-trip через драйвер БД).
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M UTC")


# ---------- internals ----------


def _empty_agenda(asset: str) -> NewsAgenda:
    """Заглушка для случая «новостей за 24h нет».

    LLM не зовём, но возвращаем структурно корректный объект — чтобы
    pipeline и формат промпта дальше не ветвились на ``Optional``.
    """
    digest = (
        f"За последние 24 часа значимых новостей по активу {asset.upper()} "
        "не зафиксировано."
    )
    bound = logger.bind(component="news_agent", asset=asset.upper())
    bound.debug("Skipping news_agenda LLM call: empty summaries window")
    return NewsAgenda(topics=tuple(), digest=digest)


def _build_messages(prompt: str) -> list[dict]:
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


__all__ = [
    "AgendaTopic",
    "AGENT_NAMES",
    "NewsAgenda",
    "NewsAgent",
    "NewsFinalScore",
    "NewsSummary",
    "SummarizedPost",
    "format_agenda_block",
    "format_history_block",
    "format_published_at",
    "format_summaries_block",
    "parse_news_agenda",
    "parse_news_final_score",
    "parse_news_summary",
]
