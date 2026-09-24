"""Move shared git and executor settings onto source and runtime records."""

import uuid
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_sources_and_runtimes"
down_revision: str | None = "0004_git_deploy_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("git_url", sa.String(length=1000), nullable=True),
        sa.Column("git_ref", sa.String(length=255), nullable=True),
        sa.Column("local_path", sa.String(length=1000), nullable=True),
        sa.Column("git_ssh_key_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("name", name="uq_sources_name"),
    )
    op.create_table(
        "runtimes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("executor", sa.String(length=32), nullable=False),
        sa.Column("secret_ref", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runtimes"),
        sa.UniqueConstraint("name", name="uq_runtimes_name"),
    )
    op.add_column("stacks", sa.Column("source_id", sa.Uuid(), nullable=True))
    op.add_column("stacks", sa.Column("runtime_id", sa.Uuid(), nullable=True))
    _backfill()
    with op.batch_alter_table("stacks") as batch:
        batch.alter_column("source_id", nullable=False)
        batch.alter_column("runtime_id", nullable=False)
        batch.create_foreign_key(
            "fk_stacks_source_id_sources",
            "sources",
            ["source_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_stacks_runtime_id_runtimes",
            "runtimes",
            ["runtime_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.drop_column("executor")
        batch.drop_column("git_url")
        batch.drop_column("local_path")
        batch.drop_column("secret_ref")
        batch.drop_column("git_ssh_key_encrypted")


def downgrade() -> None:
    raise NotImplementedError


def _backfill() -> None:
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.text(
                "SELECT id, git_url, git_ref, local_path, secret_ref, "
                "git_ssh_key_encrypted, executor FROM stacks"
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return
    sources = sa.table(
        "sources",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("git_url", sa.String()),
        sa.column("git_ref", sa.String()),
        sa.column("local_path", sa.String()),
        sa.column("git_ssh_key_encrypted", sa.Text()),
    )
    runtimes = sa.table(
        "runtimes",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("executor", sa.String()),
        sa.column("secret_ref", sa.String()),
    )
    stacks = sa.table(
        "stacks",
        sa.column("id", sa.Uuid()),
        sa.column("source_id", sa.Uuid()),
        sa.column("runtime_id", sa.Uuid()),
        sa.column("git_ref", sa.String()),
    )
    source_groups: dict[tuple, list] = defaultdict(list)
    runtime_groups: dict[tuple, list] = defaultdict(list)
    for row in rows:
        source_groups[
            (row["git_url"], row["local_path"], row["git_ssh_key_encrypted"] or "")
        ].append(row)
        runtime_groups[(row["executor"], row["secret_ref"])].append(row)
    source_ids: dict[tuple, uuid.UUID] = {}
    source_refs: dict[tuple, str | None] = {}
    source_names: set[str] = set()
    for key, group in source_groups.items():
        git_url, local_path, encrypted = key
        source_id = uuid.uuid4()
        source_ref = _source_ref(git_url, {item["git_ref"] for item in group})
        source_ids[key] = source_id
        source_refs[key] = source_ref
        connection.execute(
            sources.insert().values(
                id=source_id,
                name=_unique_name(_source_label(git_url, local_path), source_names),
                git_url=git_url,
                git_ref=source_ref,
                local_path=local_path,
                git_ssh_key_encrypted=encrypted,
            )
        )
    runtime_ids: dict[tuple, uuid.UUID] = {}
    runtime_names: set[str] = set()
    for key, _group in runtime_groups.items():
        executor, secret_ref = key
        runtime_id = uuid.uuid4()
        runtime_ids[key] = runtime_id
        connection.execute(
            runtimes.insert().values(
                id=runtime_id,
                name=_unique_name(_runtime_label(executor, secret_ref), runtime_names),
                executor=executor,
                secret_ref=secret_ref,
            )
        )
    for row in rows:
        source_key = (row["git_url"], row["local_path"], row["git_ssh_key_encrypted"] or "")
        source_ref = source_refs[source_key]
        connection.execute(
            stacks.update()
            .where(stacks.c.id == _uuid(row["id"]))
            .values(
                source_id=source_ids[source_key],
                runtime_id=runtime_ids[(row["executor"], row["secret_ref"])],
                git_ref=None if row["git_ref"] == source_ref else row["git_ref"],
            )
        )


def _source_ref(git_url: str | None, refs: set) -> str | None:
    if len(refs) == 1:
        only = next(iter(refs))
        if git_url and not only:
            return "main"
        return only
    if git_url:
        return "main"
    return None


def _source_label(git_url: str | None, local_path: str | None) -> str:
    if git_url:
        value = git_url.strip().rstrip("/")
        if "://" not in value and ":" in value:
            value = value.split(":", 1)[1]
        label = value.split("/")[-1]
        if label.endswith(".git"):
            label = label[: -len(".git")]
        return label or "repository"
    path = (local_path or "").rstrip("/")
    return path.split("/")[-1] or "local"


def _runtime_label(executor: str | None, secret_ref: str | None) -> str:
    if executor == "kubernetes":
        label = "Kubernetes"
    elif executor == "local":
        label = "Local"
    else:
        label = (executor or "runtime").capitalize()
    if secret_ref:
        label = f"{label} ({secret_ref})"
    return label


def _unique_name(label: str, used: set[str]) -> str:
    base = label.strip()[:200] or "record"
    candidate = base
    number = 2
    while candidate in used:
        suffix = f" {number}"
        candidate = f"{base[: 200 - len(suffix)]}{suffix}"
        number += 1
    used.add(candidate)
    return candidate


def _uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))
