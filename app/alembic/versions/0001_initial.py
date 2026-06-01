"""initial empty baseline

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-01

Фаза 0: пустая стартовая ревизия, нужна для якоря цепочки миграций.
Реальные таблицы и расширение pgvector добавит фаза 1.
"""

from __future__ import annotations

from typing import Sequence, Union


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
