"""CRUD для модели :class:`User`."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def create(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    initial_capital_rub: Decimal,
    initial_capital_usdt: Decimal,
    initial_usdt_rub_rate: Decimal,
) -> User:
    """Создать единственную init-запись пользователя."""
    user = User(
        telegram_id=telegram_id,
        username=username,
        initial_capital_rub=initial_capital_rub,
        initial_capital_usdt=initial_capital_usdt,
        initial_usdt_rub_rate=initial_usdt_rub_rate,
    )
    session.add(user)
    await session.flush()
    return user


async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_by_telegram_id(
    session: AsyncSession, telegram_id: int
) -> User | None:
    stmt = select(User).where(User.telegram_id == telegram_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_singleton(session: AsyncSession) -> User | None:
    """Вернуть единственного пользователя (MVP — только одна запись)."""
    stmt = select(User).order_by(User.id.asc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


__all__ = ["create", "get_by_id", "get_by_telegram_id", "get_singleton"]
