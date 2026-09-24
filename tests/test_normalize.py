import json
from pathlib import Path

from stablehand.plans.normalize import fingerprint, normalize

FIXTURE = json.loads(Path("tests/fixtures/pyinfra_check.json").read_text())
STDERR = """
[db-1] Error: could not connect to target (Connection refused)
--- web-1:/etc/motd
+++ web-1:/etc/motd
@@ -1 +1 @@
-old
+new
"""


def test_normalize_pivots_operations_onto_hosts():
    document = normalize(FIXTURE, ["web-1", "web-2", "web-3"], STDERR)
    by_name = {host["name"]: host for host in document["hosts"]}
    assert by_name["web-1"]["status"] == "change"
    assert by_name["web-1"]["operations"][0]["will_change"] is True
    assert by_name["web-2"]["operations"][0]["conditional"] is True
    assert by_name["web-3"]["status"] == "unchanged"
    assert by_name["db-1"]["status"] == "unreachable"
    assert document["blocked"] is True
    assert "old" in by_name["web-1"]["diffs"][0]
    assert document["counts"]["change"] == 2
    assert document["counts"]["unreachable"] == 1


def test_fingerprint_tracks_the_change_set():
    first = normalize(FIXTURE, ["web-1", "web-2"], "")
    second = normalize(
        {
            "plan": [
                {
                    "op_hash": "abc123",
                    "name": "Echo stablehand demo",
                    "hosts_with_change": ["web-1"],
                    "hosts_with_conditional_change": [],
                }
            ]
        },
        ["web-1", "web-2"],
        "",
    )
    assert fingerprint(first) != fingerprint(second)
    assert fingerprint(first) == fingerprint(normalize(FIXTURE, ["web-1", "web-2"], ""))


def test_apply_results_mark_failures():
    raw = {
        "plan": [
            {
                "op_hash": "abc123",
                "name": "Echo stablehand demo",
                "hosts_with_change": ["web-1"],
                "hosts_with_conditional_change": [],
            }
        ],
        "results": {
            "operations": [
                {
                    "op_hash": "abc123",
                    "success": [],
                    "error": ["web-1"],
                    "no_change": [],
                }
            ],
            "failed_hosts": ["web-1"],
        },
    }
    document = normalize(raw, ["web-1"], "")
    assert document["hosts"][0]["status"] == "failed"
    assert document["hosts"][0]["operations"][0]["result"] == "error"
