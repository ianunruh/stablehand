from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from stablehand.executors.base import Executor, runner_env
from stablehand.gitssh import private_key, write_private_key
from stablehand.models import Run, Stack


class LocalExecutor(Executor):
    def start(
        self,
        run: Run,
        stack: Stack,
        phase: str,
        token: str,
        *,
        execution_id: uuid.UUID,
    ) -> tuple[str, str | None]:
        env = runner_env(run, stack, phase, token)
        key_path = _write_git_key(stack, execution_id)
        if key_path is not None:
            env["STABLEHAND_GIT_SSH_KEY_FILE"] = str(key_path)
        try:
            process = subprocess.Popen(
                ["stablehand-runner"],
                env=env,
                start_new_session=True,
            )
        except Exception:
            _delete_git_key(None if key_path is None else str(key_path))
            raise
        return str(process.pid), None if key_path is None else str(key_path)

    def poll(self, ref: str, secret_name: str | None) -> str:
        del secret_name
        pid = int(ref)
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        else:
            return "finished" if reaped == pid else "running"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "finished"
        except PermissionError:
            return "running"
        return "running"

    def cleanup(self, ref: str, secret_name: str | None) -> None:
        del ref
        _delete_git_key(secret_name)


def _write_git_key(stack: Stack, execution_id: uuid.UUID) -> Path | None:
    key = private_key(stack)
    if not key:
        return None
    path = Path(tempfile.gettempdir()) / f"sh-git-{execution_id.hex}"
    if len(str(path)) > 200:
        raise RuntimeError("Git deploy key path is too long to record.")
    try:
        write_private_key(path, key)
    except Exception:
        _delete_git_key(str(path))
        raise
    return path


def _delete_git_key(secret_name: str | None) -> None:
    if not secret_name:
        return
    path = Path(secret_name)
    if path.name.startswith("sh-git-") and path.is_file():
        path.unlink()
