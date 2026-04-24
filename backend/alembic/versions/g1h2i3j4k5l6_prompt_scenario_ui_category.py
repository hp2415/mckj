"""prompt_scenarios.ui_category: free_chat / customer_chat / backend_only

Revision ID: g1h2i3j4k5l6
Revises: f0a1b2c3d4e5
Create Date: 2026-04-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "g1h2i3j4k5l6"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prompt_scenarios",
        sa.Column(
            "ui_category",
            sa.String(length=20),
            nullable=False,
            server_default="customer_chat",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE prompt_scenarios SET ui_category = 'backend_only' "
            "WHERE scenario_key = 'customer_profile'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE prompt_scenarios SET ui_category = 'customer_chat' "
            "WHERE scenario_key IN ('general_chat', 'product_recommend')"
        )
    )


def downgrade() -> None:
    op.drop_column("prompt_scenarios", "ui_category")
