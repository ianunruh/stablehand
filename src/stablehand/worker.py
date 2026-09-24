from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from stablehand.config import get_settings
from stablehand.db import get_sessionmaker
from stablehand.executors import for_stack
from stablehand.models import LIVE_STATES, Execution, Run, RunState, Stack
from stablehand.runs.service import (
    APPLY,
    CHECK,
    RunError,
    claim,
    create_run,
    fail_run,
    issue_run_token,
)
from stablehand.security import aware, utcnow
from stablehand.source import SourceError, resolve_commit
from stablehand.stacks.service import next_schedule

logger = logging.getLogger(__name__)
LOCK_KEY = 8142024
GRACE = timedelta(seconds=15)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session_factory = get_sessionmaker()
    while True:
        session = session_factory()
        try:
            if _acquire(session):
                reconcile(session)
        except Exception:
            logger.exception("worker tick failed")
            session.rollback()
        finally:
            session.close()
        time.sleep(2)


def reconcile(session: Session) -> None:
    _schedule_due(session)
    _dispatch(session, RunState.check_queued, RunState.check_running, CHECK)
    _dispatch(session, RunState.apply_queued, RunState.apply_running, APPLY)
    _sweep_unstarted(session)
    _poll(session)


def _acquire(session: Session) -> bool:
    url = get_settings().database_url
    if url.startswith("sqlite"):
        return True
    return bool(
        session.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}).scalar()
    )


def _schedule_due(session: Session) -> None:
    now = utcnow()
    stacks = session.scalars(
        select(Stack).where(Stack.schedule_cron.is_not(None), Stack.schedule_next_at.is_not(None))
    )
    for stack in list(stacks):
        if stack.schedule_next_at is None or aware(stack.schedule_next_at) > now:
            continue
        stack.schedule_next_at = next_schedule(stack.schedule_cron or "", now)
        session.commit()
        try:
            sha = resolve_commit(stack)
        except SourceError as exc:
            logger.warning("scheduled run for %s failed: %s", stack.name, exc.message)
            _record_schedule_failure(session, stack, exc.message)
            continue
        try:
            create_run(session, stack, commit_sha=sha, trigger="schedule", user=None)
        except RunError:
            logger.info("skipping schedule for %s because a run is already active", stack.name)


def _record_schedule_failure(session: Session, stack: Stack, message: str) -> None:
    try:
        run = create_run(session, stack, commit_sha="unresolved", trigger="schedule", user=None)
    except RunError:
        logger.info("skipping schedule failure for %s because a run is already active", stack.name)
        return
    fail_immediate(session, run, message)


def fail_immediate(session: Session, run: Run, message: str) -> None:
    if claim(session, run, RunState.check_queued, RunState.check_running):
        session.refresh(run)
        fail_run(session, run, message)


def _dispatch(session: Session, queued: RunState, running: RunState, phase: str) -> None:
    runs = session.scalars(
        select(Run)
        .where(Run.state == queued.value)
        .options(selectinload(Run.stack).selectinload(Stack.approvers))
    )
    for run in list(runs):
        if not claim(session, run, queued, running):
            continue
        session.refresh(run)
        stack = run.stack
        try:
            token = issue_run_token(session, run, phase)
            ref, secret_name = for_stack(stack.executor).start(run, stack, phase, token)
        except Exception as exc:
            logger.exception("failed to start %s for %s", phase, run.id)
            fail_run(session, run, str(exc))
            continue
        session.add(
            Execution(
                run_id=run.id,
                phase=phase,
                backend=stack.executor,
                ref=ref,
                secret_name=secret_name,
                status="running",
            )
        )
        session.commit()


def _sweep_unstarted(session: Session) -> None:
    cutoff = utcnow() - timedelta(seconds=60)
    running = [RunState.check_running.value, RunState.apply_running.value]
    runs = session.scalars(select(Run).where(Run.state.in_(running), Run.updated_at < cutoff))
    for run in list(runs):
        active = session.scalar(
            select(Execution).where(Execution.run_id == run.id, Execution.status == "running")
        )
        if active is None:
            fail_run(session, run, "The runner never started.")


def _poll(session: Session) -> None:
    executions = session.scalars(
        select(Execution)
        .where(Execution.status == "running")
        .options(selectinload(Execution.run).selectinload(Run.stack))
    )
    now = utcnow()
    for execution in list(executions):
        run = execution.run
        try:
            status = for_stack(run.stack.executor).poll(execution.ref, execution.secret_name)
        except Exception:
            logger.exception("poll failed for %s", execution.ref)
            continue
        if status == "running":
            continue
        if run.state in {state.value for state in LIVE_STATES if "running" in state.value}:
            if execution.finished_at is None:
                execution.finished_at = now
                session.commit()
                continue
            if aware(execution.finished_at) > now - GRACE:
                continue
            fail_run(session, run, "The runner exited without posting a result.")
        execution.status = "finished"
        execution.finished_at = execution.finished_at or now
        session.commit()
        try:
            for_stack(run.stack.executor).cleanup(execution.ref, execution.secret_name)
        except Exception:
            logger.exception("cleanup failed for %s", execution.ref)
