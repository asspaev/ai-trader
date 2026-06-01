"""CRUD для модели :class:`Transaction`."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Transaction
from app.models.enums import TransactionAction


async def create(
    session: AsyncSession,
    *,
    user_id: int,
    decision_id: int | None,
    symbol: str,
    asset: str,
    action: TransactionAction,
    amount_crypto: Decimal,
    price_usdt: Decimal,
    gross_usdt: Decimal,
    fee_usdt: Decimal,
    net_usdt: Decimal,
    usdt_balance_after: Decimal,
    asset_balance_after: Decimal,
) -> Transaction:
    transaction = Transaction(
        user_id=user_id,
        decision_id=decision_id,
        symbol=symbol,
        asset=asset,
        action=action,
        amount_crypto=amount_crypto,
        price_usdt=price_usdt,
        gross_usdt=gross_usdt,
        fee_usdt=fee_usdt,
        net_usdt=net_usdt,
        usdt_balance_after=usdt_balance_after,
        asset_balance_after=asset_balance_after,
    )
    session.add(transaction)
    await session.flush()
    return transaction


async def get_by_id(
    session: AsyncSession, transaction_id: int
) -> Transaction | None:
    return await session.get(Transaction, transaction_id)


async def list_recent_for_user(
    session: AsyncSession, *, user_id: int, limit: int = 10
) -> list[Transaction]:
    """Последние N сделок для команды ``/history``."""
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_for_asset(
    session: AsyncSession,
    *,
    user_id: int,
    asset: str,
    limit: int | None = None,
) -> list[Transaction]:
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user_id, Transaction.asset == asset)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def list_all_for_user(
    session: AsyncSession, *, user_id: int
) -> list[Transaction]:
    """Полная история сделок (используется для расчёта PnL)."""
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.created_at.asc(), Transaction.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = [
    "create",
    "get_by_id",
    "list_recent_for_user",
    "list_for_asset",
    "list_all_for_user",
]
