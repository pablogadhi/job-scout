"""Alembic environment.

The database URL comes from `sqlalchemy.url` when set (tests do this), otherwise from
settings.yaml with the usual env var overrides (JOB_SCOUT_DATABASE_URL,
JOB_SCOUT_PATHS__DATABASE). The config directory is JOB_SCOUT_CONFIG_DIR, default ./config.
"""

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context

from job_scout.config import load_settings_only
from job_scout.db import make_engine
from job_scout.db.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def _database_url() -> str:
    if url := config.get_main_option("sqlalchemy.url"):
        return url
    config_dir = Path(os.environ.get("JOB_SCOUT_CONFIG_DIR", "config"))
    return load_settings_only(config_dir).db_url


def _configure(**kwargs: object) -> None:
    url = kwargs.pop("url", None) or _database_url()
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=str(url).startswith("sqlite"),  # SQLite can't ALTER most things
        compare_type=True,
        url=url if "connection" not in kwargs else None,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _database_url()
    engine = make_engine(url)
    with engine.connect() as connection:
        _configure(url=url, connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
