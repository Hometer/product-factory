from alembic import context

from backend.app.db import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("请使用应用启动入口执行迁移")
context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
