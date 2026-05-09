"""profile_jobs dedupe fix: drop unique(dedupe_key), add index(dedupe_key,status)

Revision ID: y2u3i4o5p6a7
Revises: r6t7y8u9i0o1
Create Date: 2026-05-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "y2u3i4o5p6a7"
down_revision: Union[str, Sequence[str], None] = "r6t7y8u9i0o1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(conn, table: str, index: str) -> bool:
    row = conn.execute(sa.text("SHOW INDEX FROM %s WHERE Key_name=:k" % table), {"k": index}).fetchone()
    return bool(row)


def upgrade() -> None:
    conn = op.get_bind()

    # Drop unique constraint (created as unique index in MySQL)
    try:
        op.drop_constraint("uq_profile_jobs_dedupe_key", "profile_jobs", type_="unique")
    except Exception:
        # fallback: try dropping as index
        try:
            op.drop_index("uq_profile_jobs_dedupe_key", table_name="profile_jobs")
        except Exception:
            pass

    # Ensure non-unique indexes for fast NOT EXISTS checks
    if not _has_index(conn, "profile_jobs", "ix_profile_jobs_dedupe_key"):
        op.create_index("ix_profile_jobs_dedupe_key", "profile_jobs", ["dedupe_key"], unique=False)
    if not _has_index(conn, "profile_jobs", "ix_profile_jobs_dedupe_status"):
        op.create_index(
            "ix_profile_jobs_dedupe_status",
            "profile_jobs",
            ["dedupe_key", "status"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    # Best-effort rollback
    try:
        if _has_index(conn, "profile_jobs", "ix_profile_jobs_dedupe_status"):
            op.drop_index("ix_profile_jobs_dedupe_status", table_name="profile_jobs")
        if _has_index(conn, "profile_jobs", "ix_profile_jobs_dedupe_key"):
            op.drop_index("ix_profile_jobs_dedupe_key", table_name="profile_jobs")
    except Exception:
        pass
    try:
        op.create_unique_constraint("uq_profile_jobs_dedupe_key", "profile_jobs", ["dedupe_key"])
    except Exception:
        pass

