"""ORM models and engine construction (SQLite for development, PostgreSQL in production).

Tables:
    customers          one golden record per customer
    customer_sources   source records merged into each customer
    pipeline_runs      run audit log; also holds the single-run lock
    rejected_records   records that failed validation, with reasons and raw data
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import (
    Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
    create_engine, event, text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker,
)
from sqlalchemy.pool import StaticPool

from .retry import retry

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    city: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(2), index=True)
    plan: Mapped[str | None] = mapped_column(String(30), index=True)
    customer_since: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    # Optimistic locking: UPDATEs are conditional on the version that was read,
    # so a concurrent change raises StaleDataError instead of being overwritten.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    sources: Mapped[list["CustomerSource"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan", lazy="selectin"
    )

    __mapper_args__ = {"version_id_col": version}


class CustomerSource(Base):
    __tablename__ = "customer_sources"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_source_record"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(64))
    last_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_runs.id"))

    customer: Mapped[Customer] = relationship(back_populates="sources")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        # Run lock: at most one row may have status 'running'. Enforced by the
        # database, so it holds across threads, processes and API replicas.
        Index(
            "uq_one_running_run", "status", unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|success|failed
    records_read: Mapped[int] = mapped_column(Integer, default=0)
    records_valid: Mapped[int] = mapped_column(Integer, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, default=0)
    customers_created: Mapped[int] = mapped_column(Integer, default=0)
    customers_updated: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    rejects: Mapped[list["RejectedRow"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class RejectedRow(Base):
    __tablename__ = "rejected_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(64))
    reasons: Mapped[str] = mapped_column(Text)
    raw_data: Mapped[str] = mapped_column(Text)  # original record as JSON

    run: Mapped[PipelineRun] = relationship(back_populates="rejects")


def make_engine(
    database_url: str, timeout_seconds: int = 10, statement_timeout_ms: int = 30_000
) -> Engine:
    if database_url.startswith("sqlite"):
        kwargs: dict = {
            # Sessions are per request, so sharing the driver across FastAPI's
            # worker threads is safe. `timeout` is the wait for a write lock.
            "connect_args": {"check_same_thread": False, "timeout": timeout_seconds},
        }
        if ":memory:" in database_url or database_url == "sqlite://":
            kwargs["poolclass"] = StaticPool  # an in-memory DB lives on a single connection
        engine = create_engine(database_url, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            dbapi_conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer

        return engine

    connect_args: dict = {}
    if database_url.startswith("postgresql"):
        connect_args = {
            "connect_timeout": timeout_seconds,
            "options": f"-c statement_timeout={statement_timeout_ms}",
        }
    return create_engine(
        database_url,
        pool_size=5,
        max_overflow=10,
        pool_timeout=timeout_seconds,
        pool_recycle=1800,
        pool_pre_ping=True,  # discard connections the server has closed
        connect_args=connect_args,
    )


def wait_for_db(engine: Engine, attempts: int = 6, base_delay: float = 0.5, sleep=None) -> None:
    def ping() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    kwargs = {"sleep": sleep} if sleep else {}
    retry(ping, attempts=attempts, base_delay=base_delay, retry_on=(OperationalError,), **kwargs)
    logger.info("database reachable: %s", engine.url.render_as_string(hide_password=True))


def init_db(engine: Engine) -> None:
    """Create missing tables. Schema migrations (Alembic) are listed under future work."""
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
