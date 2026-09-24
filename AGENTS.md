# Stablehand

A web process serves the UI and APIs. A worker process owns schedules and execution. Both use one synchronous SQLAlchemy 2 session style against Postgres (`psycopg`). Route handlers that touch the database are plain `def` functions so FastAPI runs them in a threadpool. Do not introduce async SQLAlchemy.

HTTP requests and worker use cases own transaction boundaries. Domain services mutate or flush but never commit. A UI route that catches a domain error and returns normally must roll back first.

## Concurrency and durable effects

Postgres is the source of truth for lifecycle coordination. Enforce invariants with constraints or partial unique indexes, not application-level read-then-write checks. Change run state with compare-and-set updates (`UPDATE ... WHERE state = expected`) and treat a zero row count as a lost race. Keep the state change and its related database writes, such as token mutation, execution intent, or outbox enqueue, in one transaction.

External systems cannot participate in a database transaction. Persist durable intent and commit before starting a subprocess, Kubernetes Job, or HTTP request; perform the side effect outside a transaction; then record its outcome in a new short transaction. Give retryable effects stable identities and idempotent recovery. Local subprocess starts are not safely retryable after an uncertain result.

Deliver integrations through a transactional outbox with at-least-once semantics. Never make inline webhook calls from lifecycle services. Consumers must be able to deduplicate by delivery ID.

Any new concurrency-sensitive invariant needs a PostgreSQL concurrency test. SQLite remains useful for fast behavior tests but is not evidence that locking, uniqueness, or compare-and-set behavior is correct.

The runner (`stablehand-runner`) is the only code that shells out to pyinfra. `plans/normalize.py` is the only module that knows pyinfra's JSON shape. Templates and integrations see the normalized document.

## Run lifecycle

One run, one URL. Check is `pyinfra <inventory> <deploy> --dry --diff --json`. Apply repeats the check at the pinned commit and returns to `needs_approval` when the change-set fingerprint differs; otherwise it runs with `--yes`. Schedules open checks only. One active run per stack.

The person who triggered a run may approve it. An empty stack approver list means any member. CI tokens (`shc_`) can open a run and read status, not diffs or approval. Run tokens (`shr_`) upload logs and the result for one phase.

## UI and auth

Jinja macros in `src/stablehand/templates/components/` are the components. There is no JavaScript component library. HTMX is vendored at `src/stablehand/static/htmx.min.js`. Tailwind is compiled by `./scripts/build-css.sh` (standalone CLI, no Node). `app.css` is gitignored; the Docker build compiles it and Hatch includes it via `artifacts`.

CSRF is the `X-CSRF-Token` header only. Do not read the form body in middleware; that consumes it before the route. `base.html` sends the header with `hx-boost`. `/login`, `/healthz`, `/api/*`, and safe methods skip the check.

Humans use a session cookie. OIDC users are created disabled until an admin enables them. Settings use the `STABLEHAND_` env prefix.

## Commands

Ruff is the formatter and linter (`ruff format`, `ruff check`). Do not add Black, isort, or Flake8. Tests are `pytest` with `pythonpath` of `src` and `.`.

In Compose, only the web service runs `alembic upgrade head`. The worker waits until web is healthy. Do not commit `.tailwindcss` or `.venv`; the host Tailwind binary is the wrong architecture inside the image.

SQLite tests return naive datetimes. Compare them with `security.aware()`.
