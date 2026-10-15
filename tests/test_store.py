from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_scout.db.models import Job, Source
from job_scout.fetch.models import Posting
from job_scout.fetch.store import store_postings

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
KEY = ["company_normalized", "title_normalized"]


def posting(
    job_id: str, title: str = "Senior Backend Engineer", company: str | None = "Acme Inc."
) -> Posting:
    return Posting(
        source_job_id=job_id,
        canonical_url=f"https://board.example/{job_id}",
        title=title,
        company=company,
        raw={"id": job_id},
    )


def setup_sources(session_factory: sessionmaker[Session], *ids: str) -> None:
    with session_factory.begin() as session:
        session.add_all(Source(id=i) for i in ids)


def test_html_escaped_names_merge_with_plain_ones(
    session_factory: sessionmaker[Session],
) -> None:
    escaped = posting("1", "Senior R&amp;D Engineer", "O&#39;Brien &amp; Co")
    assert (escaped.title, escaped.company) == ("Senior R&D Engineer", "O'Brien & Co")
    setup_sources(session_factory, "a", "b")
    with session_factory.begin() as session:
        store_postings(session, "a", [escaped], KEY, NOW)
        result = store_postings(
            session, "b", [posting("2", "Senior R&D Engineer", "O'Brien & Co")], KEY, NOW
        )
    assert (result.new, result.duplicate) == (0, 1)


def test_a_title_can_be_matched_on_a_shorter_one(session_factory: sessionmaker[Session]) -> None:
    header = "Acme Inc. | Senior Backend Engineer | REMOTE (US) | Full-time"
    hn = posting("1", header).model_copy(update={"dedupe_title": "Senior Backend Engineer"})
    setup_sources(session_factory, "hn", "board")
    with session_factory.begin() as session:
        store_postings(session, "hn", [hn], KEY, NOW)
        result = store_postings(session, "board", [posting("2")], KEY, NOW)
        stored = session.scalars(select(Job).where(Job.source_id == "hn")).one()
    assert (result.new, result.duplicate) == (0, 1)
    assert stored.title == header  # only the match key uses the short title


def test_new_seen_and_cross_posted(session_factory: sessionmaker[Session]) -> None:
    setup_sources(session_factory, "a", "b")
    with session_factory.begin() as session:
        first = store_postings(session, "a", [posting("1"), posting("2", "Staff SRE")], KEY, NOW)
    assert (first.new, first.duplicate, first.seen) == (2, 0, 0)

    later = NOW + timedelta(hours=7)
    with session_factory.begin() as session:
        again = store_postings(session, "a", [posting("1"), posting("2", "Staff SRE")], KEY, later)
        # Same job on another board, title decorated differently.
        cross = store_postings(
            session, "b", [posting("x", "Sr. Back-End Engineer (Remote)", "ACME, Inc")], KEY, later
        )
    assert (again.new, again.duplicate, again.seen) == (0, 0, 2)
    assert (cross.new, cross.duplicate) == (0, 1)

    with session_factory() as session:
        jobs = {(j.source_id, j.source_job_id): j for j in session.scalars(select(Job))}
    original, copy = jobs["a", "1"], jobs["b", "x"]
    assert copy.duplicate_of == original.id
    assert original.duplicate_of is None
    assert original.first_seen_at == NOW and original.last_seen_at == later
    assert copy.company_normalized == "acme" and copy.title_normalized == "senior backend engineer"


def test_same_id_twice_in_one_batch(session_factory: sessionmaker[Session]) -> None:
    """A job listed in two of one source's feeds is stored once."""
    setup_sources(session_factory, "a")
    with session_factory.begin() as session:
        result = store_postings(session, "a", [posting("1"), posting("1")], KEY, NOW)
    assert (result.new, result.duplicate) == (1, 0)


def test_duplicates_within_one_batch_link_to_first(session_factory: sessionmaker[Session]) -> None:
    setup_sources(session_factory, "a")
    with session_factory.begin() as session:
        result = store_postings(session, "a", [posting("1"), posting("2"), posting("3")], KEY, NOW)
    assert (result.new, result.duplicate) == (1, 2)
    with session_factory() as session:
        links = session.execute(select(Job.source_job_id, Job.duplicate_of).order_by(Job.id)).all()
    first_id = session_factory().scalar(select(Job.id).where(Job.source_job_id == "1"))
    assert links == [("1", None), ("2", first_id), ("3", first_id)]


def test_missing_company_never_merges(session_factory: sessionmaker[Session]) -> None:
    setup_sources(session_factory, "a", "b")
    with session_factory.begin() as session:
        store_postings(session, "a", [posting("1", company=None)], KEY, NOW)
        result = store_postings(session, "b", [posting("2", company=None)], KEY, NOW)
    assert (result.new, result.duplicate) == (1, 0)
