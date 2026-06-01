"""CRUD-тесты для :mod:`app.crud.transaction`."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.crud import decision as decision_crud
from app.crud import transaction as transaction_crud
from app.crud import user as user_crud
from app.models.enums import DecisionAction, TransactionAction


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


async def _make_decision(session, user_id: int, asset: str) -> int:
    d = await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=uuid.uuid4(),
        asset=asset,
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.5"),
    )
    return d.id


async def test_create_buy_transaction(session):
    user_id = await _make_user(session)
    decision_id = await _make_decision(session, user_id, "BTC")

    tx = await transaction_crud.create(
        session,
        user_id=user_id,
        decision_id=decision_id,
        symbol="BTCUSDT",
        asset="BTC",
        action=TransactionAction.BUY,
        amount_crypto=Decimal("0.005"),
        price_usdt=Decimal("60000"),
        gross_usdt=Decimal("300"),
        fee_usdt=Decimal("0.3"),
        net_usdt=Decimal("300.3"),
        usdt_balance_after=Decimal("699.7"),
        asset_balance_after=Decimal("0.005"),
    )

    fetched = await transaction_crud.get_by_id(session, tx.id)
    assert fetched is not None
    assert fetched.action is TransactionAction.BUY
    assert fetched.decision_id == decision_id
    assert fetched.amount_crypto == Decimal("0.005000000000")
    assert fetched.fee_usdt == Decimal("0.30000000")


async def test_list_recent_for_user_limits(session):
    user_id = await _make_user(session)

    for idx in range(5):
        await transaction_crud.create(
            session,
            user_id=user_id,
            decision_id=None,
            symbol="BTCUSDT",
            asset="BTC",
            action=TransactionAction.BUY,
            amount_crypto=Decimal("0.001"),
            price_usdt=Decimal("60000"),
            gross_usdt=Decimal("60"),
            fee_usdt=Decimal("0.06"),
            net_usdt=Decimal("60.06"),
            usdt_balance_after=Decimal(str(1000 - idx)),
            asset_balance_after=Decimal(str(0.001 * (idx + 1))),
        )

    recent = await transaction_crud.list_recent_for_user(
        session, user_id=user_id, limit=3
    )

    assert len(recent) == 3
    # сортировка по created_at desc, id desc
    assert recent[0].usdt_balance_after == Decimal("996.00000000")


async def test_list_for_asset_filters(session):
    user_id = await _make_user(session)

    for asset in ("BTC", "ETH", "BTC"):
        await transaction_crud.create(
            session,
            user_id=user_id,
            decision_id=None,
            symbol=f"{asset}USDT",
            asset=asset,
            action=TransactionAction.SELL,
            amount_crypto=Decimal("0.001"),
            price_usdt=Decimal("1"),
            gross_usdt=Decimal("0.001"),
            fee_usdt=Decimal("0"),
            net_usdt=Decimal("0.001"),
            usdt_balance_after=Decimal("1000"),
            asset_balance_after=Decimal("0"),
        )

    btc = await transaction_crud.list_for_asset(
        session, user_id=user_id, asset="BTC"
    )
    eth = await transaction_crud.list_for_asset(
        session, user_id=user_id, asset="ETH"
    )

    assert len(btc) == 2
    assert len(eth) == 1
    assert all(tx.asset == "BTC" for tx in btc)


async def test_list_all_for_user_ordered_asc(session):
    user_id = await _make_user(session)

    for idx, asset in enumerate(("BTC", "ETH", "TON")):
        await transaction_crud.create(
            session,
            user_id=user_id,
            decision_id=None,
            symbol=f"{asset}USDT",
            asset=asset,
            action=TransactionAction.BUY,
            amount_crypto=Decimal("0.001"),
            price_usdt=Decimal("100"),
            gross_usdt=Decimal("0.1"),
            fee_usdt=Decimal("0"),
            net_usdt=Decimal("0.1"),
            usdt_balance_after=Decimal(str(1000 - idx)),
            asset_balance_after=Decimal("0.001"),
        )

    history = await transaction_crud.list_all_for_user(session, user_id=user_id)

    assert [tx.asset for tx in history] == ["BTC", "ETH", "TON"]
