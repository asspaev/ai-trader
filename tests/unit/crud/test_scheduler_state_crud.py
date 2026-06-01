"""CRUD-тесты для :mod:`app.crud.scheduler_state`."""

from __future__ import annotations

import pytest

from app.crud import scheduler_state as scheduler_state_crud


pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_is_paused_returns_false_for_empty_table(session) -> None:
    """Если строки нет — считаем, что планировщик не на паузе."""
    assert await scheduler_state_crud.is_paused(session) is False
    assert await scheduler_state_crud.get(session) is None


async def test_set_paused_creates_singleton_row(session) -> None:
    """Первый ``set_paused`` создаёт строку, возвращает ORM-сущность."""
    state = await scheduler_state_crud.set_paused(session, paused=True)

    assert state.id is not None
    assert state.paused is True
    assert await scheduler_state_crud.is_paused(session) is True


async def test_set_paused_updates_existing_row(session) -> None:
    """Повторный ``set_paused`` мутирует ту же строку, а не создаёт новую."""
    first = await scheduler_state_crud.set_paused(session, paused=True)
    second = await scheduler_state_crud.set_paused(session, paused=False)

    assert first.id == second.id
    assert second.paused is False
    assert await scheduler_state_crud.is_paused(session) is False

    # Дополнительная проверка singleton-инварианта: get() возвращает
    # ту же запись, что мы только что обновили.
    fetched = await scheduler_state_crud.get(session)
    assert fetched is not None
    assert fetched.id == first.id
    assert fetched.paused is False
