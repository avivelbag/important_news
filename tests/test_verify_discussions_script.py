"""Tests for the scheduled scripts/verify_discussions.py job."""

import datetime as dt

import pytest

from sqlalchemy import func, select

from scripts.verify_discussions import verify
from src.db import get_engine, get_session, init_db
from src.models import ExternalDiscussion, Story

NOW = dt.datetime(2026, 5, 31, 12, 0)


@pytest.fixture()
def engine():
    eng = get_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


def _seed(engine):
    session = get_session(engine)
    story = Story(
        title="t",
        url="https://example.com/a",
        source_name="HN",
        topic="ai",
        published_at=NOW,
        fetched_at=NOW,
    )
    session.add(story)
    session.commit()
    session.add(
        ExternalDiscussion(
            story_id=story.id,
            platform="reddit",
            url="https://reddit.com/x",
            title="t",
            comment_count=1,
            discovered_at=NOW,
        )
    )
    session.commit()
    session.close()


def test_default_verifier_keeps_links(engine):
    _seed(engine)
    summary = verify(engine)
    assert summary == {"verified": 1, "removed": 0, "errors": 0}


def test_custom_verifier_prunes_dead(engine):
    _seed(engine)
    summary = verify(engine, verify_fn=lambda d: None)
    assert summary == {"verified": 0, "removed": 1, "errors": 0}
    session = get_session(engine)
    assert session.scalar(select(func.count(ExternalDiscussion.id))) == 0
    session.close()


def test_verify_empty_db_is_noop(engine):
    summary = verify(engine)
    assert summary == {"verified": 0, "removed": 0, "errors": 0}
