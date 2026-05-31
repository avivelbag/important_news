from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import sessionmaker

from src.article_cache import (
    cache_story_content,
    extract_text,
    fetch_article,
    prune_cache,
)
from src.db import init_db
from src.generate_site import _cached_block
from src.models import Source, Story
from src.rss_generator import _rss_item
from src.scraper import store_stories


@pytest.fixture
def session():
    engine = init_db("sqlite:///:memory:")
    return sessionmaker(bind=engine)()


def make_story(session, url="http://example.com/a", **kwargs):
    story = Story(
        title=kwargs.get("title", "Test"),
        url=url,
        source_name=kwargs.get("source_name", "Test Source"),
        topic=kwargs.get("topic", "ai"),
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    session.add(story)
    session.commit()
    return story


def mock_response(text, status_ok=True):
    resp = MagicMock()
    resp.text = text
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = Exception("404 Not Found")
    return resp


def test_extract_text_strips_tags():
    html = "<html><body><h1>Hello</h1><p>World</p></body></html>"
    text = extract_text(html)
    assert "Hello" in text
    assert "World" in text
    assert "<" not in text


def test_extract_text_drops_script_and_style():
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><script>var bad=1;</script><p>Visible</p></body></html>"
    )
    text = extract_text(html)
    assert "Visible" in text
    assert "bad" not in text
    assert "color" not in text


def test_extract_text_empty():
    assert extract_text("") == ""


def test_fetch_article_success():
    with patch("src.article_cache.requests.get") as mock_get:
        mock_get.return_value = mock_response("<p>Body text</p>")
        result = fetch_article("http://example.com/a")
    assert result is not None
    html, text = result
    assert "Body text" in html
    assert text == "Body text"


def test_fetch_article_timeout_returns_none():
    with patch("src.article_cache.requests.get", side_effect=TimeoutError("slow")):
        assert fetch_article("http://example.com/a") is None


def test_fetch_article_http_error_returns_none():
    with patch("src.article_cache.requests.get") as mock_get:
        mock_get.return_value = mock_response("not found", status_ok=False)
        assert fetch_article("http://example.com/a") is None


def test_cache_story_content_stores(session):
    story = make_story(session)
    now = datetime(2024, 6, 1, tzinfo=timezone.utc)
    with patch("src.article_cache.requests.get") as mock_get:
        mock_get.return_value = mock_response("<article>Deep content</article>")
        ok = cache_story_content(session, story, now=now)
    assert ok is True
    assert "Deep content" in story.cached_html
    assert story.cached_text == "Deep content"
    assert story.cache_timestamp == now


def test_cache_story_content_failure_is_noop(session):
    story = make_story(session)
    with patch("src.article_cache.requests.get", side_effect=TimeoutError("x")):
        ok = cache_story_content(session, story)
    assert ok is False
    assert story.cached_html is None
    assert story.cached_text is None
    assert story.cache_timestamp is None


def test_store_stories_caches_when_enabled(session):
    source = Source(name="Test", url="http://example.com/feed")
    session.add(source)
    session.commit()
    entry = MagicMock()
    entry.link = "http://example.com/article"
    entry.title = "GPT news"
    entry.published_parsed = None
    with patch("src.article_cache.requests.get") as mock_get:
        mock_get.return_value = mock_response("<p>cached body</p>")
        count = store_stories(session, source, [entry], cache_content=True)
    assert count == 1
    story = session.query(Story).filter_by(url="http://example.com/article").first()
    assert story.cached_text == "cached body"


def test_store_stories_cache_failure_does_not_break_sync(session):
    source = Source(name="Test", url="http://example.com/feed")
    session.add(source)
    session.commit()
    entry = MagicMock()
    entry.link = "http://example.com/dead"
    entry.title = "Mars rover"
    entry.published_parsed = None
    with patch("src.article_cache.requests.get", side_effect=TimeoutError("x")):
        count = store_stories(session, source, [entry], cache_content=True)
    assert count == 1
    story = session.query(Story).filter_by(url="http://example.com/dead").first()
    assert story.cached_text is None


def test_prune_cache_removes_old_keeps_recent(session):
    old = make_story(session, url="http://example.com/old")
    recent = make_story(session, url="http://example.com/recent")
    old.cached_html = "<p>old</p>"
    old.cached_text = "old"
    old.cache_timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    recent.cached_html = "<p>recent</p>"
    recent.cached_text = "recent"
    recent.cache_timestamp = datetime(2024, 12, 1, tzinfo=timezone.utc)
    session.commit()

    cutoff = datetime(2024, 6, 1, tzinfo=timezone.utc)
    pruned = prune_cache(session, cutoff)
    assert pruned == 1
    assert old.cached_text is None
    assert old.cache_timestamp is None
    assert recent.cached_text == "recent"


def test_prune_cache_no_match_returns_zero(session):
    story = make_story(session)
    story.cached_text = "keep"
    story.cache_timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
    session.commit()
    cutoff = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert prune_cache(session, cutoff) == 0
    assert story.cached_text == "keep"


def test_cached_block_present_when_cached():
    story = Story(
        title="T",
        url="http://example.com/a",
        source_name="S",
        topic="ai",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    story.cached_text = "Archived prose"
    block = _cached_block(story)
    assert "View cached version" in block
    assert "Archived prose" in block


def test_cached_block_empty_when_not_cached():
    story = Story(
        title="T",
        url="http://example.com/a",
        source_name="S",
        topic="ai",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    assert _cached_block(story) == ""


def test_rss_item_includes_description_when_cached():
    story = Story(
        title="T",
        url="http://example.com/a",
        source_name="S",
        topic="ai",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    story.cached_text = "Cached summary"
    item = _rss_item(story)
    assert "<description>Cached summary</description>" in item


def test_rss_item_omits_description_when_uncached():
    story = Story(
        title="T",
        url="http://example.com/a",
        source_name="S",
        topic="ai",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    item = _rss_item(story)
    assert "<description>" not in item
