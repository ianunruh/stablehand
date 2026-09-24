from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stablehand.integrations.service import enqueue_needs_approval
from stablehand.models import Run, RunLog, RunState, RunToken, Stack, Trigger, User
from stablehand.plans.normalize import fingerprint, normalize
from stablehand.runs.transitions import transition_run
from stablehand.security import after, aware, hash_token, new_token, utcnow

CHECK = "check"
APPLY = "apply"


class RunError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def counts_of(run: Run) -> dict:
    document = run.apply_document or run.check_document or {}
    return document.get("counts") or {
        "hosts": 0,
        "change": 0,
        "unchanged": 0,
        "unreachable": 0,
        "failed": 0,
    }


def create_run(
    session: Session,
    stack: Stack,
    *,
    commit_sha: str,
    trigger: Trigger | str,
    user: User | None,
) -> Run:
    run = Run(
        stack_id=stack.id,
        commit_sha=commit_sha,
        trigger=trigger.value if isinstance(trigger, Trigger) else trigger,
        trigger_user_id=user.id if user else None,
        state=RunState.check_queued.value,
        updated_at=utcnow(),
    )
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError as exc:
        if _active_run_conflict(exc):
            raise RunError("This stack already has an active run.") from exc
        raise
    session.refresh(run)
    return run


def approval_error(run: Run, user: User, stack: Stack) -> str | None:
    if user.role not in {"member", "admin"} or not user.enabled:
        return "You cannot approve runs."
    if not _user_on_allow_list(stack, user):
        return "You are not allowed to approve this stack."
    if run.state != RunState.needs_approval.value:
        return "This run is not waiting for approval."
    if run.approval_blocked:
        return run.blocked_reason or "Unreachable hosts block approval."
    return None


def approve_run(session: Session, run: Run, user: User, stack: Stack) -> None:
    message = approval_error(run, user, stack)
    if message:
        raise RunError(message)
    applied = transition_run(
        session,
        run.id,
        RunState.needs_approval,
        RunState.apply_queued,
        {
            "approved_by_id": user.id,
            "approved_at": utcnow(),
            "approved_fingerprint": run.check_fingerprint,
        },
    )
    if not applied:
        session.refresh(run)
        raise RunError("This run is not waiting for approval.")
    session.refresh(run)


def reject_run(session: Session, run: Run, user: User, stack: Stack, reason: str) -> None:
    if run.state != RunState.needs_approval.value:
        raise RunError("This run is not waiting for approval.")
    if not _user_on_allow_list(stack, user):
        raise RunError("You are not allowed to reject this stack.")
    applied = transition_run(
        session,
        run.id,
        RunState.needs_approval,
        RunState.rejected,
        {
            "rejected_by_id": user.id,
            "rejected_at": utcnow(),
            "reject_reason": reason.strip() or None,
        },
    )
    if not applied:
        session.refresh(run)
        raise RunError("This run is not waiting for approval.")
    session.refresh(run)


def issue_run_token(session: Session, run: Run, phase: str) -> str:
    plaintext = new_token("shr_")
    session.add(
        RunToken(
            run_id=run.id,
            phase=phase,
            token_hash=hash_token(plaintext),
            expires_at=after(hours=6),
        )
    )
    session.flush()
    return plaintext


def append_log(session: Session, run: Run, phase: str, body: str) -> None:
    if not body:
        return
    session.add(RunLog(run_id=run.id, phase=phase, body=body))


def log_text(run: Run, phase: str | None = None) -> str:
    parts = [entry.body for entry in run.logs if phase is None or entry.phase == phase]
    return "".join(parts)


def ingest_check_result(
    session: Session,
    run: Run,
    *,
    raw: dict | None,
    inventory_hosts: list[str],
    stderr: str,
    exit_code: int,
) -> None:
    if run.state != RunState.check_running.value:
        raise RunError("This run is not checking.")
    if not raw:
        next_state = RunState.check_failed
        values = {"error": stderr.strip()[-2000:] or f"pyinfra exited {exit_code} without a plan."}
    else:
        document = normalize(raw, inventory_hosts, stderr)
        values = {
            "check_raw": raw,
            "check_document": document,
            "check_fingerprint": fingerprint(document),
            "approval_blocked": bool(document["blocked"]),
            "blocked_reason": _blocked_reason(document),
            "error": None,
        }
        if not inventory_hosts:
            next_state = RunState.check_failed
            values["error"] = "The inventory did not contain any hosts."
        elif exit_code != 0 and not document["hosts"]:
            next_state = RunState.check_failed
            values["error"] = stderr.strip()[-2000:] or f"pyinfra exited {exit_code}."
        elif document["counts"]["change"] == 0 and not document["blocked"]:
            next_state = RunState.unchanged
        else:
            next_state = RunState.needs_approval
    if not transition_run(session, run.id, RunState.check_running, next_state, values):
        session.refresh(run)
        raise RunError("This run is not checking.")
    append_log(session, run, CHECK, stderr)
    _revoke(session, run, CHECK)
    session.refresh(run)
    if run.state == RunState.needs_approval.value:
        enqueue_needs_approval(session, run)


def ingest_apply_precheck(
    session: Session,
    run: Run,
    *,
    raw: dict | None,
    inventory_hosts: list[str],
    stderr: str,
    exit_code: int,
) -> bool:
    if run.state != RunState.apply_running.value:
        raise RunError("This run is not applying.")
    if not raw or exit_code != 0:
        applied = transition_run(
            session,
            run.id,
            RunState.apply_running,
            RunState.apply_failed,
            {"error": stderr.strip()[-2000:] or "The apply-time check failed."},
        )
        if not applied:
            session.refresh(run)
            raise RunError("This run is not applying.")
        append_log(session, run, APPLY, stderr)
        _revoke(session, run, APPLY)
        session.refresh(run)
        return False
    document = normalize(raw, inventory_hosts, stderr)
    current = fingerprint(document)
    if current != run.approved_fingerprint:
        blocked_reason = _blocked_reason(document) or (
            "The change set moved after approval. Review the new check."
        )
        applied = transition_run(
            session,
            run.id,
            RunState.apply_running,
            RunState.needs_approval,
            {
                "previous_check_document": run.check_document,
                "check_document": document,
                "check_raw": raw,
                "check_fingerprint": current,
                "approved_fingerprint": None,
                "approved_by_id": None,
                "approved_at": None,
                "approval_blocked": bool(document["blocked"]),
                "blocked_reason": blocked_reason,
            },
        )
        if not applied:
            session.refresh(run)
            raise RunError("This run is not applying.")
        append_log(session, run, APPLY, stderr)
        _revoke(session, run, APPLY)
        session.refresh(run)
        enqueue_needs_approval(session, run)
        return False
    current_state = session.scalar(select(Run.state).where(Run.id == run.id))
    if current_state != RunState.apply_running.value:
        session.refresh(run)
        raise RunError("This run is not applying.")
    append_log(session, run, APPLY, stderr)
    return True


def ingest_apply_result(
    session: Session,
    run: Run,
    *,
    raw: dict | None,
    inventory_hosts: list[str],
    stderr: str,
    exit_code: int,
) -> None:
    if run.state != RunState.apply_running.value:
        raise RunError("This run is not applying.")
    if not raw:
        next_state = RunState.apply_failed
        values = {"error": stderr.strip()[-2000:] or f"pyinfra exited {exit_code} without results."}
    else:
        document = normalize(raw, inventory_hosts, stderr)
        values = {"apply_raw": raw, "apply_document": document}
        if exit_code != 0 or document["counts"]["failed"]:
            next_state = RunState.apply_failed
            values["error"] = "One or more hosts failed."
        else:
            next_state = RunState.succeeded
            values["error"] = None
    if not transition_run(session, run.id, RunState.apply_running, next_state, values):
        session.refresh(run)
        raise RunError("This run is not applying.")
    append_log(session, run, APPLY, stderr)
    _revoke(session, run, APPLY)
    session.refresh(run)


def fail_run(session: Session, run: Run, message: str) -> bool:
    if run.state == RunState.check_running.value:
        from_state = RunState.check_running
        to_state = RunState.check_failed
        phase = CHECK
    elif run.state == RunState.apply_running.value:
        from_state = RunState.apply_running
        to_state = RunState.apply_failed
        phase = APPLY
    else:
        return False
    applied = transition_run(session, run.id, from_state, to_state, {"error": message})
    if applied:
        _revoke(session, run, phase)
        session.refresh(run)
    return applied


def claim(session: Session, run: Run, from_state: RunState, to_state: RunState) -> bool:
    applied = transition_run(session, run.id, from_state, to_state)
    if applied:
        session.refresh(run)
    return applied


def _user_on_allow_list(stack: Stack, user: User) -> bool:
    if not stack.approvers:
        return True
    groups = set(user.groups or [])
    for approver in stack.approvers:
        if approver.user_id is not None and approver.user_id == user.id:
            return True
        if approver.group_name and approver.group_name in groups:
            return True
    return False


def _blocked_reason(document: dict) -> str | None:
    if not document.get("blocked"):
        return None
    names = ", ".join(document.get("unreachable") or [])
    return (
        f"Unreachable hosts block approval: {names}"
        if names
        else "Unreachable hosts block approval."
    )


def _revoke(session: Session, run: Run, phase: str) -> None:
    session.execute(
        update(RunToken)
        .where(RunToken.run_id == run.id, RunToken.phase == phase, RunToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def _active_run_conflict(exc: IntegrityError) -> bool:
    diagnostic = getattr(exc.orig, "diag", None)
    if getattr(diagnostic, "constraint_name", None) == "uq_runs_one_active_per_stack":
        return True
    return "UNIQUE constraint failed: runs.stack_id" in str(exc.orig)


def lookup_run_token(session: Session, plaintext: str, phase: str | None = None) -> Run | None:
    token = session.scalar(select(RunToken).where(RunToken.token_hash == hash_token(plaintext)))
    if token is None or token.revoked_at is not None or aware(token.expires_at) <= utcnow():
        return None
    if phase is not None and token.phase != phase:
        return None
    return session.get(Run, token.run_id)
