from pathlib import Path

from alembic import command
from alembic.config import Config

from stablehand.config import get_settings
from stablehand.db import reset_engine


def test_alembic_schema_matches_model_metadata(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'schema.db'}"
    monkeypatch.setenv("STABLEHAND_DATABASE_URL", database_url)
    get_settings.cache_clear()
    reset_engine()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))

    try:
        command.upgrade(config, "head")
        command.check(config)
    finally:
        reset_engine()
        get_settings.cache_clear()
