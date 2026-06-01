"""Базовый класс для всех ORM-моделей.

Используем декларативный стиль SQLAlchemy 2.0 (``DeclarativeBase``).
Alembic забирает ``Base.metadata`` как ``target_metadata`` в ``env.py``.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Корневой декларативный базовый класс."""


__all__ = ["Base"]
