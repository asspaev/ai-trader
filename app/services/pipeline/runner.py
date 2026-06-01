"""Оркестратор одного полного тика pipeline.

Генерирует общий ``pipeline_run_id`` (uuid4) и последовательно
обходит торгуемые активы, делегируя обработку каждой монеты
:func:`crypto_step`. Возвращает :class:`PipelineRunResult` со списком
всех :class:`CryptoStepResult` — этим объектом дальше пользуются
Telegram-нотификатор (notify_pipeline_summary, фаза 9) и метрики
(``pnl.py``, фаза 10).

Само по себе планирование (cron/interval, защита от перекрытий)
относится к фазе 8 (``services/pipeline/scheduler.py``) — runner здесь
остаётся чистой корутиной и легко тестируется в изоляции.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from loguru import logger

from app.config import settings
from app.services.pipeline.crypto_step import (
    CryptoStepResult,
    PipelineContext,
    crypto_step,
)


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    """Сводка одного тика pipeline.

    Attributes:
        pipeline_run_id: Общий идентификатор тика.
        started_at: Момент старта (UTC, tz-aware).
        finished_at: Момент завершения (UTC).
        steps: Кортеж итогов по каждому активу в порядке обхода.
    """

    pipeline_run_id: uuid.UUID
    started_at: datetime
    finished_at: datetime
    steps: tuple[CryptoStepResult, ...]

    @property
    def duration_seconds(self) -> float:
        """Длительность тика в секундах."""
        return (self.finished_at - self.started_at).total_seconds()


async def run_pipeline_once(
    *,
    context: PipelineContext,
    assets: Sequence[str] | None = None,
) -> PipelineRunResult:
    """Запустить ровно один тик и вернуть его итог.

    Args:
        context: Контекст с уже инстанцированными клиентами/агентами.
        assets: Какие активы обходить и в каком порядке. По умолчанию
            берётся ``settings.trading.symbols`` (``["BTC", "ETH",
            "TON"]``). Это переопределение полезно в тестах и при
            ручном запуске одной монеты из Telegram-команды.

    Returns:
        :class:`PipelineRunResult` с метаданными тика и списком
        результатов по каждому активу.
    """
    pipeline_run_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    use_assets = [a.upper() for a in (assets if assets is not None else settings.trading.symbols)]

    bound = logger.bind(
        component="pipeline.runner",
        pipeline_run_id=str(pipeline_run_id),
    )
    bound.info(
        "Pipeline run started: assets={assets}",
        assets=use_assets,
    )

    steps: list[CryptoStepResult] = []
    for asset in use_assets:
        step_result = await crypto_step(
            context=context,
            asset=asset,
            pipeline_run_id=pipeline_run_id,
        )
        steps.append(step_result)

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()
    bound.info(
        "Pipeline run finished in {duration:.2f}s; "
        "results={summary}",
        duration=duration,
        summary=_format_summary(steps),
    )

    return PipelineRunResult(
        pipeline_run_id=pipeline_run_id,
        started_at=started_at,
        finished_at=finished_at,
        steps=tuple(steps),
    )


def _format_summary(steps: Sequence[CryptoStepResult]) -> str:
    """Компактная сводка итогов в одну строку для INFO-лога."""
    parts: list[str] = []
    for step in steps:
        if step.failure_reason and step.execution is None:
            parts.append(f"{step.asset}=FAIL({step.failure_reason})")
            continue
        action = step.decision.action.value
        executed = step.execution.executed if step.execution else None
        if executed is True:
            parts.append(f"{step.asset}={action}/ok")
        elif executed is False:
            parts.append(f"{step.asset}={action}/skip({step.execution.not_executed_reason})")
        else:
            parts.append(f"{step.asset}={action}")
    return ", ".join(parts) or "(no assets)"


__all__ = ["PipelineRunResult", "run_pipeline_once"]
