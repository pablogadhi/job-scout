"""Database engine and sessions. Only portable SQL here: Postgres must need just a new URL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Create an engine. SQLite files get WAL mode and enforced foreign keys."""
    parsed = make_url(url)
    is_sqlite = parsed.get_backend_name() == "sqlite"
    if is_sqlite and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if is_sqlite:
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


class SchemaOutdated(Exception):
    pass


def check_schema(engine: Engine) -> None:
    """Raise SchemaOutdated unless the database is at the latest Alembic revision."""
    config = Config()
    config.set_main_option("script_location", "job_scout:migrations")
    head = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current != head:
        raise SchemaOutdated(
            f"database is at revision {current or 'none'}, code expects {head}: "
            "run `alembic upgrade head`"
        )
