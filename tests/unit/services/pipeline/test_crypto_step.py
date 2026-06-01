"""Тесты ``crypto_step``: happy-path BUY/HOLD/SELL и пути ошибок.

Все внешние сервисы заменены fake-клиентами, БД — реальная (через
``session`` фикстуру с testcontainers postgres). На каждое решение
проверяем: запись ``decisions``, запись ``transactions`` (если
сделка прошла), обновлённые балансы, корректное ``failure_reason`` для
сценариев отказа.

В тестах LLM-ответы кладутся в очередь :class:`FakeOpenRouterClient` в
точном порядке, в котором их ждёт ветка:

* PRICE-агент ↦ 1 chat,
* для каждой новой новости: 1 chat (news_summary) + 1 embedding,
* news_agenda ↦ 1 chat (только если есть новые summary),
* RAG ↦ 1 embedding (только если есть новые summary),
* news_final_score ↦ 1 chat (только если agenda не пуста ИЛИ есть history),
* TRADER ↦ 1 chat (и +1 при битом JSON-ответе — локальный ретрай).
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.crud import decision as decision_crud
from app.crud import user as user_crud
from app.crud import wallet as wallet_crud
from app.models import Decision, Transaction, Wallet
from app.models.enums import DecisionAction
from app.services.agents.news_agent import NewsAgent
from app.services.agents.price_agent import PriceAgent
from app.services.agents.trader_agent import TraderAgent
from app.services.pipeline.crypto_step import (
    PipelineContext,
    PipelineStepFailureReason,
    crypto_step,
)

from tests.unit.services.llm._helpers import FakeOpenRouterClient
from tests.unit.services.pipeline._helpers import (
    FakeBinanceClient,
    FakeNewsClient,
    embedding_response,
    klines_for_all_timeframes,
    make_book_ticker,
    make_exchange_info,
    make_news_post,
    news_agenda_chat,
    news_final_chat,
    news_summary_chat,
    price_chat,
    trader_chat,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


SYMBOL = "BTCUSDT"
ASSET = "BTC"
QUOTE = "USDT"
FEE = Decimal("0.001")


# ---------- общие фикстуры ----------


@pytest_asyncio.fixture(loop_scope="session")
async def session_factory(engine, session) -> async_sessionmaker:
    """Фабрика сессий, привязанная к тому же engine, что и ``session``.

    Зависимость от ``session`` критична: эта фикстура триггерит
    ``TRUNCATE``, выполняемый в фикстуре ``session`` перед каждым тестом.
    Без этой связи pipeline увидит мусор от предыдущих тестов.
    """
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def _seed_user(session, *, usdt: str = "1000", btc: str = "0") -> int:
    """Создать пользователя + USDT/BTC-кошельки и закоммитить.

    Pipeline открывает собственные сессии через ``session_factory``,
    поэтому seed-данные нужно зафиксировать видимо для всей БД.
    """
    user = await user_crud.create(
        session,
        telegram_id=42,
        username="trader",
        initial_capital_rub=Decimal("100000"),
        initial_capital_usdt=Decimal("1100"),
        initial_usdt_rub_rate=Decimal("90.9"),
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset=QUOTE, balance=Decimal(usdt)
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset=ASSET, balance=Decimal(btc)
    )
    await session.commit()
    return user.id


def _build_context(
    *,
    user_id: int,
    binance: FakeBinanceClient,
    news_client: FakeNewsClient,
    openrouter: FakeOpenRouterClient,
    session_factory: async_sessionmaker,
    step_timeout_seconds: int = 30,
) -> PipelineContext:
    """Контекст с реальными агентами поверх fake-клиента LLM."""
    return PipelineContext(
        user_id=user_id,
        binance_client=binance,  # type: ignore[arg-type]
        news_client=news_client,  # type: ignore[arg-type]
        openrouter_client=openrouter,
        exchange_info=make_exchange_info([SYMBOL]),
        session_factory=session_factory,
        price_agent=PriceAgent(openrouter, model="test-price"),  # type: ignore[arg-type]
        news_agent=NewsAgent(openrouter, model="test-news"),  # type: ignore[arg-type]
        trader_agent=TraderAgent(openrouter, model="test-trader", history_limit=12),  # type: ignore[arg-type]
        fee_rate=FEE,
        quote_asset=QUOTE,
        step_timeout_seconds=step_timeout_seconds,
        decisions_history_limit=12,
    )


def _binance_with_klines(*, bid: str = "66950.00", ask: str = "67050.00") -> FakeBinanceClient:
    book = make_book_ticker(bid=bid, ask=ask)
    book["symbol"] = SYMBOL
    return FakeBinanceClient(
        klines_by_interval=klines_for_all_timeframes(),
        book_ticker_by_symbol={SYMBOL: book},
    )


# ---------- happy path ----------


async def test_crypto_step_buy_creates_decision_and_transaction(
    session, session_factory
) -> None:
    """Happy path: 1 новая новость → BUY 25%; всё пишется в одной транзакции."""
    user_id = await _seed_user(session, usdt="1000")
    binance = _binance_with_klines()
    news_client = FakeNewsClient({"BTC": [make_news_post(external_id="cd-1")]})

    fake_llm = FakeOpenRouterClient(
        chat_responses=[
            price_chat(),
            news_summary_chat(),
            news_agenda_chat(),
            news_final_chat(),
            trader_chat("BUY", buy_fraction=0.25, reasoning="Сильный bullish сигнал."),
        ],
        embedding_responses=[
            embedding_response(0.1),  # save_news_with_embedding
            embedding_response(0.2),  # fetch_relevant_history (RAG)
        ],
    )

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    pipeline_run_id = uuid.uuid4()
    result = await crypto_step(
        context=ctx, asset=ASSET, pipeline_run_id=pipeline_run_id
    )

    # Pipeline-level
    assert result.asset == ASSET
    assert result.failure_reason is None
    assert result.error_text is None
    assert result.execution is not None
    assert result.execution.executed is True
    assert result.execution.transaction is not None

    # Decision в БД
    decisions = (
        await session.execute(
            select(Decision).where(Decision.pipeline_run_id == pipeline_run_id)
        )
    ).scalars().all()
    assert len(decisions) == 1
    assert decisions[0].action is DecisionAction.BUY
    assert decisions[0].executed is True
    assert decisions[0].buy_fraction == Decimal("0.2500")
    assert decisions[0].price_summary is not None
    assert decisions[0].news_score is not None

    # Транзакция и обновлённые балансы
    txs = (await session.execute(select(Transaction))).scalars().all()
    assert len(txs) == 1
    assert txs[0].asset == ASSET
    assert txs[0].decision_id == decisions[0].id

    usdt_wallet = (
        await session.execute(
            select(Wallet).where(Wallet.user_id == user_id, Wallet.asset == QUOTE)
        )
    ).scalar_one()
    btc_wallet = (
        await session.execute(
            select(Wallet).where(Wallet.user_id == user_id, Wallet.asset == ASSET)
        )
    ).scalar_one()
    assert usdt_wallet.balance < Decimal("1000")  # потратили часть
    assert btc_wallet.balance > Decimal("0")  # купили BTC

    # LLM-очереди вычерпаны до конца — лишних/пропущенных вызовов нет.
    assert len(fake_llm.chat_calls) == 5
    assert len(fake_llm.embedding_calls) == 2


async def test_crypto_step_hold_writes_decision_without_transaction(
    session, session_factory
) -> None:
    """HOLD: decision записан, transaction отсутствует, балансы те же."""
    user_id = await _seed_user(session, usdt="500", btc="0.01")
    binance = _binance_with_klines()
    news_client = FakeNewsClient({"BTC": []})  # нет свежих новостей

    fake_llm = FakeOpenRouterClient(
        chat_responses=[
            price_chat(sentiment="neutral"),
            # news_agenda пропускается (нет summaries), final_score тоже
            # пропускается (нет ни agenda.topics, ни history).
            trader_chat("HOLD", buy_fraction=None, reasoning="Боковик."),
        ],
        embedding_responses=[],
    )

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
    )
    pipeline_run_id = uuid.uuid4()
    result = await crypto_step(
        context=ctx, asset=ASSET, pipeline_run_id=pipeline_run_id
    )

    assert result.execution is not None
    assert result.execution.executed is True
    assert result.execution.transaction is None

    decisions = (await session.execute(select(Decision))).scalars().all()
    assert len(decisions) == 1
    assert decisions[0].action is DecisionAction.HOLD

    txs = (await session.execute(select(Transaction))).scalars().all()
    assert txs == []

    # Балансы не тронуты.
    usdt = (
        await session.execute(
            select(Wallet).where(Wallet.user_id == user_id, Wallet.asset == QUOTE)
        )
    ).scalar_one()
    btc = (
        await session.execute(
            select(Wallet).where(Wallet.user_id == user_id, Wallet.asset == ASSET)
        )
    ).scalar_one()
    assert usdt.balance == Decimal("500")
    assert btc.balance == Decimal("0.01")

    # ВАЖНО: news-ветка не вызывала ни news_agenda, ни news_final.
    chat_agent_names = [c["agent_name"] for c in fake_llm.chat_calls]
    assert chat_agent_names == ["price", "trader"]


async def test_crypto_step_sell_drains_asset_position(
    session, session_factory
) -> None:
    """SELL: позиция уходит в ноль, USDT растёт."""
    user_id = await _seed_user(session, usdt="100", btc="0.01")
    binance = _binance_with_klines(bid="65000.00", ask="65100.00")
    news_client = FakeNewsClient({"BTC": []})

    fake_llm = FakeOpenRouterClient(
        chat_responses=[
            price_chat(sentiment="bearish"),
            trader_chat("SELL", buy_fraction=None, reasoning="Фиксируем."),
        ],
    )

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    result = await crypto_step(
        context=ctx, asset=ASSET, pipeline_run_id=uuid.uuid4()
    )

    assert result.execution is not None
    assert result.execution.executed is True
    btc = (
        await session.execute(
            select(Wallet).where(Wallet.user_id == user_id, Wallet.asset == ASSET)
        )
    ).scalar_one()
    usdt = (
        await session.execute(
            select(Wallet).where(Wallet.user_id == user_id, Wallet.asset == QUOTE)
        )
    ).scalar_one()
    assert btc.balance == Decimal("0")
    assert usdt.balance > Decimal("100")


# ---------- failure paths ----------


async def test_crypto_step_timeout_records_hold_with_step_timeout_reason(
    session, session_factory, monkeypatch
) -> None:
    """Таймаут шага → HOLD + executed=false + STEP_TIMEOUT."""
    user_id = await _seed_user(session)
    binance = _binance_with_klines()
    news_client = FakeNewsClient({"BTC": []})

    fake_llm = FakeOpenRouterClient(chat_responses=[price_chat(), trader_chat("HOLD")])

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
        step_timeout_seconds=1,
    )

    # Подменяем klines-загрузку на «бесконечно медленную» — это
    # гарантирует, что внутренняя ветка не успеет завершиться за
    # 1 секунду таймаута. Патчим по строковому пути, потому что
    # ``app.services.pipeline.crypto_step`` как имя в пакете
    # переопределено функцией ``crypto_step`` (re-export в __init__).
    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(5)
        raise AssertionError("should not reach here")

    monkeypatch.setattr(
        "app.services.pipeline.crypto_step.fetch_price_metrics",
        slow_fetch,
    )

    result = await crypto_step(
        context=ctx, asset=ASSET, pipeline_run_id=uuid.uuid4()
    )

    assert result.failure_reason == PipelineStepFailureReason.STEP_TIMEOUT.value
    assert result.execution is None
    assert result.decision.action is DecisionAction.HOLD
    assert result.decision.executed is False
    assert result.decision.not_executed_reason == "STEP_TIMEOUT"

    # В БД появилось ровно одно «провальное» решение.
    decisions = (await session.execute(select(Decision))).scalars().all()
    assert len(decisions) == 1
    assert decisions[0].not_executed_reason == "STEP_TIMEOUT"


async def test_crypto_step_price_branch_failure_records_failure(
    session, session_factory
) -> None:
    """Пустой klines у Binance → PRICE_BRANCH_FAILED, шаг продолжает жить."""
    user_id = await _seed_user(session)
    binance = FakeBinanceClient(
        klines_by_interval={},  # ни одной свечи на любой интервал
        book_ticker_by_symbol={
            SYMBOL: {**make_book_ticker(), "symbol": SYMBOL},
        },
    )
    news_client = FakeNewsClient({"BTC": []})
    fake_llm = FakeOpenRouterClient(chat_responses=[])

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    result = await crypto_step(
        context=ctx, asset=ASSET, pipeline_run_id=uuid.uuid4()
    )

    assert result.failure_reason == PipelineStepFailureReason.PRICE_BRANCH_FAILED.value
    assert result.execution is None
    assert result.decision.executed is False
    assert "No price metrics" in (result.error_text or "")


async def test_crypto_step_news_branch_failure_records_failure(
    session, session_factory
) -> None:
    """Падение в news-ветке → NEWS_BRANCH_FAILED."""
    user_id = await _seed_user(session)
    binance = _binance_with_klines()

    class BrokenNewsClient:
        async def fetch_recent(self, asset, *, limit=None):
            raise RuntimeError("news provider down")

    fake_llm = FakeOpenRouterClient(chat_responses=[price_chat()])

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=BrokenNewsClient(),  # type: ignore[arg-type]
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    result = await crypto_step(
        context=ctx, asset=ASSET, pipeline_run_id=uuid.uuid4()
    )

    assert result.failure_reason == PipelineStepFailureReason.NEWS_BRANCH_FAILED.value
    assert "news provider down" in (result.error_text or "")


async def test_crypto_step_trader_parse_failure_records_llm_parse_failed(
    session, session_factory
) -> None:
    """Два битых JSON подряд от TRADER → LLM_PARSE_FAILED."""
    user_id = await _seed_user(session)
    binance = _binance_with_klines()
    news_client = FakeNewsClient({"BTC": []})

    fake_llm = FakeOpenRouterClient(
        chat_responses=[
            price_chat(),
            # Два битых ответа TRADER подряд исчерпывают локальный ретрай.
            {
                "id": "x",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "мусор-1"}}],
            },
            {
                "id": "x",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "мусор-2"}}],
            },
        ],
    )

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    result = await crypto_step(
        context=ctx, asset=ASSET, pipeline_run_id=uuid.uuid4()
    )

    assert result.failure_reason == PipelineStepFailureReason.LLM_PARSE_FAILED.value
    assert result.decision.executed is False
    assert result.decision.not_executed_reason == "LLM_PARSE_FAILED"


async def test_crypto_step_min_notional_rejection_keeps_decision_not_executed(
    session, session_factory
) -> None:
    """Если AI хочет купить, но сумма < min_notional, decision.executed=false с MIN_NOTIONAL."""
    user_id = await _seed_user(session, usdt="5")  # 5 USDT — мало для min_notional=10
    binance = _binance_with_klines()
    news_client = FakeNewsClient({"BTC": []})

    fake_llm = FakeOpenRouterClient(
        chat_responses=[
            price_chat(),
            trader_chat("BUY", buy_fraction=1.0, reasoning="Ставим всё."),
        ],
    )

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    result = await crypto_step(
        context=ctx, asset=ASSET, pipeline_run_id=uuid.uuid4()
    )

    assert result.execution is not None
    assert result.execution.executed is False
    assert result.execution.not_executed_reason == "MIN_NOTIONAL"
    # Decision записан, но executed=false.
    decision = (await session.execute(select(Decision))).scalar_one()
    assert decision.action is DecisionAction.BUY
    assert decision.executed is False
    assert decision.not_executed_reason == "MIN_NOTIONAL"


async def test_crypto_step_uses_history_for_trader(
    session, session_factory
) -> None:
    """Предыдущие решения по этому активу попадают в промпт TRADER-агента."""
    user_id = await _seed_user(session)
    # Заранее сохраняем 2 предыдущих решения по BTC.
    await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=uuid.uuid4(),
        asset=ASSET,
        action=DecisionAction.HOLD,
        reasoning="Старая пауза.",
        executed=True,
    )
    await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=uuid.uuid4(),
        asset=ASSET,
        action=DecisionAction.HOLD,
        reasoning="Ещё одна пауза.",
        executed=True,
    )
    await session.commit()

    binance = _binance_with_klines()
    news_client = FakeNewsClient({"BTC": []})
    fake_llm = FakeOpenRouterClient(
        chat_responses=[
            price_chat(),
            trader_chat("HOLD", buy_fraction=None, reasoning="Третья пауза."),
        ],
    )

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    await crypto_step(context=ctx, asset=ASSET, pipeline_run_id=uuid.uuid4())

    trader_call = [c for c in fake_llm.chat_calls if c["agent_name"] == "trader"][0]
    user_msg = trader_call["messages"][-1]["content"]
    # В блоке истории должны быть оба прошлых HOLD.
    assert user_msg.count("HOLD") >= 2
