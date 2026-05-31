from datetime import datetime, timezone

import pytest

from src.db import get_engine, get_session, init_db
from src.generate_site import (
    _domain,
    fetch_stories,
    generate_site,
    group_by_topic,
    render_css,
    render_html,
    render_story,
)
from src.models import Story


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
        published_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
    )
    base.update(kwargs)
    return Story(**base)


def test_generate_site_writes_both_files(session, engine, tmp_path):
    session.add(
        _make_story(
            title="Big AI breakthrough",
            url="https://www.openai.com/news",
            topic="ai",
            vote_count=42,
        )
    )
    session.commit()

    out = generate_site(engine=engine, out_dir=tmp_path / "docs")

    index = (out / "index.html").read_text(encoding="utf-8")
    css = (out / "style.css").read_text(encoding="utf-8")
    assert "Big AI breakthrough" in index
    assert "https://www.openai.com/news" in index
    assert "42 points" in index
    assert "(openai.com)" in index  # www. stripped
    assert index.startswith("<!DOCTYPE html>")
    assert 'link rel="stylesheet" href="style.css"' in index
    assert "ff6600" in css  # HN-orange accent present
    assert "@media" in css  # responsive rule present
    assert "http://" not in index.replace("https://www.openai.com/news", "")


def test_stories_sorted_by_score_then_recency(session):
    session.add_all(
        [
            _make_story(
                url="https://a/1",
                computed_score=1.0,
                published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            _make_story(
                url="https://a/2",
                computed_score=9.0,
                published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            _make_story(
                url="https://a/3",
                computed_score=9.0,
                published_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
        ]
    )
    session.commit()

    ordered = fetch_stories(session)
    assert [s.url for s in ordered] == [
        "https://a/3",  # score 9, newer
        "https://a/2",  # score 9, older
        "https://a/1",  # score 1
    ]


def test_group_by_topic_orders_known_topics_first(session):
    stories = [
        _make_story(url="https://a/z", topic="zeta"),
        _make_story(url="https://a/aero", topic="aerospace"),
        _make_story(url="https://a/ai", topic="ai"),
    ]
    grouped = group_by_topic(stories)
    assert list(grouped.keys()) == ["ai", "aerospace", "zeta"]


def test_empty_database_produces_valid_page(session, engine, tmp_path):
    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    index = (out / "index.html").read_text(encoding="utf-8")
    assert index.startswith("<!DOCTYPE html>")
    assert "No stories yet." in index
    assert (out / "style.css").exists()


def test_output_is_deterministic(session, engine, tmp_path):
    session.add(_make_story(url="https://a/det"))
    session.commit()

    first = generate_site(engine=engine, out_dir=tmp_path / "one")
    second = generate_site(engine=engine, out_dir=tmp_path / "two")
    assert (first / "index.html").read_bytes() == (second / "index.html").read_bytes()
    assert (first / "style.css").read_bytes() == (second / "style.css").read_bytes()


def test_html_escaping_prevents_injection():
    story = _make_story(
        title='<script>alert("x")</script>',
        url='https://evil/"onmouseover="x',
    )
    html = render_story(story, 1)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'onmouseover="x' not in html or "&quot;" in html


def test_creates_nested_output_directory(session, engine, tmp_path):
    nested = tmp_path / "deep" / "nested" / "docs"
    out = generate_site(engine=engine, out_dir=nested)
    assert (out / "index.html").exists()


def test_domain_returns_empty_for_unparseable_url():
    assert _domain("not a url") == ""
    assert _domain("") == ""


def test_render_story_falls_back_to_raw_score_without_votes():
    story = _make_story(vote_count=0, raw_score=7)
    assert "7 points" in render_story(story, 1)


def test_render_html_is_string_and_self_contained():
    html = render_html({})
    assert isinstance(html, str)
    # All scripts are external references; no inline script bodies.
    assert html.count("<script") == html.count("<script src=")
    assert '<script src="filter.js"></script>' in html
    assert '<script src="search.js"></script>' in html
    assert "http://" not in html and "https://" not in html
    assert isinstance(render_css(), str)
