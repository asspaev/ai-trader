"""ORM-модель пользователя (init-запись).

В MVP в системе ровно один пользователь, создаётся скриптом
``scripts/init_user.py`` (фаза 3). Хранит исходный капитал в RUB
и конвертированную сумму в USDT (плюс зафиксированный курс на момент
инициализации).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    """Пользователь системы (одна запись в MVP)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    initial_capital_rub: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False
    )
    initial_capital_usdt: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False
    )
    initial_usdt_rub_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = ["User"]
