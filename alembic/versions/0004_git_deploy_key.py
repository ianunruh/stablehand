"""Store an encrypted git deploy key on each stack."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_git_deploy_key"
down_revision: str | None = "0003_pyinfra_run_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stacks",
        sa.Column("git_ssh_key_encrypted", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("stacks", "git_ssh_key_encrypted")
