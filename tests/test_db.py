from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, insert, inspect, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session, sessionmaker

from job_scout.db import make_engine, make_session_factory
from job_scout.db.models import Base, Job, Score, ScoreStatus, Source
from tests.conftest import ROOT
from tests.scoring_helpers import seed_jobs


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


def test_roundtrip_and_utc(session_factory: sessionmaker[Session]) -> None:
    posted = datetime(2026, 10, 1, 9, 0, tzinfo=timezone(timedelta(hours=-6)))
    with session_factory.begin() as session:
        session.add(Source(id="remoteok"))
        session.flush()
        session.add(_job(posted_at=posted, tags=["python", "django"]))
    with session_factory() as session:
        job = session.query(Job).one()
        assert job.posted_at == posted
        assert job.posted_at is not None and job.posted_at.tzinfo == UTC
        assert job.tags == ["python", "django"]
        assert job.first_seen_at.tzinfo == UTC
        session.add(Score(job_id=job.id, profile_hash="h"))
        session.commit()
        assert session.query(Score).one().status is ScoreStatus.PENDING


def test_naive_datetime_rejected(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        session.add(Source(id="remoteok", last_run_at=datetime(2026, 10, 1)))
        with pytest.raises(StatementError, match="naive datetime"):
            session.commit()


def test_foreign_keys_enforced(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        session.add(_job(source_id="no-such-source"))
        with pytest.raises(Exception, match="FOREIGN KEY"):
            session.commit()


def _insert_old_scores(
    sf: sessionmaker[Session], rows: list[tuple[int, str, ScoreStatus, datetime | None]]
) -> None:
    """Score rows in an older schema: only the columns it had (the model has more now)."""
    with sf.begin() as session:
        session.execute(
            insert(Base.metadata.tables["scores"]),
            [
                {
                    "job_id": job_id,
                    "profile_hash": profile_hash,
                    "status": status,
                    "scored_at": scored_at,
                    "attempts": 0,
                    "created_at": datetime(2026, 9, 30, tzinfo=UTC),
                }
                for job_id, profile_hash, status, scored_at in rows
            ],
        )


def test_one_score_per_job_migration_keeps_the_best_row(tmp_path: Path) -> None:
    """Rows from older profiles collapse to one per job: the latest real score, else the
    newest pending row."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "ddc39a2e53b6")  # one score per (job, profile_hash)
    engine = make_engine(url)
    sf = make_session_factory(engine)
    a, b, c = seed_jobs(sf, 3)
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    rows = [
        (a, "h1", ScoreStatus.SCORED, t0),
        (a, "h2", ScoreStatus.PENDING, None),
        (b, "h1", ScoreStatus.SCORED, t0),
        (b, "h2", ScoreStatus.SCORED, t0 + timedelta(days=1)),
        (c, "h1", ScoreStatus.PENDING, None),
        (c, "h2", ScoreStatus.PENDING, None),
    ]
    _insert_old_scores(sf, rows)
    engine.dispose()
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    with make_session_factory(engine)() as session:
        kept = {s.job_id: (s.profile_hash, s.status) for s in session.query(Score)}
    engine.dispose()
    assert kept == {
        a: ("h1", ScoreStatus.SCORED),  # a real score beats a pending row
        b: ("h2", ScoreStatus.SCORED),  # the latest score
        c: ("h2", ScoreStatus.PENDING),  # no score: the newest pending row
    }


def test_points_migration_keeps_when_a_job_was_first_scored(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'old.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "cae2ac4fd837")  # before the points score
    engine = make_engine(url)
    sf = make_session_factory(engine)
    a, b = seed_jobs(sf, 2)
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    _insert_old_scores(sf, [(a, "h", ScoreStatus.SCORED, t0), (b, "h", ScoreStatus.PENDING, None)])
    engine.dispose()
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    with make_session_factory(engine)() as session:
        first = {s.job_id: s.first_scored_at for s in session.query(Score)}
        assert all(s.breakdown is None for s in session.query(Score))
    engine.dispose()
    assert first == {a: t0, b: None}
