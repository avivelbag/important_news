from datetime import datetime, timezone

import pytest

from src.db import get_engine, get_session, init_db
from src.generate_site import (
    generate_site,
    group_by_topic,
    render_feed_js,
    render_html,
)
from src.models import Story


@pytest.fixture()
def engine():
    eng = get_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


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


def test_homepage_includes_personalized_scaffold():
    html = render_html(group_by_topic([_make_story()]))
    assert 'id="personalized-feed"' in html
    assert 'id="global-feed"' in html
    assert 'src="feed.js"' in html


def test_homepage_has_view_all_and_algorithm_controls():
    html = render_html(group_by_topic([_make_story()]))
    assert "View all stories" in html
    for algo in ("balanced", "trending", "recent", "followed"):
        assert f'data-algo="{algo}"' in html
    assert 'id="toggle-feed"' in html


def test_personalized_section_hidden_by_default():
    html = render_html(group_by_topic([_make_story()]))
    assert '<section id="personalized-feed" class="personalized" hidden>' in html


def test_feed_js_calls_endpoint_and_falls_back_for_anonymous():
    js = render_feed_js()
    assert "/api/user/feed" in js
    assert "data.user_id == null" in js
    assert 'fetch("/api/user/preferences"' in js


def test_empty_homepage_still_renders_personalized_scaffold():
    html = render_html({})
    assert 'id="personalized-feed"' in html
    assert "No stories yet." in html


def test_generate_site_writes_feed_js(engine, tmp_path):
    session = get_session(engine)
    session.add(_make_story())
    session.commit()
    session.close()

    out = generate_site(engine, out_dir=tmp_path)
    feed_js = out / "feed.js"
    assert feed_js.exists()
    assert "/api/user/feed" in feed_js.read_text()
    index = (out / "index.html").read_text()
    assert 'src="feed.js"' in index
