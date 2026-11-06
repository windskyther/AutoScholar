import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_usage_migration_does_not_reclassify_or_change_existing_model_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "usage-migration.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE model_profiles (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("INSERT INTO model_profiles VALUES ('keep-model')")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "sqlite+aiosqlite:///" + database.as_posix())
    command.stamp(config, "20261106_0016")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM model_profiles").fetchall() == [("keep-model",)]
        assert connection.execute("SELECT count(*) FROM model_usage").fetchone() == (0,)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(model_usage)")}
        assert not columns.intersection({"api_key", "base_url", "prompt", "answer", "content"})
    command.downgrade(config, "20261106_0016")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM model_profiles").fetchall() == [("keep-model",)]
    command.upgrade(config, "head")
