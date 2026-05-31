"""Tests for the profile/leaderboard site generation and the SQL-backed
``get_user_articles`` rewrite (reviewer-requested follow-ups)."""

from datetime import datetime, timezone

import pytest

from src.db import get_engine, get_session, init_db
from src.generate_site import (
    generate_site,
    render_html,
    render_leaderboard_js,
    render_leaderboard_page,
    render_story,
    render_user_js,
    render_user_page,
)
from src.models import Comment, Story, Vote
from src.profiles import ProfileError, get_user_articles


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


def _story(**kwargs) -> Story:
    base = dict(
        title="A Title",
        url="https://example.com/a",
        source_name="hn",
        topic="ai",
        published_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
    )
    base.update(kwargs)
    return Story(**base)


def test_generate_site_writes_profile_and_leaderboard_assets(session, engine, tmp_path):
    session.add(_story(title="X", url="https://x.test/1"))
    session.commit()

    out = generate_site(engine=engine, out_dir=tmp_path / "docs")

    for name in ("user.html", "user.js", "leaderboard.html", "leaderboard.js"):
        assert (out / name).exists(), name
    assert "id=\"profile\"" in (out / "user.html").read_text()
    assert "id=\"leaderboard\"" in (out / "leaderboard.html").read_text()


def test_leaderboard_nav_link_present_on_index():
    html = render_html({})
    assert 'href="leaderboard.html"' in html


def test_story_row_links_submitter_to_profile():
    story = _story(title="By me", url="https://m.test/1", submitted_by="alice")
    html = render_story(story, 1)
    assert 'href="user.html?u=alice"' in html
    assert ">alice</a>" in html


def test_story_row_omits_author_when_no_submitter():
    story = _story(url="https://m.test/2", submitted_by=None)
    assert "user.html?u=" not in render_story(story, 1)


def test_comments_js_links_author_names():
    js = render_comments_js_text()
    assert "user.html?u=" in js


def render_comments_js_text() -> str:
    from src.generate_site import render_comments_js

    return render_comments_js()


def test_static_pages_reference_their_scripts():
    assert 'src="user.js"' in render_user_page()
    assert 'src="leaderboard.js"' in render_leaderboard_page()
    assert "/api/users/" in render_user_js()
    assert "/api/users/leaderboard" in render_leaderboard_js()


def test_get_user_articles_orders_submitted_and_upvoted_by_timestamp(session):
    s1 = _story(
        title="Submitted",
        url="https://s.test/1",
        submitted_by="bob",
        fetched_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    s2 = _story(title="Upvoted", url="https://s.test/2")
    session.add_all([s1, s2])
    session.commit()
    session.add(
        Vote(
            story_id=s2.id,
            user_id="bob",
            vote_value=1,
            created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
    )
    session.commit()

    result = get_user_articles(session, "bob")
    assert result["total"] == 2
    # Upvote (April) is newer than submission (March) -> appears first.
    assert [i["activity"] for i in result["items"]] == ["upvoted", "submitted"]
    assert result["items"][0]["title"] == "Upvoted"


def test_get_user_articles_paginates_at_db_layer(session):
    for i in range(5):
        s = _story(
            title=f"S{i}",
            url=f"https://p.test/{i}",
            submitted_by="carol",
            fetched_at=datetime(2026, 1, 1, 0, i, tzinfo=timezone.utc),
        )
        session.add(s)
    session.commit()

    page1 = get_user_articles(session, "carol", page=1, per_page=2)
    page2 = get_user_articles(session, "carol", page=2, per_page=2)
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    ids1 = {i["story_id"] for i in page1["items"]}
    ids2 = {i["story_id"] for i in page2["items"]}
    assert ids1.isdisjoint(ids2)


def test_get_user_articles_ignores_downvotes(session):
    s = _story(url="https://d.test/1")
    session.add(s)
    session.commit()
    session.add(
        Vote(
            story_id=s.id,
            user_id="dave",
            vote_value=-1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    # Give dave a comment so the user "exists" for visibility checks.
    session.add(
        Comment(
            story_id=s.id,
            user_id="dave",
            body="hi",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    session.commit()

    result = get_user_articles(session, "dave")
    assert result["total"] == 0
    assert result["items"] == []


def test_get_user_articles_large_history_paginates(session):
    """Many upvotes return a bounded window, not the whole set (no in-memory load)."""
    stories = [_story(url=f"https://big.test/{i}") for i in range(50)]
    session.add_all(stories)
    session.commit()
    for i, st in enumerate(stories):
        session.add(
            Vote(
                story_id=st.id,
                user_id="erin",
                vote_value=1,
                created_at=datetime(2026, 1, 1, 0, 0, i, tzinfo=timezone.utc),
            )
        )
    session.commit()

    result = get_user_articles(session, "erin", page=1, per_page=10)
    assert result["total"] == 50
    assert len(result["items"]) == 10
    # Newest upvote first.
    assert result["items"][0]["story_id"] == stories[-1].id


def test_get_user_articles_unknown_user_raises(session):
    with pytest.raises(ProfileError):
        get_user_articles(session, "nobody")
