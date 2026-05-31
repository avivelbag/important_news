from datetime import datetime, timezone

import pytest

from src.db import get_engine, get_session, init_db
from src.generate_site import (
    generate_site,
    render_html,
    render_js,
    render_section,
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


def test_nav_renders_three_filter_buttons():
    html = render_html({})
    assert 'nav class="filters"' in html
    for value, label in (("all", "All"), ("ai", "AI"), ("aerospace", "Aerospace")):
        assert f'data-filter="{value}"' in html
        assert f">{label}</button>" in html


def test_sections_carry_data_topic_attribute():
    section = render_section("aerospace", [_make_story(topic="aerospace")])
    assert 'data-topic="aerospace"' in section
    assert 'class="topic"' in section


def test_unknown_topic_is_emitted_as_data_topic_without_crashing():
    section = render_section("zeta", [_make_story(topic="zeta")])
    assert 'data-topic="zeta"' in section


def test_html_references_external_filter_script_once():
    html = render_html({"ai": [_make_story()]})
    assert html.count('<script src="filter.js"></script>') == 1
    # All scripts are external (have a src); none are inlined.
    assert html.count("<script") == html.count("<script src=")


def test_generate_site_writes_filter_js(session, engine, tmp_path):
    session.add(_make_story())
    session.commit()
    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    js = (out / "filter.js").read_text(encoding="utf-8")
    assert "addEventListener" in js
    assert "category-filter" in js


def test_filter_js_persists_state_via_hash_and_localstorage():
    js = render_js()
    assert "window.location.hash" in js
    assert "window.localStorage.getItem" in js
    assert "window.localStorage.setItem" in js
    assert 'window.addEventListener("hashchange"' in js


def test_filter_js_treats_both_topic_as_ai_and_aerospace():
    js = render_js()
    assert 'if (topic === "both") return filter === "ai" || filter === "aerospace";' in js
    assert 'if (filter === "all") return true;' in js


def test_filter_js_localstorage_access_is_guarded():
    # Private-mode browsers throw on localStorage access; guards keep filtering alive.
    js = render_js()
    assert "try {" in js
    assert "} catch (e) {}" in js


def test_filter_js_is_deterministic():
    assert render_js() == render_js()


def test_empty_feed_still_has_filters_and_script():
    html = render_html({})
    assert 'data-filter="ai"' in html
    assert '<script src="filter.js"></script>' in html
    assert "No stories yet." in html


def test_generate_site_writes_three_assets(session, engine, tmp_path):
    out = generate_site(engine=engine, out_dir=tmp_path / "docs")
    assert (out / "index.html").exists()
    assert (out / "style.css").exists()
    assert (out / "filter.js").exists()
