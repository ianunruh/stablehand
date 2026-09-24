"""Add atomic run lifecycle constraints and integration deliveries."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_atomic_run_lifecycle"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_RUN_PREDICATE = (
    "state IN ('apply_queued', 'apply_running', 'check_queued', 'check_running', 'needs_approval')"
)
ACTIVE_EXECUTION_PREDICATE = "status IN ('pending', 'running')"


def upgrade() -> None:
    connection = op.get_bind()
    duplicate_run = connection.execute(
        sa.text(
            f"SELECT stack_id FROM runs WHERE {ACTIVE_RUN_PREDICATE} "
            "GROUP BY stack_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_run is not None:
        raise RuntimeError("Resolve duplicate active runs before applying this migration.")
    duplicate_execution = connection.execute(
        sa.text(
            f"SELECT run_id, phase FROM executions WHERE {ACTIVE_EXECUTION_PREDICATE} "
            "GROUP BY run_id, phase HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_execution is not None:
        raise RuntimeError("Resolve duplicate active executions before applying this migration.")

    op.create_index(
        "uq_runs_one_active_per_stack",
        "runs",
        ["stack_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_RUN_PREDICATE),
        sqlite_where=sa.text(ACTIVE_RUN_PREDICATE),
    )
    op.create_index(
        "uq_executions_one_active_per_phase",
        "executions",
        ["run_id", "phase"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_EXECUTION_PREDICATE),
        sqlite_where=sa.text(ACTIVE_EXECUTION_PREDICATE),
    )
    op.create_table(
        "integration_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name="fk_integration_deliveries_integration_id_integrations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name="fk_integration_deliveries_run_id_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_deliveries"),
        sa.UniqueConstraint(
            "integration_id",
            "run_id",
            "event",
            "fingerprint",
            name="uq_integration_deliveries_dedupe",
        ),
    )
    op.create_index(
        "ix_integration_deliveries_due",
        "integration_deliveries",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_integration_deliveries_due", table_name="integration_deliveries")
    op.drop_table("integration_deliveries")
    op.drop_index("uq_executions_one_active_per_phase", table_name="executions")
    op.drop_index("uq_runs_one_active_per_stack", table_name="runs")
