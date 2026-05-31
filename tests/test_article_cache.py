"""Tests for article full-text caching: extraction, fetch, store, prune, render."""

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

import src.db as db
import src.models as models
import src.scraper as scraper
from src.article_cache import (
    cache_story_content,
    extract_text,
    fetch_article,
    prune_cache,
)
from src.generate_site import _cached_block, render_story
from src.rss_generator import generate_rss

NOW = dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)


def _engine():
    engine = db.get_engine("sqlite://")
    db.init_db(engine)
    return engine


def _story(**kwargs) -> models.Story:
    base = dict(
        title="A Title",
        url="http://example.com/a",
        source_name="Src",
        topic="ai",
        raw_score=3,
        published_at=dt.datetime(2024, 1, 1),
        fetched_at=dt.datetime(2024, 1, 1),
    )
    base.update(kwargs)
    return models.Story(**base)


def _mock_response(text, ok=True):
    resp = MagicMock()
    resp.text = text
    if ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = Exception("404 Not Found")
    return resp


# --- extract_text ---------------------------------------------------------


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


def test_extract_text_empty_input():
    assert extract_text("") == ""


def test_extract_text_tolerates_malformed_html():
    # Unclosed tags must not raise; the visible text is still recovered.
    assert "Dangling" in extract_text("<div><p>Dangling text")


# --- fetch_article --------------------------------------------------------


def test_fetch_article_success():
    with patch("src.article_cache.requests.get") as mock_get:
        mock_get.return_value = _mock_response("<p>Body text</p>")
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
        mock_get.return_value = _mock_response("not found", ok=False)
        assert fetch_article("http://example.com/a") is None


# --- cache_story_content --------------------------------------------------


def test_cache_story_content_stores():
    story = _story()
    with patch("src.article_cache.requests.get") as mock_get:
        mock_get.return_value = _mock_response("<article>Deep content</article>")
        ok = cache_story_content(story, now=NOW)
    assert ok is True
    assert "Deep content" in story.cached_html
    assert story.cached_text == "Deep content"
    assert story.cache_timestamp == NOW


def test_cache_story_content_failure_is_noop():
    story = _story()
    with patch("src.article_cache.requests.get", side_effect=TimeoutError("x")):
        ok = cache_story_content(story)
    assert ok is False
    assert story.cached_html is None
    assert story.cached_text is None
    assert story.cache_timestamp is None


# --- scraper integration --------------------------------------------------


def test_insert_items_caches_when_enabled():
    engine = _engine()
    session = db.get_session(engine)
    source = scraper.ensure_source(session, scraper.DEFAULT_SOURCES[0])
    items = [
        scraper.NormalizedItem(
            title="GPT news", url="http://example.com/article", category="ai"
        )
    ]
    with patch("src.article_cache.requests.get") as mock_get:
        mock_get.return_value = _mock_response("<p>cached body</p>")
        inserted, skipped = scraper.insert_items(
            session, source, items, NOW, cache_content=True
        )
    session.commit()
    assert inserted == 1
    story = session.query(models.Story).filter_by(
        url="http://example.com/article"
    ).one()
    assert story.cached_text == "cached body"
    session.close()


def test_insert_items_cache_failure_does_not_break_sync():
    engine = _engine()
    session = db.get_session(engine)
    source = scraper.ensure_source(session, scraper.DEFAULT_SOURCES[0])
    items = [
        scraper.NormalizedItem(
            title="Mars rover", url="http://example.com/dead", category="aerospace"
        )
    ]
    with patch("src.article_cache.requests.get", side_effect=TimeoutError("x")):
        inserted, skipped = scraper.insert_items(
            session, source, items, NOW, cache_content=True
        )
    session.commit()
    assert inserted == 1
    story = session.query(models.Story).filter_by(url="http://example.com/dead").one()
    assert story.cached_text is None
    session.close()


def test_insert_items_does_not_fetch_by_default():
    engine = _engine()
    session = db.get_session(engine)
    source = scraper.ensure_source(session, scraper.DEFAULT_SOURCES[0])
    items = [
        scraper.NormalizedItem(
            title="No cache", url="http://example.com/plain", category="ai"
        )
    ]
    with patch("src.article_cache.requests.get") as mock_get:
        scraper.insert_items(session, source, items, NOW)
    session.commit()
    mock_get.assert_not_called()
    session.close()


# --- prune_cache ----------------------------------------------------------


def test_prune_cache_removes_old_keeps_recent():
    engine = _engine()
    session = db.get_session(engine)
    old = _story(url="http://example.com/old")
    recent = _story(url="http://example.com/recent")
    old.cached_html = "<p>old</p>"
    old.cached_text = "old"
    old.cache_timestamp = dt.datetime(2024, 1, 1)
    recent.cached_html = "<p>recent</p>"
    recent.cached_text = "recent"
    recent.cache_timestamp = dt.datetime(2024, 12, 1)
    session.add_all([old, recent])
    session.commit()

    pruned = prune_cache(session, dt.datetime(2024, 6, 1))
    assert pruned == 1
    assert old.cached_text is None
    assert old.cached_html is None
    assert old.cache_timestamp is None
    assert recent.cached_text == "recent"
    session.close()


def test_prune_cache_no_match_returns_zero():
    engine = _engine()
    session = db.get_session(engine)
    story = _story()
    story.cached_text = "keep"
    story.cache_timestamp = dt.datetime(2025, 1, 1)
    session.add(story)
    session.commit()

    assert prune_cache(session, dt.datetime(2024, 1, 1)) == 0
    assert story.cached_text == "keep"
    session.close()


# --- site generator -------------------------------------------------------


def test_cached_block_present_when_cached():
    story = _story()
    story.cached_text = "Archived prose"
    block = _cached_block(story)
    assert "View cached version" in block
    assert "Archived prose" in block
    assert "View source" in block


def test_cached_block_empty_when_not_cached():
    assert _cached_block(_story()) == ""


def test_render_story_includes_cached_toggle():
    story = _story()
    story.cached_text = "Archived prose"
    assert "View cached version" in render_story(story, 1)


def test_render_story_metadata_only_without_cache():
    assert "View cached version" not in render_story(_story(), 1)


def test_cached_block_escapes_html():
    story = _story()
    story.cached_text = "<script>alert(1)</script>"
    block = _cached_block(story)
    assert "<script>" not in block
    assert "&lt;script&gt;" in block


# --- RSS feed -------------------------------------------------------------


def test_rss_uses_cached_text_as_description():
    story = _story()
    story.cached_text = "Cached summary"
    assert "Cached summary" in generate_rss([story])


def test_rss_falls_back_to_score_without_cache():
    xml = generate_rss([_story(raw_score=5)])
    assert "points" in xml


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
