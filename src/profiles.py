"""User profile, activity history, reputation, and leaderboard service.

Users are identified throughout the schema by a free-form ``user_id`` string
(a cookie uuid or chosen name) recorded on Votes, Comments, and Stories. This
module derives a public *profile* from that activity and stores per-user
metadata (bio, private toggle, cached counts) in the :class:`UserProfile` row.

Reputation (``karma``) is the total number of votes received on a user's
comments — story-vote counts belong to the story, not its voters, so they do
not contribute to a voter's karma. Counts are cached on the profile row and
refreshed by :func:`refresh_profile_stats` whenever activity changes, so the
profile and leaderboard pages never re-aggregate the activity tables per
request.

Privacy: a profile flagged ``is_private`` is omitted from the leaderboard and
renders as a minimal stub from :func:`get_profile`; its activity history is
never returned.
"""

from sqlalchemy import func, select

from src.models import Comment, Story, UserProfile, Vote

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100


class ProfileError(ValueError):
    """Raised for invalid profile operations (unknown user, bad pagination)."""


def get_or_create_profile(session, username: str) -> UserProfile:
    """Return the :class:`UserProfile` for *username*, creating it if absent.

    *username* must be a non-empty string (after stripping). The new row starts
    public with zeroed cached counts; callers refresh those via
    :func:`refresh_profile_stats`. The transaction is committed.
    """
    name = (username or "").strip()
    if not name:
        raise ProfileError("username must not be empty")
    profile = session.scalars(
        select(UserProfile).where(UserProfile.username == name)
    ).first()
    if profile is None:
        profile = UserProfile(username=name)
        session.add(profile)
        session.commit()
    return profile


def compute_karma(session, username: str) -> int:
    """Return *username*'s reputation: total votes received on their comments.

    Sums ``vote_count`` over every non-deleted comment authored by the user.
    Returns 0 for a user with no comments. Does not write anything.
    """
    total = session.scalar(
        select(func.coalesce(func.sum(Comment.vote_count), 0)).where(
            Comment.user_id == username, Comment.deleted.is_(False)
        )
    )
    return int(total or 0)


def refresh_profile_stats(session, username: str) -> UserProfile:
    """Recompute and cache *username*'s karma and activity counts.

    Aggregates the live activity tables — karma (votes received on comments),
    submission count (stories submitted), vote count (story votes cast), and
    comment count (non-deleted comments authored) — and stores them on the
    profile row, creating the row if needed. The transaction is committed and
    the refreshed profile returned.
    """
    profile = get_or_create_profile(session, username)
    profile.karma = compute_karma(session, username)
    profile.submission_count = int(
        session.scalar(
            select(func.count()).select_from(Story).where(
                Story.submitted_by == username
            )
        )
        or 0
    )
    profile.vote_count = int(
        session.scalar(
            select(func.count()).select_from(Vote).where(Vote.user_id == username)
        )
        or 0
    )
    profile.comment_count = int(
        session.scalar(
            select(func.count()).select_from(Comment).where(
                Comment.user_id == username, Comment.deleted.is_(False)
            )
        )
        or 0
    )
    session.commit()
    return profile


def set_private(session, username: str, is_private: bool) -> UserProfile:
    """Toggle *username*'s private-account flag and return the profile.

    A private profile is hidden from the leaderboard and exposes no activity.
    Creates the profile row if it does not yet exist. Committed.
    """
    profile = get_or_create_profile(session, username)
    profile.is_private = bool(is_private)
    session.commit()
    return profile


def get_profile(session, username: str) -> dict:
    """Return *username*'s public profile as a dict.

    For a public profile the dict carries ``username``, ``bio``, ``karma``, and
    the cached ``submission_count`` / ``vote_count`` / ``comment_count`` plus
    ``is_private: False``. For a private profile only ``username`` and
    ``is_private: True`` are returned — no stats or activity leak. Raises
    :class:`ProfileError` if the user has no profile row and no activity.
    """
    name = (username or "").strip()
    profile = session.scalars(
        select(UserProfile).where(UserProfile.username == name)
    ).first()
    if profile is None:
        # A user may have activity without ever having a metadata row; treat any
        # vote/comment/submission as proof the user exists and build on the fly.
        if not _user_exists(session, name):
            raise ProfileError(f"user {name!r} does not exist")
        profile = refresh_profile_stats(session, name)

    if profile.is_private:
        return {"username": profile.username, "is_private": True}
    return {
        "username": profile.username,
        "bio": profile.bio,
        "karma": profile.karma,
        "submission_count": profile.submission_count,
        "vote_count": profile.vote_count,
        "comment_count": profile.comment_count,
        "is_private": False,
    }


def _user_exists(session, username: str) -> bool:
    """Return True if *username* has any vote, comment, or submission on record."""
    if not username:
        return False
    has_vote = session.scalar(
        select(Vote.id).where(Vote.user_id == username).limit(1)
    )
    has_comment = session.scalar(
        select(Comment.id).where(Comment.user_id == username).limit(1)
    )
    has_story = session.scalar(
        select(Story.id).where(Story.submitted_by == username).limit(1)
    )
    return any(x is not None for x in (has_vote, has_comment, has_story))


def _paginate(page: int, per_page: int) -> tuple[int, int]:
    """Validate 1-based *page*/*per_page* and return (limit, offset).

    Raises :class:`ProfileError` if either is below 1; *per_page* is capped at
    100 so a hostile caller cannot request an unbounded page.
    """
    if page < 1:
        raise ProfileError("page must be >= 1")
    if per_page < 1:
        raise ProfileError("per_page must be >= 1")
    per_page = min(per_page, _MAX_PER_PAGE)
    return per_page, (page - 1) * per_page


def _check_visible(session, username: str) -> None:
    """Raise :class:`ProfileError` if *username* is unknown or private."""
    name = (username or "").strip()
    profile = session.scalars(
        select(UserProfile).where(UserProfile.username == name)
    ).first()
    if profile is None and not _user_exists(session, name):
        raise ProfileError(f"user {name!r} does not exist")
    if profile is not None and profile.is_private:
        raise ProfileError(f"user {name!r} is private")


def get_user_articles(
    session,
    username: str,
    page: int = 1,
    per_page: int = _DEFAULT_PER_PAGE,
) -> dict:
    """Return *username*'s timestamped, paginated article activity.

    Combines stories the user submitted (``activity: "submitted"``) with stories
    the user upvoted (``activity: "upvoted"``, i.e. a +1 vote), newest first by
    the activity timestamp. Returns ``{username, page, per_page, total, items}``
    where each item carries ``story_id``, ``title``, ``url``, ``activity``, and
    an ISO ``timestamp``. Raises :class:`ProfileError` for an unknown/private
    user or invalid pagination.
    """
    name = (username or "").strip()
    _check_visible(session, name)
    limit, offset = _paginate(page, per_page)

    submitted = session.scalars(
        select(Story).where(Story.submitted_by == name)
    ).all()
    rows: list[dict] = [
        {
            "story_id": s.id,
            "title": s.title,
            "url": s.url,
            "activity": "submitted",
            "timestamp": s.fetched_at,
        }
        for s in submitted
    ]

    upvotes = session.scalars(
        select(Vote).where(Vote.user_id == name, Vote.vote_value == 1)
    ).all()
    for v in upvotes:
        story = session.get(Story, v.story_id)
        if story is None:
            continue
        rows.append(
            {
                "story_id": story.id,
                "title": story.title,
                "url": story.url,
                "activity": "upvoted",
                "timestamp": v.updated_at or v.created_at,
            }
        )

    rows.sort(key=lambda r: r["timestamp"], reverse=True)
    total = len(rows)
    window = rows[offset : offset + limit]
    items = [
        {
            "story_id": r["story_id"],
            "title": r["title"],
            "url": r["url"],
            "activity": r["activity"],
            "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
        }
        for r in window
    ]
    return {
        "username": name,
        "page": page,
        "per_page": limit,
        "total": total,
        "items": items,
    }


def get_user_comments(
    session,
    username: str,
    page: int = 1,
    per_page: int = _DEFAULT_PER_PAGE,
) -> dict:
    """Return *username*'s timestamped, paginated comment history (newest first).

    Excludes soft-deleted comments. Returns ``{username, page, per_page, total,
    items}`` where each item carries ``comment_id``, ``story_id``, ``body``,
    ``vote_count``, and an ISO ``timestamp``. Raises :class:`ProfileError` for an
    unknown/private user or invalid pagination.
    """
    name = (username or "").strip()
    _check_visible(session, name)
    limit, offset = _paginate(page, per_page)

    base = (
        select(Comment)
        .where(Comment.user_id == name, Comment.deleted.is_(False))
    )
    total = int(
        session.scalar(
            select(func.count()).select_from(base.subquery())
        )
        or 0
    )
    comments = session.scalars(
        base.order_by(Comment.created_at.desc(), Comment.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    items = [
        {
            "comment_id": c.id,
            "story_id": c.story_id,
            "body": c.body,
            "vote_count": c.vote_count,
            "timestamp": c.created_at.isoformat() if c.created_at else None,
        }
        for c in comments
    ]
    return {
        "username": name,
        "page": page,
        "per_page": limit,
        "total": total,
        "items": items,
    }


def leaderboard(session, limit: int = 10) -> list[dict]:
    """Return the top *limit* public users ranked by cached karma (desc).

    Private profiles are excluded. Ties break on username for a stable order.
    Each entry carries ``rank`` (1-based), ``username``, ``karma``, and
    ``comment_count``. Raises :class:`ProfileError` if *limit* < 1.
    """
    if limit < 1:
        raise ProfileError("limit must be >= 1")
    profiles = session.scalars(
        select(UserProfile)
        .where(UserProfile.is_private.is_(False))
        .order_by(UserProfile.karma.desc(), UserProfile.username.asc())
        .limit(limit)
    ).all()
    return [
        {
            "rank": i + 1,
            "username": p.username,
            "karma": p.karma,
            "comment_count": p.comment_count,
        }
        for i, p in enumerate(profiles)
    ]
