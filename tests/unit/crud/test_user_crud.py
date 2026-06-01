"""CRUD-тесты для :mod:`app.crud.user`."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.crud import user as user_crud


pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_create_and_get_by_id(session):
    created = await user_crud.create(
        session,
        telegram_id=111,
        username="alice",
        initial_capital_rub=Decimal("100000.00"),
        initial_capital_usdt=Decimal("1100.00000000"),
        initial_usdt_rub_rate=Decimal("90.90909091"),
    )

    fetched = await user_crud.get_by_id(session, created.id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.telegram_id == 111
    assert fetched.username == "alice"
    assert fetched.initial_capital_rub == Decimal("100000.00")
    assert fetched.created_at is not None


async def test_get_by_telegram_id(session):
    await user_crud.create(
        session,
        telegram_id=222,
        username="bob",
        initial_capital_rub=Decimal("50000.00"),
        initial_capital_usdt=Decimal("550.00000000"),
        initial_usdt_rub_rate=Decimal("90.90909091"),
    )

    found = await user_crud.get_by_telegram_id(session, 222)
    missing = await user_crud.get_by_telegram_id(session, 999)

    assert found is not None
    assert found.username == "bob"
    assert missing is None


async def test_get_singleton_returns_first_user(session):
    first = await user_crud.create(
        session,
        telegram_id=333,
        username="first",
        initial_capital_rub=Decimal("1000.00"),
        initial_capital_usdt=Decimal("11.00000000"),
        initial_usdt_rub_rate=Decimal("90.90909091"),
    )

    singleton = await user_crud.get_singleton(session)

    assert singleton is not None
    assert singleton.id == first.id
