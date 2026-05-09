"""merge alembic heads (profile_jobs + wechat_outbound)

Revision ID: q1w2e3r4t5y6
Revises: p9q8r7s6t5u4, w5x6y7z8a9b0
Create Date: 2026-05-08
"""

from typing import Sequence, Union

# Alembic identifiers
revision: str = "q1w2e3r4t5y6"
down_revision: Union[str, Sequence[str], None] = ("p9q8r7s6t5u4", "w5x6y7z8a9b0")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # merge revision: no-op
    pass


def downgrade() -> None:
    # merge revision: no-op
    pass

