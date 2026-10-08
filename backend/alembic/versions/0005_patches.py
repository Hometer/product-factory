"""Durable patch assets and file-write operations; no changes to existing rows."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "patch_assets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("execution_id", sa.String(), sa.ForeignKey("execution_assets.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_patch_assets_project_id", "patch_assets", ["project_id"])
    op.create_table(
        "patch_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("patch_id", sa.String(), sa.ForeignKey("patch_assets.id"), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_patch_runs_project_id", "patch_runs", ["project_id"])


def downgrade():
    raise RuntimeError("禁止自动删除补丁及恢复记录")
