"""Append-only requirements, confirmation records and durable model budget."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "requirement_revisions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "revision"),
    )
    op.create_table(
        "requirement_records",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_requirement_records_project_id", "requirement_records", ["project_id"])
    op.create_index("ix_requirement_records_kind", "requirement_records", ["kind"])
    op.create_table(
        "model_budgets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("spent_micro", sa.Integer(), nullable=False),
        sa.Column("reserved_micro", sa.Integer(), nullable=False),
        sa.Column("requests_used", sa.Integer(), nullable=False),
        sa.Column("requests_reserved", sa.Integer(), nullable=False),
    )


def downgrade():
    raise RuntimeError("禁止自动删除需求、授权或费用数据")
