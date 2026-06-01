"""drop global UNIQUE(url) on news

Revision ID: 0004_drop_news_url_unique
Revises: 0003_scheduler_state
Create Date: 2026-06-02

CoinDesk Data возвращает одну и ту же статью при запросах по разным
``categories=BTC|ETH|TON`` (если она тегнута несколькими активами).
В архитектуре новости хранятся per-asset: каждый затронутый актив
получает отдельную строку со своим summary и embedding, RAG-поиск
фильтрует по ``asset``. Глобальный ``UNIQUE(url)`` ломал этот
сценарий — для второго актива вставка падала с
``UniqueViolationError: uq_news_url``.

Дедупликация остаётся за ``UNIQUE(asset, external_id)``.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0004_drop_news_url_unique"
down_revision: Union[str, None] = "0003_scheduler_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_news_url", "news", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("uq_news_url", "news", ["url"])
