"""Bookmark ("save for later") service over stored stories.

A bookmark is keyed by ``(user_id, story_id)``: a user can save a given story at
most once. Toggling either creates the row (saving the story) or deletes it
(unsaving), and the story's denormalised ``bookmark_count`` is recomputed from
the live :class:`Bookmark` rows after every change. A user's bookmark list is
private — it is only ever returned for the requesting ``user_id`` and never
exposed through another user's profile.
"""

import datetime as dt

from sqlalchemy import func, select

from src.models import Bookmark, Story

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100


class BookmarkError(ValueError):
    """Raised for invalid bookmark operations (unknown story/user, bad input).

    ``not_found`` distinguishes a missing story/bookmark (maps to HTTP 404) from
    a bad-input validation failure (HTTP 400) so the API layer can pick a status
    without string-matching the message.
    """

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _clean_user(user_id: str) -> str:
    """Return the stripped *user_id*, raising :class:`BookmarkError` if empty."""
    name = (user_id or "").strip()
    if not name:
        raise BookmarkError("user_id must not be empty")
    return name


def _recompute(session, story: Story) -> None:
    """Refresh *story*'s denormalised ``bookmark_count`` from live rows."""
    story.bookmark_count = int(
        session.scalar(
            select(func.count()).select_from(Bookmark).where(
                Bookmark.story_id == story.id
            )
        )
        or 0
    )


def _paginate(page: int, per_page: int) -> tuple[int, int]:
    """Validate 1-based *page*/*per_page* and return (limit, offset).

    Raises :class:`BookmarkError` if either is below 1; *per_page* is capped at
    100 so a hostile caller cannot request an unbounded page.
    """
    if page < 1:
        raise BookmarkError("page must be >= 1")
    if per_page < 1:
        raise BookmarkError("per_page must be >= 1")
    per_page = min(per_page, _MAX_PER_PAGE)
    return per_page, (page - 1) * per_page


def toggle_bookmark(session, story_id: int, user_id: str) -> dict:
    """Toggle *user_id*'s bookmark on *story_id* and return its new state.

    Creates the bookmark if absent (saving the story) or deletes it if present
    (unsaving), keeping the toggle idempotent via the ``(user_id, story_id)``
    unique constraint. The story's ``bookmark_count`` is recomputed and the
    transaction committed. Raises :class:`BookmarkError` (``not_found``) if the
    story does not exist or (validation) if *user_id* is empty. Returns
    ``{story_id, bookmarked, bookmark_count}``.
    """
    name = _clean_user(user_id)
    story = session.get(Story, story_id)
    if story is None:
        raise BookmarkError(f"story {story_id} does not exist", not_found=True)

    existing = session.scalars(
        select(Bookmark).where(
            Bookmark.user_id == name, Bookmark.story_id == story_id
        )
    ).first()
    if existing is None:
        session.add(
            Bookmark(user_id=name, story_id=story_id, created_at=_now())
        )
        bookmarked = True
    else:
        session.delete(existing)
        bookmarked = False

    session.flush()
    _recompute(session, story)
    session.commit()
    return {
        "story_id": story_id,
        "bookmarked": bookmarked,
        "bookmark_count": story.bookmark_count,
    }


def remove_bookmark(session, story_id: int, user_id: str) -> dict:
    """Explicitly remove *user_id*'s bookmark on *story_id* (idempotent).

    Unlike :func:`toggle_bookmark` this never creates a row — removing an absent
    bookmark is a no-op that still returns the current state. The story's
    ``bookmark_count`` is recomputed and committed. Raises :class:`BookmarkError`
    (``not_found``) if the story does not exist. Returns ``{story_id,
    bookmarked: False, bookmark_count}``.
    """
    name = _clean_user(user_id)
    story = session.get(Story, story_id)
    if story is None:
        raise BookmarkError(f"story {story_id} does not exist", not_found=True)

    existing = session.scalars(
        select(Bookmark).where(
            Bookmark.user_id == name, Bookmark.story_id == story_id
        )
    ).first()
    if existing is not None:
        session.delete(existing)
        session.flush()
        _recompute(session, story)
        session.commit()
    return {
        "story_id": story_id,
        "bookmarked": False,
        "bookmark_count": story.bookmark_count,
    }


def bulk_remove_bookmarks(session, story_ids: list[int], user_id: str) -> dict:
    """Remove *user_id*'s bookmarks on every id in *story_ids* (idempotent).

    Ids the user has not bookmarked are silently skipped. Each affected story's
    ``bookmark_count`` is recomputed once and the whole batch committed together.
    Raises :class:`BookmarkError` if *user_id* is empty. Returns ``{removed}``
    with the number of bookmark rows actually deleted.
    """
    name = _clean_user(user_id)
    if not story_ids:
        return {"removed": 0}

    rows = session.scalars(
        select(Bookmark).where(
            Bookmark.user_id == name, Bookmark.story_id.in_(story_ids)
        )
    ).all()
    affected = {b.story_id for b in rows}
    for b in rows:
        session.delete(b)
    session.flush()
    for story_id in affected:
        story = session.get(Story, story_id)
        if story is not None:
            _recompute(session, story)
    session.commit()
    return {"removed": len(rows)}


def is_bookmarked(session, story_id: int, user_id: str) -> bool:
    """Return True if *user_id* currently has *story_id* bookmarked."""
    name = (user_id or "").strip()
    if not name:
        return False
    return (
        session.scalar(
            select(Bookmark.id).where(
                Bookmark.user_id == name, Bookmark.story_id == story_id
            )
        )
        is not None
    )


def list_bookmarks(
    session,
    user_id: str,
    page: int = 1,
    per_page: int = _DEFAULT_PER_PAGE,
    category: str | None = None,
) -> dict:
    """Return *user_id*'s paginated bookmark list, newest saved first.

    The list is private to *user_id* — no other user's saves are ever included.
    An optional *category* filters by ``Story.topic``. Returns ``{user_id, page,
    per_page, total, items}`` where each item carries ``story_id``, ``title``,
    ``url``, ``topic``, ``bookmark_count``, and an ISO ``created_at`` (the save
    timestamp). Raises :class:`BookmarkError` for an empty user or invalid
    pagination. Ordering and slicing happen in SQL so a large reading list never
    loads in full.
    """
    name = _clean_user(user_id)
    limit, offset = _paginate(page, per_page)

    base = (
        select(Bookmark, Story)
        .join(Story, Story.id == Bookmark.story_id)
        .where(Bookmark.user_id == name)
    )
    if category:
        base = base.where(Story.topic == category)

    total = int(
        session.scalar(select(func.count()).select_from(base.subquery())) or 0
    )
    rows = session.execute(
        base.order_by(Bookmark.created_at.desc(), Bookmark.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    items = [
        {
            "story_id": story.id,
            "title": story.title,
            "url": story.url,
            "topic": story.topic,
            "bookmark_count": story.bookmark_count,
            "created_at": bm.created_at.isoformat() if bm.created_at else None,
        }
        for bm, story in rows
    ]
    return {
        "user_id": name,
        "page": page,
        "per_page": limit,
        "total": total,
        "items": items,
    }
