import datetime as dt

import pytest

import src.db as db
import src.merge_service as merge_service
import src.models as models

NOW = dt.datetime(2024, 6, 1, 12, 0, 0)


def _engine():
    engine = db.get_engine("sqlite://")
    db.init_db(engine)
    return engine


def _add_story(session, *, title, url, source="A", published=NOW, votes=0, comments=0):
    story = models.Story(
        title=title,
        url=url,
        source_name=source,
        topic="ai",
        vote_count=votes,
        comment_count=comments,
        published_at=published,
        fetched_at=NOW,
    )
    session.add(story)
    session.flush()
    return story


def _add_comment(session, story_id, body="hi", user="u1"):
    comment = models.Comment(
        story_id=story_id,
        user_id=user,
        body=body,
        created_at=NOW,
    )
    session.add(comment)
    session.flush()
    return comment


# --- potential_duplicates ------------------------------------------------


def test_potential_duplicates_matches_similar_title():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(
        session, title="SpaceX launches new Starship rocket to orbit",
        url="https://hn.com/x",
    )
    similar = _add_story(
        session, title="SpaceX launches a new Starship rocket to orbit",
        url="https://reddit.com/y", source="Reddit",
    )
    _add_story(session, title="OpenAI ships a brand new language model", url="https://o.com/z")
    session.commit()

    candidates = merge_service.potential_duplicates(session, target.id, now=NOW)
    ids = [c["id"] for c in candidates]
    assert similar.id in ids
    assert len(candidates) == 1
    assert candidates[0]["similarity"] > merge_service.DEFAULT_SIMILARITY_THRESHOLD
    session.close()


def test_potential_duplicates_matches_url_variation():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="A headline here", url="https://x.com/a")
    dupe = _add_story(
        session, title="Completely unrelated words", url="https://www.x.com/a/?utm=1",
        source="B",
    )
    session.commit()
    candidates = merge_service.potential_duplicates(session, target.id, now=NOW)
    assert [c["id"] for c in candidates] == [dupe.id]
    assert candidates[0]["similarity"] == 1.0
    session.close()


def test_potential_duplicates_excludes_old_and_already_merged():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="Shared exact headline today", url="https://a.com/1")
    _add_story(
        session, title="Shared exact headline today", url="https://b.com/2", source="B",
        published=NOW - dt.timedelta(days=30),
    )
    merged = _add_story(
        session, title="Shared exact headline today", url="https://c.com/3", source="C",
    )
    merged.merge_status = "merged"
    merged.canonical_id = target.id
    session.commit()
    candidates = merge_service.potential_duplicates(session, target.id, now=NOW)
    assert candidates == []
    session.close()


def test_potential_duplicates_unknown_article_raises():
    engine = _engine()
    session = db.get_session(engine)
    with pytest.raises(merge_service.MergeError) as exc:
        merge_service.potential_duplicates(session, 999, now=NOW)
    assert exc.value.not_found
    session.close()


# --- merge_articles ------------------------------------------------------


def test_merge_transfers_votes_and_redirects_comments():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="Canonical story", url="https://a.com/1",
                        source="Hacker News", votes=3)
    source = _add_story(session, title="Canonical story copy", url="https://b.com/2",
                        source="Reddit", votes=5)
    c1 = _add_comment(session, source.id, body="great", user="alice")
    c2 = _add_comment(session, source.id, body="agreed", user="bob")
    session.commit()

    result = merge_service.merge_articles(session, source.id, target.id,
                                          merged_by="admin", now=NOW)
    assert result["vote_count_transferred"] == 5
    assert result["comments_transferred"] == 2

    session2 = db.get_session(engine)
    t = session2.get(models.Story, target.id)
    s = session2.get(models.Story, source.id)
    assert t.vote_count == 8
    assert s.vote_count == 0
    assert s.canonical_id == t.id
    assert s.merge_status == "merged"
    assert t.merge_status == "canonical"
    # comments now belong to the canonical target
    assert session2.get(models.Comment, c1.id).story_id == t.id
    assert session2.get(models.Comment, c2.id).story_id == t.id
    assert t.comment_count == 2
    assert s.comment_count == 0
    import json
    assert set(json.loads(t.merged_sources)) == {"Hacker News", "Reddit"}
    session2.close()


def test_merge_logs_audit_row():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="Story", url="https://a.com/1")
    source = _add_story(session, title="Story copy", url="https://b.com/2", votes=2)
    session.commit()
    result = merge_service.merge_articles(session, source.id, target.id,
                                          merged_by="mod7", now=NOW)
    log = merge_service.list_merges(session)
    assert len(log) == 1
    assert log[0]["merge_id"] == result["merge_id"]
    assert log[0]["merged_by"] == "mod7"
    assert log[0]["vote_count_transferred"] == 2
    assert log[0]["active"] is True
    session.close()


def test_merge_into_self_raises():
    engine = _engine()
    session = db.get_session(engine)
    story = _add_story(session, title="X", url="https://a.com/1")
    session.commit()
    with pytest.raises(merge_service.MergeError):
        merge_service.merge_articles(session, story.id, story.id, now=NOW)
    session.close()


def test_merge_already_merged_source_raises():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="X", url="https://a.com/1")
    source = _add_story(session, title="X copy", url="https://b.com/2")
    other = _add_story(session, title="X copy 2", url="https://c.com/3")
    session.commit()
    merge_service.merge_articles(session, source.id, target.id, now=NOW)
    with pytest.raises(merge_service.MergeError, match="already merged"):
        merge_service.merge_articles(session, source.id, other.id, now=NOW)
    session.close()


def test_merge_unknown_story_raises_not_found():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="X", url="https://a.com/1")
    session.commit()
    with pytest.raises(merge_service.MergeError) as exc:
        merge_service.merge_articles(session, 999, target.id, now=NOW)
    assert exc.value.not_found
    session.close()


# --- rollback_merge ------------------------------------------------------


def test_rollback_restores_votes_and_comments():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="Canonical", url="https://a.com/1", votes=3)
    source = _add_story(session, title="Canonical copy", url="https://b.com/2", votes=5)
    c1 = _add_comment(session, source.id, user="alice")
    session.commit()
    result = merge_service.merge_articles(session, source.id, target.id, now=NOW)

    undo = merge_service.rollback_merge(
        session, result["merge_id"], rolled_back_by="admin",
        now=NOW + dt.timedelta(hours=1),
    )
    assert undo["comments_restored"] == 1

    session2 = db.get_session(engine)
    t = session2.get(models.Story, target.id)
    s = session2.get(models.Story, source.id)
    assert t.vote_count == 3
    assert s.vote_count == 5
    assert s.canonical_id is None
    assert s.merge_status == "none"
    assert t.merge_status == "none"
    assert t.merged_sources is None
    assert session2.get(models.Comment, c1.id).story_id == source.id
    assert t.comment_count == 0
    assert s.comment_count == 1
    # audit row marked inactive
    assert merge_service.list_merges(session2)[0]["active"] is False
    session2.close()


def test_rollback_outside_window_raises():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="X", url="https://a.com/1")
    source = _add_story(session, title="X copy", url="https://b.com/2", votes=1)
    session.commit()
    result = merge_service.merge_articles(session, source.id, target.id, now=NOW)
    with pytest.raises(merge_service.MergeError, match="rollback window"):
        merge_service.rollback_merge(
            session, result["merge_id"],
            now=NOW + dt.timedelta(hours=merge_service.ROLLBACK_WINDOW_HOURS + 1),
        )
    session.close()


def test_rollback_twice_raises():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="X", url="https://a.com/1")
    source = _add_story(session, title="X copy", url="https://b.com/2", votes=1)
    session.commit()
    result = merge_service.merge_articles(session, source.id, target.id, now=NOW)
    merge_service.rollback_merge(session, result["merge_id"], now=NOW)
    with pytest.raises(merge_service.MergeError, match="already been rolled back"):
        merge_service.rollback_merge(session, result["merge_id"], now=NOW)
    session.close()


def test_rollback_unknown_merge_raises_not_found():
    engine = _engine()
    session = db.get_session(engine)
    with pytest.raises(merge_service.MergeError) as exc:
        merge_service.rollback_merge(session, 12345, now=NOW)
    assert exc.value.not_found
    session.close()


# --- merged_into / banner ------------------------------------------------


def test_merged_into_returns_canonical_for_merged_story():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="Canonical headline", url="https://a.com/1")
    source = _add_story(session, title="Canonical headline copy", url="https://b.com/2")
    session.commit()
    merge_service.merge_articles(session, source.id, target.id, now=NOW)
    banner = merge_service.merged_into(session, source.id)
    assert banner["id"] == target.id
    assert banner["title"] == "Canonical headline"
    assert merge_service.merged_into(session, target.id) is None
    session.close()


def test_list_merges_active_only_filters_rolled_back():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="X", url="https://a.com/1")
    s1 = _add_story(session, title="X copy 1", url="https://b.com/2", votes=1)
    s2 = _add_story(session, title="X copy 2", url="https://c.com/3", votes=2)
    session.commit()
    r1 = merge_service.merge_articles(session, s1.id, target.id, now=NOW)
    merge_service.merge_articles(session, s2.id, target.id, now=NOW)
    merge_service.rollback_merge(session, r1["merge_id"], now=NOW)
    # one rolled back -> target still canonical because s2 merge remains active
    active = merge_service.list_merges(session, active_only=True)
    assert len(active) == 1
    assert merge_service.list_merges(session)  # full log keeps both
    assert len(merge_service.list_merges(session)) == 2
    t = session.get(models.Story, target.id)
    assert t.merge_status == "canonical"
    session.close()


def test_list_merges_clamps_bad_limit():
    engine = _engine()
    session = db.get_session(engine)
    # A non-positive limit is a caller bug, not a domain error: it is clamped
    # to a sane value rather than raising a confusing MergeError.
    assert merge_service.list_merges(session, limit=0) == []
    assert merge_service.list_merges(session, limit=-5) == []
    session.close()


# --- partial rollback rebuilds merged_sources ----------------------------


def test_partial_rollback_keeps_other_merged_sources():
    engine = _engine()
    session = db.get_session(engine)
    target = _add_story(session, title="Canonical", url="https://a.com/1", source="HN")
    s1 = _add_story(session, title="Canonical copy 1", url="https://b.com/2", source="Reddit")
    s2 = _add_story(session, title="Canonical copy 2", url="https://c.com/3", source="Lobsters")
    session.commit()

    r1 = merge_service.merge_articles(session, s1.id, target.id, now=NOW)
    merge_service.merge_articles(session, s2.id, target.id, now=NOW)

    import json
    t = session.get(models.Story, target.id)
    assert set(json.loads(t.merged_sources)) == {"HN", "Reddit", "Lobsters"}

    # Roll back only the first merge: Reddit must drop out, the rest remain.
    merge_service.rollback_merge(session, r1["merge_id"], now=NOW + dt.timedelta(hours=1))
    session.expire_all()
    t = session.get(models.Story, target.id)
    assert t.merge_status == "canonical"
    assert set(json.loads(t.merged_sources)) == {"HN", "Lobsters"}
    assert "Reddit" not in t.merged_sources
    session.close()


# --- automatic detection on ingest ---------------------------------------


def test_flag_duplicates_on_ingest_queues_near_duplicate():
    engine = _engine()
    session = db.get_session(engine)
    existing = _add_story(
        session, title="SpaceX launches new Starship rocket to orbit",
        url="https://hn.com/x",
    )
    new = _add_story(
        session, title="SpaceX launches a new Starship rocket to orbit",
        url="https://reddit.com/y", source="Reddit",
    )
    session.commit()

    flagged = merge_service.flag_duplicates_on_ingest(session, new.id, now=NOW)
    assert [c["id"] for c in flagged] == [existing.id]

    queue = merge_service.list_duplicate_flags(session)
    assert len(queue) == 1
    assert queue[0]["story_id"] == new.id
    assert queue[0]["candidate_id"] == existing.id
    assert queue[0]["resolved"] is False
    session.close()


def test_flag_duplicates_on_ingest_no_match_queues_nothing():
    engine = _engine()
    session = db.get_session(engine)
    _add_story(session, title="OpenAI ships a new language model", url="https://o.com/a")
    new = _add_story(session, title="Boeing unveils a new airliner", url="https://b.com/b")
    session.commit()

    flagged = merge_service.flag_duplicates_on_ingest(session, new.id, now=NOW)
    assert flagged == []
    assert merge_service.list_duplicate_flags(session) == []
    session.close()


def test_flag_duplicates_on_ingest_is_idempotent():
    engine = _engine()
    session = db.get_session(engine)
    _add_story(session, title="Identical breaking headline today", url="https://a.com/1")
    new = _add_story(
        session, title="Identical breaking headline today", url="https://b.com/2", source="B",
    )
    session.commit()

    merge_service.flag_duplicates_on_ingest(session, new.id, now=NOW)
    merge_service.flag_duplicates_on_ingest(session, new.id, now=NOW)
    assert len(merge_service.list_duplicate_flags(session)) == 1
    session.close()


def test_flag_duplicates_on_ingest_missing_story_is_noop():
    engine = _engine()
    session = db.get_session(engine)
    assert merge_service.flag_duplicates_on_ingest(session, 999, now=NOW) == []
    session.close()


def test_approve_submission_flags_duplicate():
    import src.submissions as submissions

    engine = _engine()
    session = db.get_session(engine)
    # approve_submission stamps the new story with the real current time, so the
    # existing story must sit inside the lookback window relative to that.
    recent = dt.datetime.now(dt.timezone.utc)
    existing = _add_story(
        session, title="NASA delays Artemis launch again", url="https://nasa.com/a",
        published=recent,
    )
    sub = models.Submission(
        user_id="u1",
        title="NASA delays the Artemis launch again",
        url="https://space.com/b",
        category="aerospace",
        status="pending",
        created_at=NOW,
    )
    session.add(sub)
    session.commit()

    submissions.approve_submission(session, sub.id)
    queue = merge_service.list_duplicate_flags(session)
    assert len(queue) == 1
    assert queue[0]["candidate_id"] == existing.id
    session.close()


def test_merge_resolves_open_duplicate_flag():
    engine = _engine()
    session = db.get_session(engine)
    existing = _add_story(session, title="Shared headline now", url="https://a.com/1")
    new = _add_story(session, title="Shared headline now", url="https://b.com/2", source="B")
    session.commit()
    merge_service.flag_duplicates_on_ingest(session, new.id, now=NOW)

    merge_service.merge_articles(session, new.id, existing.id, now=NOW)
    assert merge_service.list_duplicate_flags(session, unresolved_only=True) == []
    assert len(merge_service.list_duplicate_flags(session, unresolved_only=False)) == 1
    session.close()
