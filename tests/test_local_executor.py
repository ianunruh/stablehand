import uuid

from stablehand.executors import local
from stablehand.executors.base import runner_env
from stablehand.executors.local import LocalExecutor
from stablehand.models import Run, Stack


def test_runner_env_includes_snapshotted_limit():
    stack_id = uuid.uuid4()
    run = Run(id=uuid.uuid4(), stack_id=stack_id, commit_sha="abc", limit="web-*, canary")
    stack = Stack(
        id=stack_id,
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor="local",
        local_path="/tmp/demo",
    )

    env = runner_env(run, stack, "check", "shr_token")

    assert env["STABLEHAND_LIMIT"] == "web-*, canary"


def test_poll_reaps_finished_child(monkeypatch):
    monkeypatch.setattr(local.os, "waitpid", lambda pid, options: (pid, 0))

    def unexpected_kill(pid, signal):
        raise AssertionError("a reaped child must not be probed")

    monkeypatch.setattr(local.os, "kill", unexpected_kill)

    assert LocalExecutor().poll("123", None) == "finished"


def test_poll_keeps_live_child_running(monkeypatch):
    monkeypatch.setattr(local.os, "waitpid", lambda pid, options: (0, 0))

    assert LocalExecutor().poll("123", None) == "running"


def test_poll_falls_back_for_process_not_owned_by_worker(monkeypatch):
    def not_a_child(pid, options):
        raise ChildProcessError

    monkeypatch.setattr(local.os, "waitpid", not_a_child)
    monkeypatch.setattr(local.os, "kill", lambda pid, signal: None)

    assert LocalExecutor().poll("123", None) == "running"
