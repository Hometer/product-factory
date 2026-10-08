"""Append-only execution preparation, checks and local scope decisions."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "execution_assets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("planning_id", sa.String(), sa.ForeignKey("planning_assets.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "task_id", "kind", "revision"),
    )
    op.create_index("ix_execution_assets_project_id", "execution_assets", ["project_id"])


def downgrade():
    raise RuntimeError("禁止自动删除执行计划、开工检查与授权历史")
