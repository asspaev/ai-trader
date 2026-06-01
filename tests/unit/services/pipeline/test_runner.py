"""Тесты ``run_pipeline_once``: последовательный обход 3 монет, общий pipeline_run_id."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.crud import user as user_crud
from app.crud import wallet as wallet_crud
from app.models import Decision
from app.services.agents.news_agent import NewsAgent
from app.services.agents.price_agent import PriceAgent
from app.services.agents.trader_agent import TraderAgent
from app.services.binance.exchange_info import ExchangeInfoCache, SymbolFilters
from app.services.pipeline.crypto_step import (
    PipelineContext,
    PipelineStepFailureReason,
)
from app.services.pipeline.runner import run_pipeline_once

from tests.unit.services.llm._helpers import FakeOpenRouterClient
from tests.unit.services.pipeline._helpers import (
    FakeBinanceClient,
    FakeNewsClient,
    klines_for_all_timeframes,
    make_book_ticker,
    price_chat,
    trader_chat,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


FEE = Decimal("0.001")
QUOTE = "USDT"
ASSETS = ("BTC", "ETH", "TON")


@pytest_asyncio.fixture(loop_scope="session")
async def session_factory(engine, session) -> async_sessionmaker:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


def _filters(symbol: str, asset: str) -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        base_asset=asset,
        quote_asset=QUOTE,
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_notional=Decimal("10"),
    )


def _three_asset_binance() -> FakeBinanceClient:
    """FakeBinance с одинаковыми klines под каждый интервал и bookTicker под 3 пары."""
    klines = klines_for_all_timeframes()
    book_ticker_by_symbol = {}
    for asset in ASSETS:
        symbol = f"{asset}{QUOTE}"
        book = {**make_book_ticker(bid="100.00", ask="101.00"), "symbol": symbol}
        book_ticker_by_symbol[symbol] = book
    return FakeBinanceClient(
        klines_by_interval=klines,
        book_ticker_by_symbol=book_ticker_by_symbol,
    )


def _exchange_info() -> ExchangeInfoCache:
    return ExchangeInfoCache([_filters(f"{a}{QUOTE}", a) for a in ASSETS])


async def _seed_user(session) -> int:
    user = await user_crud.create(
        session,
        telegram_id=42,
        username="trader",
        initial_capital_rub=Decimal("100000"),
        initial_capital_usdt=Decimal("1100"),
        initial_usdt_rub_rate=Decimal("90.9"),
    )
    await wallet_crud.upsert(session, user_id=user.id, asset=QUOTE, balance=Decimal("1000"))
    for asset in ASSETS:
        await wallet_crud.upsert(session, user_id=user.id, asset=asset, balance=Decimal("0"))
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
    return PipelineContext(
        user_id=user_id,
        binance_client=binance,  # type: ignore[arg-type]
        news_client=news_client,  # type: ignore[arg-type]
        openrouter_client=openrouter,
        exchange_info=_exchange_info(),
        session_factory=session_factory,
        price_agent=PriceAgent(openrouter, model="test-price"),  # type: ignore[arg-type]
        news_agent=NewsAgent(openrouter, model="test-news"),  # type: ignore[arg-type]
        trader_agent=TraderAgent(openrouter, model="test-trader", history_limit=12),  # type: ignore[arg-type]
        fee_rate=FEE,
        quote_asset=QUOTE,
        step_timeout_seconds=step_timeout_seconds,
        decisions_history_limit=12,
    )


# ---------- happy path ----------


async def test_run_pipeline_once_processes_three_assets_with_shared_run_id(
    session, session_factory
) -> None:
    """3 монеты подряд → 3 decisions с одним pipeline_run_id."""
    user_id = await _seed_user(session)

    binance = _three_asset_binance()
    news_client = FakeNewsClient({a: [] for a in ASSETS})

    # На каждую монету — 1 price + 1 trader (новостей нет, NEWS-агент
    # short-circuits, как в test_hold_writes_decision_without_transaction).
    fake_llm = FakeOpenRouterClient(
        chat_responses=[
            price_chat(),
            trader_chat("HOLD", buy_fraction=None, reasoning="BTC ждём."),
            price_chat(),
            trader_chat("HOLD", buy_fraction=None, reasoning="ETH ждём."),
            price_chat(),
            trader_chat("HOLD", buy_fraction=None, reasoning="TON ждём."),
        ],
    )

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    result = await run_pipeline_once(context=ctx, assets=list(ASSETS))

    assert tuple(step.asset for step in result.steps) == ASSETS
    assert all(step.failure_reason is None for step in result.steps)
    assert all(step.execution and step.execution.executed for step in result.steps)
    # pipeline_run_id одинаков и совпадает с результатом.
    assert {step.pipeline_run_id for step in result.steps} == {result.pipeline_run_id}

    decisions = (
        await session.execute(
            select(Decision).where(Decision.pipeline_run_id == result.pipeline_run_id)
        )
    ).scalars().all()
    assert {d.asset for d in decisions} == set(ASSETS)
    assert len(decisions) == 3


async def test_run_pipeline_once_continues_after_step_failure(
    session, session_factory
) -> None:
    """Если первая монета упала, остальные всё равно обрабатываются."""
    user_id = await _seed_user(session)

    binance = _three_asset_binance()

    class FlakyNewsClient:
        def __init__(self):
            self._calls = 0

        async def fetch_recent(self, asset, *, limit=None):
            self._calls += 1
            if asset.upper() == "BTC":
                raise RuntimeError("flaky on BTC")
            return []

    fake_llm = FakeOpenRouterClient(
        chat_responses=[
            # BTC: PRICE стартует, NEWS падает; PRICE результат
            # отбрасывается (целая ветка считается провальной).
            price_chat(),
            # ETH: price + trader
            price_chat(),
            trader_chat("HOLD", buy_fraction=None, reasoning="ETH ждём."),
            # TON: price + trader
            price_chat(),
            trader_chat("HOLD", buy_fraction=None, reasoning="TON ждём."),
        ],
    )

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=FlakyNewsClient(),  # type: ignore[arg-type]
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    result = await run_pipeline_once(context=ctx, assets=list(ASSETS))

    btc_step = next(s for s in result.steps if s.asset == "BTC")
    eth_step = next(s for s in result.steps if s.asset == "ETH")
    ton_step = next(s for s in result.steps if s.asset == "TON")

    assert btc_step.failure_reason == PipelineStepFailureReason.NEWS_BRANCH_FAILED.value
    assert btc_step.execution is None
    assert eth_step.failure_reason is None
    assert eth_step.execution and eth_step.execution.executed
    assert ton_step.failure_reason is None
    assert ton_step.execution and ton_step.execution.executed

    # В БД ровно 3 decision, все с общим pipeline_run_id.
    decisions = (
        await session.execute(
            select(Decision).where(Decision.pipeline_run_id == result.pipeline_run_id)
        )
    ).scalars().all()
    assert len(decisions) == 3


async def test_run_pipeline_once_uses_default_assets_from_settings(
    session, session_factory
) -> None:
    """Без явного ``assets`` runner берёт ``settings.trading.symbols``."""
    user_id = await _seed_user(session)

    binance = _three_asset_binance()
    news_client = FakeNewsClient({a: [] for a in ASSETS})

    fake_llm = FakeOpenRouterClient(
        chat_responses=[
            price_chat(),
            trader_chat("HOLD", buy_fraction=None),
        ]
        * len(ASSETS),
    )

    ctx = _build_context(
        user_id=user_id,
        binance=binance,
        news_client=news_client,
        openrouter=fake_llm,
        session_factory=session_factory,
    )

    # ASSETS совпадает с дефолтом settings.trading.symbols.
    result = await run_pipeline_once(context=ctx)

    assert tuple(step.asset for step in result.steps) == ASSETS
    assert result.duration_seconds >= 0
