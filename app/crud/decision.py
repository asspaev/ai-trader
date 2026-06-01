"""CRUD для модели :class:`Decision` (решения TRADER-агента)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Decision
from app.models.enums import DecisionAction


async def create(
    session: AsyncSession,
    *,
    user_id: int,
    pipeline_run_id: uuid.UUID,
    asset: str,
    action: DecisionAction,
    buy_fraction: Decimal | None = None,
    price_summary: str | None = None,
    news_score: str | None = None,
    reasoning: str | None = None,
    executed: bool | None = None,
    not_executed_reason: str | None = None,
) -> Decision:
    decision = Decision(
        user_id=user_id,
        pipeline_run_id=pipeline_run_id,
        asset=asset,
        action=action,
        buy_fraction=buy_fraction,
        price_summary=price_summary,
        news_score=news_score,
        reasoning=reasoning,
        executed=executed,
        not_executed_reason=not_executed_reason,
    )
    session.add(decision)
    await session.flush()
    return decision


async def get_by_id(session: AsyncSession, decision_id: int) -> Decision | None:
    return await session.get(Decision, decision_id)


async def mark_executed(
    session: AsyncSession,
    *,
    decision_id: int,
    executed: bool,
    not_executed_reason: str | None = None,
) -> Decision:
    decision = await session.get(Decision, decision_id)
    if decision is None:
        raise LookupError(f"Decision id={decision_id} not found")
    decision.executed = executed
    decision.not_executed_reason = not_executed_reason
    await session.flush()
    return decision


async def list_last_for_asset(
    session: AsyncSession, *, user_id: int, asset: str, limit: int = 12
) -> list[Decision]:
    """Последние N решений по одной монете (для контекста TRADER-агента)."""
    stmt = (
        select(Decision)
        .where(Decision.user_id == user_id, Decision.asset == asset)
        .order_by(Decision.created_at.desc(), Decision.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_for_pipeline_run(
    session: AsyncSession, *, pipeline_run_id: uuid.UUID
) -> list[Decision]:
    stmt = (
        select(Decision)
        .where(Decision.pipeline_run_id == pipeline_run_id)
        .order_by(Decision.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = [
    "create",
    "get_by_id",
    "mark_executed",
    "list_last_for_asset",
    "list_for_pipeline_run",
]
