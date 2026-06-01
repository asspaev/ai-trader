"""CRUD для модели :class:`Wallet` (кэш-балансы по активам)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Wallet


async def create(
    session: AsyncSession,
    *,
    user_id: int,
    asset: str,
    balance: Decimal = Decimal("0"),
) -> Wallet:
    wallet = Wallet(user_id=user_id, asset=asset, balance=balance)
    session.add(wallet)
    await session.flush()
    return wallet


async def get(
    session: AsyncSession, *, user_id: int, asset: str
) -> Wallet | None:
    stmt = select(Wallet).where(
        Wallet.user_id == user_id, Wallet.asset == asset
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_for_user(
    session: AsyncSession, *, user_id: int
) -> list[Wallet]:
    stmt = (
        select(Wallet)
        .where(Wallet.user_id == user_id)
        .order_by(Wallet.asset.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def upsert(
    session: AsyncSession, *, user_id: int, asset: str, balance: Decimal
) -> Wallet:
    """Создать или обновить баланс кошелька по (user_id, asset)."""
    wallet = await get(session, user_id=user_id, asset=asset)
    if wallet is None:
        return await create(
            session, user_id=user_id, asset=asset, balance=balance
        )
    wallet.balance = balance
    await session.flush()
    return wallet


async def add_balance(
    session: AsyncSession, *, user_id: int, asset: str, delta: Decimal
) -> Wallet:
    """Прибавить ``delta`` к балансу (создаст кошелёк при отсутствии)."""
    wallet = await get(session, user_id=user_id, asset=asset)
    if wallet is None:
        return await create(
            session, user_id=user_id, asset=asset, balance=delta
        )
    wallet.balance = (wallet.balance or Decimal("0")) + delta
    await session.flush()
    return wallet


__all__ = ["create", "get", "list_for_user", "upsert", "add_balance"]
