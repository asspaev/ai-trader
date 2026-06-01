"""ORM-модель состояния планировщика (singleton-таблица).

Хранит один флаг ``paused``, которым команды ``/stop`` и ``/resume``
выключают и включают pipeline между тиками. Флаг живёт в БД, чтобы
переживать рестарты процесса.

В MVP в таблице ровно одна строка (``id=1``). Если строки нет —
считаем, что планировщик не на паузе.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SchedulerState(Base):
    """Singleton-состояние pipeline-планировщика."""

    __tablename__ = "scheduler_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    paused: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["SchedulerState"]
