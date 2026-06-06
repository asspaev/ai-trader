"""Шаг pipeline по одной монете.

Один вызов :func:`crypto_step` = одна монета на одном тике. Алгоритм
полностью соответствует ``architecture.md`` §9:

1. **Параллельно** через ``asyncio.gather``:

   * PRICE-ветка: тянет klines у Binance, агрегирует :data:`TIMEFRAMES`,
     гонит метрики через :class:`PriceAgent` → :class:`PriceSummary`.
   * NEWS-ветка: тянет статьи CoinDesk Data, фильтрует дубликаты,
     для каждого нового вызывает :meth:`NewsAgent.summarize_post`
     и сохраняет новость + эмбеддинг отдельной транзакцией. Затем
     :meth:`NewsAgent.build_agenda`, RAG (cosine top-K, исключая
     последние 24h) и :meth:`NewsAgent.final_score`.

2. После завершения веток — одна общая БД-транзакция:

   * читаем кошельки и последние ``decisions_history_limit`` решений,
   * берём свежий ``bookTicker`` (bid/ask) для оценки и исполнения,
   * вызываем :class:`TraderAgent`,
   * пишем :class:`Decision` (``executed=None``),
   * запускаем :func:`execute_decision` (mock-биржа сама проставляет
     ``executed`` и ``not_executed_reason``),
   * коммитим.

3. На любую ошибку или таймаут шага (см.
   ``TRADING_PIPELINE_STEP_TIMEOUT_SECONDS``) пишем в отдельной
   транзакции «провальный» :class:`Decision` с ``action=HOLD``,
   ``executed=False`` и стандартизированной причиной из
   :class:`PipelineStepFailureReason`. Pipeline-runner ловит результат
   и продолжает со следующей монеты.

Все сервисы (Binance, CoinDesk Data, OpenRouter, агенты) собраны в
:class:`PipelineContext`, чтобы сигнатуры функций оставались узкими и
не плодились параметры.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.crud import decision as decision_crud
from app.crud import news as news_crud
from app.crud import wallet as wallet_crud
from app.models import Decision, News
from app.models.enums import DecisionAction
from app.services.agents.base import AgentError, AgentJSONParseError, Sentiment
from app.services.agents.news_agent import (
    NewsAgent,
    NewsFinalScore,
    NewsSummary,
    SummarizedPost,
)
from app.services.agents.price_agent import PriceAgent, PriceSummary
from app.services.agents.trader_agent import TraderAgent, WalletSnapshot
from app.services.binance.client import BinanceClient
from app.services.binance.exchange_info import ExchangeInfoCache
from app.services.binance.prices import (
    BookTicker,
    fetch_book_ticker,
    fetch_price_metrics,
)
from app.services.mock_exchange.executor import ExecutionResult, execute_decision
from app.services.news.coindesk import CoinDeskNewsClient, NewsPost
from app.services.news.deduplicator import filter_new_posts
from app.services.news.rag import fetch_relevant_history
from app.services.news.storage import (
    save_news_reusing_cached,
    save_news_with_embedding,
)


class PipelineStepFailureReason(StrEnum):
    """Стандартизированные причины неисполнения, специфичные для pipeline.

    Дополняет :class:`app.services.mock_exchange.NotExecutedReason`,
    которая описывает чисто биржевые отказы. Здесь собраны причины,
    возникающие до фазы исполнения сделки.
    """

    STEP_TIMEOUT = "STEP_TIMEOUT"
    """Шаг не уложился в ``TRADING_PIPELINE_STEP_TIMEOUT_SECONDS``."""

    LLM_PARSE_FAILED = "LLM_PARSE_FAILED"
    """TRADER-агент не вернул валидный JSON за допустимое число попыток."""

    AGENT_ERROR = "AGENT_ERROR"
    """LLM-сервис ошибся (сеть/5xx после ретраев, неожиданный формат)."""

    PRICE_BRANCH_FAILED = "PRICE_BRANCH_FAILED"
    """Не получилось собрать ценовые метрики (нет ответа Binance и т.п.)."""

    NEWS_BRANCH_FAILED = "NEWS_BRANCH_FAILED"
    """NEWS-ветка упала (CoinDesk Data недоступен, embedding-ошибка и т.п.)."""

    STEP_ERROR = "STEP_ERROR"
    """Любая другая необработанная ошибка — последний рубеж."""


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """Набор «синглтонов» pipeline на один тик/процесс.

    Сюда складываем уже инстанцированных клиентов и агентов, плюс
    параметры из ``settings`` — так шаг тика не зависит от глобального
    ``settings``-объекта и легко мокируется в тестах.

    Attributes:
        user_id: ID единственного пользователя (создаётся
            ``scripts/init_user.py`` в фазе 3).
        binance_client: Долгоживущий :class:`BinanceClient` с keep-alive.
        news_client: Долгоживущий :class:`CoinDeskNewsClient`.
        openrouter_client: Клиент OpenRouter с трекингом в ``llm_calls``.
        exchange_info: Кэш биржевых фильтров, загруженный один раз при
            старте процесса.
        session_factory: Фабрика async-сессий БД. Каждая «маленькая»
            транзакция (сохранение новости, dedup, финальная сделка)
            открывает собственную сессию — outer не делим.
        price_agent / news_agent / trader_agent: Инстансы агентов.
        fee_rate: Доля комиссии taker (``settings.binance.taker_fee``).
        quote_asset: Котировочный актив (``"USDT"``).
        step_timeout_seconds: Жёсткий потолок на один шаг монеты.
        decisions_history_limit: Сколько прошлых решений показывать
            TRADER-агенту (от ``TRADING_DECISIONS_HISTORY_LIMIT``).
    """

    user_id: int
    binance_client: BinanceClient
    news_client: CoinDeskNewsClient
    openrouter_client: object  # OpenRouterClient | FakeOpenRouterClient
    exchange_info: ExchangeInfoCache
    session_factory: async_sessionmaker[AsyncSession]
    price_agent: PriceAgent
    news_agent: NewsAgent
    trader_agent: TraderAgent
    fee_rate: Decimal
    quote_asset: str
    step_timeout_seconds: int
    decisions_history_limit: int

    @classmethod
    def build(
        cls,
        *,
        user_id: int,
        binance_client: BinanceClient,
        news_client: CoinDeskNewsClient,
        openrouter_client: object,
        exchange_info: ExchangeInfoCache,
        session_factory: async_sessionmaker[AsyncSession],
        price_agent: PriceAgent | None = None,
        news_agent: NewsAgent | None = None,
        trader_agent: TraderAgent | None = None,
        fee_rate: Decimal | None = None,
        quote_asset: str | None = None,
        step_timeout_seconds: int | None = None,
        decisions_history_limit: int | None = None,
    ) -> "PipelineContext":
        """Сконструировать контекст с дефолтами из ``settings``.

        Все «производственные» значения берутся из ``settings``, но
        каждое можно переопределить — это нужно в тестах, где не нужны
        реальные модели и реальные таймауты.
        """
        return cls(
            user_id=user_id,
            binance_client=binance_client,
            news_client=news_client,
            openrouter_client=openrouter_client,
            exchange_info=exchange_info,
            session_factory=session_factory,
            price_agent=price_agent or PriceAgent(openrouter_client),  # type: ignore[arg-type]
            news_agent=news_agent or NewsAgent(openrouter_client),  # type: ignore[arg-type]
            trader_agent=trader_agent or TraderAgent(openrouter_client),  # type: ignore[arg-type]
            fee_rate=fee_rate if fee_rate is not None else settings.binance.taker_fee,
            quote_asset=quote_asset or settings.trading.quote_asset,
            step_timeout_seconds=(
                step_timeout_seconds
                if step_timeout_seconds is not None
                else settings.trading.pipeline_step_timeout_seconds
            ),
            decisions_history_limit=(
                decisions_history_limit
                if decisions_history_limit is not None
                else settings.trading.decisions_history_limit
            ),
        )


@dataclass(frozen=True, slots=True)
class CryptoStepResult:
    """Итог одного шага pipeline по одной монете.

    Attributes:
        asset: Тикер актива (``"BTC"`` и т.д.).
        pipeline_run_id: Идентификатор тика (общий для всех монет).
        decision: Сохранённый :class:`Decision`. Всегда не ``None`` —
            даже при ошибке/таймауте мы пишем «провальный» HOLD.
        execution: Итог mock-биржи (только если шаг дошёл до неё),
            иначе ``None``.
        failure_reason: Заполнено, если шаг не дошёл до TRADER или
            TRADER вернул невалидный ответ (см.
            :class:`PipelineStepFailureReason`).
        error_text: Короткое описание исключения, если оно было.
        duration_seconds: Сколько шёл шаг (для метрик/уведомлений).
    """

    asset: str
    pipeline_run_id: uuid.UUID
    decision: Decision
    execution: ExecutionResult | None
    failure_reason: str | None
    error_text: str | None
    duration_seconds: float


# ---------- публичная точка входа ----------


async def crypto_step(
    *,
    context: PipelineContext,
    asset: str,
    pipeline_run_id: uuid.UUID,
) -> CryptoStepResult:
    """Обработать одну монету и вернуть подробный итог.

    Никогда не бросает: любая ошибка превращается в
    :class:`CryptoStepResult` с ``failure_reason`` — pipeline-runner
    должен продолжить со следующей монеты без падения всего тика.
    """
    asset_upper = asset.upper()
    bound = logger.bind(
        component="pipeline.crypto_step",
        asset=asset_upper,
        pipeline_run_id=str(pipeline_run_id),
    )
    started_at = datetime.now(timezone.utc)
    bound.info("Crypto step started")

    try:
        result = await asyncio.wait_for(
            _crypto_step_inner(
                context=context,
                asset=asset_upper,
                pipeline_run_id=pipeline_run_id,
                bound=bound,
            ),
            timeout=context.step_timeout_seconds,
        )
        duration = (datetime.now(timezone.utc) - started_at).total_seconds()
        bound.info(
            "Crypto step finished: action={action}, executed={executed}, "
            "duration={duration:.2f}s",
            action=result.decision.action.value,
            executed=result.execution.executed if result.execution else None,
            duration=duration,
        )
        return _with_duration(result, duration)

    except asyncio.TimeoutError:
        duration = (datetime.now(timezone.utc) - started_at).total_seconds()
        bound.error(
            "Crypto step timed out after {timeout}s",
            timeout=context.step_timeout_seconds,
        )
        return await _record_failure(
            context=context,
            asset=asset_upper,
            pipeline_run_id=pipeline_run_id,
            reason=PipelineStepFailureReason.STEP_TIMEOUT,
            error_text=f"step timeout after {context.step_timeout_seconds}s",
            duration_seconds=duration,
        )

    except AgentJSONParseError as exc:
        duration = (datetime.now(timezone.utc) - started_at).total_seconds()
        bound.error("Crypto step failed: trader JSON parse failed: {exc}", exc=exc)
        return await _record_failure(
            context=context,
            asset=asset_upper,
            pipeline_run_id=pipeline_run_id,
            reason=PipelineStepFailureReason.LLM_PARSE_FAILED,
            error_text=_truncate(f"{type(exc).__name__}: {exc}"),
            duration_seconds=duration,
        )

    except _BranchFailure as exc:
        duration = (datetime.now(timezone.utc) - started_at).total_seconds()
        bound.error(
            "Crypto step failed in {branch} branch: {exc}",
            branch=exc.branch,
            exc=exc.original,
        )
        return await _record_failure(
            context=context,
            asset=asset_upper,
            pipeline_run_id=pipeline_run_id,
            reason=exc.reason,
            error_text=_truncate(f"{type(exc.original).__name__}: {exc.original}"),
            duration_seconds=duration,
        )

    except AgentError as exc:
        duration = (datetime.now(timezone.utc) - started_at).total_seconds()
        bound.exception("Crypto step failed: agent error")
        return await _record_failure(
            context=context,
            asset=asset_upper,
            pipeline_run_id=pipeline_run_id,
            reason=PipelineStepFailureReason.AGENT_ERROR,
            error_text=_truncate(f"{type(exc).__name__}: {exc}"),
            duration_seconds=duration,
        )

    except Exception as exc:  # noqa: BLE001 — последний рубеж
        duration = (datetime.now(timezone.utc) - started_at).total_seconds()
        bound.exception("Crypto step failed with unexpected error")
        return await _record_failure(
            context=context,
            asset=asset_upper,
            pipeline_run_id=pipeline_run_id,
            reason=PipelineStepFailureReason.STEP_ERROR,
            error_text=_truncate(f"{type(exc).__name__}: {exc}"),
            duration_seconds=duration,
        )


# ---------- happy-path: основное тело ----------


async def _crypto_step_inner(
    *,
    context: PipelineContext,
    asset: str,
    pipeline_run_id: uuid.UUID,
    bound,
) -> CryptoStepResult:
    """Полный happy-path шага монеты.

    Может бросить :class:`_BranchFailure` (вверх ловится в
    :func:`crypto_step`) или :class:`AgentJSONParseError` от TRADER.
    """
    symbol = f"{asset}{context.quote_asset}"

    price_task = asyncio.create_task(
        _price_branch(context=context, asset=asset, pipeline_run_id=pipeline_run_id),
        name=f"price-{asset}",
    )
    news_task = asyncio.create_task(
        _news_branch(context=context, asset=asset, pipeline_run_id=pipeline_run_id),
        name=f"news-{asset}",
    )

    results = await asyncio.gather(price_task, news_task, return_exceptions=True)
    price_outcome, news_outcome = results

    if isinstance(price_outcome, BaseException):
        # Подождём, пока news-task тоже завершится, чтобы не оставить
        # болтающуюся таску в фоне (она держит httpx-соединения).
        if not news_task.done():
            news_task.cancel()
            try:
                await news_task
            except BaseException:  # noqa: BLE001 — игнорируем
                pass
        raise _BranchFailure(
            branch="price",
            reason=PipelineStepFailureReason.PRICE_BRANCH_FAILED,
            original=price_outcome,
        )
    if isinstance(news_outcome, BaseException):
        raise _BranchFailure(
            branch="news",
            reason=PipelineStepFailureReason.NEWS_BRANCH_FAILED,
            original=news_outcome,
        )

    price_summary: PriceSummary = price_outcome
    news_final: NewsFinalScore = news_outcome
    bound.info(
        "Branches done: price_sentiment={price}, news_sentiment={news}",
        price=price_summary.sentiment.value,
        news=news_final.sentiment.value,
    )

    book_ticker = await fetch_book_ticker(context.binance_client, symbol)
    bound.debug(
        "Book ticker: bid={bid}, ask={ask}",
        bid=str(book_ticker.bid_price),
        ask=str(book_ticker.ask_price),
    )

    return await _decide_and_execute(
        context=context,
        asset=asset,
        symbol=symbol,
        pipeline_run_id=pipeline_run_id,
        price_summary=price_summary,
        news_final=news_final,
        book_ticker=book_ticker,
    )


async def _decide_and_execute(
    *,
    context: PipelineContext,
    asset: str,
    symbol: str,
    pipeline_run_id: uuid.UUID,
    price_summary: PriceSummary,
    news_final: NewsFinalScore,
    book_ticker: BookTicker,
) -> CryptoStepResult:
    """TRADER + сохранение Decision + исполнение mock-сделки в одной транзакции."""
    filters = context.exchange_info.get(symbol)

    async with context.session_factory() as session:
        wallet = await _build_wallet_snapshot(
            session,
            user_id=context.user_id,
            asset=asset,
            quote_asset=context.quote_asset,
            book_ticker=book_ticker,
        )
        history = await decision_crud.list_last_for_asset(
            session,
            user_id=context.user_id,
            asset=asset,
            limit=context.decisions_history_limit,
        )

        trader_decision = await context.trader_agent.decide(
            asset=asset,
            wallet=wallet,
            price=price_summary,
            news=news_final,
            history=history,
            pipeline_run_id=pipeline_run_id,
        )

        decision = await decision_crud.create(
            session,
            user_id=context.user_id,
            pipeline_run_id=pipeline_run_id,
            asset=asset,
            action=trader_decision.action,
            buy_fraction=trader_decision.buy_fraction,
            price_summary=price_summary.summary,
            news_score=news_final.score,
            reasoning=trader_decision.reasoning,
            executed=None,
        )

        execution = await execute_decision(
            session,
            decision=decision,
            symbol=symbol,
            quote_asset=context.quote_asset,
            filters=filters,
            book_ticker=book_ticker,
            fee_rate=context.fee_rate,
        )

        await session.commit()

    return CryptoStepResult(
        asset=asset,
        pipeline_run_id=pipeline_run_id,
        decision=decision,
        execution=execution,
        failure_reason=(
            execution.not_executed_reason if execution and not execution.executed else None
        ),
        error_text=None,
        duration_seconds=0.0,  # будет заполнено в crypto_step
    )


# ---------- ветка PRICE ----------


async def _price_branch(
    *,
    context: PipelineContext,
    asset: str,
    pipeline_run_id: uuid.UUID,
) -> PriceSummary:
    """Скачать klines и прогнать через :class:`PriceAgent`."""
    symbol = f"{asset}{context.quote_asset}"
    metrics = await fetch_price_metrics(context.binance_client, symbol)
    if not metrics:
        raise RuntimeError(f"No price metrics returned for {symbol}")
    return await context.price_agent.run(
        asset=asset,
        metrics=metrics,
        pipeline_run_id=pipeline_run_id,
    )


# ---------- ветка NEWS ----------


async def _news_branch(
    *,
    context: PipelineContext,
    asset: str,
    pipeline_run_id: uuid.UUID,
) -> NewsFinalScore:
    """Все три LLM-вызова NEWS + RAG; новости сохраняются по одной.

    Если статья уже есть в БД под другим активом (одна и та же новость
    приходит под разные ``categories=BTC|ETH|TON``), переиспользуем её
    сохранённые ``summary_text`` + ``summary_sentiment`` + ``embedding``
    и не зовём LLM/embedding-сервис повторно — это экономит как
    summary-вызов, так и embedding-вызов на каждый такой дубль.
    """
    bound = logger.bind(
        component="pipeline.crypto_step",
        asset=asset,
        pipeline_run_id=str(pipeline_run_id),
    )

    posts = await context.news_client.fetch_recent(asset)

    async with context.session_factory() as dedup_session:
        new_posts = await filter_new_posts(
            dedup_session, asset=asset, posts=posts
        )
        cached_by_eid = await news_crud.fetch_cached_by_external_ids(
            dedup_session,
            external_ids=[p.external_id for p in new_posts],
        )

    # Per-post обработка параллельно через asyncio.gather: каждая
    # задача делает свой LLM-summary + embedding + сохранение в
    # отдельной транзакции — это I/O-bound работа, и при 10–20
    # свежих постах последовательный цикл легко съедал весь
    # бюджет шага (300с). gather сохраняет порядок результатов,
    # поэтому хронология summaries в agenda-блоке не меняется.
    post_outcomes = await asyncio.gather(
        *(
            _process_one_post(
                context=context,
                post=post,
                cached_by_eid=cached_by_eid,
                pipeline_run_id=pipeline_run_id,
            )
            for post in new_posts
        )
    )
    summarized: list[SummarizedPost] = [item for item, _ in post_outcomes]
    reused_count = sum(1 for _, reused in post_outcomes if reused)

    if reused_count:
        bound.info(
            "Reused cached summary/embedding for {n}/{total} news posts",
            n=reused_count,
            total=len(new_posts),
        )

    agenda = await context.news_agent.build_agenda(
        asset, summarized, pipeline_run_id=pipeline_run_id
    )

    # RAG имеет смысл, только если в окне 24h появилось что-то новое:
    # на «тихом» дне build_agenda возвращает заглушку с шаблонным
    # digest «значимых новостей не зафиксировано» — эмбеддить такой
    # текст и тянуть к нему «исторические аналоги» — пустая трата
    # токенов и времени. Если новых summary нет — RAG пропускаем.
    history: Sequence = []
    if summarized:
        async with context.session_factory() as rag_session:
            history = await fetch_relevant_history(
                rag_session,
                context.openrouter_client,  # type: ignore[arg-type]
                asset=asset,
                query_text=agenda.digest,
                pipeline_run_id=pipeline_run_id,
            )

    return await context.news_agent.final_score(
        asset,
        agenda=agenda,
        history=history,
        pipeline_run_id=pipeline_run_id,
    )


async def _process_one_post(
    *,
    context: PipelineContext,
    post: NewsPost,
    cached_by_eid: dict[str, News],
    pipeline_run_id: uuid.UUID,
) -> tuple[SummarizedPost, bool]:
    """Обработать одну новость: переиспользовать кэш либо вызвать LLM+embedding.

    Возвращает пару ``(SummarizedPost, reused_from_cache)``. Любой провал
    (LLM-ошибка, embedding-ошибка, БД) поднимается наверх — :func:`asyncio.gather`
    приведёт это к падению NEWS-ветки, что в :func:`crypto_step` превратится в
    ``NEWS_BRANCH_FAILED`` (поведение симметрично прежней последовательной версии).
    """
    cached = cached_by_eid.get(post.external_id)
    reusable = _extract_reusable_summary(cached)

    if reusable is not None:
        cached_summary, cached_embedding = reusable
        async with context.session_factory() as save_session:
            await save_news_reusing_cached(
                save_session,
                post=post,
                summary_text=cached_summary.summary,
                summary_sentiment=cached_summary.sentiment.value,
                embedding=cached_embedding,
            )
            await save_session.commit()
        return SummarizedPost(post=post, summary=cached_summary), True

    summary = await context.news_agent.summarize_post(
        post, pipeline_run_id=pipeline_run_id
    )
    async with context.session_factory() as save_session:
        await save_news_with_embedding(
            save_session,
            context.openrouter_client,  # type: ignore[arg-type]
            post=post,
            summary_text=summary.summary,
            summary_sentiment=summary.sentiment.value,
            pipeline_run_id=pipeline_run_id,
        )
        await save_session.commit()
    return SummarizedPost(post=post, summary=summary), False


# ---------- failure-path: запись HOLD/executed=False ----------


async def _record_failure(
    *,
    context: PipelineContext,
    asset: str,
    pipeline_run_id: uuid.UUID,
    reason: PipelineStepFailureReason,
    error_text: str,
    duration_seconds: float,
) -> CryptoStepResult:
    """В отдельной транзакции записать «провальный» HOLD-decision.

    Используется при любом отказе — таймауте, краше LLM, упавшей ветке.
    Если запись падает (БД недоступна) — поднимаем выше: тогда даже
    pipeline-runner не сможет продолжить, и это правильная сигнализация.
    """
    async with context.session_factory() as session:
        decision = await decision_crud.create(
            session,
            user_id=context.user_id,
            pipeline_run_id=pipeline_run_id,
            asset=asset,
            action=DecisionAction.HOLD,
            buy_fraction=None,
            price_summary=None,
            news_score=None,
            reasoning=error_text,
            executed=False,
            not_executed_reason=reason.value,
        )
        await session.commit()

    return CryptoStepResult(
        asset=asset,
        pipeline_run_id=pipeline_run_id,
        decision=decision,
        execution=None,
        failure_reason=reason.value,
        error_text=error_text,
        duration_seconds=duration_seconds,
    )


# ---------- helpers ----------


async def _build_wallet_snapshot(
    session: AsyncSession,
    *,
    user_id: int,
    asset: str,
    quote_asset: str,
    book_ticker: BookTicker,
) -> WalletSnapshot:
    """Снимок кошелька для TRADER-агента.

    В качестве «текущей цены» отдаём bid — это консервативная оценка
    стоимости позиции (как если бы её продали сейчас).
    """
    usdt_wallet = await wallet_crud.get(session, user_id=user_id, asset=quote_asset)
    asset_wallet = await wallet_crud.get(session, user_id=user_id, asset=asset)
    return WalletSnapshot(
        free_usdt=usdt_wallet.balance if usdt_wallet else Decimal("0"),
        asset_balance=asset_wallet.balance if asset_wallet else Decimal("0"),
        asset_price_usdt=book_ticker.bid_price,
    )


def _with_duration(result: CryptoStepResult, duration: float) -> CryptoStepResult:
    """Вернуть копию ``result`` с проставленным ``duration_seconds``."""
    return CryptoStepResult(
        asset=result.asset,
        pipeline_run_id=result.pipeline_run_id,
        decision=result.decision,
        execution=result.execution,
        failure_reason=result.failure_reason,
        error_text=result.error_text,
        duration_seconds=duration,
    )


def _extract_reusable_summary(
    cached: News | None,
) -> tuple[NewsSummary, list[float]] | None:
    """Если в БД уже есть строка по этому ``external_id`` (для другого
    актива) с полным набором кэшируемых полей — собрать из неё
    :class:`NewsSummary` + embedding для копирования. Иначе вернуть
    ``None``: тогда вызывающий сходит в LLM/embedding как обычно.

    Не переиспользуем, если у строки нет ``summary_text``,
    ``summary_sentiment`` или ``embedding``: это могут быть «древние»
    записи до миграции 0005 или незавершённые из-за давнего сбоя —
    лучше прогнать заново, чем складировать неполный кэш.
    """
    if cached is None:
        return None
    if not cached.summary_text or not cached.summary_sentiment:
        return None
    if cached.embedding is None:
        return None
    try:
        sentiment = Sentiment.parse(cached.summary_sentiment)
    except ValueError:
        return None
    return (
        NewsSummary(summary=cached.summary_text, sentiment=sentiment),
        list(cached.embedding),
    )


def _truncate(text: str, max_len: int = 128) -> str:
    """Урезаем длину под колонку ``decisions.not_executed_reason``-смежных полей.

    ``reasoning`` — TEXT, но не хотим складывать туда мегабайты
    стектрейсов. Хватит первой строки и пары сотен символов.
    """
    cleaned = " ".join(text.split())
    return cleaned[:max_len]


class _BranchFailure(Exception):
    """Внутренний сигнал об отказе одной из веток PRICE/NEWS.

    Несёт исходное исключение и стандартизированную причину для записи
    в :class:`Decision`. Наверх не выходит — :func:`crypto_step` сразу
    превращает её в :class:`CryptoStepResult` через
    :func:`_record_failure`.
    """

    def __init__(
        self,
        *,
        branch: str,
        reason: PipelineStepFailureReason,
        original: BaseException,
    ) -> None:
        super().__init__(f"{branch} branch failed: {original!r}")
        self.branch = branch
        self.reason = reason
        self.original = original


__all__ = [
    "CryptoStepResult",
    "PipelineContext",
    "PipelineStepFailureReason",
    "crypto_step",
]
