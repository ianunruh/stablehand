"""Add pyinfra stack and run limits."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_pyinfra_run_limits"
down_revision: str | None = "0002_atomic_run_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stacks", sa.Column("default_limit", sa.String(length=1000), nullable=True))
    op.add_column("runs", sa.Column("limit", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "limit")
    op.drop_column("stacks", "default_limit")
