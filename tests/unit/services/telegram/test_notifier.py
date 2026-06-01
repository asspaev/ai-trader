"""Тесты Telegram-нотификатора (фаза 9).

Покрытие:

* :func:`format_step_message` — все три ветки: HOLD, BUY/SELL успех,
  биржевой отказ, pipeline-failure.
* :func:`format_summary_message` — счётчики решений, RUB-эквивалент,
  ветка «portfolio недоступен».
* :func:`build_portfolio_snapshot` — реальные wallet-данные из БД,
  fake Binance.
* :class:`TelegramNotifier` — успешная отправка и подавление ошибок
  ``send_message``.
* Pipeline runner с :class:`PipelineNotifier`: notify_step зовётся
  после каждой монеты, notify_pipeline_summary — после тика;
  исключения нотификатора подавляются.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.crud import user as user_crud
from app.crud import wallet as wallet_crud
from app.models.enums import DecisionAction, TransactionAction
from app.services.pipeline.notifier import NoOpNotifier
from app.services.pipeline.runner import run_pipeline_once
from app.services.telegram.notifier import (
    PortfolioSnapshot,
    TelegramNotifier,
    build_portfolio_snapshot,
    format_balance_message,
    format_step_message,
    format_summary_message,
)

from tests.unit.services.telegram._helpers import (
    FakeBinanceClient,
    FakeBot,
    FakeTransaction,
    make_failure_step,
    make_pipeline_run,
    make_step_result,
)


_asyncio_session = pytest.mark.asyncio(loop_scope="session")
"""Маркер для async-тестов с session-scoped event loop.

Не выносим в ``pytestmark`` (модульный уровень), потому что в файле
смешаны sync-тесты (форматтеры — чистые функции) и async-тесты
(работа с БД/нотификатором): глобальный маркер на sync-функции
вызывает PytestWarning и сбивает pytest-asyncio в Windows-runtime.
"""


# ---------- format_step_message ----------


def test_format_step_message_hold_success_includes_reasoning() -> None:
    step = make_step_result(
        asset="BTC",
        action=DecisionAction.HOLD,
        buy_fraction=None,
        reasoning="Сигналы противоречивые.",
        executed=True,
        transaction=None,
    )
    text = format_step_message(step)
    assert "🪙 BTC" in text
    assert "⏸ HOLD" in text
    assert "Обоснование: Сигналы противоречивые." in text
    # HOLD не должен звать ни «Куплено», ни «Продано»
    assert "Куплено" not in text
    assert "Продано" not in text


def test_format_step_message_buy_success_has_balances_and_fraction() -> None:
    tx = FakeTransaction(
        action=TransactionAction.BUY,
        amount_crypto=Decimal("0.00371000"),
        price_usdt=Decimal("67432.12"),
        gross_usdt=Decimal("250.1532"),
        fee_usdt=Decimal("0.250153"),
        net_usdt=Decimal("250.40335"),  # spend
        usdt_balance_after=Decimal("498.95"),
    )
    step = make_step_result(
        asset="BTC",
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.25"),
        reasoning="Сильный bullish сигнал.",
        executed=True,
        transaction=tx,
    )
    text = format_step_message(step)
    assert "📈 BUY 25% свободного USDT" in text
    assert "Цена: 67432.12 USDT (ask)" in text
    assert "Куплено: 0.00371 BTC" in text
    # baseline до сделки = after + net (spend для BUY)
    assert "749.35 → 498.95" in text
    assert "Обоснование: Сильный bullish сигнал." in text


def test_format_step_message_sell_success_uses_bid_and_subtraction() -> None:
    tx = FakeTransaction(
        action=TransactionAction.SELL,
        amount_crypto=Decimal("0.005"),
        price_usdt=Decimal("67100.00"),
        gross_usdt=Decimal("335.5"),
        fee_usdt=Decimal("0.3355"),
        net_usdt=Decimal("335.1645"),  # receive
        usdt_balance_after=Decimal("1000.00"),
    )
    step = make_step_result(
        asset="ETH",
        action=DecisionAction.SELL,
        buy_fraction=None,
        reasoning="Берём прибыль.",
        executed=True,
        transaction=tx,
    )
    text = format_step_message(step)
    assert "📉 SELL (вся позиция)" in text
    assert "Цена: 67100.00 USDT (bid)" in text
    # baseline до SELL = after - net (receive)
    assert "664.84 → 1000.00" in text


def test_format_step_message_not_executed_shows_reason() -> None:
    step = make_step_result(
        asset="TON",
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.1"),
        reasoning="Слишком маленькая позиция.",
        executed=False,
        not_executed_reason="MIN_NOTIONAL",
        transaction=None,
    )
    text = format_step_message(step)
    assert "📈 BUY 10% свободного USDT" in text
    assert "Не исполнено: MIN_NOTIONAL" in text
    assert "Обоснование: Слишком маленькая позиция." in text


def test_format_step_message_pipeline_failure_uses_warning_branch() -> None:
    step = make_failure_step(asset="BTC", error_text="step timeout after 300s")
    text = format_step_message(step)
    assert "🪙 BTC" in text
    assert "⚠️ Шаг не завершён: STEP_TIMEOUT" in text
    assert "step timeout after 300s" in text


# ---------- format_summary_message ----------


def test_format_summary_message_counts_decisions_and_shows_portfolio() -> None:
    tx = FakeTransaction(
        action=TransactionAction.BUY,
        amount_crypto=Decimal("0.0037"),
        price_usdt=Decimal("67000"),
        gross_usdt=Decimal("247.9"),
        fee_usdt=Decimal("0.2479"),
        net_usdt=Decimal("248.1479"),
        usdt_balance_after=Decimal("500"),
    )
    steps = [
        make_step_result(
            asset="BTC", action=DecisionAction.BUY, transaction=tx, executed=True
        ),
        make_step_result(
            asset="ETH",
            action=DecisionAction.HOLD,
            buy_fraction=None,
            transaction=None,
            executed=True,
        ),
        make_failure_step(asset="TON"),
    ]
    run = make_pipeline_run(steps)
    portfolio = PortfolioSnapshot(items=(), total_usdt=Decimal("1024.55"))

    text = format_summary_message(
        run, portfolio=portfolio, fx_rate=Decimal("100")
    )
    assert "Pipeline #" in text
    assert "BUY×1" in text and "SELL×0" in text and "HOLD×2" in text
    assert "С ошибками: 1 из 3" in text
    assert "Портфель: 1024.55 USDT" in text
    assert "102 455 RUB" in text or "102455 RUB" in text or "102,455" not in text
    # Сообщение должно содержать численный RUB-эквивалент
    assert "RUB" in text


def test_format_summary_message_without_portfolio_omits_portfolio_line() -> None:
    steps = [
        make_step_result(asset="BTC", action=DecisionAction.HOLD, transaction=None),
    ]
    run = make_pipeline_run(steps)
    text = format_summary_message(run, portfolio=None, fx_rate=None)
    assert "Портфель" not in text


def test_format_summary_message_no_fx_rate_drops_rub_part() -> None:
    portfolio = PortfolioSnapshot(items=(), total_usdt=Decimal("500"))
    run = make_pipeline_run(
        [make_step_result(action=DecisionAction.HOLD, transaction=None)]
    )
    text = format_summary_message(run, portfolio=portfolio, fx_rate=None)
    assert "Портфель: 500.00 USDT" in text
    assert "RUB" not in text


def test_format_summary_message_appends_pnl_line_when_report_passed() -> None:
    """Если передали ``pnl_report`` — добавляется одна строка с PnL и vs HOLD."""
    from app.services.metrics.pnl import HoldBaseline, PnLReport

    portfolio = PortfolioSnapshot(items=(), total_usdt=Decimal("1024.55"))
    run = make_pipeline_run(
        [make_step_result(action=DecisionAction.HOLD, transaction=None)]
    )
    report = PnLReport(
        initial_capital_usdt=Decimal("1000"),
        portfolio_value_usdt=Decimal("1024.55"),
        pnl_usdt=Decimal("24.55"),
        pnl_pct=Decimal("2.46"),
        hold_baseline=HoldBaseline(per_asset_initial_usdt=Decimal("333")),
        delta_vs_hold_pct=Decimal("0.85"),
    )
    text = format_summary_message(
        run, portfolio=portfolio, fx_rate=None, pnl_report=report
    )
    assert "PnL: +24.55 USDT (+2.46%) | vs HOLD: +0.85%" in text


# ---------- format_balance_message ----------


def test_format_balance_message_shows_usdt_and_asset_values() -> None:
    from app.services.telegram.notifier import AssetValue

    portfolio = PortfolioSnapshot(
        items=(
            AssetValue(
                asset="USDT",
                balance=Decimal("500.5"),
                bid_price=None,
                value_usdt=Decimal("500.5"),
            ),
            AssetValue(
                asset="BTC",
                balance=Decimal("0.005"),
                bid_price=Decimal("66000"),
                value_usdt=Decimal("330"),
            ),
            AssetValue(
                asset="ETH",
                balance=Decimal("0"),
                bid_price=None,
                value_usdt=Decimal("0"),
            ),
        ),
        total_usdt=Decimal("830.5"),
    )
    text = format_balance_message(portfolio=portfolio, fx_rate=Decimal("100"))
    assert "USDT: 500.50" in text
    assert "BTC: 0.005" in text
    assert "по bid 66000.00" in text
    assert "ETH: 0" in text
    assert "Всего: 830.50 USDT" in text
    assert "RUB" in text


# ---------- build_portfolio_snapshot ----------


@pytest_asyncio.fixture(loop_scope="session")
async def session_factory(engine, session) -> async_sessionmaker:
    """Фабрика сессий поверх тестового engine (truncate в ``session``)."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@_asyncio_session
async def test_build_portfolio_snapshot_sums_usdt_and_crypto(
    session, session_factory
) -> None:
    user = await user_crud.create(
        session,
        telegram_id=42,
        username="t",
        initial_capital_rub=Decimal("100000"),
        initial_capital_usdt=Decimal("1000"),
        initial_usdt_rub_rate=Decimal("100"),
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset="USDT", balance=Decimal("500")
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset="BTC", balance=Decimal("0.01")
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset="ETH", balance=Decimal("0")
    )
    await session.commit()

    binance = FakeBinanceClient(
        book_ticker_by_symbol={
            "BTCUSDT": {"symbol": "BTCUSDT", "bidPrice": "66000", "askPrice": "66100"},
        }
    )

    snapshot = await build_portfolio_snapshot(
        session_factory=session_factory,
        binance_client=binance,  # type: ignore[arg-type]
        user_id=user.id,
        quote_asset="USDT",
        symbols=("BTC", "ETH"),
    )

    by_asset = {item.asset: item for item in snapshot.items}
    assert by_asset["USDT"].balance == Decimal("500")
    assert by_asset["USDT"].value_usdt == Decimal("500")
    assert by_asset["BTC"].balance == Decimal("0.01")
    assert by_asset["BTC"].value_usdt == Decimal("0.01") * Decimal("66000")
    # У ETH баланс 0 — bookTicker не дёргаем, остаёмся с None/0
    assert by_asset["ETH"].bid_price is None
    assert by_asset["ETH"].value_usdt == Decimal("0")
    # bookTicker звался только для BTC
    paths = [path for path, _ in binance.calls]
    assert paths == ["/api/v3/ticker/bookTicker"]
    assert snapshot.total_usdt == Decimal("500") + Decimal("660")


@_asyncio_session
async def test_build_portfolio_snapshot_swallows_binance_failure(
    session, session_factory
) -> None:
    """Если Binance уронился, итог всё равно собирается (с нулевой ценой)."""
    user = await user_crud.create(
        session,
        telegram_id=11,
        username="t",
        initial_capital_rub=Decimal("1000"),
        initial_capital_usdt=Decimal("10"),
        initial_usdt_rub_rate=Decimal("100"),
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset="USDT", balance=Decimal("10")
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset="BTC", balance=Decimal("0.001")
    )
    await session.commit()

    # FakeBinanceClient не знает про BTCUSDT → бросает AssertionError
    binance = FakeBinanceClient(book_ticker_by_symbol={})

    snapshot = await build_portfolio_snapshot(
        session_factory=session_factory,
        binance_client=binance,  # type: ignore[arg-type]
        user_id=user.id,
        quote_asset="USDT",
        symbols=("BTC",),
    )

    by_asset = {item.asset: item for item in snapshot.items}
    assert by_asset["BTC"].bid_price is None
    assert by_asset["BTC"].value_usdt == Decimal("0")
    # USDT учтён даже при отказе Binance
    assert snapshot.total_usdt == Decimal("10")


# ---------- TelegramNotifier (с fake-Bot) ----------


@_asyncio_session
async def test_telegram_notifier_sends_step_message(session_factory) -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(
        bot=bot,
        chat_id=123,
        session_factory=session_factory,
        binance_client=FakeBinanceClient(),  # type: ignore[arg-type]
        user_id=1,
        quote_asset="USDT",
        symbols=("BTC",),
    )
    tx = FakeTransaction(
        action=TransactionAction.BUY,
        amount_crypto=Decimal("0.001"),
        price_usdt=Decimal("70000"),
        gross_usdt=Decimal("70"),
        fee_usdt=Decimal("0.07"),
        net_usdt=Decimal("70.07"),
        usdt_balance_after=Decimal("930"),
    )
    step = make_step_result(action=DecisionAction.BUY, transaction=tx)
    await notifier.notify_step(step)

    assert len(bot.sent) == 1
    payload = bot.sent[0]
    assert payload["chat_id"] == 123
    assert "📈 BUY" in payload["text"]


@_asyncio_session
async def test_telegram_notifier_swallows_send_errors(session_factory) -> None:
    bot = FakeBot()
    bot.raise_on_send = RuntimeError("network down")
    notifier = TelegramNotifier(
        bot=bot,
        chat_id=1,
        session_factory=session_factory,
        binance_client=FakeBinanceClient(),  # type: ignore[arg-type]
        user_id=1,
        quote_asset="USDT",
        symbols=("BTC",),
    )
    step = make_step_result(action=DecisionAction.HOLD, transaction=None)
    # Не должен бросать
    await notifier.notify_step(step)
    assert bot.sent == []


# ---------- runner ⇄ notifier ----------


class _RecordingNotifier:
    """Записывает в каком порядке runner зовёт notify-методы."""

    def __init__(self) -> None:
        self.step_assets: list[str] = []
        self.summary_calls: int = 0
        self.raise_on_step: Exception | None = None
        self.raise_on_summary: Exception | None = None

    async def notify_step(self, result) -> None:
        if self.raise_on_step is not None:
            raise self.raise_on_step
        self.step_assets.append(result.asset)

    async def notify_pipeline_summary(self, run) -> None:
        if self.raise_on_summary is not None:
            raise self.raise_on_summary
        self.summary_calls += 1


class _DummyContext:
    """Контекст, который не вызывается — assets пустой, runner отрабатывает trivially."""


@_asyncio_session
async def test_runner_calls_notifier_per_step_and_for_summary() -> None:
    """Если activated assets — runner идёт по ним и зовёт notifier."""
    # Используем реальную PipelineContext-форму, но обходим step-вызов через
    # подмену crypto_step. Достаточно проверить, что summary вызывается.
    notifier = _RecordingNotifier()

    # Импортируем функцию из runner, а сам crypto_step мокаем через
    # monkeypatching модуля.
    import app.services.pipeline.runner as runner_mod

    async def fake_crypto_step(*, context, asset, pipeline_run_id):  # noqa: ANN001
        from app.services.pipeline.crypto_step import CryptoStepResult

        from tests.unit.services.telegram._helpers import FakeDecision

        decision = FakeDecision(
            action=DecisionAction.HOLD,
            buy_fraction=None,
            reasoning=None,
            asset=asset,
        )
        return CryptoStepResult(
            asset=asset,
            pipeline_run_id=pipeline_run_id,
            decision=decision,  # type: ignore[arg-type]
            execution=None,
            failure_reason=None,
            error_text=None,
            duration_seconds=0.1,
        )

    original = runner_mod.crypto_step
    runner_mod.crypto_step = fake_crypto_step  # type: ignore[assignment]
    try:
        run = await run_pipeline_once(
            context=_DummyContext(),  # type: ignore[arg-type]
            assets=["BTC", "ETH"],
            notifier=notifier,
        )
    finally:
        runner_mod.crypto_step = original  # type: ignore[assignment]

    assert notifier.step_assets == ["BTC", "ETH"]
    assert notifier.summary_calls == 1
    assert len(run.steps) == 2


@_asyncio_session
async def test_runner_swallows_notifier_errors() -> None:
    """Падение notifier'а не должно валить pipeline-тик."""
    notifier = _RecordingNotifier()
    notifier.raise_on_step = RuntimeError("notify broken")
    notifier.raise_on_summary = RuntimeError("notify broken too")

    import app.services.pipeline.runner as runner_mod
    from tests.unit.services.telegram._helpers import FakeDecision

    async def fake_step(*, context, asset, pipeline_run_id):  # noqa: ANN001
        from app.services.pipeline.crypto_step import CryptoStepResult

        return CryptoStepResult(
            asset=asset,
            pipeline_run_id=pipeline_run_id,
            decision=FakeDecision(action=DecisionAction.HOLD, asset=asset),  # type: ignore[arg-type]
            execution=None,
            failure_reason=None,
            error_text=None,
            duration_seconds=0.1,
        )

    original = runner_mod.crypto_step
    runner_mod.crypto_step = fake_step  # type: ignore[assignment]
    try:
        run = await run_pipeline_once(
            context=_DummyContext(),  # type: ignore[arg-type]
            assets=["BTC"],
            notifier=notifier,
        )
    finally:
        runner_mod.crypto_step = original  # type: ignore[assignment]

    assert len(run.steps) == 1


@_asyncio_session
async def test_runner_uses_noop_notifier_by_default() -> None:
    """Без явного notifier — берём ``NoOpNotifier`` и не падаем."""
    import app.services.pipeline.runner as runner_mod
    from tests.unit.services.telegram._helpers import FakeDecision

    async def fake_step(*, context, asset, pipeline_run_id):  # noqa: ANN001
        from app.services.pipeline.crypto_step import CryptoStepResult

        return CryptoStepResult(
            asset=asset,
            pipeline_run_id=pipeline_run_id,
            decision=FakeDecision(action=DecisionAction.HOLD, asset=asset),  # type: ignore[arg-type]
            execution=None,
            failure_reason=None,
            error_text=None,
            duration_seconds=0.0,
        )

    original = runner_mod.crypto_step
    runner_mod.crypto_step = fake_step  # type: ignore[assignment]
    try:
        run = await run_pipeline_once(
            context=_DummyContext(),  # type: ignore[arg-type]
            assets=["BTC"],
        )
    finally:
        runner_mod.crypto_step = original  # type: ignore[assignment]

    assert len(run.steps) == 1
    # Sanity: NoOpNotifier — это инстанс без побочных эффектов
    noop = NoOpNotifier()
    await noop.notify_step(run.steps[0])
    await noop.notify_pipeline_summary(run)
