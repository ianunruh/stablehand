from stablehand.models import RunState, Stack, StackApprover
from stablehand.plans.normalize import fingerprint, normalize
from stablehand.runs.service import (
    RunError,
    approve_run,
    create_run,
    ingest_apply_precheck,
    ingest_check_result,
    issue_run_token,
    lookup_run_token,
)
from tests.conftest import add_user
from tests.test_normalize import FIXTURE


def _stack(db) -> Stack:
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
    return stack


def test_run_snapshots_inherited_overridden_and_cleared_limits(db):
    stack = _stack(db)
    stack.default_limit = "web-*, canary"
    db.commit()

    inherited = create_run(db, stack, commit_sha="one", trigger="schedule", user=None)
    assert inherited.limit == "web-*, canary"

    inherited.state = RunState.unchanged.value
    stack.default_limit = "new-default"
    db.commit()
    assert inherited.limit == "web-*, canary"

    overridden = create_run(
        db,
        stack,
        commit_sha="two",
        trigger="manual",
        user=None,
        limit=" db-* , api ",
    )
    assert overridden.limit == "db-*, api"

    overridden.state = RunState.unchanged.value
    db.commit()
    cleared = create_run(db, stack, commit_sha="three", trigger="manual", user=None, limit="")
    assert cleared.limit is None


def test_check_with_changes_waits_for_approval(db):
    stack = _stack(db)
    run = create_run(db, stack, commit_sha="local", trigger="manual", user=None)
    run.state = RunState.check_running.value
    db.commit()
    ingest_check_result(
        db, run, raw=FIXTURE, inventory_hosts=["web-1", "web-2"], stderr="", exit_code=0
    )
    assert run.state == RunState.needs_approval.value
    assert run.check_fingerprint


def test_empty_change_set_finishes_unchanged(db):
    stack = _stack(db)
    run = create_run(db, stack, commit_sha="local", trigger="schedule", user=None)
    run.state = RunState.check_running.value
    db.commit()
    ingest_check_result(
        db,
        run,
        raw={"plan": [], "results": None},
        inventory_hosts=["web-1"],
        stderr="",
        exit_code=0,
    )
    assert run.state == RunState.unchanged.value


def test_empty_inventory_fails_check(db):
    stack = _stack(db)
    run = create_run(db, stack, commit_sha="local", trigger="schedule", user=None)
    run.state = RunState.check_running.value
    db.commit()
    ingest_check_result(
        db,
        run,
        raw={"plan": [], "results": None},
        inventory_hosts=[],
        stderr="",
        exit_code=0,
    )
    assert run.state == RunState.check_failed.value
    assert run.error == "The inventory did not contain any hosts."


def test_unreachable_host_blocks_approval(db):
    stack = _stack(db)
    user = add_user(db, "ops@example.com")
    run = create_run(db, stack, commit_sha="local", trigger="ci", user=None)
    run.state = RunState.check_running.value
    db.commit()
    ingest_check_result(
        db,
        run,
        raw=FIXTURE,
        inventory_hosts=["web-1"],
        stderr="[db-1] Error: could not connect to target\n",
        exit_code=1,
    )
    assert run.state == RunState.needs_approval.value
    assert run.approval_blocked is True
    try:
        approve_run(db, run, user, stack)
    except RunError as exc:
        assert "Unreachable" in exc.message
    else:
        raise AssertionError("approval should be blocked")


def test_triggering_user_can_approve(db):
    stack = _stack(db)
    user = add_user(db, "ada@example.com")
    run = create_run(db, stack, commit_sha="abc", trigger="manual", user=user)
    run.state = RunState.needs_approval.value
    run.check_fingerprint = "fp"
    db.commit()
    approve_run(db, run, user, stack)
    assert run.state == RunState.apply_queued.value
    assert run.approved_by_id == user.id


def test_allow_list_limits_approvers(db):
    stack = _stack(db)
    allowed = add_user(db, "allowed@example.com")
    outsider = add_user(db, "outsider@example.com")
    stack.approvers.append(StackApprover(user_id=allowed.id))
    db.commit()
    run = create_run(db, stack, commit_sha="abc", trigger="ci", user=None)
    run.state = RunState.needs_approval.value
    run.check_fingerprint = "fp"
    db.commit()
    try:
        approve_run(db, run, outsider, stack)
    except RunError as exc:
        assert "not allowed" in exc.message
    else:
        raise AssertionError("outsider should not approve")
    approve_run(db, run, allowed, stack)
    assert run.state == RunState.apply_queued.value
    assert run.approved_fingerprint == "fp"


def test_fingerprint_mismatch_returns_to_approval(db):
    stack = _stack(db)
    run = create_run(db, stack, commit_sha="abc", trigger="ci", user=None)
    approved_stderr = """--- web-1:/etc/motd
+++ web-1:/etc/motd
@@ -1 +1 @@
-old
+approved
"""
    approved_document = normalize(FIXTURE, ["web-1", "web-2"], approved_stderr)
    run.state = RunState.apply_running.value
    run.approved_fingerprint = fingerprint(approved_document)
    run.check_document = approved_document
    db.commit()
    proceed = ingest_apply_precheck(
        db,
        run,
        raw=FIXTURE,
        inventory_hosts=["web-1", "web-2"],
        stderr=approved_stderr.replace("+approved", "+changed"),
        exit_code=0,
    )
    assert proceed is False
    assert run.state == RunState.needs_approval.value
    assert run.approved_fingerprint is None
    assert run.previous_check_document == approved_document
    assert "moved" in (run.blocked_reason or "")


def test_only_one_active_run(db):
    stack = _stack(db)
    create_run(db, stack, commit_sha="abc", trigger="manual", user=None)
    try:
        create_run(db, stack, commit_sha="def", trigger="manual", user=None)
    except RunError as exc:
        assert "active run" in exc.message
    else:
        raise AssertionError("second run should be rejected")


def test_run_token_is_stored_hashed_and_phase_scoped(db):
    stack = _stack(db)
    run = create_run(db, stack, commit_sha="abc", trigger="ci", user=None)
    plaintext = issue_run_token(db, run, "check")
    assert plaintext.startswith("shr_")
    assert plaintext not in {token.token_hash for token in run.tokens}
    assert lookup_run_token(db, plaintext, "check") is not None
    assert lookup_run_token(db, plaintext, "apply") is None
