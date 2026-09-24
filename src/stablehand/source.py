from __future__ import annotations

import subprocess
from pathlib import Path

from stablehand.gitssh import GitKeyError, private_key, ssh_environment
from stablehand.models import Stack


class SourceError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def effective_git_ref(stack: Stack) -> str | None:
    if stack.git_ref:
        return stack.git_ref
    source = stack.source
    if source is None:
        return None
    return source.git_ref


def resolve_commit(stack: Stack) -> str:
    source = stack.source
    if source is None:
        raise SourceError("Stack has no source.")
    if source.local_path and not source.git_url:
        path = Path(source.local_path)
        if not path.exists():
            raise SourceError(f"Local path does not exist: {source.local_path}")
        if (path / ".git").exists():
            try:
                return subprocess.check_output(
                    ["git", "-C", str(path), "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.PIPE,
                ).strip()
            except subprocess.CalledProcessError as exc:
                raise SourceError(
                    exc.stderr.strip() or "Could not read the local git revision."
                ) from exc
        return "local"
    if not source.git_url:
        raise SourceError("Stack has no git URL or local path.")
    ref = effective_git_ref(stack) or "HEAD"
    try:
        key = private_key(stack)
        with ssh_environment(key) as env:
            output = subprocess.check_output(
                ["git", "ls-remote", source.git_url, ref],
                text=True,
                stderr=subprocess.PIPE,
                env=env,
            )
    except GitKeyError as exc:
        raise SourceError(exc.message) from exc
    except subprocess.CalledProcessError as exc:
        raise SourceError(exc.stderr.strip() or "Could not resolve the git revision.") from exc
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise SourceError(f"No git revision found for {ref}.")
    return lines[0].split()[0]
