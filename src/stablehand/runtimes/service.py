from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stablehand.models import ExecutorKind, Runtime, Stack


class RuntimeConfigError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def save_runtime(
    session: Session,
    *,
    runtime: Runtime | None,
    name: str,
    executor: str,
    secret_ref: str,
) -> Runtime:
    cleaned_name = name.strip()
    if not cleaned_name:
        raise RuntimeConfigError("Name is required.")
    if executor not in {ExecutorKind.local.value, ExecutorKind.kubernetes.value}:
        raise RuntimeConfigError("Choose a local or Kubernetes executor.")
    existing = session.scalar(select(Runtime).where(Runtime.name == cleaned_name))
    if existing is not None and (runtime is None or existing.id != runtime.id):
        raise RuntimeConfigError("A runtime with this name already exists.")
    if runtime is None:
        runtime = Runtime(name=cleaned_name)
        session.add(runtime)
    runtime.name = cleaned_name
    runtime.executor = executor
    runtime.secret_ref = secret_ref.strip() or None
    session.flush()
    session.refresh(runtime)
    return runtime


def delete_runtime(session: Session, runtime: Runtime) -> None:
    count = int(
        session.scalar(
            select(func.count()).select_from(Stack).where(Stack.runtime_id == runtime.id)
        )
        or 0
    )
    if count:
        noun = "stack" if count == 1 else "stacks"
        raise RuntimeConfigError(f"{count} {noun} use this runtime.")
    session.delete(runtime)
    try:
        session.flush()
    except IntegrityError as exc:
        raise RuntimeConfigError("Stacks still use this runtime.") from exc


def get_runtime(session: Session, runtime_id: uuid.UUID) -> Runtime | None:
    return session.get(Runtime, runtime_id)
