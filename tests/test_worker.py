from unittest.mock import Mock

from sqlalchemy import select

from stablehand import worker
from stablehand.models import Execution, RunState, Stack
from stablehand.runs.service import CHECK, create_run


def test_dispatch_passes_execution_identity_to_executor(db, monkeypatch):
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
    executor = Mock()
    executor.start.return_value = ("runner-ref", None)
    monkeypatch.setattr(worker, "for_stack", lambda kind: executor)

    worker._dispatch(db, RunState.check_queued, RunState.check_running, CHECK)

    execution = db.scalar(select(Execution).where(Execution.run_id == run.id))
    assert execution is not None
    assert executor.start.call_args.kwargs["execution_id"] == execution.id
    assert execution.ref == "runner-ref"
