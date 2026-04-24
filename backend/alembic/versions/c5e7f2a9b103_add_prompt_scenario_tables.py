"""add prompt scenario / version / doc tables

Revision ID: c5e7f2a9b103
Revises: a1b2c3d4e5f6
Create Date: 2026-04-23

新增提示词场景化与管理平台所需的全部核心表：
- prompt_scenarios：场景主表
- prompt_versions：场景提示词版本（含 template/doc_refs/params JSON）
- prompt_docs：参考话术文档主表
- prompt_doc_versions：话术文档版本内容
- prompt_rules：动态标签决策规则（Phase3 预留，建表不走查询）
- prompt_audit_log：管理操作审计
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5e7f2a9b103"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, name: str) -> bool:
    res = conn.execute(sa.text("SHOW TABLES LIKE :n"), {"n": name})
    return res.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, "prompt_scenarios"):
        op.create_table(
            "prompt_scenarios",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("scenario_key", sa.String(80), nullable=False, unique=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
            sa.Column("tools_enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
            sa.Column(
                "created_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    if not _table_exists(conn, "prompt_versions"):
        op.create_table(
            "prompt_versions",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "scenario_id",
                sa.Integer,
                sa.ForeignKey("prompt_scenarios.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("template_json", sa.JSON, nullable=False),
            sa.Column("doc_refs_json", sa.JSON, nullable=True),
            sa.Column("params_json", sa.JSON, nullable=True),
            sa.Column("rollout_json", sa.JSON, nullable=True),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("created_by", sa.Integer, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("published_at", sa.DateTime, nullable=True),
            sa.UniqueConstraint("scenario_id", "version", name="uq_prompt_version_sv"),
        )
        op.create_index(
            "ix_prompt_versions_scenario_status",
            "prompt_versions",
            ["scenario_id", "status"],
        )

    if not _table_exists(conn, "prompt_docs"):
        op.create_table(
            "prompt_docs",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("doc_key", sa.String(80), nullable=False, unique=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    if not _table_exists(conn, "prompt_doc_versions"):
        op.create_table(
            "prompt_doc_versions",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "doc_id",
                sa.Integer,
                sa.ForeignKey("prompt_docs.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("content", sa.Text(length=16_000_000), nullable=False),
            sa.Column("source_filename", sa.String(255), nullable=True),
            sa.Column("created_by", sa.Integer, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("published_at", sa.DateTime, nullable=True),
            sa.UniqueConstraint("doc_id", "version", name="uq_prompt_doc_version_dv"),
        )
        op.create_index(
            "ix_prompt_doc_versions_doc_status",
            "prompt_doc_versions",
            ["doc_id", "status"],
        )

    if not _table_exists(conn, "prompt_rules"):
        op.create_table(
            "prompt_rules",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "scenario_id",
                sa.Integer,
                sa.ForeignKey("prompt_scenarios.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
            sa.Column("condition_json", sa.JSON, nullable=True),
            sa.Column("action_json", sa.JSON, nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="disabled"),
            sa.Column("description", sa.String(255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    if not _table_exists(conn, "prompt_audit_log"):
        op.create_table(
            "prompt_audit_log",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("actor_id", sa.Integer, nullable=True),
            sa.Column("action", sa.String(50), nullable=False),
            sa.Column("target_type", sa.String(50), nullable=False),
            sa.Column("target_id", sa.Integer, nullable=True),
            sa.Column("payload_json", sa.JSON, nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_prompt_audit_log_target",
            "prompt_audit_log",
            ["target_type", "target_id"],
        )


def downgrade() -> None:
    for tbl in (
        "prompt_audit_log",
        "prompt_rules",
        "prompt_doc_versions",
        "prompt_docs",
        "prompt_versions",
        "prompt_scenarios",
    ):
        try:
            op.drop_table(tbl)
        except Exception:
            pass
