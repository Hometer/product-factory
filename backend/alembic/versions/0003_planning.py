"""Durable append-only technical statements and version/stage plans."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "planning_assets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("baseline_id", sa.String(), sa.ForeignKey("requirement_records.id"), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "kind", "revision"),
    )
    op.create_index("ix_planning_assets_project_id", "planning_assets", ["project_id"])
    op.create_index("ix_planning_assets_kind", "planning_assets", ["kind"])


def downgrade():
    raise RuntimeError("禁止自动删除技术方案、计划与人工确认历史")
