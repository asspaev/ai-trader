"""ORM-модель кошелька — кэш балансов по активам.

Источник правды для балансов — таблица ``transactions``. Кошельки
обновляются в той же БД-транзакции, что и запись о сделке.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Wallet(Base):
    """Баланс одного актива у одного пользователя."""

    __tablename__ = "wallets"
    __table_args__ = (
        UniqueConstraint("user_id", "asset", name="uq_wallets_user_asset"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    balance: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=Decimal("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["Wallet"]
