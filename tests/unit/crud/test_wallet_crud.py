"""CRUD-тесты для :mod:`app.crud.wallet`."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.crud import user as user_crud
from app.crud import wallet as wallet_crud


pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_user(session) -> int:
    u = await user_crud.create(
        session,
        telegram_id=10,
        username="u",
        initial_capital_rub=Decimal("100000"),
        initial_capital_usdt=Decimal("1100"),
        initial_usdt_rub_rate=Decimal("90.9"),
    )
    return u.id


async def test_create_and_get(session):
    user_id = await _make_user(session)

    wallet = await wallet_crud.create(
        session, user_id=user_id, asset="USDT", balance=Decimal("500")
    )

    fetched = await wallet_crud.get(session, user_id=user_id, asset="USDT")
    assert fetched is not None
    assert fetched.id == wallet.id
    assert fetched.balance == Decimal("500.000000000000")


async def test_unique_user_asset(session):
    user_id = await _make_user(session)
    await wallet_crud.create(session, user_id=user_id, asset="BTC")

    with pytest.raises(IntegrityError):
        await wallet_crud.create(session, user_id=user_id, asset="BTC")


async def test_upsert_inserts_then_updates(session):
    user_id = await _make_user(session)

    first = await wallet_crud.upsert(
        session, user_id=user_id, asset="ETH", balance=Decimal("1")
    )
    second = await wallet_crud.upsert(
        session, user_id=user_id, asset="ETH", balance=Decimal("3")
    )

    assert first.id == second.id
    assert second.balance == Decimal("3.000000000000")


async def test_add_balance_creates_or_increments(session):
    user_id = await _make_user(session)

    created = await wallet_crud.add_balance(
        session, user_id=user_id, asset="TON", delta=Decimal("2.5")
    )
    # Снимок балансом сразу после создания: ORM возвращает один и тот же
    # инстанс (identity-map), поэтому после второго вызова поле ``balance``
    # на ``created`` уже изменится.
    balance_after_create = created.balance
    created_id = created.id

    incremented = await wallet_crud.add_balance(
        session, user_id=user_id, asset="TON", delta=Decimal("1.25")
    )

    assert balance_after_create == Decimal("2.500000000000")
    assert incremented.balance == Decimal("3.750000000000")
    assert incremented.id == created_id


async def test_list_for_user_sorted_by_asset(session):
    user_id = await _make_user(session)
    await wallet_crud.create(session, user_id=user_id, asset="USDT")
    await wallet_crud.create(session, user_id=user_id, asset="BTC")
    await wallet_crud.create(session, user_id=user_id, asset="ETH")

    wallets = await wallet_crud.list_for_user(session, user_id=user_id)

    assert [w.asset for w in wallets] == ["BTC", "ETH", "USDT"]
