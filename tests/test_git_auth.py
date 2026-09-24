import subprocess
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from stablehand.executors.kubernetes import KubernetesExecutor
from stablehand.executors.local import LocalExecutor
from stablehand.gitssh import private_key
from stablehand.models import Stack
from stablehand.runner import main as runner
from stablehand.security import encrypt_secret
from stablehand.source import SourceError, resolve_commit
from stablehand.stacks.service import StackError, save_stack
from tests.conftest import add_user, auth, login

DEPLOY_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "deploy-key-marker-9f3a\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


def _save(db, stack=None, **overrides):
    values = {
        "name": "ops",
        "deploy_file": "deploy.py",
        "inventory": "inventory.py",
        "default_limit": "",
        "executor": "kubernetes",
        "git_url": "git@github.com:org/kcloud-ops.git",
        "git_ref": "main",
        "local_path": "",
        "secret_ref": "host-ssh",
        "schedule_cron": "",
        "approver_user_ids": [],
        "approver_groups": "",
    }
    values.update(overrides)
    return save_stack(db, stack=stack, **values)


def test_deploy_key_is_stored_encrypted_and_blank_keeps_it(db):
    stack = _save(db, git_ssh_key=DEPLOY_KEY)
    db.commit()

    assert stack.git_ssh_key_encrypted
    assert "deploy-key-marker-9f3a" not in stack.git_ssh_key_encrypted
    assert private_key(stack) == DEPLOY_KEY

    kept = _save(db, stack=stack, git_ssh_key="  ")
    assert private_key(kept) == DEPLOY_KEY

    cleared = _save(db, stack=stack, clear_git_ssh_key=True)
    assert private_key(cleared) == ""


def test_https_url_rejects_a_deploy_key(db):
    with pytest.raises(StackError, match="SSH git URL"):
        _save(db, git_url="https://github.com/org/kcloud-ops.git", git_ssh_key=DEPLOY_KEY)


def test_deploy_key_must_be_a_private_key(db):
    with pytest.raises(StackError, match="PEM SSH private key"):
        _save(db, git_ssh_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample")


def test_resolve_commit_uses_deploy_key_and_removes_it(monkeypatch):
    captured = {}

    def check_output(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        key_path = Path(kwargs["env"]["GIT_SSH_COMMAND"].split()[2])
        captured["existed"] = key_path.exists()
        captured["mode"] = key_path.stat().st_mode & 0o777
        return "abc123\trefs/heads/main\n"

    monkeypatch.setattr("stablehand.source.subprocess.check_output", check_output)
    stack = Stack(
        git_url="git@github.com:org/kcloud-ops.git",
        git_ref="main",
        git_ssh_key_encrypted=encrypt_secret(DEPLOY_KEY),
    )

    assert resolve_commit(stack) == "abc123"

    assert captured["command"][:3] == ["git", "ls-remote", "git@github.com:org/kcloud-ops.git"]
    assert captured["existed"] is True
    assert captured["mode"] == 0o600
    assert "IdentitiesOnly=yes" in captured["env"]["GIT_SSH_COMMAND"]
    key_path = Path(captured["env"]["GIT_SSH_COMMAND"].split()[2])
    assert not key_path.exists()


def test_resolve_commit_without_a_deploy_key_leaves_ssh_alone(monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    captured = {}

    def check_output(command, **kwargs):
        del command
        captured["env"] = kwargs["env"]
        return "abc123\tHEAD\n"

    monkeypatch.setattr("stablehand.source.subprocess.check_output", check_output)
    stack = Stack(git_url="https://example.test/repo.git", git_ref="main")

    assert resolve_commit(stack) == "abc123"
    assert "GIT_SSH_COMMAND" not in captured["env"]


def test_unreadable_deploy_key_fails_commit_resolution():
    stack = Stack(
        git_url="git@github.com:org/kcloud-ops.git",
        git_ref="main",
        git_ssh_key_encrypted="not-a-fernet-token",
    )

    with pytest.raises(SourceError, match="stored git deploy key"):
        resolve_commit(stack)


def test_runner_uses_only_the_deploy_key_file(monkeypatch, tmp_path):
    deploy_key = tmp_path / "deploy"
    deploy_key.write_text("deploy")
    host_secrets = tmp_path / "secrets"
    host_secrets.mkdir()
    (host_secrets / "id_ed25519").write_text("host")
    monkeypatch.setenv("STABLEHAND_GIT_SSH_KEY_FILE", str(deploy_key))
    monkeypatch.setenv("STABLEHAND_SECRETS", str(host_secrets))

    env = runner.git_env()

    assert f"ssh -i {deploy_key}" in env["GIT_SSH_COMMAND"]
    assert "id_ed25519" not in env["GIT_SSH_COMMAND"]


def test_runner_ignores_host_secrets_without_a_deploy_key(monkeypatch, tmp_path):
    monkeypatch.delenv("STABLEHAND_GIT_SSH_KEY_FILE", raising=False)
    monkeypatch.setenv("STABLEHAND_SECRETS", str(tmp_path))
    (tmp_path / "id_rsa").write_text("host")

    assert "GIT_SSH_COMMAND" not in runner.git_env()


def test_local_executor_passes_a_private_key_file(monkeypatch, tmp_path):
    monkeypatch.setattr("stablehand.executors.local.tempfile.gettempdir", lambda: str(tmp_path))
    captured = {}

    def popen(command, *, env, start_new_session):
        del command, start_new_session
        captured["env"] = env
        process = Mock()
        process.pid = 4321
        return process

    monkeypatch.setattr("stablehand.executors.local.subprocess.Popen", popen)
    execution_id = uuid.uuid4()
    stack = Stack(git_ssh_key_encrypted=encrypt_secret(DEPLOY_KEY))
    run = Mock(id=uuid.uuid4(), commit_sha="abc", limit=None)

    ref, secret_name = LocalExecutor().start(
        run, stack, "check", "shr_token", execution_id=execution_id
    )

    path = Path(secret_name)
    assert ref == "4321"
    assert path.name == f"sh-git-{execution_id.hex}"
    assert path.read_text() == DEPLOY_KEY
    assert path.stat().st_mode & 0o777 == 0o600
    assert captured["env"]["STABLEHAND_GIT_SSH_KEY_FILE"] == str(path)
    assert all(
        "deploy-key-marker-9f3a" not in value
        for value in captured["env"].values()
        if isinstance(value, str)
    )

    LocalExecutor().cleanup(ref, secret_name)
    assert not path.exists()


def test_local_executor_removes_the_key_when_start_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("stablehand.executors.local.tempfile.gettempdir", lambda: str(tmp_path))

    def popen(command, *, env, start_new_session):
        del command, env, start_new_session
        raise OSError("runner missing")

    monkeypatch.setattr("stablehand.executors.local.subprocess.Popen", popen)
    execution_id = uuid.uuid4()
    stack = Stack(git_ssh_key_encrypted=encrypt_secret(DEPLOY_KEY))
    run = Mock(id=uuid.uuid4(), commit_sha="abc", limit=None)

    with pytest.raises(OSError, match="runner missing"):
        LocalExecutor().start(run, stack, "check", "shr_token", execution_id=execution_id)

    assert not (tmp_path / f"sh-git-{execution_id.hex}").exists()


def test_kubernetes_mounts_deploy_key_apart_from_the_host_secret(monkeypatch):
    core = Mock()
    batch = Mock()
    monkeypatch.setattr("stablehand.executors.kubernetes._load_config", lambda: None)
    monkeypatch.setattr("stablehand.executors.kubernetes.client.CoreV1Api", lambda: core)
    monkeypatch.setattr("stablehand.executors.kubernetes.client.BatchV1Api", lambda: batch)
    stack = Stack(
        id=uuid.uuid4(),
        name="ops",
        deploy_file="deploy.py",
        inventory="inventory.py",
        executor="kubernetes",
        git_url="git@github.com:org/kcloud-ops.git",
        git_ref="main",
        secret_ref="host-ssh",
        git_ssh_key_encrypted=encrypt_secret(DEPLOY_KEY),
    )
    run = Mock(id=uuid.uuid4(), commit_sha="abc", limit=None)

    KubernetesExecutor().start(run, stack, "check", "shr_token", execution_id=uuid.uuid4())

    secret = core.create_namespaced_secret.call_args.args[1]
    assert secret.string_data["token"] == "shr_token"
    assert secret.string_data["id_ed25519"] == DEPLOY_KEY
    job = batch.create_namespaced_job.call_args.args[1]
    pod = job.spec.template.spec
    mounts = {mount.name: mount for mount in pod.containers[0].volume_mounts}
    assert mounts["git-ssh"].mount_path == "/git-ssh"
    assert mounts["git-ssh"].read_only is True
    assert mounts["secrets"].mount_path == "/secrets"
    git_volume = next(volume for volume in pod.volumes if volume.name == "git-ssh")
    assert git_volume.secret.default_mode == 0o400
    assert git_volume.secret.items[0].mode == 0o400
    assert git_volume.secret.items[0].path == "id_ed25519"
    env = {item.name: item.value for item in pod.containers[0].env}
    assert env["STABLEHAND_GIT_SSH_KEY_FILE"] == "/git-ssh/id_ed25519"
    assert env["STABLEHAND_SECRETS"] == "/secrets"
    assert all(not value or "deploy-key-marker-9f3a" not in value for value in env.values())


def test_kubernetes_omits_the_git_mount_without_a_deploy_key(monkeypatch):
    core = Mock()
    batch = Mock()
    monkeypatch.setattr("stablehand.executors.kubernetes._load_config", lambda: None)
    monkeypatch.setattr("stablehand.executors.kubernetes.client.CoreV1Api", lambda: core)
    monkeypatch.setattr("stablehand.executors.kubernetes.client.BatchV1Api", lambda: batch)
    stack = Stack(
        id=uuid.uuid4(),
        deploy_file="deploy.py",
        inventory="inventory.py",
        git_url="https://example.test/repo.git",
        git_ref="main",
    )
    run = Mock(id=uuid.uuid4(), commit_sha="abc", limit=None)

    KubernetesExecutor().start(run, stack, "check", "shr_token", execution_id=uuid.uuid4())

    secret = core.create_namespaced_secret.call_args.args[1]
    assert secret.string_data == {"token": "shr_token"}
    pod = batch.create_namespaced_job.call_args.args[1].spec.template.spec
    assert pod.volumes is None
    assert "STABLEHAND_GIT_SSH_KEY_FILE" not in {item.name for item in pod.containers[0].env}


def test_stack_form_saves_a_deploy_key_without_showing_it(client, db):
    add_user(db, "admin@example.com", role="admin")
    token = login(client, "admin@example.com")
    created = client.post(
        "/stacks",
        data={
            "name": "ops",
            "deploy_file": "deploy.py",
            "inventory": "inventory.py",
            "executor": "kubernetes",
            "git_url": "git@github.com:org/kcloud-ops.git",
            "git_ref": "main",
            "secret_ref": "host-ssh",
            "git_ssh_key": DEPLOY_KEY,
        },
        headers=auth(token),
        follow_redirects=False,
    )
    assert created.status_code == 303
    db.expire_all()
    stack = db.scalar(select(Stack).where(Stack.name == "ops"))
    assert private_key(stack) == DEPLOY_KEY

    detail = client.get(f"/stacks/{stack.id}")
    assert "Deploy key saved" in detail.text
    assert "deploy-key-marker-9f3a" not in detail.text

    edit = client.get(f"/stacks/{stack.id}/edit")
    assert "A deploy key is saved" in edit.text
    assert 'name="clear_git_ssh_key"' in edit.text
    assert "deploy-key-marker-9f3a" not in edit.text
    assert "<textarea" in edit.text

    rejected = client.post(
        f"/stacks/{stack.id}",
        data={
            "name": "ops",
            "deploy_file": "deploy.py",
            "inventory": "inventory.py",
            "executor": "kubernetes",
            "git_url": "https://github.com/org/kcloud-ops.git",
            "git_ref": "main",
            "git_ssh_key": DEPLOY_KEY,
        },
        headers=auth(token),
    )
    assert rejected.status_code == 400
    assert "SSH git URL" in rejected.text
    assert "deploy-key-marker-9f3a" not in rejected.text
    db.expire_all()
    assert private_key(stack) == DEPLOY_KEY

    cleared = client.post(
        f"/stacks/{stack.id}",
        data={
            "name": "ops",
            "deploy_file": "deploy.py",
            "inventory": "inventory.py",
            "executor": "kubernetes",
            "git_url": "git@github.com:org/kcloud-ops.git",
            "git_ref": "main",
            "clear_git_ssh_key": "on",
        },
        headers=auth(token),
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    db.expire_all()
    assert private_key(stack) == ""
    assert "Deploy key saved" not in client.get(f"/stacks/{stack.id}").text


def test_resolve_commit_failure_still_reports_git_stderr(monkeypatch):
    def check_output(command, **kwargs):
        del command, kwargs
        raise subprocess.CalledProcessError(128, ["git"], stderr="permission denied\n")

    monkeypatch.setattr("stablehand.source.subprocess.check_output", check_output)
    stack = Stack(
        git_url="git@github.com:org/kcloud-ops.git",
        git_ssh_key_encrypted=encrypt_secret(DEPLOY_KEY),
    )

    with pytest.raises(SourceError, match="permission denied"):
        resolve_commit(stack)
