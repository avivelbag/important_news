"""Tests for SQLAlchemy ORM models and database helpers."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from src.db import get_engine, get_session, init_db
from src.models import Source, Story, Vote


@pytest.fixture()
def engine():
    """In-memory SQLite engine with schema initialised."""
    eng = get_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    """Session bound to the in-memory engine, rolled back after each test."""
    sess = get_session(engine)
    yield sess
    sess.close()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_story_persist_and_read_back(session):
    """Create a Story, flush it, read it back, and assert all field values."""
    now = _utcnow()
    story = Story(
        title="GPT-5 outperforms humans on ARC-AGI",
        url="https://example.com/gpt5",
        source_name="Hacker News",
        topic="ai",
        raw_score=42,
        computed_score=3.14,
        published_at=now,
        fetched_at=now,
    )
    session.add(story)
    session.flush()

    fetched = session.get(Story, story.id)
    assert fetched is not None
    assert fetched.title == "GPT-5 outperforms humans on ARC-AGI"
    assert fetched.url == "https://example.com/gpt5"
    assert fetched.source_name == "Hacker News"
    assert fetched.topic == "ai"
    assert fetched.raw_score == 42
    assert fetched.computed_score == pytest.approx(3.14)
    assert fetched.vote_count == 0


def test_vote_persists_with_story_fk(session):
    """Vote FK resolves back to the parent Story."""
    now = _utcnow()
    story = Story(
        title="SpaceX Starship reaches orbit",
        url="https://example.com/starship",
        source_name="NASA",
        topic="aerospace",
        published_at=now,
        fetched_at=now,
    )
    session.add(story)
    session.flush()

    vote = Vote(story_id=story.id, created_at=now, ip_hash="abc123")
    session.add(vote)
    session.flush()

    assert vote.story.id == story.id
    assert vote.ip_hash == "abc123"


def test_source_persist_and_read_back(session):
    """Create a Source record and verify it round-trips correctly."""
    src = Source(name="Hacker News", url="https://news.ycombinator.com")
    session.add(src)
    session.flush()

    fetched = session.get(Source, src.id)
    assert fetched is not None
    assert fetched.name == "Hacker News"
    assert fetched.url == "https://news.ycombinator.com"


def test_story_defaults(session):
    """Unspecified numeric fields default to zero / 0.0."""
    now = _utcnow()
    story = Story(
        title="Default field test",
        url="https://example.com/defaults",
        source_name="Test",
        topic="both",
        published_at=now,
        fetched_at=now,
    )
    session.add(story)
    session.flush()

    fetched = session.get(Story, story.id)
    assert fetched.raw_score == 0
    assert fetched.vote_count == 0
    assert fetched.computed_score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_url_uniqueness_constraint(session):
    """Inserting two stories with the same URL raises IntegrityError."""
    now = _utcnow()
    url = "https://example.com/duplicate"

    session.add(Story(
        title="First story",
        url=url,
        source_name="src",
        topic="ai",
        published_at=now,
        fetched_at=now,
    ))
    session.flush()

    session.add(Story(
        title="Duplicate URL story",
        url=url,
        source_name="src",
        topic="ai",
        published_at=now,
        fetched_at=now,
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_all_topic_values_accepted(session):
    """All three valid topic strings are stored and retrieved without error."""
    now = _utcnow()
    for i, topic in enumerate(["ai", "aerospace", "both"]):
        story = Story(
            title=f"Story {i}",
            url=f"https://example.com/story-{i}",
            source_name="src",
            topic=topic,
            published_at=now,
            fetched_at=now,
        )
        session.add(story)
    session.flush()

    from sqlalchemy import select
    rows = session.execute(select(Story.topic)).scalars().all()
    assert set(rows) == {"ai", "aerospace", "both"}


def test_vote_without_ip_hash(session):
    """ip_hash is nullable — a Vote without it must persist successfully."""
    now = _utcnow()
    story = Story(
        title="Anonymous vote story",
        url="https://example.com/anon-vote",
        source_name="src",
        topic="ai",
        published_at=now,
        fetched_at=now,
    )
    session.add(story)
    session.flush()

    vote = Vote(story_id=story.id, created_at=now)
    session.add(vote)
    session.flush()

    fetched = session.get(Vote, vote.id)
    assert fetched.ip_hash is None


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_story_missing_required_field_raises(session):
    """Omitting a required field (title) must raise an error on flush."""
    now = _utcnow()
    story = Story(
        # title intentionally omitted
        url="https://example.com/no-title",
        source_name="src",
        topic="ai",
        published_at=now,
        fetched_at=now,
    )
    session.add(story)
    with pytest.raises(Exception):
        session.flush()


def test_vote_invalid_story_fk_raises(session):
    """A Vote referencing a non-existent story_id must fail the FK constraint."""
    vote = Vote(story_id=99999, created_at=_utcnow())
    session.add(vote)
    with pytest.raises(Exception):
        session.flush()


def test_source_name_uniqueness(session):
    """Two Sources with the same name violate the UNIQUE constraint."""
    session.add(Source(name="Duplicate", url="https://a.com"))
    session.flush()
    session.add(Source(name="Duplicate", url="https://b.com"))
    with pytest.raises(IntegrityError):
        session.flush()
