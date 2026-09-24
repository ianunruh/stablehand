from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from stablehand.integrations.service import notify_needs_approval
from stablehand.models import ACTIVE_STATES, Run, RunLog, RunState, RunToken, Stack, Trigger, User
from stablehand.plans.normalize import fingerprint, normalize
from stablehand.security import after, aware, hash_token, new_token, utcnow

logger = logging.getLogger(__name__)

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
    active = session.scalar(
        select(Run).where(
            Run.stack_id == stack.id, Run.state.in_([state.value for state in ACTIVE_STATES])
        )
    )
    if active is not None:
        raise RunError("This stack already has an active run.")
    run = Run(
        stack_id=stack.id,
        commit_sha=commit_sha,
        trigger=trigger.value if isinstance(trigger, Trigger) else trigger,
        trigger_user_id=user.id if user else None,
        state=RunState.check_queued.value,
        updated_at=utcnow(),
    )
    session.add(run)
    session.commit()
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
    run.state = RunState.apply_queued.value
    run.approved_by_id = user.id
    run.approved_at = utcnow()
    run.approved_fingerprint = run.check_fingerprint
    run.updated_at = utcnow()
    session.commit()


def reject_run(session: Session, run: Run, user: User, stack: Stack, reason: str) -> None:
    if run.state != RunState.needs_approval.value:
        raise RunError("This run is not waiting for approval.")
    if not _user_on_allow_list(stack, user):
        raise RunError("You are not allowed to reject this stack.")
    run.state = RunState.rejected.value
    run.rejected_by_id = user.id
    run.rejected_at = utcnow()
    run.reject_reason = reason.strip() or None
    run.updated_at = utcnow()
    session.commit()


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
    session.commit()
    return plaintext


def append_log(session: Session, run: Run, phase: str, body: str) -> None:
    if not body:
        return
    session.add(RunLog(run_id=run.id, phase=phase, body=body))
    session.commit()


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
    if stderr:
        append_log(session, run, CHECK, stderr)
    if not raw:
        run.state = RunState.check_failed.value
        run.error = stderr.strip()[-2000:] or f"pyinfra exited {exit_code} without a plan."
        run.updated_at = utcnow()
        _revoke(session, run, CHECK)
        session.commit()
        return
    document = normalize(raw, inventory_hosts, stderr)
    run.check_raw = raw
    run.check_document = document
    run.check_fingerprint = fingerprint(document)
    run.approval_blocked = bool(document["blocked"])
    run.blocked_reason = _blocked_reason(document)
    if exit_code != 0 and not document["hosts"]:
        run.state = RunState.check_failed.value
        run.error = stderr.strip()[-2000:] or f"pyinfra exited {exit_code}."
    elif document["counts"]["change"] == 0 and not document["blocked"]:
        run.state = RunState.unchanged.value
    elif document["counts"]["hosts"] == 0:
        run.state = RunState.check_failed.value
        run.error = "The inventory did not contain any hosts."
    else:
        run.state = RunState.needs_approval.value
    run.updated_at = utcnow()
    _revoke(session, run, CHECK)
    session.commit()
    if run.state == RunState.needs_approval.value:
        _notify(session, run)


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
    if stderr:
        append_log(session, run, APPLY, stderr)
    if not raw or exit_code != 0:
        run.state = RunState.apply_failed.value
        run.error = stderr.strip()[-2000:] or "The apply-time check failed."
        run.updated_at = utcnow()
        _revoke(session, run, APPLY)
        session.commit()
        return False
    document = normalize(raw, inventory_hosts, stderr)
    current = fingerprint(document)
    if current != run.approved_fingerprint:
        run.previous_check_document = run.check_document
        run.check_document = document
        run.check_raw = raw
        run.check_fingerprint = current
        run.approved_fingerprint = None
        run.approved_by_id = None
        run.approved_at = None
        run.approval_blocked = bool(document["blocked"])
        run.blocked_reason = _blocked_reason(document) or (
            "The change set moved after approval. Review the new check."
        )
        if not run.approval_blocked:
            run.blocked_reason = "The change set moved after approval. Review the new check."
        run.state = RunState.needs_approval.value
        run.updated_at = utcnow()
        _revoke(session, run, APPLY)
        session.commit()
        _notify(session, run)
        return False
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
    if stderr:
        append_log(session, run, APPLY, stderr)
    if not raw:
        run.state = RunState.apply_failed.value
        run.error = stderr.strip()[-2000:] or f"pyinfra exited {exit_code} without results."
        run.updated_at = utcnow()
        _revoke(session, run, APPLY)
        session.commit()
        return
    document = normalize(raw, inventory_hosts, stderr)
    run.apply_raw = raw
    run.apply_document = document
    if exit_code != 0 or document["counts"]["failed"]:
        run.state = RunState.apply_failed.value
        run.error = "One or more hosts failed."
    else:
        run.state = RunState.succeeded.value
        run.error = None
    run.updated_at = utcnow()
    _revoke(session, run, APPLY)
    session.commit()


def fail_run(session: Session, run: Run, message: str) -> None:
    if run.state == RunState.check_running.value:
        run.state = RunState.check_failed.value
    elif run.state == RunState.apply_running.value:
        run.state = RunState.apply_failed.value
    else:
        return
    run.error = message
    run.updated_at = utcnow()
    session.commit()


def claim(session: Session, run: Run, from_state: RunState, to_state: RunState) -> bool:
    result = session.execute(
        update(Run)
        .where(Run.id == run.id, Run.state == from_state.value)
        .values(state=to_state.value, updated_at=utcnow())
    )
    session.commit()
    return result.rowcount == 1


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


def _notify(session: Session, run: Run) -> None:
    try:
        notify_needs_approval(session, run)
    except Exception:
        logger.exception("integration notification failed")


def lookup_run_token(session: Session, plaintext: str, phase: str | None = None) -> Run | None:
    token = session.scalar(select(RunToken).where(RunToken.token_hash == hash_token(plaintext)))
    if token is None or token.revoked_at is not None or aware(token.expires_at) <= utcnow():
        return None
    if phase is not None and token.phase != phase:
        return None
    return session.get(Run, token.run_id)
