"""CRUD для :class:`SchedulerState` — singleton-флага паузы планировщика.

Все обращения к таблице ``scheduler_state`` идут через эти функции:
``services/pipeline/scheduler.py`` читает флаг перед каждым тиком,
а Telegram-команды ``/stop`` и ``/resume`` (фаза 9) его переключают.

В MVP в таблице ровно одна строка. Если строки нет — считаем, что
планировщик не на паузе (см. :func:`is_paused`); при первом
``set_paused`` строка создаётся.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SchedulerState


async def get(session: AsyncSession) -> SchedulerState | None:
    """Вернуть singleton-строку состояния либо ``None``, если её ещё нет."""
    stmt = select(SchedulerState).order_by(SchedulerState.id.asc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def is_paused(session: AsyncSession) -> bool:
    """Вернуть текущее значение флага ``paused`` (false по умолчанию)."""
    state = await get(session)
    return bool(state and state.paused)


async def set_paused(session: AsyncSession, *, paused: bool) -> SchedulerState:
    """Установить флаг ``paused``; при отсутствии строки — создать её.

    Возвращает актуальную ORM-сущность (после ``flush``). Коммит остаётся
    на вызывающей стороне — это позволяет включить операцию в общую
    транзакцию telegram-хендлера или теста.
    """
    state = await get(session)
    if state is None:
        state = SchedulerState(paused=paused)
        session.add(state)
    else:
        state.paused = paused
    await session.flush()
    return state


__all__ = ["get", "is_paused", "set_paused"]
