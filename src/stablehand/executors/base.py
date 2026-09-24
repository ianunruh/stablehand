from __future__ import annotations

import os
import uuid

from stablehand.config import get_settings
from stablehand.models import Run, Stack


class Executor:
    def start(
        self,
        run: Run,
        stack: Stack,
        phase: str,
        token: str,
        *,
        execution_id: uuid.UUID,
    ) -> tuple[str, str | None]:
        """Start an execution. Returns (ref, secret_name)."""
        raise NotImplementedError

    def poll(self, ref: str, secret_name: str | None) -> str:
        """Return running, succeeded, or failed."""
        raise NotImplementedError

    def cleanup(self, ref: str, secret_name: str | None) -> None:
        return None


def runner_env(run: Run, stack: Stack, phase: str, token: str) -> dict[str, str]:
    settings = get_settings()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYINFRA_PROGRESS": "off",
        "STABLEHAND_API_URL": settings.runner_api_url.rstrip("/"),
        "STABLEHAND_RUN_TOKEN": token,
        "STABLEHAND_RUN_ID": str(run.id),
        "STABLEHAND_PHASE": phase,
        "STABLEHAND_COMMIT": run.commit_sha,
        "STABLEHAND_DEPLOY": stack.deploy_file,
        "STABLEHAND_INVENTORY": stack.inventory,
        "STABLEHAND_GIT_URL": stack.git_url or "",
        "STABLEHAND_GIT_REF": stack.git_ref or "",
        "STABLEHAND_LOCAL_PATH": stack.local_path or "",
        "STABLEHAND_SOURCE_KIND": "path" if stack.local_path and not stack.git_url else "git",
    }
    if stack.secret_ref:
        env["STABLEHAND_SECRETS"] = stack.secret_ref
    return env
