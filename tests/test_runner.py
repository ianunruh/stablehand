import os
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest

from stablehand.runner import main as runner


def test_limit_is_applied_to_inventory_check_and_apply(monkeypatch, tmp_path):
    monkeypatch.setenv("STABLEHAND_INVENTORY", "inventory.py")
    monkeypatch.setenv("STABLEHAND_DEPLOY", "deploy.py")
    monkeypatch.setenv("STABLEHAND_LIMIT", "web-*, canary")
    commands = []

    def pyinfra(source, args):
        assert source == tmp_path
        commands.append(args)
        stdout = "[]" if "debug-inventory" in args else "{}"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runner, "_pyinfra", pyinfra)

    runner.debug_inventory(tmp_path)
    runner.run_pyinfra(tmp_path, apply=False)
    runner.run_pyinfra(tmp_path, apply=True)

    limit_args = ["--limit", "web-*", "--limit", "canary"]
    assert commands == [
        ["inventory.py", "debug-inventory", "--json", *limit_args],
        ["inventory.py", "deploy.py", "--diff", "--json", *limit_args, "--dry"],
        ["inventory.py", "deploy.py", "--diff", "--json", *limit_args, "--yes"],
    ]


def test_check_upload_forwards_stderr_without_duplicate_log(monkeypatch, tmp_path):
    monkeypatch.setenv("STABLEHAND_PHASE", "check")
    monkeypatch.setattr(runner, "materialize_source", lambda: nullcontext(tmp_path))
    monkeypatch.setattr(
        runner,
        "debug_inventory",
        lambda source: (["web-1"], "inventory warning\n"),
    )
    monkeypatch.setattr(
        runner,
        "run_pyinfra",
        lambda source, *, apply: ({"plan": []}, "check diff\n", 0),
    )
    logs = []
    uploads = []
    monkeypatch.setattr(runner, "post_log", lambda phase, text: logs.append((phase, text)))
    monkeypatch.setattr(
        runner,
        "post_json",
        lambda path, payload: uploads.append((path, payload)) or {},
    )

    runner.main()

    assert logs == [("check", "inventory warning\n")]
    assert uploads == [
        (
            "/check-result",
            {
                "raw": {"plan": []},
                "inventory_hosts": ["web-1"],
                "stderr": "check diff\n",
                "exit_code": 0,
            },
        )
    ]


def test_apply_uploads_each_invocation_stderr(monkeypatch, tmp_path):
    monkeypatch.setenv("STABLEHAND_PHASE", "apply")
    monkeypatch.setattr(runner, "materialize_source", lambda: nullcontext(tmp_path))
    monkeypatch.setattr(runner, "debug_inventory", lambda source: (["web-1"], ""))
    results = iter(
        [
            ({"plan": []}, "precheck diff\n", 0),
            ({"plan": [], "results": {}}, "apply output\n", 0),
        ]
    )
    monkeypatch.setattr(runner, "run_pyinfra", lambda source, *, apply: next(results))
    uploads = []

    def post_json(path, payload):
        uploads.append((path, payload))
        return {"proceed": True} if path == "/apply-precheck" else {}

    monkeypatch.setattr(runner, "post_json", post_json)
    monkeypatch.setattr(runner, "post_log", lambda phase, text: None)

    runner.main()

    assert uploads[0][0] == "/apply-precheck"
    assert uploads[0][1]["stderr"] == "precheck diff\n"
    assert uploads[1][0] == "/apply-result"
    assert uploads[1][1]["stderr"] == "apply output\n"


def test_git_checkout_is_removed_after_use(monkeypatch):
    monkeypatch.setenv("STABLEHAND_SOURCE_KIND", "git")
    monkeypatch.setenv("STABLEHAND_GIT_URL", "https://example.test/repo.git")
    monkeypatch.setenv("STABLEHAND_GIT_REF", "main")
    monkeypatch.setenv("STABLEHAND_COMMIT", "abc123")

    def check_call(command, **kwargs):
        if command[1] == "clone":
            source = runner.Path(command[-1])
            source.mkdir()

    monkeypatch.setattr(runner.subprocess, "check_call", check_call)

    with runner.materialize_source() as source:
        checkout_root = source.parent
        assert source.exists()

    assert not checkout_root.exists()


def test_git_checkout_is_removed_when_clone_fails(monkeypatch):
    monkeypatch.setenv("STABLEHAND_SOURCE_KIND", "git")
    monkeypatch.setenv("STABLEHAND_GIT_URL", "https://example.test/repo.git")
    monkeypatch.setenv("STABLEHAND_GIT_REF", "main")
    monkeypatch.setenv("STABLEHAND_COMMIT", "abc123")
    checkout_root = None

    def fail_clone(command, **kwargs):
        nonlocal checkout_root
        checkout_root = runner.Path(command[-1]).parent
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(runner.subprocess, "check_call", fail_clone)

    with pytest.raises(subprocess.CalledProcessError), runner.materialize_source():
        pass

    assert checkout_root is not None
    assert not checkout_root.exists()


def test_host_secret_becomes_the_default_ssh_identity(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "id_ed25519").write_text("host-key\n")
    (secrets / "note.txt").write_text("ignore")
    monkeypatch.setenv("STABLEHAND_SECRETS", str(secrets))
    monkeypatch.setenv("HOME", str(tmp_path / "original"))

    runner.install_host_keys()

    home = Path(os.environ["HOME"])
    installed = home / ".ssh" / "id_ed25519"
    assert home != tmp_path / "original"
    assert installed.read_text() == "host-key\n"
    assert installed.stat().st_mode & 0o777 == 0o600
    assert not (home / ".ssh" / "note.txt").exists()


def test_host_secret_without_a_default_identity_keeps_home(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "note.txt").write_text("ignore")
    original = tmp_path / "original"
    monkeypatch.setenv("STABLEHAND_SECRETS", str(secrets))
    monkeypatch.setenv("HOME", str(original))

    runner.install_host_keys()

    assert os.environ["HOME"] == str(original)
