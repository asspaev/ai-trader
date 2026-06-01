"""Перечисления, используемые в ORM-моделях.

Хранятся отдельным модулем, чтобы переиспользоваться между моделями,
CRUD-слоем и сервисами без циклических импортов.
"""

from __future__ import annotations

import enum


class TransactionAction(str, enum.Enum):
    """Тип фактической сделки на mock-бирже."""

    BUY = "BUY"
    SELL = "SELL"


class DecisionAction(str, enum.Enum):
    """Решение TRADER-агента."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class LLMCallStatus(str, enum.Enum):
    """Статус жизненного цикла одного вызова LLM."""

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


__all__ = [
    "TransactionAction",
    "DecisionAction",
    "LLMCallStatus",
]
