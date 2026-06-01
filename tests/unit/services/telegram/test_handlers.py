"""Тесты команд Telegram-бота (фаза 9).

Покрытие:

* :class:`AuthMiddleware` — допуск только ``allowed_telegram_id``,
  ответ ``Not authorized`` неавторизованным, тихий ignore не-команд.
* `/start` — приветствие.
* `/balance` — баланс + RUB-эквивалент.
* `/history N` — парсинг N, ограничение по ``history_limit_max``,
  пустая история.
* `/stats` — счётчики решений и сделок.
* `/start_pipeline` — quick reply + фоновый запуск через scheduler.
* `/stop` / `/resume` — round-trip флага паузы и тексты ответов.

Handlers тестируются напрямую (минуя aiogram-Dispatcher) — это
проще, быстрее и не требует event loop'а Telegram-клиента.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import SchedulerSettings
from app.crud import decision as decision_crud
from app.crud import transaction as transaction_crud
from app.crud import user as user_crud
from app.crud import wallet as wallet_crud
from app.models.enums import DecisionAction, TransactionAction
from app.services.pipeline.scheduler import PipelineScheduler
from app.services.telegram.handlers import (
    AuthMiddleware,
    CommandHandlers,
    HandlerDeps,
)

from tests.unit.services.telegram._helpers import (
    FakeBinanceClient,
    FakeMessage,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


ALLOWED_TELEGRAM_ID = 42


# ---------- общие фикстуры ----------


@pytest_asyncio.fixture(loop_scope="session")
async def session_factory(engine, session) -> async_sessionmaker:
    """async_sessionmaker поверх тестового движка."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def _seed_user(session, *, usdt: str = "1000", btc: str = "0.01") -> int:
    user = await user_crud.create(
        session,
        telegram_id=ALLOWED_TELEGRAM_ID,
        username="trader",
        initial_capital_rub=Decimal("100000"),
        initial_capital_usdt=Decimal("1000"),
        initial_usdt_rub_rate=Decimal("100"),
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset="USDT", balance=Decimal(usdt)
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset="BTC", balance=Decimal(btc)
    )
    await session.commit()
    return user.id


class _NoopRunner:
    """async-runner, который ничего не делает (для scheduler в тестах)."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


def _build_scheduler(session_factory: async_sessionmaker) -> tuple[PipelineScheduler, _NoopRunner]:
    runner = _NoopRunner()
    scheduler = PipelineScheduler(
        runner=runner,
        session_factory=session_factory,
        config=SchedulerSettings(mode="cron"),
    )
    return scheduler, runner


async def _build_handlers(
    *,
    session_factory: async_sessionmaker,
    user_id: int,
    binance_client: Any | None = None,
    scheduler: PipelineScheduler | None = None,
    history_limit_default: int = 10,
    history_limit_max: int = 50,
) -> tuple[CommandHandlers, _NoopRunner | None]:
    """Собрать ``CommandHandlers`` с готовыми зависимостями."""
    runner: _NoopRunner | None = None
    if scheduler is None:
        scheduler, runner = _build_scheduler(session_factory)
    binance = binance_client or FakeBinanceClient(
        book_ticker_by_symbol={
            "BTCUSDT": {"symbol": "BTCUSDT", "bidPrice": "60000", "askPrice": "60100"},
            "USDTRUB": {"symbol": "USDTRUB", "bidPrice": "99", "askPrice": "101"},
        }
    )
    deps = HandlerDeps(
        session_factory=session_factory,
        scheduler=scheduler,
        binance_client=binance,  # type: ignore[arg-type]
        user_id=user_id,
        allowed_telegram_id=ALLOWED_TELEGRAM_ID,
        quote_asset="USDT",
        symbols=("BTC", "ETH", "TON"),
        history_limit_default=history_limit_default,
        history_limit_max=history_limit_max,
    )
    return CommandHandlers(deps), runner


# ---------- AuthMiddleware ----------


async def test_auth_middleware_passes_message_from_allowed_user(
    session_factory,  # подключаем session-scoped фикстуру для инициализации loop
) -> None:
    """Сообщение от ``allowed_telegram_id`` доходит до handler'а."""
    mw = AuthMiddleware(ALLOWED_TELEGRAM_ID)
    msg = FakeMessage(text="/balance", from_user_id=ALLOWED_TELEGRAM_ID)
    called = {"hit": False}

    async def handler(event: Any, data: dict[str, Any]) -> str:
        called["hit"] = True
        return "ok"

    result = await mw(handler, msg, {})
    assert result == "ok"
    assert called["hit"] is True
    assert msg.replies == []


async def test_auth_middleware_rejects_foreign_user_with_message(
    session_factory,
) -> None:
    """Чужой telegram_id получает 'Not authorized' и handler не зовётся."""
    mw = AuthMiddleware(ALLOWED_TELEGRAM_ID)
    msg = FakeMessage(text="/balance", from_user_id=999)
    called = {"hit": False}

    async def handler(event: Any, data: dict[str, Any]) -> str:
        called["hit"] = True
        return "ok"

    result = await mw(handler, msg, {})
    assert result is None
    assert called["hit"] is False
    assert msg.replies == ["Not authorized"]


async def test_auth_middleware_silently_ignores_foreign_non_commands(
    session_factory,
) -> None:
    """Произвольный текст от чужого пользователя — без ответа."""
    mw = AuthMiddleware(ALLOWED_TELEGRAM_ID)
    msg = FakeMessage(text="hi", from_user_id=999)

    async def handler(event: Any, data: dict[str, Any]) -> str:  # pragma: no cover
        raise AssertionError("handler should not be called")

    await mw(handler, msg, {})
    assert msg.replies == []


# ---------- /start ----------


async def test_start_command_returns_greeting(session, session_factory) -> None:
    user_id = await _seed_user(session)
    handlers, _ = await _build_handlers(
        session_factory=session_factory, user_id=user_id
    )
    msg = FakeMessage(text="/start", from_user_id=ALLOWED_TELEGRAM_ID)
    await handlers.on_start(msg)
    assert len(msg.replies) == 1
    assert "/balance" in msg.replies[0]
    assert "/history" in msg.replies[0]


# ---------- /balance ----------


async def test_balance_command_lists_wallets_and_rub_equivalent(
    session, session_factory
) -> None:
    user_id = await _seed_user(session, usdt="500", btc="0.01")
    handlers, _ = await _build_handlers(
        session_factory=session_factory, user_id=user_id
    )
    msg = FakeMessage(text="/balance", from_user_id=ALLOWED_TELEGRAM_ID)
    await handlers.on_balance(msg)

    assert len(msg.replies) == 1
    text = msg.replies[0]
    assert "USDT: 500.00" in text
    assert "BTC: 0.01" in text
    # 500 + 0.01 * 60000 = 1100 USDT, mid-rate USDT/RUB = 100 → 110 000 RUB
    assert "1100.00 USDT" in text
    assert "RUB" in text


# ---------- /history ----------


async def test_history_command_returns_empty_when_no_transactions(
    session, session_factory
) -> None:
    user_id = await _seed_user(session)
    handlers, _ = await _build_handlers(
        session_factory=session_factory, user_id=user_id
    )
    msg = FakeMessage(text="/history", from_user_id=ALLOWED_TELEGRAM_ID)

    # Симулируем работу aiogram-фильтра Command: command=None, raw args=None
    class _FakeCmd:
        args: str | None = None

    await handlers.on_history(msg, _FakeCmd())  # type: ignore[arg-type]
    assert msg.replies == ["История сделок пуста."]


async def test_history_command_parses_n_and_caps_to_max(
    session, session_factory
) -> None:
    user_id = await _seed_user(session)
    # Добавим 3 сделки
    for idx in range(3):
        await transaction_crud.create(
            session,
            user_id=user_id,
            decision_id=None,
            symbol="BTCUSDT",
            asset="BTC",
            action=TransactionAction.BUY,
            amount_crypto=Decimal("0.001"),
            price_usdt=Decimal("60000"),
            gross_usdt=Decimal("60"),
            fee_usdt=Decimal("0.06"),
            net_usdt=Decimal("60.06"),
            usdt_balance_after=Decimal("940") - Decimal(idx),
            asset_balance_after=Decimal("0.001"),
        )
    await session.commit()

    handlers, _ = await _build_handlers(
        session_factory=session_factory,
        user_id=user_id,
        history_limit_default=10,
        history_limit_max=5,
    )

    class _FakeCmd:
        def __init__(self, args: str | None) -> None:
            self.args = args

    msg_default = FakeMessage(text="/history", from_user_id=ALLOWED_TELEGRAM_ID)
    await handlers.on_history(msg_default, _FakeCmd(None))  # type: ignore[arg-type]
    assert "Последние 3 сделок" in msg_default.replies[0]
    assert "≤ 10" in msg_default.replies[0]

    msg_capped = FakeMessage(text="/history 999", from_user_id=ALLOWED_TELEGRAM_ID)
    await handlers.on_history(msg_capped, _FakeCmd("999"))  # type: ignore[arg-type]
    # max=5, поэтому в шапке ≤ 5; реально вернётся min(5, 3) = 3 сделки
    assert "≤ 5" in msg_capped.replies[0]

    msg_garbage = FakeMessage(text="/history foo", from_user_id=ALLOWED_TELEGRAM_ID)
    await handlers.on_history(msg_garbage, _FakeCmd("foo"))  # type: ignore[arg-type]
    # «foo» — не число → дефолтный лимит
    assert "≤ 10" in msg_garbage.replies[0]


# ---------- /stats ----------


async def test_stats_command_counts_decisions_and_transactions(
    session, session_factory
) -> None:
    user_id = await _seed_user(session)
    run_id = uuid.uuid4()
    # 2 BUY (один не исполнен), 1 HOLD
    d1 = await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=run_id,
        asset="BTC",
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.25"),
        executed=True,
    )
    await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=run_id,
        asset="ETH",
        action=DecisionAction.BUY,
        executed=False,
        not_executed_reason="MIN_NOTIONAL",
    )
    await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=run_id,
        asset="TON",
        action=DecisionAction.HOLD,
        executed=True,
    )
    # 1 фактическая сделка (для исполненного BUY)
    await transaction_crud.create(
        session,
        user_id=user_id,
        decision_id=d1.id,
        symbol="BTCUSDT",
        asset="BTC",
        action=TransactionAction.BUY,
        amount_crypto=Decimal("0.001"),
        price_usdt=Decimal("60000"),
        gross_usdt=Decimal("60"),
        fee_usdt=Decimal("0.06"),
        net_usdt=Decimal("60.06"),
        usdt_balance_after=Decimal("939.94"),
        asset_balance_after=Decimal("0.001"),
    )
    await session.commit()

    handlers, _ = await _build_handlers(
        session_factory=session_factory, user_id=user_id
    )
    msg = FakeMessage(text="/stats", from_user_id=ALLOWED_TELEGRAM_ID)
    await handlers.on_stats(msg)

    text = msg.replies[0]
    assert "Решений всего: 3" in text
    assert "BUY×2" in text
    assert "SELL×0" in text
    assert "HOLD×1" in text
    assert "исполнено: 2" in text
    assert "пропущено: 1" in text
    assert "Сделок: 1" in text
    assert "Портфель" in text


# ---------- /start_pipeline ----------


async def test_start_pipeline_acknowledges_and_triggers_runner(
    session, session_factory
) -> None:
    user_id = await _seed_user(session)
    scheduler, runner = _build_scheduler(session_factory)
    handlers, _ = await _build_handlers(
        session_factory=session_factory,
        user_id=user_id,
        scheduler=scheduler,
    )
    msg = FakeMessage(text="/start_pipeline", from_user_id=ALLOWED_TELEGRAM_ID)
    await handlers.on_start_pipeline(msg)
    # Дать background-таске исполниться
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert msg.replies[0].startswith("🚀")
    assert runner.calls == 1


# ---------- /stop, /resume ----------


async def test_stop_and_resume_toggle_pause_flag(session, session_factory) -> None:
    user_id = await _seed_user(session)
    scheduler, _ = _build_scheduler(session_factory)
    handlers, _ = await _build_handlers(
        session_factory=session_factory, user_id=user_id, scheduler=scheduler
    )

    assert await scheduler.is_paused() is False

    msg_stop = FakeMessage(text="/stop", from_user_id=ALLOWED_TELEGRAM_ID)
    await handlers.on_stop(msg_stop)
    assert await scheduler.is_paused() is True
    assert "⏸" in msg_stop.replies[0]

    msg_resume = FakeMessage(text="/resume", from_user_id=ALLOWED_TELEGRAM_ID)
    await handlers.on_resume(msg_resume)
    assert await scheduler.is_paused() is False
    assert "▶️" in msg_resume.replies[0]
