from __future__ import annotations

import subprocess
from pathlib import Path

from stablehand.models import Stack


class SourceError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def resolve_commit(stack: Stack) -> str:
    if stack.local_path and not stack.git_url:
        path = Path(stack.local_path)
        if not path.exists():
            raise SourceError(f"Local path does not exist: {stack.local_path}")
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
    if not stack.git_url:
        raise SourceError("Stack has no git URL or local path.")
    ref = stack.git_ref or "HEAD"
    try:
        output = subprocess.check_output(
            ["git", "ls-remote", stack.git_url, ref],
            text=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise SourceError(exc.stderr.strip() or "Could not resolve the git revision.") from exc
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise SourceError(f"No git revision found for {ref}.")
    return lines[0].split()[0]
