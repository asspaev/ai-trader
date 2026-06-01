"""Test-doubles для Telegram-слоя.

Содержит минимальные fake'и aiogram-объектов и pipeline-моделей,
которых хватает для проверки форматтеров, авторизации и команд
без поднятия полноценного Dispatcher'а.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from app.models.enums import DecisionAction, TransactionAction
from app.services.binance.prices import BookTicker
from app.services.pipeline.crypto_step import (
    CryptoStepResult,
    PipelineStepFailureReason,
)
from app.services.pipeline.runner import PipelineRunResult


# ---------- pipeline-result builders ----------


@dataclass(slots=True)
class FakeDecision:
    """Подмена ``models.Decision`` — даём только поля, нужные форматтеру."""

    action: DecisionAction
    buy_fraction: Decimal | None = None
    reasoning: str | None = None
    asset: str = "BTC"


@dataclass(slots=True)
class FakeTransaction:
    """Подмена ``models.Transaction``."""

    action: TransactionAction
    amount_crypto: Decimal
    price_usdt: Decimal
    gross_usdt: Decimal
    fee_usdt: Decimal
    net_usdt: Decimal
    usdt_balance_after: Decimal
    asset_balance_after: Decimal = Decimal("0")
    asset: str = "BTC"
    created_at: datetime = field(
        default_factory=lambda: datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    )


@dataclass(slots=True)
class FakeExecution:
    """Подмена ``ExecutionResult``."""

    executed: bool
    not_executed_reason: str | None
    transaction: FakeTransaction | None


def make_step_result(
    *,
    asset: str = "BTC",
    action: DecisionAction = DecisionAction.BUY,
    buy_fraction: Decimal | None = Decimal("0.25"),
    reasoning: str | None = "Сильный bullish сигнал.",
    executed: bool = True,
    not_executed_reason: str | None = None,
    transaction: FakeTransaction | None = None,
    failure_reason: str | None = None,
    error_text: str | None = None,
    duration_seconds: float = 5.0,
    pipeline_run_id: uuid.UUID | None = None,
) -> CryptoStepResult:
    """Собрать ``CryptoStepResult`` из fake-моделей."""
    decision = FakeDecision(
        action=action,
        buy_fraction=buy_fraction,
        reasoning=reasoning,
        asset=asset,
    )
    execution: FakeExecution | None
    if failure_reason is not None and transaction is None and executed is True:
        execution = None
    else:
        execution = FakeExecution(
            executed=executed,
            not_executed_reason=not_executed_reason,
            transaction=transaction,
        )
    return CryptoStepResult(
        asset=asset,
        pipeline_run_id=pipeline_run_id or uuid.uuid4(),
        decision=decision,  # type: ignore[arg-type]
        execution=execution,  # type: ignore[arg-type]
        failure_reason=failure_reason,
        error_text=error_text,
        duration_seconds=duration_seconds,
    )


def make_failure_step(
    *,
    asset: str = "BTC",
    reason: PipelineStepFailureReason = PipelineStepFailureReason.STEP_TIMEOUT,
    error_text: str = "step timeout after 300s",
    duration_seconds: float = 300.0,
) -> CryptoStepResult:
    """Шаг, который не дошёл до биржи (execution=None)."""
    decision = FakeDecision(
        action=DecisionAction.HOLD,
        buy_fraction=None,
        reasoning=error_text,
        asset=asset,
    )
    return CryptoStepResult(
        asset=asset,
        pipeline_run_id=uuid.uuid4(),
        decision=decision,  # type: ignore[arg-type]
        execution=None,
        failure_reason=reason.value,
        error_text=error_text,
        duration_seconds=duration_seconds,
    )


def make_pipeline_run(steps: list[CryptoStepResult]) -> PipelineRunResult:
    """Обёртка для тестов summary-сообщения."""
    started = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    finished = datetime(2026, 6, 1, 12, 2, 14, tzinfo=timezone.utc)
    return PipelineRunResult(
        pipeline_run_id=uuid.UUID("12345678-aaaa-bbbb-cccc-1234567890ab"),
        started_at=started,
        finished_at=finished,
        steps=tuple(steps),
    )


# ---------- fake clients ----------


class FakeBinanceClient:
    """Реюз минимального fake-Binance из pipeline-тестов.

    Дублируем здесь, чтобы Telegram-тесты не зависели от тест-хелперов
    другого пакета. ``get_json`` поддерживает ``bookTicker`` и
    USDTRUB-ветку, которые нужны notifier'у/handler'ам.
    """

    def __init__(
        self,
        *,
        book_ticker_by_symbol: Mapping[str, dict[str, str]] | None = None,
    ) -> None:
        self._book = dict(book_ticker_by_symbol or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_json(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> Any:
        self.calls.append((path, dict(params or {})))
        if path == "/api/v3/ticker/bookTicker":
            symbol = (params or {}).get("symbol")
            return self._book[symbol]
        raise AssertionError(f"FakeBinanceClient: unexpected path {path!r}")

    async def aclose(self) -> None:
        return None


class FakeBot:
    """``aiogram.Bot``-заглушка — фиксирует send_message-вызовы."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.raise_on_send: Exception | None = None

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


def FakeMessage(*, text: str, from_user_id: int | None):
    """Двойник ``aiogram.types.Message`` для тестов handlers.

    Возвращает ``MagicMock(spec=Message)``: ``isinstance(msg, Message)``
    отдаёт ``True`` (важно для :class:`AuthMiddleware`), при этом
    ``text``/``from_user``/``answer`` управляемы.

    ``msg.replies`` — список текстов, переданных в ``msg.answer(...)``,
    которым удобно ассертить в тестах.
    """
    from unittest.mock import MagicMock
    from aiogram.types import Message, User

    msg = MagicMock(spec=Message)
    msg.text = text
    if from_user_id is None:
        msg.from_user = None
    else:
        user = MagicMock(spec=User)
        user.id = from_user_id
        msg.from_user = user
    msg.replies = []

    async def _answer(text: str, **kwargs: Any) -> None:
        msg.replies.append(text)

    msg.answer = _answer
    return msg


__all__ = [
    "FakeBinanceClient",
    "FakeBot",
    "FakeDecision",
    "FakeExecution",
    "FakeMessage",
    "FakeTransaction",
    "make_failure_step",
    "make_pipeline_run",
    "make_step_result",
]
