from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, selectinload

from stablehand.config import get_settings
from stablehand.db import get_sessionmaker
from stablehand.executors import for_stack
from stablehand.integrations.service import WebhookError, send_delivery
from stablehand.models import (
    LIVE_STATES,
    DeliveryStatus,
    Execution,
    ExecutionStatus,
    IntegrationDelivery,
    Run,
    RunState,
    Stack,
    new_id,
)
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
PENDING_GRACE = timedelta(seconds=10)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session_factory = get_sessionmaker()
    while True:
        session = session_factory()
        try:
            acquired = _acquire(session)
            session.commit()
            if acquired:
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
    _recover_pending(session)
    _sweep_unstarted(session)
    _poll(session)
    _deliver_webhooks(session)


def _acquire(session: Session) -> bool:
    url = get_settings().database_url
    if url.startswith("sqlite"):
        return True
    return bool(
        session.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}).scalar()
    )


def _schedule_due(session: Session) -> None:
    now = utcnow()
    stacks = list(
        session.scalars(
            select(Stack).where(
                Stack.schedule_cron.is_not(None), Stack.schedule_next_at.is_not(None)
            )
        )
    )
    session.commit()
    for stack in stacks:
        if stack.schedule_next_at is None or aware(stack.schedule_next_at) > now:
            continue
        next_at = next_schedule(stack.schedule_cron or "", now)
        try:
            sha = resolve_commit(stack)
        except SourceError as exc:
            logger.warning("scheduled run for %s failed: %s", stack.name, exc.message)
            _record_schedule_failure(session, stack.id, stack.name, next_at, exc.message)
            continue
        try:
            persistent = session.get(Stack, stack.id)
            if persistent is None:
                session.rollback()
                continue
            persistent.schedule_next_at = next_at
            create_run(session, persistent, commit_sha=sha, trigger="schedule", user=None)
            session.commit()
        except RunError:
            session.rollback()
            persistent = session.get(Stack, stack.id)
            if persistent is not None:
                persistent.schedule_next_at = next_at
                session.commit()
            logger.info("skipping schedule for %s because a run is already active", stack.name)


def _record_schedule_failure(
    session: Session, stack_id, stack_name: str, next_at, message: str
) -> None:
    try:
        stack = session.get(Stack, stack_id)
        if stack is None:
            session.rollback()
            return
        stack.schedule_next_at = next_at
        run = create_run(session, stack, commit_sha="unresolved", trigger="schedule", user=None)
        fail_immediate(session, run, message)
        session.commit()
    except RunError:
        session.rollback()
        stack = session.get(Stack, stack_id)
        if stack is not None:
            stack.schedule_next_at = next_at
            session.commit()
        logger.info("skipping schedule failure for %s because a run is already active", stack_name)


def fail_immediate(session: Session, run: Run, message: str) -> None:
    if claim(session, run, RunState.check_queued, RunState.check_running):
        session.refresh(run)
        fail_run(session, run, message)


def _dispatch(session: Session, queued: RunState, running: RunState, phase: str) -> None:
    runs = list(
        session.scalars(
            select(Run)
            .where(Run.state == queued.value)
            .options(selectinload(Run.stack).selectinload(Stack.approvers))
        )
    )
    session.commit()
    for run in runs:
        try:
            if not claim(session, run, queued, running):
                session.rollback()
                continue
            session.refresh(run)
            stack = run.stack
            execution = Execution(
                id=new_id(),
                run_id=run.id,
                phase=phase,
                backend=stack.executor,
                status=ExecutionStatus.pending.value,
            )
            session.add(execution)
            token = issue_run_token(session, run, phase)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("failed to persist %s dispatch for %s", phase, run.id)
            continue
        _start_execution(session, execution.id, token)


def _start_execution(session: Session, execution_id, token: str) -> None:
    execution = session.scalar(
        select(Execution)
        .where(Execution.id == execution_id)
        .options(selectinload(Execution.run).selectinload(Run.stack))
    )
    if execution is None or execution.status != ExecutionStatus.pending.value:
        session.rollback()
        return
    run = execution.run
    stack = run.stack
    session.commit()
    try:
        ref, secret_name = for_stack(stack.executor).start(
            run,
            stack,
            execution.phase,
            token,
            execution_id=execution.id,
        )
    except Exception as exc:
        logger.exception("failed to start %s for %s", execution.phase, run.id)
        current = session.get(Execution, execution.id)
        current_run = session.get(Run, run.id)
        if current is not None:
            current.status = ExecutionStatus.failed.value
            current.finished_at = utcnow()
        if current_run is not None:
            fail_run(session, current_run, str(exc))
        session.commit()
        return
    current = session.get(Execution, execution.id)
    if current is None or current.status != ExecutionStatus.pending.value:
        session.rollback()
        return
    current.ref = ref
    current.secret_name = secret_name
    current.status = ExecutionStatus.running.value
    session.commit()


def _recover_pending(session: Session) -> None:
    cutoff = utcnow() - PENDING_GRACE
    execution_ids = list(
        session.scalars(
            select(Execution.id).where(
                Execution.status == ExecutionStatus.pending.value,
                Execution.started_at < cutoff,
            )
        )
    )
    session.commit()
    for execution_id in execution_ids:
        execution = session.scalar(
            select(Execution)
            .where(Execution.id == execution_id)
            .options(selectinload(Execution.run).selectinload(Run.stack))
        )
        if execution is None or execution.status != ExecutionStatus.pending.value:
            session.rollback()
            continue
        run = execution.run
        expected_state = (
            RunState.check_running.value
            if execution.phase == CHECK
            else RunState.apply_running.value
        )
        if run.state != expected_state:
            execution.status = ExecutionStatus.failed.value
            execution.finished_at = utcnow()
            session.commit()
            continue
        executor = for_stack(execution.backend)
        session.commit()
        try:
            recovered = executor.recover(execution, run, run.stack)
        except Exception:
            logger.exception("failed to recover execution %s", execution.id)
            session.rollback()
            continue
        current = session.get(Execution, execution.id)
        current_run = session.get(Run, run.id)
        if current is None or current.status != ExecutionStatus.pending.value:
            session.rollback()
            continue
        if recovered is not None:
            current.ref, current.secret_name = recovered
            current.status = ExecutionStatus.running.value
        else:
            current.status = ExecutionStatus.failed.value
            current.finished_at = utcnow()
            if current_run is not None:
                fail_run(session, current_run, "The runner never started.")
        session.commit()


def _sweep_unstarted(session: Session) -> None:
    cutoff = utcnow() - timedelta(seconds=60)
    running = [RunState.check_running.value, RunState.apply_running.value]
    runs = list(session.scalars(select(Run).where(Run.state.in_(running), Run.updated_at < cutoff)))
    session.commit()
    for run in runs:
        active = session.scalar(
            select(Execution).where(
                Execution.run_id == run.id,
                Execution.status.in_(
                    [ExecutionStatus.pending.value, ExecutionStatus.running.value]
                ),
            )
        )
        if active is None:
            fail_run(session, run, "The runner never started.")
        session.commit()


def _poll(session: Session) -> None:
    executions = list(
        session.scalars(
            select(Execution)
            .where(Execution.status == ExecutionStatus.running.value)
            .options(selectinload(Execution.run).selectinload(Run.stack))
        )
    )
    session.commit()
    now = utcnow()
    for execution in executions:
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
        execution.status = ExecutionStatus.finished.value
        execution.finished_at = execution.finished_at or now
        session.commit()
        try:
            for_stack(run.stack.executor).cleanup(execution.ref, execution.secret_name)
        except Exception:
            logger.exception("cleanup failed for %s", execution.ref)


def _deliver_webhooks(session: Session) -> None:
    now = utcnow()
    delivery_ids = list(
        session.scalars(
            select(IntegrationDelivery.id)
            .where(
                IntegrationDelivery.status == DeliveryStatus.pending.value,
                IntegrationDelivery.next_attempt_at <= now,
            )
            .order_by(IntegrationDelivery.created_at)
            .limit(20)
        )
    )
    session.commit()
    for delivery_id in delivery_ids:
        delivery = session.scalar(
            select(IntegrationDelivery)
            .where(IntegrationDelivery.id == delivery_id)
            .options(selectinload(IntegrationDelivery.integration))
        )
        if delivery is None or delivery.status != DeliveryStatus.pending.value:
            session.rollback()
            continue
        integration = delivery.integration
        attempt = delivery.attempt_count + 1
        if not integration.enabled:
            error = "Webhook integration is disabled."
            session.commit()
        else:
            session.commit()
            try:
                send_delivery(delivery, integration)
            except WebhookError as exc:
                error = str(exc)[-2000:]
                logger.warning(
                    "webhook delivery %s attempt %s failed: %s",
                    delivery.id,
                    attempt,
                    error,
                )
            else:
                result = session.execute(
                    update(IntegrationDelivery)
                    .where(
                        IntegrationDelivery.id == delivery_id,
                        IntegrationDelivery.status == DeliveryStatus.pending.value,
                    )
                    .values(
                        status=DeliveryStatus.delivered.value,
                        attempt_count=attempt,
                        last_error=None,
                        updated_at=utcnow(),
                    )
                )
                session.commit()
                if result.rowcount == 0:
                    logger.info("webhook delivery %s was already handled", delivery_id)
                continue
        terminal = attempt >= 10 or not integration.enabled
        delay = min(600, 2**attempt)
        session.execute(
            update(IntegrationDelivery)
            .where(
                IntegrationDelivery.id == delivery_id,
                IntegrationDelivery.status == DeliveryStatus.pending.value,
            )
            .values(
                status=(DeliveryStatus.failed.value if terminal else DeliveryStatus.pending.value),
                attempt_count=attempt,
                next_attempt_at=utcnow() + timedelta(seconds=delay),
                last_error=error,
                updated_at=utcnow(),
            )
        )
        session.commit()
