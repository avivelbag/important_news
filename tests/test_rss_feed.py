"""Tests for RSS 2.0 feed generation and static feed export."""

from datetime import datetime
from xml.etree import ElementTree as ET

import pytest

from src.db import get_engine, get_session, init_db
from src.generate_site import generate_site, write_feeds
from src.models import Story
from src.rss_generator import generate_rss, story_in_category


@pytest.fixture()
def engine():
    eng = get_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    sess = get_session(engine)
    yield sess
    sess.close()


def _make_story(**kwargs) -> Story:
    base = dict(
        title="A Title",
        url="https://example.com/article",
        source_name="hn",
        topic="ai",
        raw_score=10,
        vote_count=5,
        computed_score=1.0,
        published_at=datetime(2026, 1, 2, 3, 4),
        fetched_at=datetime(2026, 1, 2, 3, 5),
    )
    base.update(kwargs)
    return Story(**base)


def _sample() -> list[Story]:
    return [
        _make_story(title="Older AI", url="http://a.test/1", source_name="Src A",
                    topic="ai", published_at=datetime(2024, 1, 1)),
        _make_story(title="Newer AI", url="http://a.test/2", source_name="Src A",
                    topic="ai", published_at=datetime(2024, 3, 1)),
        _make_story(title="Aerospace one", url="http://b.test/3", source_name="Src B",
                    topic="aerospace", published_at=datetime(2024, 2, 1)),
        _make_story(title="Crossover", url="http://c.test/4", source_name="Src C",
                    topic="both", published_at=datetime(2024, 2, 15)),
    ]


def test_generate_rss_is_valid_rss2():
    root = ET.fromstring(generate_rss(_sample()))
    assert root.tag == "rss"
    assert root.attrib["version"] == "2.0"
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("title") == "Important News"
    assert channel.find("link") is not None
    assert channel.find("description") is not None


def test_item_includes_required_fields():
    root = ET.fromstring(generate_rss(_sample()))
    item = root.find("channel/item")
    assert item.findtext("title")
    assert item.findtext("link").startswith("http")
    assert item.find("description") is not None
    assert item.findtext("category") in ("ai", "aerospace", "both")
    assert item.findtext("source")  # source attribution present
    assert item.findtext("pubDate")


def test_guid_is_article_url_permalink():
    root = ET.fromstring(generate_rss(_sample()))
    for item in root.findall("channel/item"):
        guid = item.find("guid")
        assert guid.attrib["isPermaLink"] == "true"
        assert guid.text == item.findtext("link")


def test_items_in_descending_publish_order():
    root = ET.fromstring(generate_rss(_sample()))
    titles = [i.findtext("title") for i in root.findall("channel/item")]
    assert titles == ["Newer AI", "Crossover", "Aerospace one", "Older AI"]


def test_category_filter_reduces_items_and_includes_both():
    root = ET.fromstring(generate_rss(_sample(), category="ai"))
    titles = {i.findtext("title") for i in root.findall("channel/item")}
    # "both" stories surface under ai; aerospace-only does not.
    assert titles == {"Older AI", "Newer AI", "Crossover"}
    assert "Aerospace one" not in titles


def test_category_filter_unknown_yields_empty_feed():
    root = ET.fromstring(generate_rss(_sample(), category="business"))
    assert root.findall("channel/item") == []
    # Channel metadata must still be present even with zero items.
    assert root.findtext("channel/title") == "Important News"


def test_empty_stories_produces_valid_feed_without_builddate():
    root = ET.fromstring(generate_rss([]))
    assert root.find("channel") is not None
    assert root.findall("channel/item") == []
    assert root.find("channel/lastBuildDate") is None


def test_special_characters_are_escaped():
    story = _make_story(title="A & B <c>", url="http://x.test/?a=1&b=2", topic="ai")
    xml = generate_rss([story])
    assert "<title>A & B <c>" not in xml
    # Must round-trip through a strict parser without raising.
    root = ET.fromstring(xml)
    assert root.findtext("channel/item/title") == "A & B <c>"
    assert root.findtext("channel/item/link") == "http://x.test/?a=1&b=2"


def test_build_date_is_deterministic_newest_story():
    feed_a = generate_rss(_sample())
    feed_b = generate_rss(_sample())
    assert feed_a == feed_b
    root = ET.fromstring(feed_a)
    assert "2024" in root.findtext("channel/lastBuildDate")


def test_naive_pubdate_rendered_as_utc():
    story = _make_story(published_at=datetime(2024, 5, 1, 12, 0))
    root = ET.fromstring(generate_rss([story]))
    pub = root.findtext("channel/item/pubDate")
    assert pub.endswith("+0000")
    assert "01 May 2024 12:00:00" in pub


def test_story_in_category_helper():
    assert story_in_category("ai", "ai")
    assert story_in_category("both", "ai")
    assert story_in_category("both", "aerospace")
    assert not story_in_category("aerospace", "ai")
    assert not story_in_category("ai", "business")


def test_write_feeds_creates_main_and_category_files(session, engine, tmp_path):
    paths = write_feeds(tmp_path, _sample())
    assert (tmp_path / "feed.rss").exists()
    assert (tmp_path / "feed-ai.rss").exists()
    assert (tmp_path / "feed-aerospace.rss").exists()
    assert (tmp_path / "feed.rss") in paths
    # Every written file must be valid XML.
    for path in paths:
        ET.fromstring(path.read_text(encoding="utf-8"))


def test_write_feeds_skips_categories_without_stories(tmp_path):
    only_ai = [_make_story(url="http://a/1", topic="ai")]
    write_feeds(tmp_path, only_ai)
    assert (tmp_path / "feed-ai.rss").exists()
    assert not (tmp_path / "feed-aerospace.rss").exists()


def test_generate_site_writes_feed_and_discovery_link(session, engine, tmp_path):
    session.add(_make_story(title="Big AI story", url="https://x/news", topic="ai"))
    session.commit()
    out = generate_site(engine=engine, out_dir=tmp_path / "docs")

    feed = out / "feed.rss"
    assert feed.exists()
    root = ET.fromstring(feed.read_text(encoding="utf-8"))
    assert root.findtext("channel/item/title") == "Big AI story"

    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'rel="alternate"' in index
    assert 'type="application/rss+xml"' in index
    assert 'href="feed.rss"' in index


def test_generate_site_empty_db_writes_valid_feed(session, engine, tmp_path):
    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    feed = out / "feed.rss"
    assert feed.exists()
    root = ET.fromstring(feed.read_text(encoding="utf-8"))
    assert root.findall("channel/item") == []
