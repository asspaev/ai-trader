"""ORM-модель решения TRADER-агента.

Одно решение = одна монета на одном pipeline-тике. ``pipeline_run_id``
общий для всех монет в рамках одного тика (uuid).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import DecisionAction


class Decision(Base):
    """Решение TRADER-агента по одной монете."""

    __tablename__ = "decisions"
    __table_args__ = (
        Index(
            "ix_decisions_user_asset_created",
            "user_id",
            "asset",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    asset: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[DecisionAction] = mapped_column(
        PG_ENUM(
            DecisionAction,
            name="decision_action_enum",
            create_type=False,
        ),
        nullable=False,
    )
    buy_fraction: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    executed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    not_executed_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    price_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    news_score: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = ["Decision"]
