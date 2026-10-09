"""Persistent command control requests and bounded attempt lineage."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "command_controls",
        sa.Column("run_id", sa.String(), sa.ForeignKey("command_runs.id"), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("root_id", sa.String(), sa.ForeignKey("command_runs.id"), nullable=False),
        sa.Column("parent_id", sa.String(), sa.ForeignKey("command_runs.id"), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("root_id", "attempt"),
    )
    for column in ("project_id", "root_id"):
        op.create_index("ix_command_controls_" + column, "command_controls", [column])


def downgrade():
    raise RuntimeError("禁止删除控制请求、尝试原因和结果历史")
