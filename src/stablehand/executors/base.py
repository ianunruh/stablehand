from __future__ import annotations

import os
import uuid

from stablehand.config import get_settings
from stablehand.models import Execution, Run, Stack
from stablehand.source import effective_git_ref


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

    def recover(
        self, execution: Execution, run: Run, stack: Stack
    ) -> tuple[str, str | None] | None:
        return None

    def cleanup(self, ref: str, secret_name: str | None) -> None:
        return None


def runner_env(run: Run, stack: Stack, phase: str, token: str) -> dict[str, str]:
    settings = get_settings()
    source = stack.source
    runtime = stack.runtime
    git_url = source.git_url if source is not None and source.git_url else ""
    local_path = source.local_path if source is not None and source.local_path else ""
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
        "STABLEHAND_LIMIT": run.limit or "",
        "STABLEHAND_GIT_URL": git_url,
        "STABLEHAND_GIT_REF": effective_git_ref(stack) or "",
        "STABLEHAND_LOCAL_PATH": local_path,
        "STABLEHAND_SOURCE_KIND": "path" if local_path and not git_url else "git",
    }
    if runtime is not None and runtime.secret_ref:
        env["STABLEHAND_SECRETS"] = runtime.secret_ref
    return env
