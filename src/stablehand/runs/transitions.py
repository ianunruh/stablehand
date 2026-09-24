from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from stablehand.models import Run, RunState
from stablehand.security import utcnow

ALLOWED_TRANSITIONS = {
    (RunState.check_queued, RunState.check_running),
    (RunState.check_running, RunState.needs_approval),
    (RunState.check_running, RunState.unchanged),
    (RunState.check_running, RunState.check_failed),
    (RunState.needs_approval, RunState.apply_queued),
    (RunState.needs_approval, RunState.rejected),
    (RunState.apply_queued, RunState.apply_running),
    (RunState.apply_running, RunState.needs_approval),
    (RunState.apply_running, RunState.succeeded),
    (RunState.apply_running, RunState.apply_failed),
}


def transition_run(
    session: Session,
    run_id: uuid.UUID,
    from_state: RunState,
    to_state: RunState,
    values: dict[str, Any] | None = None,
) -> bool:
    if (from_state, to_state) not in ALLOWED_TRANSITIONS:
        raise ValueError(f"Run transition is not allowed: {from_state} -> {to_state}")
    result = session.execute(
        update(Run)
        .where(Run.id == run_id, Run.state == from_state.value)
        .values(state=to_state.value, updated_at=utcnow(), **(values or {}))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
