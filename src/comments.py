"""Comment posting, threading, soft-delete, and voting over stored stories.

Comments form a discussion tree per story: a top-level comment has no parent,
a reply points at the comment it answers. The tree is stored as a flat
adjacency list (``parent_comment_id``) and reassembled by :func:`get_thread`
into nested dicts ordered by ``vote_count`` (highest first), then creation
time. A deleted comment is soft-deleted — its row stays so replies remain
reachable, but it renders as a ``[deleted]`` stub. The owning story keeps a
denormalised ``comment_count`` of its non-deleted comments for listings.

Comment votes are intentionally simpler than story votes: a vote of -1/+1
adjusts the comment's ``vote_count`` directly and is not deduplicated per user.
This keeps comment ranking decoupled from the per-user story Vote table.
"""

import datetime as dt

from sqlalchemy import func, select

from src.models import Comment, Story

DELETED_BODY = "[deleted]"

_MAX_BODY = 10_000
_VALID_VOTES = (-1, 1)


class CommentError(ValueError):
    """Base error for invalid comment operations.

    ``not_found`` distinguishes an unknown story/comment id (HTTP 404) from
    malformed input (HTTP 400) so callers route status without matching on the
    error message text. Subclasses set it; the base default is bad-input.
    """

    not_found = False


class CommentNotFound(CommentError):
    """Raised when a referenced story or comment id does not exist (404)."""

    not_found = True


class CommentBadInput(CommentError):
    """Raised when comment input is malformed or out of range (400)."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _recount(session, story: Story) -> None:
    """Refresh *story*'s denormalised count of its non-deleted comments."""
    story.comment_count = session.scalar(
        select(func.count())
        .select_from(Comment)
        .where(Comment.story_id == story.id, Comment.deleted.is_(False))
    )


def post_comment(
    session,
    story_id: int,
    body: str,
    user_id: str | None = None,
    parent_comment_id: int | None = None,
) -> Comment:
    """Create and persist a new comment on *story_id* and return it.

    *body* must be non-empty (after stripping) and at most 10k chars, else
    :class:`CommentError` is raised. The story must exist. When
    *parent_comment_id* is given it must reference an existing comment that
    belongs to the *same* story — a reply cannot cross stories — otherwise
    :class:`CommentError` is raised. The story's ``comment_count`` is refreshed
    and the transaction committed.
    """
    text = (body or "").strip()
    if not text:
        raise CommentBadInput("comment body must not be empty")
    if len(text) > _MAX_BODY:
        raise CommentBadInput(f"comment body exceeds {_MAX_BODY} chars")

    story = session.get(Story, story_id)
    if story is None:
        raise CommentNotFound(f"story {story_id} does not exist")

    if parent_comment_id is not None:
        parent = session.get(Comment, parent_comment_id)
        if parent is None:
            raise CommentNotFound(f"parent comment {parent_comment_id} does not exist")
        if parent.story_id != story_id:
            raise CommentBadInput("parent comment belongs to a different story")

    comment = Comment(
        story_id=story_id,
        parent_comment_id=parent_comment_id,
        user_id=user_id,
        body=text,
        created_at=_now(),
    )
    session.add(comment)
    session.flush()
    _recount(session, story)
    session.commit()
    return comment


def delete_comment(session, comment_id: int) -> Comment:
    """Soft-delete *comment_id*: flag it deleted, keep the row, return it.

    Raises :class:`CommentError` if the comment does not exist. Deleting an
    already-deleted comment is a no-op (still returns the row). The owning
    story's ``comment_count`` is refreshed and committed. The row is preserved
    so any replies underneath it stay reachable in the thread.
    """
    comment = session.get(Comment, comment_id)
    if comment is None:
        raise CommentNotFound(f"comment {comment_id} does not exist")
    if not comment.deleted:
        comment.deleted = True
        comment.updated_at = _now()
        session.flush()
        _recount(session, comment.story)
        session.commit()
    return comment


def vote_comment(session, comment_id: int, vote_value: int) -> int:
    """Apply a -1/+1 *vote_value* to *comment_id* and return its new vote_count.

    *vote_value* must be -1 or +1 (0 is not a comment vote), else
    :class:`CommentError` is raised; an unknown comment also raises. The vote is
    added directly to the comment's ``vote_count`` (not deduplicated per user)
    and committed.
    """
    if vote_value not in _VALID_VOTES:
        raise CommentBadInput(f"vote_value must be one of {_VALID_VOTES}")
    comment = session.get(Comment, comment_id)
    if comment is None:
        raise CommentNotFound(f"comment {comment_id} does not exist")
    comment.vote_count += vote_value
    session.commit()
    # Reputation is votes received on comments; refresh the author's cached
    # karma so the profile/leaderboard reflect this vote without re-aggregating.
    if comment.user_id:
        from src.profiles import refresh_profile_stats

        refresh_profile_stats(session, comment.user_id)
    return comment.vote_count


def _serialize(comment: Comment) -> dict:
    """Render one comment to a dict, masking body/author when soft-deleted."""
    return {
        "id": comment.id,
        "story_id": comment.story_id,
        "parent_comment_id": comment.parent_comment_id,
        "user_id": None if comment.deleted else comment.user_id,
        "body": DELETED_BODY if comment.deleted else comment.body,
        "vote_count": comment.vote_count,
        "deleted": comment.deleted,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
        "replies": [],
    }


def get_thread(session, story_id: int) -> list[dict]:
    """Return the nested comment thread for *story_id* as a list of dict nodes.

    Raises :class:`CommentError` if the story does not exist. Each node carries
    its comment fields plus a ``replies`` list of child nodes built recursively.
    Siblings at every level are ordered by ``vote_count`` descending, then by
    creation time ascending (stable tie-break). Soft-deleted comments remain in
    the tree as ``[deleted]`` stubs so their replies stay visible.
    """
    if session.get(Story, story_id) is None:
        raise CommentNotFound(f"story {story_id} does not exist")

    comments = session.scalars(
        select(Comment).where(Comment.story_id == story_id)
    ).all()

    nodes = {c.id: _serialize(c) for c in comments}
    roots: list[dict] = []
    for c in comments:
        node = nodes[c.id]
        parent = nodes.get(c.parent_comment_id) if c.parent_comment_id else None
        # A reply whose parent is missing (shouldn't happen with FK on) is
        # promoted to a root so it is never silently dropped from the thread.
        (parent["replies"] if parent is not None else roots).append(node)

    def _sort(level: list[dict]) -> None:
        level.sort(key=lambda n: (-n["vote_count"], n["created_at"] or ""))
        for n in level:
            _sort(n["replies"])

    _sort(roots)
    return roots
