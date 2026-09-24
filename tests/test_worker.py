from datetime import timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from stablehand import worker
from stablehand.db import get_sessionmaker
from stablehand.models import Execution, ExecutionStatus, Run, RunState, Stack, new_id
from stablehand.runs.service import CHECK, claim, create_run
from stablehand.security import aware, utcnow


def _stack_run(db):
    stack = Stack(
        name="demo",
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor="local",
        local_path="/tmp/demo",
    )
    db.add(stack)
    db.commit()
    db.refresh(stack)
    run = create_run(db, stack, commit_sha="abc123", trigger="manual", user=None)
    db.commit()
    return stack, run


def test_dispatch_persists_pending_before_start(db, monkeypatch):
    _, run = _stack_run(db)
    executor = Mock()

    def start(run, stack, phase, token, *, execution_id):
        verification = get_sessionmaker()()
        try:
            persisted = verification.get(Execution, execution_id)
            assert persisted is not None
            assert persisted.status == ExecutionStatus.pending.value
        finally:
            verification.close()
        return "runner-ref", None

    executor.start.side_effect = start
    monkeypatch.setattr(worker, "for_stack", lambda kind: executor)

    worker._dispatch(db, RunState.check_queued, RunState.check_running, CHECK)

    execution = db.scalar(select(Execution).where(Execution.run_id == run.id))
    assert execution is not None
    assert executor.start.call_args.kwargs["execution_id"] == execution.id
    assert execution.ref == "runner-ref"
    assert execution.status == ExecutionStatus.running.value


def test_start_failure_marks_execution_and_run_failed(db, monkeypatch):
    _, run = _stack_run(db)
    executor = Mock()
    executor.start.side_effect = RuntimeError("could not start")
    monkeypatch.setattr(worker, "for_stack", lambda kind: executor)

    worker._dispatch(db, RunState.check_queued, RunState.check_running, CHECK)

    db.refresh(run)
    execution = db.scalar(select(Execution).where(Execution.run_id == run.id))
    assert run.state == RunState.check_failed.value
    assert execution is not None
    assert execution.status == ExecutionStatus.failed.value


def test_recover_pending_execution(db, monkeypatch):
    stack, run = _stack_run(db)
    assert claim(db, run, RunState.check_queued, RunState.check_running)
    execution = Execution(
        id=new_id(),
        run_id=run.id,
        phase=CHECK,
        backend=stack.executor,
        status=ExecutionStatus.pending.value,
        started_at=utcnow() - worker.PENDING_GRACE - timedelta(seconds=1),
    )
    db.add(execution)
    db.commit()
    executor = Mock()
    executor.recover.return_value = ("recovered-ref", "recovered-secret")
    monkeypatch.setattr(worker, "for_stack", lambda kind: executor)

    worker._recover_pending(db)

    db.refresh(execution)
    assert execution.status == ExecutionStatus.running.value
    assert execution.ref == "recovered-ref"


def test_pending_execution_prevents_unstarted_sweep(db):
    stack, run = _stack_run(db)
    assert claim(db, run, RunState.check_queued, RunState.check_running)
    run.updated_at = utcnow() - timedelta(minutes=2)
    db.add(
        Execution(
            id=new_id(),
            run_id=run.id,
            phase=CHECK,
            backend=stack.executor,
            status=ExecutionStatus.pending.value,
        )
    )
    db.commit()

    worker._sweep_unstarted(db)

    db.refresh(run)
    assert run.state == RunState.check_running.value


def test_schedule_advance_rolls_back_with_run_creation(db, monkeypatch):
    stack = Stack(
        name="scheduled",
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor="local",
        local_path="/tmp/demo",
        schedule_cron="* * * * *",
        schedule_next_at=utcnow() - timedelta(minutes=1),
    )
    db.add(stack)
    db.commit()
    previous_next = stack.schedule_next_at
    monkeypatch.setattr(worker, "resolve_commit", lambda current: "scheduled-sha")
    create = worker.create_run

    def create_then_fail(*args, **kwargs):
        create(*args, **kwargs)
        raise RuntimeError("transaction failed")

    monkeypatch.setattr(worker, "create_run", create_then_fail)

    with pytest.raises(RuntimeError, match="transaction failed"):
        worker._schedule_due(db)
    db.rollback()

    db.refresh(stack)
    run = db.scalar(select(Run).where(Run.stack_id == stack.id))
    assert run is None
    assert aware(stack.schedule_next_at) == aware(previous_next)
