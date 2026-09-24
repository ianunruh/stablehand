# Stablehand

Stablehand runs a pyinfra check, waits for a person to approve it, then applies that commit.

## Run locally

```sh
docker compose up --build
```

Open http://localhost:8000 and sign in as `admin@example.com` / `changeme`. A demo stack is created for `examples/demo`, which targets `@local`.

Start a check from the stack page. The worker runs `pyinfra --dry --diff --json`, and the run page lists hosts that would change. Approve it to apply. The person who started the run can approve it. A stack approver list, when set, still limits who may approve.

CI opens a check and can read status, not diffs:

```sh
curl -X POST http://localhost:8000/api/stacks/$STACK_ID/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"commit": "local", "limit": "web-*, canary"}'
```

Omit `limit` to inherit the stack default, or send an empty string to target the full inventory.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
./scripts/build-css.sh
pytest
ruff format --check .
ruff check .
```

Set `STABLEHAND_DATABASE_URL` to a Postgres URL before `alembic upgrade head`.

Kubernetes stacks run `ghcr.io/ianunruh/stablehand:main` as a Job. GitHub Actions publishes that tag from `main`. See [deploy/kubernetes.yaml](deploy/kubernetes.yaml). The worker service account can create Jobs and Secrets in `stablehand-runners`. Runner pods do not mount a service account token. Provide Postgres and the app settings, including `STABLEHAND_RUNNER_API_URL`, in the `stablehand` secret.
