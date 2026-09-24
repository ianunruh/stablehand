import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from stablehand.config import get_settings
from stablehand.db import reset_engine


def test_upgrade_shares_source_and_runtime_and_keeps_ref_overrides(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'migrate.db'}"
    monkeypatch.setenv("STABLEHAND_DATABASE_URL", database_url)
    get_settings.cache_clear()
    reset_engine()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    stacks = sa.table(
        "stacks",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("deploy_file", sa.String()),
        sa.column("inventory", sa.String()),
        sa.column("executor", sa.String()),
        sa.column("git_url", sa.String()),
        sa.column("git_ref", sa.String()),
        sa.column("local_path", sa.String()),
        sa.column("secret_ref", sa.String()),
        sa.column("git_ssh_key_encrypted", sa.Text()),
    )
    web_id = uuid.uuid4()
    db_id = uuid.uuid4()
    try:
        command.upgrade(config, "0004_git_deploy_key")
        with create_engine(database_url).begin() as connection:
            connection.execute(
                stacks.insert(),
                [
                    {
                        "id": web_id,
                        "name": "web",
                        "deploy_file": "deploy.py",
                        "inventory": "inventory.py",
                        "executor": "kubernetes",
                        "git_url": "git@github.com:org/kcloud-ops.git",
                        "git_ref": "main",
                        "local_path": None,
                        "secret_ref": "host-ssh",
                        "git_ssh_key_encrypted": "ciphertext",
                    },
                    {
                        "id": db_id,
                        "name": "db",
                        "deploy_file": "db.py",
                        "inventory": "inventory.py",
                        "executor": "kubernetes",
                        "git_url": "git@github.com:org/kcloud-ops.git",
                        "git_ref": "release",
                        "local_path": None,
                        "secret_ref": "host-ssh",
                        "git_ssh_key_encrypted": "ciphertext",
                    },
                ],
            )
        command.upgrade(config, "head")
        with create_engine(database_url).connect() as connection:
            sources = connection.execute(sa.text("SELECT name, git_ref FROM sources")).all()
            runtimes = connection.execute(
                sa.text("SELECT name, executor, secret_ref FROM runtimes")
            ).all()
            refs = dict(connection.execute(sa.text("SELECT name, git_ref FROM stacks")).all())
        assert sources == [("kcloud-ops", "main")]
        assert runtimes == [("Kubernetes (host-ssh)", "kubernetes", "host-ssh")]
        assert refs == {"web": None, "db": "release"}
    finally:
        reset_engine()
        get_settings.cache_clear()
