"""Append-only command checkpoints; keep all existing business records."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "command_checkpoints",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(), sa.ForeignKey("command_runs.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("project_id", "run_id"):
        op.create_index("ix_command_checkpoints_" + column, "command_checkpoints", [column])


def downgrade():
    raise RuntimeError("禁止自动删除恢复事实和确认历史")
