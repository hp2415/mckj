"""cleanup deprecated system_configs keys

Revision ID: v1w2x3y4z5a6
Revises: u7v8w9x0y1z2
Create Date: 2026-04-25

目标：
- 安全迁移画像模型旧键 llm_model → profile_llm_model（仅当新键不存在且旧值非空）
- 清理已废弃的 dify_* 配置项（代码已不再读取）

注意：
- 只动 system_configs 行数据，不改表结构。
- 不删除运行态自动写入的 sync_* / wechat_* 状态键，避免影响任务进度面板。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v1w2x3y4z5a6"
down_revision: Union[str, Sequence[str], None] = "u7v8w9x0y1z2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1) llm_model -> profile_llm_model（仅在新键缺失时迁移一次，且旧值非空）
    conn.execute(
        sa.text(
            """
            INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
            SELECT 'profile_llm_model', sc.config_value, 'ai', NOW()
            FROM system_configs sc
            WHERE sc.config_key = 'llm_model'
              AND TRIM(IFNULL(sc.config_value, '')) <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM system_configs x WHERE x.config_key = 'profile_llm_model'
              )
            """
        )
    )

    # 2) 清理废弃 Dify 配置（不再有任何代码读取）
    conn.execute(
        sa.text(
            """
            DELETE FROM system_configs
            WHERE config_key IN ('dify_api_url', 'dify_api_key', 'dify_base_url')
            """
        )
    )

    # 3) 删除旧画像模型键（已迁移后无必要继续保留；若未迁移则可能为空/无效）
    conn.execute(sa.text("DELETE FROM system_configs WHERE config_key = 'llm_model'"))


def downgrade() -> None:
    conn = op.get_bind()

    # 1) 恢复 llm_model（若缺失且 profile_llm_model 有值，则复制回去）
    conn.execute(
        sa.text(
            """
            INSERT INTO system_configs (config_key, config_value, config_group, updated_at)
            SELECT 'llm_model', sc.config_value, 'ai', NOW()
            FROM system_configs sc
            WHERE sc.config_key = 'profile_llm_model'
              AND TRIM(IFNULL(sc.config_value, '')) <> ''
              AND NOT EXISTS (SELECT 1 FROM system_configs x WHERE x.config_key = 'llm_model')
            """
        )
    )

    # 2) dify_* 不做恢复：已废弃且无代码读取（保留为空也没有意义）

