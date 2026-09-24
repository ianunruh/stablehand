from stablehand.executors import local
from stablehand.executors.local import LocalExecutor


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
