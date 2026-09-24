from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Role(enum.StrEnum):
    member = "member"
    admin = "admin"


class ExecutorKind(enum.StrEnum):
    local = "local"
    kubernetes = "kubernetes"


class Trigger(enum.StrEnum):
    manual = "manual"
    ci = "ci"
    schedule = "schedule"


class RunState(enum.StrEnum):
    check_queued = "check_queued"
    check_running = "check_running"
    needs_approval = "needs_approval"
    unchanged = "unchanged"
    check_failed = "check_failed"
    apply_queued = "apply_queued"
    apply_running = "apply_running"
    succeeded = "succeeded"
    apply_failed = "apply_failed"
    rejected = "rejected"


ACTIVE_STATES = {
    RunState.check_queued,
    RunState.check_running,
    RunState.needs_approval,
    RunState.apply_queued,
    RunState.apply_running,
}

LIVE_STATES = {
    RunState.check_queued,
    RunState.check_running,
    RunState.apply_queued,
    RunState.apply_running,
}

TERMINAL_STATES = {
    RunState.unchanged,
    RunState.check_failed,
    RunState.succeeded,
    RunState.apply_failed,
    RunState.rejected,
}


def new_id() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    role: Mapped[str] = mapped_column(String(32), default=Role.member.value)
    groups: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sessions: Mapped[list[Session]] = relationship(back_populates="user")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    oauth_state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="sessions")


class OidcSettings(Base):
    __tablename__ = "oidc_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    issuer: Mapped[str] = mapped_column(String(500), default="")
    client_id: Mapped[str] = mapped_column(String(255), default="")
    client_secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    scopes: Mapped[str] = mapped_column(String(255), default="openid email profile")


class Stack(Base):
    __tablename__ = "stacks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    deploy_file: Mapped[str] = mapped_column(String(500))
    inventory: Mapped[str] = mapped_column(String(500))
    executor: Mapped[str] = mapped_column(String(32), default=ExecutorKind.local.value)
    git_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    git_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    local_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    schedule_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    schedule_next_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    approvers: Mapped[list[StackApprover]] = relationship(
        back_populates="stack", cascade="all, delete-orphan"
    )
    tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="stack", cascade="all, delete-orphan"
    )
    runs: Mapped[list[Run]] = relationship(back_populates="stack", cascade="all, delete-orphan")


class StackApprover(Base):
    __tablename__ = "stack_approvers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    stack_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stacks.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    stack: Mapped[Stack] = relationship(back_populates="approvers")
    user: Mapped[User | None] = relationship()


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    stack_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stacks.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    stack: Mapped[Stack] = relationship(back_populates="tokens")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_stack_id", "stack_id"),
        Index("ix_runs_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    stack_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stacks.id", ondelete="CASCADE"))
    commit_sha: Mapped[str] = mapped_column(String(64))
    trigger: Mapped[str] = mapped_column(String(32))
    trigger_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), default=RunState.check_queued.value)
    check_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    apply_raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    check_document: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    previous_check_document: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    apply_document: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approval_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    stack: Mapped[Stack] = relationship(back_populates="runs")
    trigger_user: Mapped[User | None] = relationship(foreign_keys=[trigger_user_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
    rejected_by: Mapped[User | None] = relationship(foreign_keys=[rejected_by_id])
    logs: Mapped[list[RunLog]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunLog.id"
    )
    tokens: Mapped[list[RunToken]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    executions: Mapped[list[Execution]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class RunToken(Base):
    __tablename__ = "run_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    phase: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[Run] = relationship(back_populates="tokens")


class RunLog(Base):
    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    phase: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[Run] = relationship(back_populates="logs")


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    phase: Mapped[str] = mapped_column(String(16))
    backend: Mapped[str] = mapped_column(String(32))
    ref: Mapped[str] = mapped_column(String(200), default="")
    secret_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="executions")


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
