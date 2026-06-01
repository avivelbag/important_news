import datetime as dt
import json

from sqlalchemy import select

from src.deduplicator import normalize_url, title_similarity
from src.models import ArticleMerge, Comment, DuplicateCandidate, Story

# A near-duplicate candidate must clear this title similarity to be surfaced for
# merging; mirrors the deduplicator's automatic threshold.
DEFAULT_SIMILARITY_THRESHOLD = 0.8

# How far back to scan for potential duplicates of a story (breaking-news clones
# arrive within days, so a 7-day window keeps the candidate set small).
DEFAULT_LOOKBACK_DAYS = 7

# A merge may be undone only within this many hours of being made; after that it
# is considered settled and rollback is refused.
ROLLBACK_WINDOW_HOURS = 24


class MergeError(ValueError):
    # ``not_found`` distinguishes a missing story/merge (HTTP 404) from a
    # bad-input or rule violation (HTTP 400) so the API can pick a status code
    # without inspecting the message text.
    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_naive_utc(value: dt.datetime) -> dt.datetime:
    # SQLite drops tzinfo, so a merged_at read back from the DB is naive while a
    # freshly produced _now() is tz-aware; normalise both before subtracting so
    # the rollback-window check never crashes on a naive/aware mismatch.
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def _get_story(session, story_id: int) -> Story:
    story = session.get(Story, story_id)
    if story is None:
        raise MergeError(f"story {story_id} does not exist", not_found=True)
    return story


def _recount_comments(session, story: Story) -> None:
    story.comment_count = len(
        [c for c in session.scalars(
            select(Comment).where(Comment.story_id == story.id)
        ).all() if not c.deleted]
    )


def _rebuild_merged_sources(session, target: Story) -> list[str]:
    # Rebuild the canonical's contributing-source list purely from its own
    # source name plus the source story of every *currently active* merge that
    # targets it. We never read ``target.merged_sources`` here: it is derived
    # state that may still list a source whose merge was just rolled back, so
    # deriving from the live merge rows is the only way a partial rollback drops
    # exactly the undone source and keeps the rest.
    merges = session.scalars(
        select(ArticleMerge).where(
            ArticleMerge.target_article_id == target.id,
            ArticleMerge.active.is_(True),
        )
    ).all()

    names: list[str] = []
    if target.source_name and target.source_name not in names:
        names.append(target.source_name)
    for merge in merges:
        source = session.get(Story, merge.source_article_id)
        if source is None:
            continue
        existing = json.loads(source.merged_sources) if source.merged_sources else []
        for name in [*existing, source.source_name]:
            if name and name not in names:
                names.append(name)
    return names


def potential_duplicates(
    session,
    story_id: int,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    now: dt.datetime | None = None,
) -> list[dict]:
    if now is None:
        now = _now()
    target = _get_story(session, story_id)
    target_url = normalize_url(target.url)

    # Push the time window into SQL so we only load stories published within
    # ``lookback_days`` of the target, never the whole table. Title similarity
    # itself (SequenceMatcher) cannot be expressed in SQL, so the surviving rows
    # are still scored in Python.
    window = dt.timedelta(days=lookback_days)
    query = select(Story).where(
        Story.id != target.id,
        Story.canonical_id.is_(None),
        Story.merge_status != "merged",
        Story.published_at >= target.published_at - window,
        Story.published_at <= target.published_at + window,
    )

    candidates = []
    for other in session.scalars(query).all():
        similarity = title_similarity(target.title, other.title)
        url_match = bool(target_url) and normalize_url(other.url) == target_url
        if not url_match and similarity < threshold:
            continue
        candidates.append(
            {
                "id": other.id,
                "title": other.title,
                "url": other.url,
                "source": other.source_name,
                "similarity": 1.0 if url_match else round(similarity, 3),
                "vote_count": other.vote_count,
                "comment_count": other.comment_count,
                "published_at": other.published_at.isoformat()
                if other.published_at
                else None,
            }
        )

    candidates.sort(key=lambda c: (-c["similarity"], c["id"]))
    return candidates


def flag_duplicates_on_ingest(
    session,
    story_id: int,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    now: dt.datetime | None = None,
) -> list[dict]:
    # Automatic detection step run when a new story enters the system (see
    # src/submissions.py::approve_submission). It reuses the same similarity
    # check the admin lookup uses, but persists each near-duplicate pair into the
    # detection queue (``DuplicateCandidate``) so an admin can review and merge
    # later. Already-queued, unresolved pairs are skipped so re-ingesting the
    # same story does not pile up rows. Never raises for a missing story — ingest
    # must not fail just because detection cannot run — returning ``[]`` instead.
    if now is None:
        now = _now()
    if session.get(Story, story_id) is None:
        return []

    candidates = potential_duplicates(
        session, story_id, lookback_days=lookback_days, threshold=threshold, now=now
    )

    flagged = []
    for candidate in candidates:
        already = session.scalars(
            select(DuplicateCandidate).where(
                DuplicateCandidate.story_id == story_id,
                DuplicateCandidate.candidate_id == candidate["id"],
                DuplicateCandidate.resolved.is_(False),
            )
        ).first()
        if already is not None:
            continue
        flag = DuplicateCandidate(
            story_id=story_id,
            candidate_id=candidate["id"],
            similarity=candidate["similarity"],
            detected_at=now,
            resolved=False,
        )
        session.add(flag)
        flagged.append(candidate)

    if flagged:
        session.commit()
    return flagged


def list_duplicate_flags(
    session, *, unresolved_only: bool = True, limit: int = 100
) -> list[dict]:
    limit = max(1, min(limit, 500))
    query = select(DuplicateCandidate)
    if unresolved_only:
        query = query.where(DuplicateCandidate.resolved.is_(False))
    rows = session.scalars(
        query.order_by(
            DuplicateCandidate.detected_at.desc(), DuplicateCandidate.id.desc()
        ).limit(limit)
    ).all()

    result = []
    for flag in rows:
        story = session.get(Story, flag.story_id)
        candidate = session.get(Story, flag.candidate_id)
        result.append(
            {
                "flag_id": flag.id,
                "story_id": flag.story_id,
                "story_title": story.title if story else None,
                "candidate_id": flag.candidate_id,
                "candidate_title": candidate.title if candidate else None,
                "similarity": flag.similarity,
                "resolved": flag.resolved,
                "detected_at": flag.detected_at.isoformat()
                if flag.detected_at
                else None,
            }
        )
    return result


def merge_articles(
    session,
    source_id: int,
    target_id: int,
    merged_by: str | None = None,
    *,
    now: dt.datetime | None = None,
) -> dict:
    if now is None:
        now = _now()
    if source_id == target_id:
        raise MergeError("cannot merge a story into itself")

    source = _get_story(session, source_id)
    target = _get_story(session, target_id)

    if source.merge_status == "merged" or source.canonical_id is not None:
        raise MergeError(f"story {source_id} is already merged")
    if target.canonical_id is not None:
        raise MergeError(
            f"target {target_id} is itself merged into {target.canonical_id}"
        )

    votes_transferred = source.vote_count or 0
    target.vote_count = (target.vote_count or 0) + votes_transferred
    source.vote_count = 0

    moved_comment_ids = []
    for comment in session.scalars(
        select(Comment).where(Comment.story_id == source.id)
    ).all():
        comment.story_id = target.id
        moved_comment_ids.append(comment.id)

    source.canonical_id = target.id
    source.merge_status = "merged"
    target.merge_status = "canonical"

    # Any open detection-queue entries pairing these two are now settled.
    for flag in session.scalars(
        select(DuplicateCandidate).where(
            DuplicateCandidate.story_id.in_([source.id, target.id]),
            DuplicateCandidate.candidate_id.in_([source.id, target.id]),
            DuplicateCandidate.resolved.is_(False),
        )
    ).all():
        flag.resolved = True

    merge = ArticleMerge(
        source_article_id=source.id,
        target_article_id=target.id,
        merged_by=merged_by,
        merged_at=now,
        vote_count_transferred=votes_transferred,
        transferred_comment_ids=json.dumps(moved_comment_ids),
        active=True,
    )
    session.add(merge)
    session.flush()

    target.merged_sources = json.dumps(_rebuild_merged_sources(session, target))
    _recount_comments(session, source)
    _recount_comments(session, target)
    session.commit()

    return {
        "merge_id": merge.id,
        "source_id": source.id,
        "target_id": target.id,
        "merged_by": merged_by,
        "vote_count_transferred": votes_transferred,
        "comments_transferred": len(moved_comment_ids),
        "merged_at": now.isoformat(),
    }


def rollback_merge(
    session,
    merge_id: int,
    rolled_back_by: str | None = None,
    *,
    window_hours: int = ROLLBACK_WINDOW_HOURS,
    now: dt.datetime | None = None,
) -> dict:
    if now is None:
        now = _now()
    merge = session.get(ArticleMerge, merge_id)
    if merge is None:
        raise MergeError(f"merge {merge_id} does not exist", not_found=True)
    if not merge.active:
        raise MergeError(f"merge {merge_id} has already been rolled back")

    age_hours = (
        _as_naive_utc(now) - _as_naive_utc(merge.merged_at)
    ).total_seconds() / 3600
    if age_hours > window_hours:
        raise MergeError(
            f"merge {merge_id} is {age_hours:.0f}h old; rollback window is "
            f"{window_hours}h"
        )

    source = _get_story(session, merge.source_article_id)
    target = _get_story(session, merge.target_article_id)

    target.vote_count = (target.vote_count or 0) - merge.vote_count_transferred
    source.vote_count = merge.vote_count_transferred

    moved_ids = (
        json.loads(merge.transferred_comment_ids)
        if merge.transferred_comment_ids
        else []
    )
    for comment_id in moved_ids:
        comment = session.get(Comment, comment_id)
        # The comment may have been deleted since the merge; only move back ones
        # that still exist and still sit on the target.
        if comment is not None and comment.story_id == target.id:
            comment.story_id = source.id

    source.canonical_id = None
    source.merge_status = "none"

    merge.active = False
    merge.rolled_back_at = now
    merge.rolled_back_by = rolled_back_by
    session.flush()

    # Recompute the canonical's state from the merges that *remain* active. If
    # this was the last one the target is no longer canonical; otherwise its
    # merged-source list is rebuilt without the just-undone source.
    remaining = session.scalars(
        select(ArticleMerge).where(
            ArticleMerge.target_article_id == target.id,
            ArticleMerge.active.is_(True),
        )
    ).first()
    if remaining is None:
        target.merge_status = "none"
        target.merged_sources = None
    else:
        target.merged_sources = json.dumps(_rebuild_merged_sources(session, target))

    _recount_comments(session, source)
    _recount_comments(session, target)
    session.commit()

    return {
        "merge_id": merge.id,
        "source_id": source.id,
        "target_id": target.id,
        "rolled_back_by": rolled_back_by,
        "comments_restored": len(moved_ids),
        "rolled_back_at": now.isoformat(),
    }


def merged_into(session, story_id: int) -> dict | None:
    story = _get_story(session, story_id)
    if story.canonical_id is None:
        return None
    canonical = session.get(Story, story.canonical_id)
    if canonical is None:
        return None
    return {"id": canonical.id, "title": canonical.title, "url": canonical.url}


def list_merges(
    session, *, active_only: bool = False, limit: int = 100
) -> list[dict]:
    limit = max(1, min(limit, 500))
    query = select(ArticleMerge)
    if active_only:
        query = query.where(ArticleMerge.active.is_(True))
    rows = session.scalars(
        query.order_by(ArticleMerge.merged_at.desc(), ArticleMerge.id.desc()).limit(
            limit
        )
    ).all()

    result = []
    for merge in rows:
        source = session.get(Story, merge.source_article_id)
        target = session.get(Story, merge.target_article_id)
        result.append(
            {
                "merge_id": merge.id,
                "source_id": merge.source_article_id,
                "source_title": source.title if source else None,
                "target_id": merge.target_article_id,
                "target_title": target.title if target else None,
                "merged_by": merge.merged_by,
                "merged_at": merge.merged_at.isoformat() if merge.merged_at else None,
                "vote_count_transferred": merge.vote_count_transferred,
                "active": merge.active,
                "rolled_back_by": merge.rolled_back_by,
                "rolled_back_at": merge.rolled_back_at.isoformat()
                if merge.rolled_back_at
                else None,
            }
        )
    return result
