"""backfill raw_customers + sales_customer_profiles from legacy tables

Revision ID: s0c1d2e3f4g5
Revises: s0b1c2d3e4f5
Create Date: 2026-04-24

- Migrate legacy customers/user_customer_relations/ucr_profile_tags into the new model:
  raw_customers + sales_customer_profiles + scp_profile_tags.
- Prepare chat_messages migration by backfilling a nullable raw_customer_id column.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s0c1d2e3f4g5"
down_revision: Union[str, Sequence[str], None] = "s0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, name: str) -> bool:
    res = conn.execute(sa.text("SHOW TABLES LIKE :n"), {"n": name})
    return res.fetchone() is not None


def _colnames(conn, table: str) -> set[str]:
    rows = conn.execute(sa.text(f"SHOW COLUMNS FROM {table}")).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    conn = op.get_bind()

    legacy_has_customers = _table_exists(conn, "customers")
    legacy_has_ucr = _table_exists(conn, "user_customer_relations")
    legacy_has_ucr_tags = _table_exists(conn, "ucr_profile_tags")

    if legacy_has_customers:
        # Merge legacy entity fields onto raw_customers by external_id.
        # purchase_months: legacy is comma-separated string; keep it as JSON array.
        conn.execute(
            sa.text(
                """
                UPDATE raw_customers rc
                INNER JOIN customers c ON c.external_id = rc.id
                SET
                  rc.phone_normalized = COALESCE(NULLIF(TRIM(c.phone), ''), rc.phone_normalized),
                  rc.customer_name = COALESCE(NULLIF(TRIM(c.customer_name), ''), rc.customer_name),
                  rc.unit_name = COALESCE(NULLIF(TRIM(c.unit_name), ''), rc.unit_name),
                  rc.unit_type = COALESCE(NULLIF(TRIM(c.unit_type), ''), rc.unit_type),
                  rc.admin_division = COALESCE(NULLIF(TRIM(c.admin_division), ''), rc.admin_division),
                  rc.profile_status = CASE WHEN c.profile_status = 1 THEN 1 ELSE rc.profile_status END,
                  rc.profile_updated_at = CASE
                    WHEN c.profile_status = 1 THEN COALESCE(rc.profile_updated_at, NOW())
                    ELSE rc.profile_updated_at
                  END
                """
            )
        )
        # MySQL doesn't allow JSON_ARRAYAGG in UPDATE like above; do a second pass building JSON with REPLACE.
        # If purchase_months is already JSON keep it; else convert "1,2,3" -> ["1","2","3"].
        conn.execute(
            sa.text(
                """
                UPDATE raw_customers rc
                INNER JOIN customers c ON c.external_id = rc.id
                SET rc.purchase_months = CASE
                  WHEN c.purchase_months IS NULL OR TRIM(c.purchase_months) = '' THEN rc.purchase_months
                  WHEN JSON_VALID(c.purchase_months) THEN CAST(c.purchase_months AS JSON)
                  ELSE CAST(CONCAT('["', REPLACE(TRIM(c.purchase_months), ',', '","'), '"]') AS JSON)
                END
                """
            )
        )

    if legacy_has_ucr and legacy_has_customers:
        # Backfill per-sales profiles.
        # - raw_customer_id from customers.external_id
        # - sales_wechat_id: prefer ucr.sales_wechat_id, else user's primary binding.
        # - wechat_remark: prefer ucr.wechat_remark, else raw_customers.remark.
        cols = _colnames(conn, "user_customer_relations")
        has_sales_wechat_id = "sales_wechat_id" in cols

        if has_sales_wechat_id:
            conn.execute(
                sa.text(
                    """
                    INSERT IGNORE INTO sales_customer_profiles
                      (raw_customer_id, sales_wechat_id, user_id, relation_type, title, budget_amount,
                       contact_date, purchase_type, wechat_remark, ai_profile, suggested_followup_date,
                       dify_conversation_id, created_at, updated_at)
                    SELECT
                      c.external_id AS raw_customer_id,
                      COALESCE(NULLIF(TRIM(ucr.sales_wechat_id), ''), usw.sales_wechat_id) AS sales_wechat_id,
                      ucr.user_id AS user_id,
                      COALESCE(NULLIF(TRIM(ucr.relation_type), ''), 'active') AS relation_type,
                      ucr.title AS title,
                      ucr.budget_amount AS budget_amount,
                      ucr.contact_date AS contact_date,
                      ucr.purchase_type AS purchase_type,
                      COALESCE(NULLIF(TRIM(ucr.wechat_remark), ''), rc.remark) AS wechat_remark,
                      ucr.ai_profile AS ai_profile,
                      ucr.suggested_followup_date AS suggested_followup_date,
                      ucr.dify_conversation_id AS dify_conversation_id,
                      NOW(), NOW()
                    FROM user_customer_relations ucr
                    INNER JOIN customers c ON c.id = ucr.customer_id
                    INNER JOIN raw_customers rc ON rc.id = c.external_id
                    LEFT JOIN user_sales_wechats usw
                      ON usw.user_id = ucr.user_id AND usw.is_primary = 1
                    WHERE c.external_id IS NOT NULL AND TRIM(c.external_id) != ''
                    """
                )
            )
        else:
            # Legacy without sales_wechat_id: only primary binding.
            conn.execute(
                sa.text(
                    """
                    INSERT IGNORE INTO sales_customer_profiles
                      (raw_customer_id, sales_wechat_id, user_id, relation_type, title, budget_amount,
                       contact_date, purchase_type, wechat_remark, ai_profile, suggested_followup_date,
                       dify_conversation_id, created_at, updated_at)
                    SELECT
                      c.external_id AS raw_customer_id,
                      usw.sales_wechat_id AS sales_wechat_id,
                      ucr.user_id AS user_id,
                      COALESCE(NULLIF(TRIM(ucr.relation_type), ''), 'active') AS relation_type,
                      ucr.title AS title,
                      ucr.budget_amount AS budget_amount,
                      ucr.contact_date AS contact_date,
                      ucr.purchase_type AS purchase_type,
                      COALESCE(NULLIF(TRIM(ucr.wechat_remark), ''), rc.remark) AS wechat_remark,
                      ucr.ai_profile AS ai_profile,
                      ucr.suggested_followup_date AS suggested_followup_date,
                      ucr.dify_conversation_id AS dify_conversation_id,
                      NOW(), NOW()
                    FROM user_customer_relations ucr
                    INNER JOIN customers c ON c.id = ucr.customer_id
                    INNER JOIN raw_customers rc ON rc.id = c.external_id
                    LEFT JOIN user_sales_wechats usw
                      ON usw.user_id = ucr.user_id AND usw.is_primary = 1
                    WHERE c.external_id IS NOT NULL AND TRIM(c.external_id) != ''
                    """
                )
            )

    if legacy_has_ucr_tags and legacy_has_ucr and legacy_has_customers:
        # Backfill tag mapping: ucr -> scp via (raw_customer_id, sales_wechat_id) match.
        cols = _colnames(conn, "user_customer_relations")
        has_sales_wechat_id = "sales_wechat_id" in cols

        if has_sales_wechat_id:
            conn.execute(
                sa.text(
                    """
                    INSERT IGNORE INTO scp_profile_tags (sales_customer_profile_id, profile_tag_id)
                    SELECT
                      scp.id AS sales_customer_profile_id,
                      upt.profile_tag_id AS profile_tag_id
                    FROM ucr_profile_tags upt
                    INNER JOIN user_customer_relations ucr ON ucr.id = upt.user_customer_relation_id
                    INNER JOIN customers c ON c.id = ucr.customer_id
                    INNER JOIN sales_customer_profiles scp
                      ON scp.raw_customer_id = c.external_id
                      AND (scp.sales_wechat_id <=> ucr.sales_wechat_id)
                    """
                )
            )
        else:
            conn.execute(
                sa.text(
                    """
                    INSERT IGNORE INTO scp_profile_tags (sales_customer_profile_id, profile_tag_id)
                    SELECT
                      scp.id AS sales_customer_profile_id,
                      upt.profile_tag_id AS profile_tag_id
                    FROM ucr_profile_tags upt
                    INNER JOIN user_customer_relations ucr ON ucr.id = upt.user_customer_relation_id
                    INNER JOIN customers c ON c.id = ucr.customer_id
                    INNER JOIN sales_customer_profiles scp
                      ON scp.raw_customer_id = c.external_id
                    """
                )
            )

    # Prepare chat_messages migration: add and backfill nullable raw_customer_id
    if _table_exists(conn, "chat_messages") and legacy_has_customers:
        cm_cols = _colnames(conn, "chat_messages")
        if "raw_customer_id" not in cm_cols:
            op.add_column("chat_messages", sa.Column("raw_customer_id", sa.String(length=100), nullable=True))
            op.create_index("ix_chat_messages_raw_customer_id", "chat_messages", ["raw_customer_id"], unique=False)

        # Backfill from legacy customer_id -> customers.external_id
        if "customer_id" in cm_cols:
            conn.execute(
                sa.text(
                    """
                    UPDATE chat_messages cm
                    INNER JOIN customers c ON c.id = cm.customer_id
                    SET cm.raw_customer_id = c.external_id
                    WHERE (cm.raw_customer_id IS NULL OR TRIM(cm.raw_customer_id) = '')
                      AND c.external_id IS NOT NULL AND TRIM(c.external_id) != ''
                    """
                )
            )


def downgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, "chat_messages"):
        cols = _colnames(conn, "chat_messages")
        if "raw_customer_id" in cols:
            try:
                op.drop_index("ix_chat_messages_raw_customer_id", table_name="chat_messages")
            except Exception:
                pass
            op.drop_column("chat_messages", "raw_customer_id")

