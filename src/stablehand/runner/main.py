from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from stablehand.gitssh import ssh_command
from stablehand.limits import limit_patterns

_HOST_KEY_NAMES = ("id_ed25519", "id_rsa", "id_ecdsa")


def main() -> None:
    install_host_keys()
    phase = os.environ["STABLEHAND_PHASE"]
    try:
        with materialize_source() as source:
            inventory_hosts, inventory_stderr = debug_inventory(source)
            post_log(phase, inventory_stderr)
            check_raw, check_stderr, check_code = run_pyinfra(source, apply=False)
            if phase == "check":
                post_json(
                    "/check-result",
                    {
                        "raw": check_raw,
                        "inventory_hosts": inventory_hosts,
                        "stderr": check_stderr,
                        "exit_code": check_code,
                    },
                )
                return
            decision = post_json(
                "/apply-precheck",
                {
                    "raw": check_raw,
                    "inventory_hosts": inventory_hosts,
                    "stderr": check_stderr,
                    "exit_code": check_code,
                },
            )
            if not decision.get("proceed"):
                return
            apply_raw, apply_stderr, apply_code = run_pyinfra(source, apply=True)
            post_json(
                "/apply-result",
                {
                    "raw": apply_raw,
                    "inventory_hosts": inventory_hosts,
                    "stderr": apply_stderr,
                    "exit_code": apply_code,
                },
            )
    except Exception as exc:
        post_log(phase, f"{exc}\n")
        raise SystemExit(1) from exc


@contextmanager
def materialize_source() -> Iterator[Path]:
    if os.environ.get("STABLEHAND_SOURCE_KIND") == "path":
        path = Path(os.environ["STABLEHAND_LOCAL_PATH"])
        if not path.exists():
            raise RuntimeError(f"Local path does not exist: {path}")
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="stablehand-") as work:
        dest = Path(work) / "source"
        url = os.environ["STABLEHAND_GIT_URL"]
        ref = os.environ.get("STABLEHAND_GIT_REF") or "HEAD"
        sha = os.environ["STABLEHAND_COMMIT"]
        env = git_env()
        subprocess.check_call(["git", "clone", "--branch", ref, url, str(dest)], env=env)
        subprocess.check_call(["git", "checkout", sha], cwd=dest, env=env)
        yield dest


def install_host_keys() -> None:
    """Point pyinfra's default key search at the host secret.

    With no inventory ``ssh_key``, Paramiko only tries ``~/.ssh/id_*``. The
    secret is mounted elsewhere, so copy the standard names into a private home.
    """
    secrets = os.environ.get("STABLEHAND_SECRETS", "").strip()
    if not secrets:
        return
    source = Path(secrets)
    if not source.is_dir():
        return
    names = [name for name in _HOST_KEY_NAMES if (source / name).is_file()]
    if not names:
        return
    home = Path(tempfile.mkdtemp(prefix="stablehand-home-"))
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    for name in names:
        dest = ssh_dir / name
        dest.write_bytes((source / name).read_bytes())
        dest.chmod(0o600)
    os.environ["HOME"] = str(home)


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    key_file = os.environ.get("STABLEHAND_GIT_SSH_KEY_FILE", "").strip()
    if key_file:
        env["GIT_SSH_COMMAND"] = ssh_command(Path(key_file))
    return env


def debug_inventory(source: Path) -> tuple[list[str], str]:
    completed = _pyinfra(
        source,
        [
            os.environ["STABLEHAND_INVENTORY"],
            "debug-inventory",
            "--json",
            *_limit_args(),
        ],
    )
    raw = _parse_json(completed.stdout)
    hosts: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                hosts.append(str(item["name"]))
            elif isinstance(item, str):
                hosts.append(item)
    return hosts, completed.stderr


def run_pyinfra(source: Path, *, apply: bool) -> tuple[dict | None, str, int]:
    command = [
        os.environ["STABLEHAND_INVENTORY"],
        os.environ["STABLEHAND_DEPLOY"],
        "--diff",
        "--json",
        *_limit_args(),
    ]
    if apply:
        command.append("--yes")
    else:
        command.append("--dry")
    completed = _pyinfra(source, command)
    return _parse_json(completed.stdout), completed.stderr, completed.returncode


def _limit_args() -> list[str]:
    args: list[str] = []
    for pattern in limit_patterns(os.environ.get("STABLEHAND_LIMIT")):
        args.extend(["--limit", pattern])
    return args


def _pyinfra(source: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pyinfra", *args],
        cwd=source,
        env=git_env(),
        text=True,
        capture_output=True,
        check=False,
    )


def _parse_json(stdout: str) -> dict | list | None:
    text = stdout.strip()
    if not text:
        return None
    start = text.find("{")
    list_start = text.find("[")
    if list_start != -1 and (start == -1 or list_start < start):
        start = list_start
    if start == -1:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return None


def post_log(phase: str, text: str) -> None:
    if not text:
        return
    _request("POST", "/logs", {"phase": phase, "text": text})


def post_json(path: str, payload: dict) -> dict:
    response = _request("POST", path, payload)
    if response is None or response.status_code >= 300:
        detail = "" if response is None else response.text[:500]
        raise RuntimeError(f"Result upload failed ({path}): {detail}")
    try:
        data = response.json()
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _request(method: str, path: str, payload: dict) -> httpx.Response | None:
    base = os.environ["STABLEHAND_API_URL"].rstrip("/")
    run_id = os.environ["STABLEHAND_RUN_ID"]
    token = os.environ["STABLEHAND_RUN_TOKEN"]
    try:
        return httpx.request(
            method,
            f"{base}/api/runs/{run_id}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
    except httpx.HTTPError:
        return None
