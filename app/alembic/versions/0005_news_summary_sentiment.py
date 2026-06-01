"""cache news summary sentiment

Revision ID: 0005_news_summary_sentiment
Revises: 0004_drop_news_url_unique
Create Date: 2026-06-02

Добавляет колонку ``news.summary_sentiment`` (nullable VARCHAR(16)) —
кэш sentiment, присвоенного NEWS-агентом при суммаризации. Нужна,
чтобы при появлении той же статьи под другим активом переиспользовать
готовый summary + embedding и не платить за повторные LLM-вызовы.

Значения соответствуют :class:`app.services.agents.base.Sentiment`
(``bullish`` / ``bearish`` / ``neutral``). Для строк, созданных до
этой миграции, остаётся ``NULL`` — pipeline в таком случае не
переиспользует кэш и зовёт LLM как раньше.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005_news_summary_sentiment"
down_revision: Union[str, None] = "0004_drop_news_url_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "news",
        sa.Column("summary_sentiment", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("news", "summary_sentiment")
