from stablehand.runner import main as runner


def test_check_upload_forwards_stderr_without_duplicate_log(monkeypatch, tmp_path):
    monkeypatch.setenv("STABLEHAND_PHASE", "check")
    monkeypatch.setattr(runner, "materialize_source", lambda: tmp_path)
    monkeypatch.setattr(
        runner,
        "debug_inventory",
        lambda source: (["web-1"], "inventory warning\n"),
    )
    monkeypatch.setattr(
        runner,
        "run_pyinfra",
        lambda source, *, apply: ({"plan": []}, "check diff\n", 0),
    )
    logs = []
    uploads = []
    monkeypatch.setattr(runner, "post_log", lambda phase, text: logs.append((phase, text)))
    monkeypatch.setattr(
        runner,
        "post_json",
        lambda path, payload: uploads.append((path, payload)) or {},
    )

    runner.main()

    assert logs == [("check", "inventory warning\n")]
    assert uploads == [
        (
            "/check-result",
            {
                "raw": {"plan": []},
                "inventory_hosts": ["web-1"],
                "stderr": "check diff\n",
                "exit_code": 0,
            },
        )
    ]


def test_apply_uploads_each_invocation_stderr(monkeypatch, tmp_path):
    monkeypatch.setenv("STABLEHAND_PHASE", "apply")
    monkeypatch.setattr(runner, "materialize_source", lambda: tmp_path)
    monkeypatch.setattr(runner, "debug_inventory", lambda source: (["web-1"], ""))
    results = iter(
        [
            ({"plan": []}, "precheck diff\n", 0),
            ({"plan": [], "results": {}}, "apply output\n", 0),
        ]
    )
    monkeypatch.setattr(runner, "run_pyinfra", lambda source, *, apply: next(results))
    uploads = []

    def post_json(path, payload):
        uploads.append((path, payload))
        return {"proceed": True} if path == "/apply-precheck" else {}

    monkeypatch.setattr(runner, "post_json", post_json)
    monkeypatch.setattr(runner, "post_log", lambda phase, text: None)

    runner.main()

    assert uploads[0][0] == "/apply-precheck"
    assert uploads[0][1]["stderr"] == "precheck diff\n"
    assert uploads[1][0] == "/apply-result"
    assert uploads[1][1]["stderr"] == "apply output\n"
