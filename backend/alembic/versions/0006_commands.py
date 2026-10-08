"""Persistent reviewed command recipes and subprocess records."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    for name, fk, target in (
        ("command_assets", "execution_id", "execution_assets.id"),
        ("command_runs", "asset_id", "command_assets.id"),
    ):
        columns = [
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column(fk, sa.String(), sa.ForeignKey(target), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ]
        if name == "command_assets":
            columns.append(sa.Column("payload", sa.JSON(), nullable=False))
        else:
            columns.extend(
                [
                    sa.Column("state", sa.String(), nullable=False),
                    sa.Column("details", sa.JSON(), nullable=False),
                ]
            )
        op.create_table(name, *columns)
        op.create_index("ix_" + name + "_project_id", name, ["project_id"])


def downgrade():
    raise RuntimeError("禁止自动删除命令批准、运行与产物记录")
