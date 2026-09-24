import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from stablehand.config import get_settings
from stablehand.db import get_sessionmaker
from stablehand.models import ACTIVE_STATE_VALUES, Run, RunState, Stack
from stablehand.runs.service import (
    RunError,
    approve_run,
    claim,
    create_run,
    ingest_check_result,
    reject_run,
)
from tests.conftest import add_user
from tests.test_normalize import FIXTURE

pytestmark = pytest.mark.postgres


def _require_postgres():
    if not get_settings().database_url.startswith("postgresql"):
        pytest.skip("requires PostgreSQL")


def _parallel(first, second):
    barrier = threading.Barrier(2)

    def run(function):
        return function(barrier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        return [future.result() for future in [pool.submit(run, first), pool.submit(run, second)]]


def test_concurrent_run_creation_allows_one_active_run(database):
    _require_postgres()
    session_factory = get_sessionmaker()
    setup = session_factory()
    stack = Stack(
        name="demo",
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor="local",
        local_path="/tmp/demo",
    )
    setup.add(stack)
    setup.commit()
    stack_id = stack.id
    setup.close()

    def attempt(commit):
        def create(barrier):
            session = session_factory()
            try:
                current_stack = session.get(Stack, stack_id)
                barrier.wait()
                create_run(
                    session,
                    current_stack,
                    commit_sha=commit,
                    trigger="ci",
                    user=None,
                )
                session.commit()
                return "created"
            except RunError:
                session.rollback()
                return "conflict"
            finally:
                session.close()

        return create

    assert sorted(_parallel(attempt("one"), attempt("two"))) == ["conflict", "created"]
    verification = session_factory()
    try:
        active = verification.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.stack_id == stack_id, Run.state.in_(ACTIVE_STATE_VALUES))
        )
        assert active == 1
    finally:
        verification.close()


def test_concurrent_approve_and_reject_have_one_winner(database):
    _require_postgres()
    session_factory = get_sessionmaker()
    setup = session_factory()
    stack = Stack(
        name="demo",
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor="local",
        local_path="/tmp/demo",
    )
    setup.add(stack)
    setup.commit()
    approver = add_user(setup, "approver@example.com")
    rejector = add_user(setup, "rejector@example.com")
    run = create_run(setup, stack, commit_sha="abc", trigger="ci", user=None)
    run.state = RunState.needs_approval.value
    run.check_fingerprint = "fingerprint"
    setup.commit()
    run_id = run.id
    user_ids = [approver.id, rejector.id]
    setup.close()

    def act(index, approve):
        def perform(barrier):
            session = session_factory()
            try:
                current = session.scalar(
                    select(Run)
                    .where(Run.id == run_id)
                    .options(selectinload(Run.stack).selectinload(Stack.approvers))
                )
                user = session.get(type(approver), user_ids[index])
                barrier.wait()
                if approve:
                    approve_run(session, current, user, current.stack)
                else:
                    reject_run(session, current, user, current.stack, "no")
                session.commit()
                return "applied"
            except RunError:
                session.rollback()
                return "conflict"
            finally:
                session.close()

        return perform

    assert sorted(_parallel(act(0, True), act(1, False))) == ["applied", "conflict"]


def test_duplicate_check_ingest_has_one_winner(database):
    _require_postgres()
    session_factory = get_sessionmaker()
    setup = session_factory()
    stack = Stack(
        name="demo",
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor="local",
        local_path="/tmp/demo",
    )
    setup.add(stack)
    setup.commit()
    run = create_run(setup, stack, commit_sha="abc", trigger="ci", user=None)
    assert claim(setup, run, RunState.check_queued, RunState.check_running)
    setup.commit()
    run_id = run.id
    setup.close()

    def ingest(barrier):
        session = session_factory()
        try:
            current = session.get(Run, run_id)
            barrier.wait()
            ingest_check_result(
                session,
                current,
                raw=FIXTURE,
                inventory_hosts=["web-1", "web-2"],
                stderr="",
                exit_code=0,
            )
            session.commit()
            return "applied"
        except RunError:
            session.rollback()
            return "conflict"
        finally:
            session.close()

    assert sorted(_parallel(ingest, ingest)) == ["applied", "conflict"]


def test_concurrent_worker_claim_has_one_winner(database):
    _require_postgres()
    session_factory = get_sessionmaker()
    setup = session_factory()
    stack = Stack(
        name="demo",
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor="local",
        local_path="/tmp/demo",
    )
    setup.add(stack)
    setup.commit()
    run = create_run(setup, stack, commit_sha="abc", trigger="ci", user=None)
    setup.commit()
    run_id = run.id
    setup.close()

    def attempt(barrier):
        session = session_factory()
        try:
            current = session.get(Run, run_id)
            barrier.wait()
            applied = claim(
                session,
                current,
                RunState.check_queued,
                RunState.check_running,
            )
            session.commit()
            return applied
        finally:
            session.close()

    assert sorted(_parallel(attempt, attempt)) == [False, True]
