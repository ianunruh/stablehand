from __future__ import annotations

import os
import subprocess
import uuid

from stablehand.executors.base import Executor, runner_env
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
        del execution_id
        process = subprocess.Popen(
            ["stablehand-runner"],
            env=runner_env(run, stack, phase, token),
            start_new_session=True,
        )
        return str(process.pid), None

    def poll(self, ref: str, secret_name: str | None) -> str:
        del secret_name
        try:
            os.kill(int(ref), 0)
        except ProcessLookupError:
            return "finished"
        except PermissionError:
            return "running"
        return "running"

    def cleanup(self, ref: str, secret_name: str | None) -> None:
        return None
