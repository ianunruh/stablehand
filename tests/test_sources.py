import pytest
from sqlalchemy import select

from stablehand.models import Runtime, Source, Stack
from stablehand.runtimes.service import RuntimeConfigError, delete_runtime, save_runtime
from stablehand.source import effective_git_ref
from stablehand.sources.service import SourceError, delete_source, save_source
from stablehand.stacks.service import StackError, save_stack
from tests.conftest import add_user, auth, login


def _source(db, **overrides) -> Source:
    values = {
        "name": "ops",
        "git_url": "git@github.com:org/kcloud-ops.git",
        "git_ref": "main",
        "local_path": "",
    }
    values.update(overrides)
    return save_source(db, source=None, **values)


def _runtime(db, **overrides) -> Runtime:
    values = {"name": "cluster", "executor": "kubernetes", "secret_ref": "host-ssh"}
    values.update(overrides)
    return save_runtime(db, runtime=None, **values)


def _stack(db, source: Source, runtime: Runtime, **overrides) -> Stack:
    values = {
        "name": "web",
        "deploy_file": "deploy.py",
        "inventory": "inventory.py",
        "default_limit": "",
        "source_id": str(source.id),
        "runtime_id": str(runtime.id),
        "git_ref": "",
        "schedule_cron": "",
        "approver_user_ids": [],
        "approver_groups": "",
    }
    values.update(overrides)
    return save_stack(db, stack=None, **values)


def test_stacks_share_one_source_and_runtime(db):
    source = _source(db)
    runtime = _runtime(db)
    web = _stack(db, source, runtime, name="web")
    db_stack = _stack(db, source, runtime, name="db", deploy_file="db.py")
    db.commit()

    assert web.source_id == db_stack.source_id == source.id
    assert web.runtime_id == db_stack.runtime_id == runtime.id
    assert effective_git_ref(web) == "main"
    assert web.git_ref is None


def test_stack_ref_overrides_the_source_ref(db):
    source = _source(db, git_ref="main")
    runtime = _runtime(db)
    pinned = _stack(db, source, runtime, git_ref="release")
    inherited = _stack(db, source, runtime, name="inherited")

    assert pinned.git_ref == "release"
    assert effective_git_ref(pinned) == "release"
    assert inherited.git_ref is None
    assert effective_git_ref(inherited) == "main"


def test_kubernetes_runtime_rejects_a_path_only_source(db):
    source = _source(db, name="local", git_url="", local_path="/tmp/demo")
    runtime = _runtime(db)
    with pytest.raises(StackError, match="git URL"):
        _stack(db, source, runtime)


def test_path_source_rejects_a_ref_override(db):
    source = _source(db, name="local", git_url="", local_path="/tmp/demo")
    runtime = _runtime(db, name="local", executor="local", secret_ref="")
    with pytest.raises(StackError, match="git ref"):
        _stack(db, source, runtime, git_ref="release")


def test_delete_is_refused_while_a_stack_uses_the_record(db):
    source = _source(db)
    runtime = _runtime(db)
    _stack(db, source, runtime)
    db.commit()

    with pytest.raises(SourceError, match="1 stack"):
        delete_source(db, source)
    with pytest.raises(RuntimeConfigError, match="1 stack"):
        delete_runtime(db, runtime)


def test_delete_removes_an_unused_source_and_runtime(client, db):
    add_user(db, "admin@example.com", role="admin")
    token = login(client, "admin@example.com")
    source = _source(db, name="unused")
    runtime = _runtime(db, name="unused", executor="local", secret_ref="")
    source_id = source.id
    runtime_id = runtime.id
    db.commit()

    removed_source = client.post(
        f"/sources/{source_id}/delete", headers=auth(token), follow_redirects=False
    )
    removed_runtime = client.post(
        f"/runtimes/{runtime_id}/delete", headers=auth(token), follow_redirects=False
    )

    assert removed_source.status_code == 303
    assert removed_runtime.status_code == 303
    db.expire_all()
    assert db.scalar(select(Source).where(Source.id == source_id)) is None
    assert db.scalar(select(Runtime).where(Runtime.id == runtime_id)) is None


def test_http_delete_reports_stacks_still_using_the_source(client, db):
    add_user(db, "admin@example.com", role="admin")
    token = login(client, "admin@example.com")
    source = _source(db)
    runtime = _runtime(db)
    _stack(db, source, runtime)
    db.commit()

    response = client.post(
        f"/sources/{source.id}/delete", headers=auth(token), follow_redirects=False
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert db.get(Source, source.id) is not None
