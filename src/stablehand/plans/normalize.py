from __future__ import annotations

import hashlib
import json
import re

_HOST_ERROR = re.compile(
    r"^\[(?P<host>[^\]]+)\].*(?:could not connect|connection refused|authentication failed|"
    r"no route to host|timed out|unreachable)",
    re.IGNORECASE,
)
_DIFF_START = re.compile(r"^(?:diff --git |--- )", re.MULTILINE)


def normalize(
    raw: dict | None,
    inventory_hosts: list[str],
    stderr: str = "",
) -> dict:
    hosts: dict[str, dict] = {
        name: {"name": name, "status": "unchanged", "operations": [], "diffs": []}
        for name in inventory_hosts
    }
    unreachable = _unreachable_hosts(stderr)
    for name in unreachable:
        hosts.setdefault(
            name, {"name": name, "status": "unreachable", "operations": [], "diffs": []}
        )
        hosts[name]["status"] = "unreachable"

    results_by_hash = _results_index(raw)
    for operation in (raw or {}).get("plan") or []:
        changed = set(operation.get("hosts_with_change") or [])
        conditional = set(operation.get("hosts_with_conditional_change") or [])
        mentioned = changed | conditional
        if not mentioned:
            mentioned = set(operation.get("hosts") or [])
        for name in sorted(mentioned):
            host = hosts.setdefault(
                name, {"name": name, "status": "unchanged", "operations": [], "diffs": []}
            )
            will_change = name in changed or name in conditional
            result = results_by_hash.get(operation.get("op_hash", ""), {}).get(name)
            host["operations"].append(
                {
                    "name": operation.get("name") or operation.get("op_hash") or "operation",
                    "op_hash": operation.get("op_hash") or "",
                    "args_hash": _json_digest(operation.get("args") or []),
                    "will_change": will_change,
                    "conditional": name in conditional,
                    "result": result,
                    "diff": None,
                }
            )

    failed_hosts = set(((raw or {}).get("results") or {}).get("failed_hosts") or [])
    for name in failed_hosts:
        hosts.setdefault(name, {"name": name, "status": "failed", "operations": [], "diffs": []})

    diffs = _diff_blocks(stderr)
    for block in diffs:
        attached = False
        for host in hosts.values():
            if host["name"] and host["name"] in block:
                host["diffs"].append(block)
                attached = True
        if not attached and hosts:
            next(iter(hosts.values()))["diffs"].append(block)

    for host in hosts.values():
        if host["status"] == "unreachable":
            continue
        results = [op["result"] for op in host["operations"]]
        preview = any(op["will_change"] for op in host["operations"]) and all(
            result is None for result in results
        )
        if host["name"] in failed_hosts or "error" in results:
            host["status"] = "failed"
        elif "success" in results or preview:
            host["status"] = "change"
        else:
            host["status"] = "unchanged"

    ordered = [hosts[name] for name in inventory_hosts if name in hosts]
    ordered.extend(host for name, host in hosts.items() if name not in inventory_hosts)
    counts = {
        "hosts": len(ordered),
        "change": sum(1 for host in ordered if host["status"] == "change"),
        "unchanged": sum(1 for host in ordered if host["status"] == "unchanged"),
        "unreachable": sum(1 for host in ordered if host["status"] == "unreachable"),
        "failed": sum(1 for host in ordered if host["status"] == "failed"),
    }
    blocked = counts["unreachable"] > 0
    return {
        "hosts": ordered,
        "counts": counts,
        "unreachable": sorted(
            {host["name"] for host in ordered if host["status"] == "unreachable"}
        ),
        "blocked": blocked,
    }


def fingerprint(document: dict) -> str:
    operations: list[dict] = []
    diffs: list[dict] = []
    for host in document.get("hosts") or []:
        host_name = host["name"]
        for operation in host.get("operations") or []:
            if operation.get("will_change"):
                operations.append(
                    {
                        "host": host_name,
                        "op_hash": operation["op_hash"],
                        "args_hash": operation["args_hash"],
                        "conditional": bool(operation.get("conditional")),
                    }
                )
        for diff in host.get("diffs") or []:
            diffs.append({"host": host_name, "diff_hash": _text_digest(diff)})
    operations.sort(key=_canonical_json)
    diffs.sort(key=_canonical_json)
    return _text_digest(
        _canonical_json(
            {
                "version": 2,
                "operations": operations,
                "diffs": diffs,
            }
        )
    )


def _json_digest(value) -> str:
    return _text_digest(_canonical_json(value))


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _results_index(raw: dict | None) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    results = (raw or {}).get("results") or {}
    for operation in results.get("operations") or []:
        op_hash = operation.get("op_hash") or ""
        per_host: dict[str, str] = {}
        for name in operation.get("success") or []:
            per_host[name] = "success"
        for name in operation.get("error") or []:
            per_host[name] = "error"
        for name in operation.get("no_change") or []:
            per_host[name] = "no_change"
        index[op_hash] = per_host
    return index


def _unreachable_hosts(stderr: str) -> list[str]:
    found: list[str] = []
    for line in stderr.splitlines():
        match = _HOST_ERROR.search(line.strip())
        if match:
            found.append(match.group("host"))
    return list(dict.fromkeys(found))


def _diff_blocks(stderr: str) -> list[str]:
    matches = list(_DIFF_START.finditer(stderr))
    blocks: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(stderr)
        block = stderr[match.start() : end].strip()
        if block.startswith("--- ") and "+++" not in block:
            continue
        blocks.append(block)
    return blocks
