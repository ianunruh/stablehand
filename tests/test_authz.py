from unittest.mock import patch

from sqlalchemy import select

from stablehand.models import ApiToken, Run, RunState, Stack
from stablehand.security import hash_token
from tests.conftest import add_user, auth, login


def _stack(db, path: str) -> Stack:
    stack = Stack(
        name="demo",
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor="local",
        local_path=path,
    )
    db.add(stack)
    db.commit()
    db.refresh(stack)
    return stack


def test_login_and_csrf(client, db):
    add_user(db, "admin@example.com", role="admin")
    response = client.post("/stacks", data={"name": "x"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    token = login(client, "admin@example.com")
    denied = client.post("/stacks", data={"name": "Nope"}, follow_redirects=False)
    assert denied.status_code == 403
    ok = client.post(
        "/stacks",
        data={
            "name": "Edge",
            "deploy_file": "deploy.py",
            "inventory": "inventory.py",
            "executor": "local",
            "local_path": "/tmp/edge",
        },
        headers=auth(token),
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_member_cannot_edit_stacks(client, db):
    add_user(db, "member@example.com")
    token = login(client, "member@example.com")
    response = client.post(
        "/stacks",
        data={
            "name": "Edge",
            "deploy_file": "deploy.py",
            "inventory": "inventory.py",
            "executor": "local",
            "local_path": "/tmp/edge",
        },
        headers=auth(token),
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_caught_stack_error_rolls_back_request(client, db):
    add_user(db, "admin@example.com", role="admin")
    token = login(client, "admin@example.com")
    response = client.post(
        "/stacks",
        data={
            "name": "Must Roll Back",
            "deploy_file": "deploy.py",
            "inventory": "inventory.py",
            "executor": "local",
            "local_path": "/tmp/edge",
            "approver_user_id": "not-a-uuid",
        },
        headers=auth(token),
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert db.scalar(select(Stack).where(Stack.name == "Must Roll Back")) is None


def test_ci_token_opens_a_run_without_diffs(client, db, tmp_path):
    admin = add_user(db, "admin@example.com", role="admin")
    deploy = tmp_path / "deploy.py"
    deploy.write_text("print('x')\n")
    stack = _stack(db, str(tmp_path))
    token = login(client, admin.email)
    created = client.post(
        f"/stacks/{stack.id}/tokens",
        data={"name": "ci"},
        headers=auth(token),
        follow_redirects=False,
    )
    assert created.status_code == 303
    plaintext = client.cookies["stablehand_token_flash"]
    stored = db.scalar(select(ApiToken).where(ApiToken.stack_id == stack.id))
    assert stored is not None
    assert stored.token_hash == hash_token(plaintext)
    assert plaintext not in stored.token_hash
    client.cookies.clear()
    opened = client.post(
        f"/api/stacks/{stack.id}/runs",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={"commit": "local"},
    )
    assert opened.status_code == 201
    body = opened.json()
    assert "check_document" not in body
    assert body["counts"]["hosts"] == 0
    status = client.get(
        f"/api/runs/{body['id']}",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert status.status_code == 200
    assert "check_document" not in status.json()
    approve = client.post(
        f"/api/runs/{body['id']}/approve",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert approve.status_code == 404


def test_triggering_user_can_approve_in_the_ui(client, db, tmp_path):
    user = add_user(db, "ada@example.com")
    stack = _stack(db, str(tmp_path))
    token = login(client, user.email)
    with patch("stablehand.ui.routes.resolve_commit", return_value="abc123"):
        started = client.post(
            f"/stacks/{stack.id}/runs",
            headers=auth(token),
            follow_redirects=False,
        )
    assert started.status_code == 303
    run = db.scalar(select(Run))
    run.state = RunState.needs_approval.value
    run.check_fingerprint = "fp"
    run.check_document = {
        "hosts": [{"name": "web-1", "status": "change", "operations": [], "diffs": []}],
        "counts": {"hosts": 1, "change": 1, "unchanged": 0, "unreachable": 0, "failed": 0},
        "blocked": False,
    }
    db.commit()
    allowed = client.post(f"/runs/{run.id}/approve", headers=auth(token), follow_redirects=False)
    assert allowed.status_code == 303
    assert "error=" not in allowed.headers["location"]
    db.refresh(run)
    assert run.state == RunState.apply_queued.value
