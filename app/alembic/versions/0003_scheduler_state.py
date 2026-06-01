"""phase 8 — scheduler_state singleton (paused flag)

Revision ID: 0003_scheduler_state
Revises: 0002_phase1_schema
Create Date: 2026-06-01

Заводит singleton-таблицу ``scheduler_state`` с булевым флагом
``paused``. Telegram-команды ``/stop`` и ``/resume`` (фаза 9)
переключают этот флаг, а pipeline-scheduler перед каждым тиком его
читает. Хранится в БД, чтобы переживать рестарты процесса.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0003_scheduler_state"
down_revision: Union[str, None] = "0002_phase1_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduler_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("scheduler_state")
