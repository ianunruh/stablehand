from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from stablehand.models import Run, RunLog, RunState
from stablehand.runs.service import create_run
from stablehand.ui.timeline import timeline
from tests.conftest import add_user, login
from tests.test_runs import _stack


def test_succeeded_run_timeline_is_newest_first(db):
    user = add_user(db, "ada@example.com")
    stack = _stack(db)
    run = create_run(db, stack, commit_sha="abc123def456", trigger="manual", user=user)
    run.state = RunState.succeeded.value
    run.approved_by_id = user.id
    run.approved_at = datetime(2026, 9, 24, 0, 45, 27, tzinfo=UTC)
    run.check_document = {"hosts": [], "counts": {}}
    run.apply_document = {"hosts": [], "counts": {}}
    db.add_all(
        [
            RunLog(
                run_id=run.id,
                phase="check",
                body="check output",
                created_at=datetime(2026, 9, 24, 0, 45, 16, tzinfo=UTC),
            ),
            RunLog(
                run_id=run.id,
                phase="apply",
                body="apply output",
                created_at=datetime(2026, 9, 24, 0, 45, 30, tzinfo=UTC),
            ),
        ]
    )
    db.commit()
    loaded = db.scalar(
        select(Run)
        .where(Run.id == run.id)
        .options(
            selectinload(Run.logs),
            selectinload(Run.executions),
            selectinload(Run.trigger_user),
            selectinload(Run.approved_by),
            selectinload(Run.rejected_by),
        )
    )

    steps = timeline(loaded)
    assert [step.title for step in steps] == ["Applied", "Approved", "Check", "Opened"]
    assert steps[0].actor == "Runner"
    assert steps[0].log_open is True
    assert steps[1].actor == "ada@example.com"
    assert steps[1].detail == "Approved apply of abc123def456"
    assert steps[2].log_open is False
    assert steps[3].actor == "ada@example.com"
    assert steps[3].detail == "Manual check of commit abc123def456"


def test_run_page_keeps_timestamps_on_one_line(client, db):
    user = add_user(db, "ada@example.com")
    stack = _stack(db)
    token = login(client, user.email)
    del token
    run = create_run(db, stack, commit_sha="abc123def456", trigger="manual", user=user)
    run.state = RunState.needs_approval.value
    run.check_document = {
        "hosts": [
            {
                "name": "web-1",
                "status": "change",
                "operations": [
                    {
                        "name": "Install nginx",
                        "will_change": True,
                        "conditional": False,
                        "result": None,
                    }
                ],
                "diffs": [],
            }
        ],
        "counts": {"hosts": 1, "change": 1, "unchanged": 0, "unreachable": 0, "failed": 0},
        "blocked": False,
    }
    db.add(RunLog(run_id=run.id, phase="check", body="line one\nline two"))
    db.commit()

    response = client.get(f"/runs/{run.id}")
    assert response.status_code == 200
    page = response.text
    assert page.index(">Check<") < page.index(">Opened<")
    assert 'class="sh-time"' in page
    assert "sh-timeline-head" in page
    assert 'data-state="needs_approval"' in page
    assert "sh-badge-warning" in page
    assert "Decision required" in page
    assert "Option A" not in page
    assert "Option B" not in page
    assert "sh-decision-or" in page
    assert "Approve apply of abc123def456" in page
    assert "Reject run" in page
    assert "Manual by ada@example.com" in page
    assert "Install nginx" in page
    assert 'data-log-open="log-overlay-' in page
    assert 'class="sh-log-dialog"' in page
    assert "sh-log-fullscreen" in page
    assert page.count("line one\nline two") == 2
