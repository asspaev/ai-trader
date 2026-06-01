"""CRUD для модели :class:`LLMCall` (трекинг вызовов LLM)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LLMCall
from app.models.enums import LLMCallStatus


async def create_in_progress(
    session: AsyncSession,
    *,
    agent_name: str,
    model: str,
    request_payload: dict[str, Any],
    pipeline_run_id: uuid.UUID | None = None,
) -> LLMCall:
    """Открыть запись ``LLMCall`` в статусе ``IN_PROGRESS``.

    Вызывается ``LLMCallTracker`` до фактического HTTP-запроса.
    """
    call = LLMCall(
        pipeline_run_id=pipeline_run_id,
        agent_name=agent_name,
        model=model,
        status=LLMCallStatus.IN_PROGRESS,
        request_payload=request_payload,
    )
    session.add(call)
    await session.flush()
    return call


async def complete(
    session: AsyncSession,
    *,
    call_id: int,
    response_payload: dict[str, Any] | None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: Decimal | None = None,
) -> LLMCall:
    call = await session.get(LLMCall, call_id)
    if call is None:
        raise LookupError(f"LLMCall id={call_id} not found")
    call.status = LLMCallStatus.COMPLETE
    call.response_payload = response_payload
    call.prompt_tokens = prompt_tokens
    call.completion_tokens = completion_tokens
    call.cost_usd = cost_usd
    call.finished_at = datetime.now(timezone.utc)
    await session.flush()
    return call


async def mark_error(
    session: AsyncSession, *, call_id: int, error_text: str
) -> LLMCall:
    call = await session.get(LLMCall, call_id)
    if call is None:
        raise LookupError(f"LLMCall id={call_id} not found")
    call.status = LLMCallStatus.ERROR
    call.error_text = error_text
    call.finished_at = datetime.now(timezone.utc)
    await session.flush()
    return call


async def get_by_id(session: AsyncSession, call_id: int) -> LLMCall | None:
    return await session.get(LLMCall, call_id)


async def list_for_pipeline_run(
    session: AsyncSession, *, pipeline_run_id: uuid.UUID
) -> list[LLMCall]:
    stmt = (
        select(LLMCall)
        .where(LLMCall.pipeline_run_id == pipeline_run_id)
        .order_by(LLMCall.created_at.asc(), LLMCall.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_by_status(
    session: AsyncSession,
) -> dict[LLMCallStatus, int]:
    stmt = select(LLMCall.status, func.count()).group_by(LLMCall.status)
    rows = (await session.execute(stmt)).all()
    return {status: count for status, count in rows}


__all__ = [
    "create_in_progress",
    "complete",
    "mark_error",
    "get_by_id",
    "list_for_pipeline_run",
    "count_by_status",
]
