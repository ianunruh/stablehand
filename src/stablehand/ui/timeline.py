from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from stablehand.models import Run, RunState
from stablehand.runs.service import APPLY, CHECK, log_text
from stablehand.security import aware

RUNNER = "Runner"

_CHECK_ACTIVE = {RunState.check_queued.value, RunState.check_running.value}
_APPLY_ACTIVE = {RunState.apply_queued.value, RunState.apply_running.value}
_APPLY_STATES = _APPLY_ACTIVE | {RunState.succeeded.value, RunState.apply_failed.value}


@dataclass(frozen=True)
class TimelineStep:
    title: str
    actor: str
    at: datetime
    detail: str = ""
    document: dict | None = None
    log_phase: str | None = None
    log_label: str = ""
    log_open: bool = False
    error: str | None = None
    approval: bool = False
    previous_document: dict | None = None


def timeline(run: Run) -> list[TimelineStep]:
    steps = [_apply_step(run)] if _has_apply(run) else []
    if run.rejected_at is not None:
        steps.append(_rejected_step(run))
    elif run.approved_at is not None:
        steps.append(_approved_step(run))
    steps.append(_check_step(run))
    steps.append(_opened_step(run))
    return _open_newest_log(run, steps)


def format_timestamp(value: datetime) -> str:
    moment = aware(value).astimezone(UTC)
    hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"
    return f"{moment.strftime('%b')} {moment.day}, {hour}:{moment.minute:02d}:{moment.second:02d} {suffix} UTC"


def isoformat_utc(value: datetime) -> str:
    return aware(value).isoformat()


def _open_newest_log(run: Run, steps: list[TimelineStep]) -> list[TimelineStep]:
    opened = False
    result = []
    for step in steps:
        has_log = bool(step.log_phase and log_text(run, step.log_phase).strip())
        result.append(replace(step, log_open=has_log and not opened))
        opened = opened or has_log
    return result


def _has_apply(run: Run) -> bool:
    if run.state in _APPLY_STATES or run.apply_document:
        return True
    return any(entry.phase == APPLY for entry in run.logs)


def _apply_step(run: Run) -> TimelineStep:
    titles = {
        RunState.apply_queued.value: "Apply queued",
        RunState.apply_running.value: "Applying",
        RunState.succeeded.value: "Applied",
        RunState.apply_failed.value: "Apply failed",
    }
    return TimelineStep(
        title=titles.get(run.state, "Apply"),
        actor=RUNNER,
        at=_phase_time(run, APPLY, active=run.state in _APPLY_ACTIVE),
        document=run.apply_document,
        log_phase=APPLY,
        log_label="Apply log",
        error=run.error if run.state == RunState.apply_failed.value else None,
    )


def _approved_step(run: Run) -> TimelineStep:
    actor = run.approved_by.email if run.approved_by is not None else RUNNER
    return TimelineStep(
        title="Approved",
        actor=actor,
        at=run.approved_at,
        detail=f"Approved apply of {run.commit_sha[:12]}",
    )


def _rejected_step(run: Run) -> TimelineStep:
    actor = run.rejected_by.email if run.rejected_by is not None else RUNNER
    return TimelineStep(
        title="Rejected",
        actor=actor,
        at=run.rejected_at,
        detail=run.reject_reason or "Rejected the check.",
    )


def _check_step(run: Run) -> TimelineStep:
    titles = {
        RunState.check_queued.value: "Check queued",
        RunState.check_running.value: "Checking",
        RunState.check_failed.value: "Check failed",
    }
    return TimelineStep(
        title=titles.get(run.state, "Check"),
        actor=RUNNER,
        at=_phase_time(run, CHECK, active=run.state in _CHECK_ACTIVE),
        document=run.check_document,
        log_phase=CHECK,
        log_label="Check log",
        error=run.error if run.state == RunState.check_failed.value else None,
        approval=run.state == RunState.needs_approval.value,
        previous_document=run.previous_check_document,
    )


def _opened_step(run: Run) -> TimelineStep:
    labels = {"manual": "Manual", "ci": "CI", "schedule": "Scheduled"}
    label = labels.get(run.trigger, run.trigger.capitalize())
    return TimelineStep(
        title="Opened",
        actor=_open_actor(run),
        at=run.created_at,
        detail=f"{label} check of commit {run.commit_sha}",
    )


def _open_actor(run: Run) -> str:
    if run.trigger == "ci":
        return "CI"
    if run.trigger == "schedule":
        return "Schedule"
    if run.trigger_user is not None:
        return run.trigger_user.email
    return "Manual"


def _phase_time(run: Run, phase: str, *, active: bool) -> datetime:
    logs = [entry for entry in run.logs if entry.phase == phase]
    executions = [row for row in run.executions if row.phase == phase]
    if active:
        if executions:
            return max(executions, key=lambda row: aware(row.started_at)).started_at
        if logs:
            return min(logs, key=lambda entry: aware(entry.created_at)).created_at
        return run.updated_at
    if logs:
        return max(logs, key=lambda entry: aware(entry.created_at)).created_at
    if executions:
        latest = max(executions, key=lambda row: aware(row.started_at))
        return latest.finished_at or latest.started_at
    return run.updated_at
