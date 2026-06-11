"""llm_usage_log for token/cost observability (A1-6)

Revision ID: e1f2g3h4i5j6
Revises: d6e7f8a9b0c1
Create Date: 2026-06-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1f2g3h4i5j6"
down_revision: Union[str, Sequence[str], None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(conn, name: str) -> bool:
    row = conn.execute(sa.text("SHOW TABLES LIKE :t"), {"t": name}).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, "llm_usage_log"):
        return

    op.create_table(
        "llm_usage_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("api_host", sa.String(length=200), nullable=True),
        sa.Column("scenario_key", sa.String(length=80), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stream_mode", sa.String(length=20), nullable=False, server_default="stream"),
        sa.Column("fallback_reason", sa.String(length=120), nullable=True),
        sa.Column("extra_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_llm_usage_log_created_at", "llm_usage_log", ["created_at"])
    op.create_index("ix_llm_usage_log_scenario_key", "llm_usage_log", ["scenario_key"])
    op.create_index("ix_llm_usage_log_user_id", "llm_usage_log", ["user_id"])
    op.create_index("ix_llm_usage_created_scenario", "llm_usage_log", ["created_at", "scenario_key"])
    op.create_index("ix_llm_usage_scenario_created", "llm_usage_log", ["scenario_key", "created_at"])


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "llm_usage_log"):
        return
    op.drop_table("llm_usage_log")
