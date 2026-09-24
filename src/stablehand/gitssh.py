from __future__ import annotations

import os
import shlex
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import InvalidToken

from stablehand.models import Stack
from stablehand.security import decrypt_secret

MOUNTED_KEY = Path("/git-ssh/id_ed25519")
_SSH_OPTIONS = "-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"


class GitKeyError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def ssh_command(key_path: Path) -> str:
    return f"ssh -i {shlex.quote(str(key_path))} {_SSH_OPTIONS}"


def is_ssh_git_url(url: str) -> bool:
    value = url.strip()
    if value.startswith("ssh://"):
        return True
    if "://" in value or "@" not in value:
        return False
    return ":" in value.split("@", 1)[1]


def private_key(stack: Stack) -> str:
    encrypted = stack.git_ssh_key_encrypted or ""
    if not encrypted:
        return ""
    try:
        return decrypt_secret(encrypted)
    except InvalidToken as exc:
        raise GitKeyError("Could not read the stored git deploy key.") from exc


def write_private_key(path: Path, private_key_text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            fd = -1
            handle.write(private_key_text.strip() + "\n")
    finally:
        if fd >= 0:
            os.close(fd)


@contextmanager
def ssh_environment(private_key_text: str) -> Iterator[dict[str, str]]:
    env = os.environ.copy()
    if not private_key_text.strip():
        yield env
        return
    with tempfile.TemporaryDirectory(prefix="stablehand-git-") as directory:
        path = Path(directory) / "id_ed25519"
        write_private_key(path, private_key_text)
        env["GIT_SSH_COMMAND"] = ssh_command(path)
        yield env
