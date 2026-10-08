from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    canonical_path: Mapped[str] = mapped_column(String, unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Preview(Base):
    __tablename__ = "previews"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON)
    manifest: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    confirmed_project_id: Mapped[str | None] = mapped_column(String, nullable=True)


class RequestRecord(Base):
    __tablename__ = "requests"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String)
    response: Mapped[dict] = mapped_column(JSON)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    details: Mapped[dict] = mapped_column(JSON)


class RequirementRevision(Base):
    __tablename__ = "requirement_revisions"
    __table_args__ = (UniqueConstraint("project_id", "revision"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RequirementRecord(Base):
    __tablename__ = "requirement_records"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ModelBudget(Base):
    __tablename__ = "model_budgets"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    spent_micro: Mapped[int] = mapped_column(Integer, default=0)
    reserved_micro: Mapped[int] = mapped_column(Integer, default=0)
    requests_used: Mapped[int] = mapped_column(Integer, default=0)
    requests_reserved: Mapped[int] = mapped_column(Integer, default=0)


def connect(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{data_dir / 'factory.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        # Explicit BEGIN is shared with Alembic and supports transactional DDL.
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    return engine, sessionmaker(engine, expire_on_commit=False)
