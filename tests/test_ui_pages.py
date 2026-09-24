from datetime import UTC, datetime, timedelta

from stablehand.models import ApiToken, Integration, Role, RunState
from stablehand.runs.service import create_run
from tests.conftest import add_user, login
from tests.test_runs import _stack


def test_operational_pages_render_shared_components_and_metadata(client, db):
    user = add_user(db, "admin@example.com", role=Role.admin.value)
    stack = _stack(db)
    stack.default_limit = "web-*, canary"
    run = create_run(db, stack, commit_sha="abc123def456", trigger="manual", user=user)
    run.state = RunState.needs_approval.value
    run.check_document = {
        "hosts": [],
        "counts": {"hosts": 1, "change": 1, "unchanged": 0, "unreachable": 0, "failed": 0},
    }
    token = ApiToken(
        stack_id=stack.id,
        name="Build pipeline",
        token_hash="token-hash",
        token_prefix="shc_demo",
        expires_at=datetime.now(UTC) + timedelta(days=30),
        created_by_id=user.id,
    )
    integration = Integration(
        kind="webhook",
        name="Operations",
        enabled=True,
        config={"url": "https://example.test/hook"},
    )
    db.add_all([run, token, integration])
    db.commit()
    login(client, user.email)

    inbox = client.get("/")
    assert inbox.status_code == 200
    assert '<header class="sh-page-header">' in inbox.text
    assert "Checks that are ready for a human decision." in inbox.text
    assert "Manual by admin@example.com" in inbox.text
    assert 'data-state="needs_approval"' in inbox.text
    assert 'class="sh-time"' in inbox.text

    stacks = client.get("/stacks")
    assert stacks.status_code == 200
    assert "Deployment definitions, schedules, and recent check activity." in stacks.text
    assert "abc123def456" in stacks.text
    assert 'datetime="' in stacks.text

    detail = client.get(f"/stacks/{stack.id}")
    assert detail.status_code == 200
    assert "Recent runs" in detail.text
    assert "1 change" in detail.text
    assert "Manual by admin@example.com" in detail.text
    assert 'name="limit" value="web-*, canary"' in detail.text
    assert 'class="sh-check-popover"' in detail.text
    assert 'popover role="dialog"' in detail.text
    page_header = detail.text.split('<header class="sh-page-header">', 1)[1].split("</header>", 1)[
        0
    ]
    assert 'name="limit"' not in page_header

    run_page = client.get(f"/runs/{run.id}")
    assert run_page.status_code == 200
    assert 'Limit <span class="sh-mono">web-*, canary</span>' in run_page.text

    tokens = client.get(f"/stacks/{stack.id}/tokens")
    assert tokens.status_code == 200
    assert "Build pipeline" in tokens.text
    assert "Expires" in tokens.text
    assert tokens.text.count('class="sh-time"') >= 2

    users = client.get("/settings/users")
    assert users.status_code == 200
    assert "Joined" in users.text
    assert "admin@example.com" in users.text

    integrations = client.get("/settings/integrations")
    assert integrations.status_code == 200
    assert "Operations" in integrations.text
    assert "Added" in integrations.text
    assert "sh-badge-success" in integrations.text


def test_base_shell_exposes_active_navigation_and_theme_control(client, db):
    user = add_user(db, "member@example.com")
    login(client, user.email)

    response = client.get("/stacks")

    assert response.status_code == 200
    assert 'aria-current="page">Stacks</a>' in response.text
    assert "data-theme-toggle" in response.text
    assert 'localStorage.getItem("stablehand-theme")' in response.text
