"""ORM-модель фактической mock-сделки (источник правды по балансам)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import TransactionAction


class Transaction(Base):
    """Запись о фактической сделке BUY или SELL.

    ``net_usdt`` — фактическое изменение USDT-баланса (gross ∓ fee, в
    зависимости от направления). ``*_balance_after`` — снимок балансов
    сразу после применения сделки.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        Index(
            "ix_transactions_user_asset_created",
            "user_id",
            "asset",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[TransactionAction] = mapped_column(
        PG_ENUM(
            TransactionAction,
            name="transaction_action_enum",
            create_type=False,
        ),
        nullable=False,
    )
    amount_crypto: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    price_usdt: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    gross_usdt: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    fee_usdt: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    net_usdt: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    usdt_balance_after: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    asset_balance_after: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = ["Transaction"]
