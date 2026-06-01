"""CRUD-тесты для :mod:`app.crud.decision`."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.crud import decision as decision_crud
from app.crud import user as user_crud
from app.models.enums import DecisionAction


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


async def test_create_buy_decision_with_fraction(session):
    user_id = await _make_user(session)
    run_id = uuid.uuid4()

    created = await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=run_id,
        asset="BTC",
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.2500"),
        reasoning="bullish",
    )

    fetched = await decision_crud.get_by_id(session, created.id)
    assert fetched is not None
    assert fetched.action is DecisionAction.BUY
    assert fetched.buy_fraction == Decimal("0.2500")
    assert fetched.executed is None
    assert fetched.pipeline_run_id == run_id


async def test_mark_executed_success(session):
    user_id = await _make_user(session)
    d = await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=uuid.uuid4(),
        asset="ETH",
        action=DecisionAction.HOLD,
    )

    updated = await decision_crud.mark_executed(
        session, decision_id=d.id, executed=True
    )

    assert updated.executed is True
    assert updated.not_executed_reason is None


async def test_mark_executed_with_reason(session):
    user_id = await _make_user(session)
    d = await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=uuid.uuid4(),
        asset="TON",
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.1"),
    )

    updated = await decision_crud.mark_executed(
        session, decision_id=d.id, executed=False, not_executed_reason="MIN_NOTIONAL"
    )

    assert updated.executed is False
    assert updated.not_executed_reason == "MIN_NOTIONAL"


async def test_list_last_for_asset_orders_desc(session):
    user_id = await _make_user(session)
    actions = [
        DecisionAction.HOLD,
        DecisionAction.BUY,
        DecisionAction.SELL,
        DecisionAction.HOLD,
    ]
    created = []
    for action in actions:
        d = await decision_crud.create(
            session,
            user_id=user_id,
            pipeline_run_id=uuid.uuid4(),
            asset="BTC",
            action=action,
            buy_fraction=(
                Decimal("0.5") if action is DecisionAction.BUY else None
            ),
        )
        created.append(d)

    # Ещё одно решение по другой монете не должно попасть в выборку.
    await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=uuid.uuid4(),
        asset="ETH",
        action=DecisionAction.HOLD,
    )

    last_two = await decision_crud.list_last_for_asset(
        session, user_id=user_id, asset="BTC", limit=2
    )

    assert [d.id for d in last_two] == [created[-1].id, created[-2].id]


async def test_list_for_pipeline_run(session):
    user_id = await _make_user(session)
    run_id = uuid.uuid4()
    for asset in ("BTC", "ETH", "TON"):
        await decision_crud.create(
            session,
            user_id=user_id,
            pipeline_run_id=run_id,
            asset=asset,
            action=DecisionAction.HOLD,
        )
    # Решение из другого тика не должно попасть.
    await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=uuid.uuid4(),
        asset="BTC",
        action=DecisionAction.HOLD,
    )

    decisions = await decision_crud.list_for_pipeline_run(
        session, pipeline_run_id=run_id
    )

    assert [d.asset for d in decisions] == ["BTC", "ETH", "TON"]
