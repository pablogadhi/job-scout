from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import StatementError

from job_scout.db import make_engine, make_session_factory
from job_scout.db.models import Base, Job, Score, ScoreStatus, Source

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'nested' / 'test.db'}"
    cfg = Config(ROOT / "alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    yield engine
    engine.dispose()


def test_migration_creates_all_tables(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables


def test_migration_matches_models(engine: Engine) -> None:
    """Fails when a model changes without a new Alembic revision."""
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    assert diff == []


def test_sqlite_pragmas(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def _job(**kwargs: object) -> Job:
    defaults: dict[str, object] = dict(
        source_id="remoteok",
        source_job_id="1",
        canonical_url="https://example.com/1",
        title="Senior Backend Engineer",
        title_normalized="senior backend engineer",
        raw={"id": 1},
        dedupe_key="k1",
    )
    return Job(**{**defaults, **kwargs})


def test_roundtrip_and_utc(engine: Engine) -> None:
    session_factory = make_session_factory(engine)
    posted = datetime(2026, 10, 1, 9, 0, tzinfo=timezone(timedelta(hours=-6)))
    with session_factory.begin() as session:
        session.add(Source(id="remoteok"))
        session.flush()
        session.add(_job(posted_at=posted, tags=["python", "django"]))
    with session_factory() as session:
        job = session.query(Job).one()
        assert job.posted_at == posted
        assert job.posted_at.tzinfo == UTC
        assert job.tags == ["python", "django"]
        assert job.first_seen_at.tzinfo == UTC
        session.add(Score(job_id=job.id, profile_hash="h"))
        session.commit()
        assert session.query(Score).one().status is ScoreStatus.PENDING


def test_naive_datetime_rejected(engine: Engine) -> None:
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        session.add(Source(id="remoteok", last_run_at=datetime(2026, 10, 1)))
        with pytest.raises(StatementError, match="naive datetime"):
            session.commit()


def test_foreign_keys_enforced(engine: Engine) -> None:
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        session.add(_job(source_id="no-such-source"))
        with pytest.raises(Exception, match="FOREIGN KEY"):
            session.commit()
