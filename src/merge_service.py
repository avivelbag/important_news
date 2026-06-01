"""Admin-initiated near-duplicate article merging, with undo and an audit log.

URL-based and automatic fuzzy-title dedup already happens in
:mod:`src.deduplicator`. This module is the *manual* layer on top: an admin
reviews near-duplicate candidates and explicitly folds one story (the *source*)
into another (the canonical *target*). Unlike the automatic pass, every manual
merge is recorded as an :class:`~src.models.ArticleMerge` audit row and is fully
reversible within :data:`ROLLBACK_WINDOW_HOURS`.

Merging consolidates engagement so a single canonical story carries the whole
conversation: the source's denormalised ``vote_count`` is added onto the target
(matching the deduplicator's count-accumulation approach), every comment on the
source is reassigned to the target so the discussion threads merge, and the
source is linked to the target via ``canonical_id`` with ``merge_status``
``"merged"`` so the site can render a "merged into [canonical]" pointer.

Rollback reverses exactly what a specific merge did — restoring the transferred
vote count and moving back only the comments that merge redirected — so undoing
one merge never disturbs another.
"""

import datetime as dt
import json

from sqlalchemy import select

from src.deduplicator import normalize_url, title_similarity
from src.models import ArticleMerge, Comment, Story

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
    """Raised for invalid merge/rollback operations.

    ``not_found`` distinguishes a missing story/merge (HTTP 404) from a
    bad-input or rule violation (HTTP 400) so the API layer can pick a status
    without inspecting the message text.
    """

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _get_story(session, story_id: int) -> Story:
    story = session.get(Story, story_id)
    if story is None:
        raise MergeError(f"story {story_id} does not exist", not_found=True)
    return story


def _recount_comments(session, story: Story) -> None:
    """Refresh *story*'s denormalised count of its non-deleted comments."""
    story.comment_count = len(
        [c for c in session.scalars(
            select(Comment).where(Comment.story_id == story.id)
        ).all() if not c.deleted]
    )


def _merged_source_names(*stories: Story) -> list[str]:
    """Return the distinct contributing source names across *stories*, in order.

    Re-derives the union from each story's own ``merged_sources`` (if it already
    accumulated some) plus its ``source_name``, so merging a story that was
    itself a prior canonical preserves every original source.
    """
    names: list[str] = []
    for story in stories:
        existing = json.loads(story.merged_sources) if story.merged_sources else []
        for name in [*existing, story.source_name]:
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
    """Return stories that look like duplicates of *story_id*, best match first.

    Scans stories published within *lookback_days* of the target (and not
    already merged away) and keeps those whose normalised URL matches or whose
    title clears *threshold*. The target itself, stories already merged into
    something, and the target's existing duplicates are excluded. Each candidate
    is returned as a merge-preview dict (``id, title, url, source, similarity,
    vote_count, comment_count, published_at``) so the admin UI can show a
    side-by-side comparison before merging. Raises :class:`MergeError` if the
    target story does not exist.
    """
    if now is None:
        now = _now()
    target = _get_story(session, story_id)
    target_url = normalize_url(target.url)

    candidates = []
    for other in session.scalars(select(Story)).all():
        if other.id == target.id:
            continue
        if other.merge_status == "merged" or other.canonical_id is not None:
            continue
        delta = abs((other.published_at - target.published_at).total_seconds())
        if delta > lookback_days * 86400:
            continue
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


def merge_articles(
    session,
    source_id: int,
    target_id: int,
    merged_by: str | None = None,
    *,
    now: dt.datetime | None = None,
) -> dict:
    """Fold *source_id* into the canonical *target_id* and log the merge.

    Transfers the source's denormalised ``vote_count`` onto the target (zeroing
    the source), reassigns every comment on the source to the target so the
    discussion threads consolidate, links the source to the target via
    ``canonical_id`` (``merge_status="merged"``), marks the target
    ``"canonical"``, and unions their contributing source names onto the target's
    ``merged_sources``. An :class:`~src.models.ArticleMerge` audit row captures
    who merged, when, how many votes moved, and exactly which comments were
    redirected (so rollback is precise).

    Raises :class:`MergeError` when either story is missing (404), when source
    and target are the same story, or when the source has already been merged.
    Returns a summary dict including the new ``merge_id``.
    """
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
    target.merged_sources = json.dumps(_merged_source_names(target, source))

    session.flush()
    _recount_comments(session, source)
    _recount_comments(session, target)

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
    """Undo merge *merge_id*, restoring the source story, within the time window.

    Reverses precisely what :func:`merge_articles` did: subtracts the recorded
    transferred votes from the target and restores them on the source, moves the
    exact set of redirected comments back to the source, clears the source's
    ``canonical_id``/``merge_status``, and recomputes ``merged_sources`` on the
    target. The audit row is marked inactive and stamped with the undoing actor.

    Raises :class:`MergeError` when the merge is unknown (404), already rolled
    back, or older than *window_hours* (the merge is considered settled). Returns
    a summary dict.
    """
    if now is None:
        now = _now()
    merge = session.get(ArticleMerge, merge_id)
    if merge is None:
        raise MergeError(f"merge {merge_id} does not exist", not_found=True)
    if not merge.active:
        raise MergeError(f"merge {merge_id} has already been rolled back")

    age_hours = (now - merge.merged_at).total_seconds() / 3600
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
    target.merged_sources = json.dumps(_merged_source_names(target))
    if not target.merged_sources or target.merged_sources == "[]":
        target.merged_sources = None
    # The target is only still "canonical" if other merges remain pointing at it.
    remaining = session.scalars(
        select(ArticleMerge).where(
            ArticleMerge.target_article_id == target.id,
            ArticleMerge.active.is_(True),
            ArticleMerge.id != merge.id,
        )
    ).first()
    if remaining is None:
        target.merge_status = "none"
        target.merged_sources = None

    merge.active = False
    merge.rolled_back_at = now
    merge.rolled_back_by = rolled_back_by

    session.flush()
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
    """Return the canonical story *story_id* was merged into, or ``None``.

    Powers the "this story was merged into [canonical]" banner: returns
    ``{id, title, url}`` of the canonical when *story_id* is a merged duplicate,
    and ``None`` when the story is canonical or untouched. Raises
    :class:`MergeError` if *story_id* does not exist.
    """
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
    """Return the merge audit log, most recent first.

    Each entry reports the source/target ids and titles, who merged, the
    transferred vote count, and the merge's current state (``active`` plus any
    rollback stamp). Set *active_only* to hide already-undone merges. *limit*
    (1..500) bounds the result. Raises :class:`MergeError` on a non-positive
    limit.
    """
    if limit < 1:
        raise MergeError("limit must be >= 1")
    limit = min(limit, 500)
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
