"""phase 1 — основная схема: users, wallets, transactions, decisions, news (pgvector), llm_calls

Revision ID: 0002_phase1_schema
Revises: 0001_initial
Create Date: 2026-06-01

Создаёт расширение ``vector`` (pgvector), все базовые таблицы,
enum-типы и индексы, включая IVFFlat над ``news.embedding``
(``vector_cosine_ops``, lists=100).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.config import settings


revision: str = "0002_phase1_schema"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ``create_type=False`` отключает авто-создание enum'ов внутри
# ``op.create_table`` (без ``checkfirst``). Создаём их явно в начале
# ``upgrade()`` через ``.create(bind, checkfirst=True)`` — иначе при
# использовании одного и того же ENUM в нескольких CREATE TABLE
# SQLAlchemy попытается создать тип повторно и упадёт с
# DuplicateObjectError.
_DECISION_ACTION = postgresql.ENUM(
    "BUY", "SELL", "HOLD",
    name="decision_action_enum",
    create_type=False,
)
_TRANSACTION_ACTION = postgresql.ENUM(
    "BUY", "SELL",
    name="transaction_action_enum",
    create_type=False,
)
_LLM_CALL_STATUS = postgresql.ENUM(
    "IN_PROGRESS", "COMPLETE", "ERROR",
    name="llm_call_status_enum",
    create_type=False,
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    bind = op.get_bind()
    _DECISION_ACTION.create(bind, checkfirst=True)
    _TRANSACTION_ACTION.create(bind, checkfirst=True)
    _LLM_CALL_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("initial_capital_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("initial_capital_usdt", sa.Numeric(18, 8), nullable=False),
        sa.Column("initial_usdt_rub_rate", sa.Numeric(18, 8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "wallets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset", sa.String(10), nullable=False),
        sa.Column(
            "balance",
            sa.Numeric(28, 12),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "asset", name="uq_wallets_user_asset"),
    )

    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pipeline_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("asset", sa.String(10), nullable=False),
        sa.Column("action", _DECISION_ACTION, nullable=False),
        sa.Column("buy_fraction", sa.Numeric(5, 4), nullable=True),
        sa.Column("executed", sa.Boolean(), nullable=True),
        sa.Column("not_executed_reason", sa.String(128), nullable=True),
        sa.Column("price_summary", sa.Text(), nullable=True),
        sa.Column("news_score", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_decisions_user_asset_created",
        "decisions",
        ["user_id", "asset", "created_at"],
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "decision_id",
            sa.Integer(),
            sa.ForeignKey("decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("asset", sa.String(10), nullable=False),
        sa.Column("action", _TRANSACTION_ACTION, nullable=False),
        sa.Column("amount_crypto", sa.Numeric(28, 12), nullable=False),
        sa.Column("price_usdt", sa.Numeric(28, 8), nullable=False),
        sa.Column("gross_usdt", sa.Numeric(28, 8), nullable=False),
        sa.Column("fee_usdt", sa.Numeric(28, 8), nullable=False),
        sa.Column("net_usdt", sa.Numeric(28, 8), nullable=False),
        sa.Column("usdt_balance_after", sa.Numeric(28, 8), nullable=False),
        sa.Column("asset_balance_after", sa.Numeric(28, 12), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_transactions_user_asset_created",
        "transactions",
        ["user_id", "asset", "created_at"],
    )

    op.create_table(
        "news",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset", sa.String(10), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("source", sa.String(128), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(settings.agent.embedding_dim), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("url", name="uq_news_url"),
        sa.UniqueConstraint("asset", "external_id", name="uq_news_asset_external_id"),
    )
    op.create_index(
        "ix_news_asset_published_at",
        "news",
        ["asset", "published_at"],
    )
    # IVFFlat-индекс для cosine-поиска по эмбеддингам.
    # ``lists=100`` — стартовое значение, тюним при росте корпуса >10k.
    op.execute(
        "CREATE INDEX ix_news_embedding_ivfflat "
        "ON news USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pipeline_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("agent_name", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("status", _LLM_CALL_STATUS, nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_llm_calls_agent_created",
        "llm_calls",
        ["agent_name", "created_at"],
    )
    op.create_index(
        "ix_llm_calls_pipeline_run_id",
        "llm_calls",
        ["pipeline_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_calls_pipeline_run_id", table_name="llm_calls")
    op.drop_index("ix_llm_calls_agent_created", table_name="llm_calls")
    op.drop_table("llm_calls")

    op.execute("DROP INDEX IF EXISTS ix_news_embedding_ivfflat")
    op.drop_index("ix_news_asset_published_at", table_name="news")
    op.drop_table("news")

    op.drop_index("ix_transactions_user_asset_created", table_name="transactions")
    op.drop_table("transactions")

    op.drop_index("ix_decisions_user_asset_created", table_name="decisions")
    op.drop_table("decisions")

    op.drop_table("wallets")
    op.drop_table("users")

    bind = op.get_bind()
    _LLM_CALL_STATUS.drop(bind, checkfirst=True)
    _TRANSACTION_ACTION.drop(bind, checkfirst=True)
    _DECISION_ACTION.drop(bind, checkfirst=True)

    op.execute("DROP EXTENSION IF EXISTS vector")
