import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_model_migration_roundtrip_preserves_existing_data(tmp_path: Path) -> None:
    database = tmp_path / "model-schema.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE projects (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("INSERT INTO projects VALUES ('keep-project')")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "sqlite+aiosqlite:///" + database.as_posix())
    command.stamp(config, "20261105_0015")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        names = {row[1] for row in connection.execute("PRAGMA table_info(model_profiles)")}
        assert "encrypted_key" in names and "api_key" not in names
        assert connection.execute("SELECT * FROM projects").fetchall() == [("keep-project",)]
    command.downgrade(config, "20261105_0015")
    with sqlite3.connect(database) as connection:
        assert not connection.execute("PRAGMA table_info(model_profiles)").fetchall()
        assert connection.execute("SELECT * FROM projects").fetchall() == [("keep-project",)]
    command.upgrade(config, "head")
