"""Протокол нотификатора, который зовёт pipeline.

Живёт в пакете ``pipeline``, а не в ``telegram``, чтобы избежать
циклической зависимости: pipeline-runner импортирует :class:`PipelineNotifier`
для аннотации параметра, а Telegram-реализация (живущая в
``app/services/telegram/notifier.py``) — наоборот, импортирует из
pipeline только готовые dataclass'ы результата.

В этом модуле — только Protocol + безопасный no-op. Сама реальная
доставка сообщений Telegram'ом находится в Telegram-пакете.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.pipeline.crypto_step import CryptoStepResult
from app.services.pipeline.runner import PipelineRunResult


@runtime_checkable
class PipelineNotifier(Protocol):
    """Контракт, который зовёт pipeline-runner после монеты и тика.

    :class:`PipelineRunner` всегда зовёт обе ручки — реализация решает,
    куда (и нужно ли вообще) их доставлять.
    """

    async def notify_step(self, result: CryptoStepResult) -> None:  # pragma: no cover
        ...

    async def notify_pipeline_summary(
        self, run: PipelineRunResult
    ) -> None:  # pragma: no cover
        ...


class NoOpNotifier:
    """Заглушка без побочных эффектов.

    Используется в тестах pipeline и в ``app/main.py``, если
    ``TELEGRAM_BOT_TOKEN`` пуст — pipeline в этом случае работает, но
    никаких сообщений не уходит.
    """

    async def notify_step(self, result: CryptoStepResult) -> None:
        return None

    async def notify_pipeline_summary(self, run: PipelineRunResult) -> None:
        return None


__all__ = ["NoOpNotifier", "PipelineNotifier"]
