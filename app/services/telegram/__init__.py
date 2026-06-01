"""Telegram-интеграция (фаза 9).

Содержит публичный API нотификатора (:mod:`.notifier`) и сборку
aiogram-бота (:mod:`.bot`/:mod:`.handlers`). Pipeline получает только
:class:`PipelineNotifier` — реальная реализация (Telegram) или no-op
для тестов.
"""

from app.services.telegram.notifier import (
    NoOpNotifier,
    PipelineNotifier,
    TelegramNotifier,
    build_portfolio_snapshot,
    format_step_message,
    format_summary_message,
)

__all__ = [
    "NoOpNotifier",
    "PipelineNotifier",
    "TelegramNotifier",
    "build_portfolio_snapshot",
    "format_step_message",
    "format_summary_message",
]
