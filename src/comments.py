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

from src.models import Comment, CommentVote, Story

DELETED_BODY = "[deleted]"
HIDDEN_BODY = "[flagged and hidden pending review]"

_MAX_BODY = 10_000
_VALID_VOTES = (-1, 1)
# Per-user votes additionally accept 0, which reverses (removes) an existing vote.
_VALID_USER_VOTES = (-1, 0, 1)
# A comment whose net score is at or below this is collapsed by default in the
# UI; readers can expand it via a show/hide toggle. Net low-quality replies are
# hidden without being deleted, improving thread signal-to-noise.
_COLLAPSE_THRESHOLD = -2
# Valid thread sort orders exposed through get_thread / the comments API.
_SORTS = ("score", "newest", "oldest")


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


def _recompute_comment_score(session, comment: Comment) -> None:
    """Refresh *comment*'s denormalised ``vote_count`` from its CommentVote rows.

    ``vote_count`` is the net score (upvotes minus downvotes) and is kept in sync
    so threads can be sorted and rendered without re-aggregating votes per read.
    """
    comment.vote_count = session.scalar(
        select(func.coalesce(func.sum(CommentVote.vote_value), 0)).where(
            CommentVote.comment_id == comment.id
        )
    )


def _vote_counts(session, comment_id: int) -> tuple[int, int]:
    """Return ``(upvotes, downvotes)`` for *comment_id* from its CommentVote rows."""
    rows = session.scalars(
        select(CommentVote.vote_value).where(CommentVote.comment_id == comment_id)
    ).all()
    upvotes = sum(1 for v in rows if v == 1)
    downvotes = sum(1 for v in rows if v == -1)
    return upvotes, downvotes


def cast_comment_vote(
    session, comment_id: int, user_id: str, vote_value: int
) -> dict:
    """Cast/change/reverse *user_id*'s vote on *comment_id*; return its vote state.

    *vote_value* must be -1, 0, or +1 (0 reverses an existing vote), else
    :class:`CommentBadInput` is raised. *user_id* must be non-empty — anonymous
    callers cannot be deduplicated. An unknown comment raises
    :class:`CommentNotFound`; voting on one's *own* comment raises
    :class:`CommentBadInput`.

    The vote is stored in a unique ``(user_id, comment_id)`` row so a repeat vote
    updates that row instead of double-counting, and a 0 deletes it. The
    comment's ``vote_count`` (net score) is recomputed from the live rows and the
    author's cached karma refreshed. Returns the dict from :func:`get_comment_votes`.
    """
    if vote_value not in _VALID_USER_VOTES:
        raise CommentBadInput(f"vote_value must be one of {_VALID_USER_VOTES}")
    if not user_id:
        raise CommentBadInput("a user_id is required to vote on a comment")

    comment = session.get(Comment, comment_id)
    if comment is None:
        raise CommentNotFound(f"comment {comment_id} does not exist")
    if comment.user_id is not None and comment.user_id == user_id:
        raise CommentBadInput("cannot vote on your own comment")

    existing = session.scalars(
        select(CommentVote).where(
            CommentVote.user_id == user_id, CommentVote.comment_id == comment_id
        )
    ).first()
    if vote_value == 0:
        if existing is not None:
            session.delete(existing)
    elif existing is None:
        session.add(
            CommentVote(
                comment_id=comment_id,
                user_id=user_id,
                vote_value=vote_value,
                created_at=_now(),
            )
        )
    else:
        existing.vote_value = vote_value
        existing.updated_at = _now()

    session.flush()
    _recompute_comment_score(session, comment)
    session.commit()

    if comment.user_id:
        from src.profiles import refresh_profile_stats

        refresh_profile_stats(session, comment.user_id)
    return get_comment_votes(session, comment_id, user_id)


def get_comment_votes(
    session, comment_id: int, user_id: str | None = None
) -> dict:
    """Return the vote state for *comment_id*, including *user_id*'s own vote.

    Raises :class:`CommentNotFound` if the comment does not exist. The dict
    carries the net ``score`` (== ``vote_count``), ``upvotes``/``downvotes``
    counts, the caller's ``user_vote`` (-1/0/+1, 0 when they have not voted or
    no *user_id* was given), and the ``collapsed`` default for low-score comments.
    """
    comment = session.get(Comment, comment_id)
    if comment is None:
        raise CommentNotFound(f"comment {comment_id} does not exist")

    upvotes, downvotes = _vote_counts(session, comment_id)
    user_vote = 0
    if user_id:
        row = session.scalars(
            select(CommentVote).where(
                CommentVote.user_id == user_id,
                CommentVote.comment_id == comment_id,
            )
        ).first()
        if row is not None:
            user_vote = row.vote_value
    return {
        "comment_id": comment_id,
        "score": comment.vote_count,
        "vote_count": comment.vote_count,
        "upvotes": upvotes,
        "downvotes": downvotes,
        "user_vote": user_vote,
        "collapsed": comment.vote_count <= _COLLAPSE_THRESHOLD,
    }


def _serialize(comment: Comment, voted: dict | None = None, op_id: str | None = None) -> dict:
    """Render one comment to a dict, masking body/author when soft-deleted or hidden.

    *voted* maps comment id to the requesting user's vote (-1/0/+1) so each node
    reports the caller's ``user_vote``. *op_id* is the story submitter so a
    comment authored by the original poster is flagged ``is_op``. ``score`` is an
    alias of ``vote_count`` and ``collapsed`` marks low-score nodes hidden by
    default; both are omitted from a deleted stub's meaning but still reported.
    A comment moderated to ``is_hidden`` is masked the same way as a deleted one
    so the public thread keeps its structure (replies stay visible) while the
    flagged body is withheld.
    """
    user_vote = (voted or {}).get(comment.id, 0)
    masked = comment.deleted or comment.is_hidden
    if comment.deleted:
        body = DELETED_BODY
    elif comment.is_hidden:
        body = HIDDEN_BODY
    else:
        body = comment.body
    return {
        "id": comment.id,
        "story_id": comment.story_id,
        "parent_comment_id": comment.parent_comment_id,
        "user_id": None if masked else comment.user_id,
        "body": body,
        "vote_count": comment.vote_count,
        "score": comment.vote_count,
        "user_vote": user_vote,
        "collapsed": comment.vote_count <= _COLLAPSE_THRESHOLD,
        "is_op": (
            not masked
            and op_id is not None
            and comment.user_id is not None
            and comment.user_id == op_id
        ),
        "deleted": comment.deleted,
        "is_hidden": comment.is_hidden,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
        "replies": [],
    }


def _sort_key(sort: str):
    """Return ``(key_fn, reverse)`` for *sort* ("score" | "newest" | "oldest").

    "score" orders by net ``vote_count`` descending with creation time ascending
    as a stable tie-break; "newest"/"oldest" order purely by creation time
    (``id`` breaks ties so equal timestamps stay deterministic). An unknown value
    raises :class:`CommentBadInput`.
    """
    if sort not in _SORTS:
        raise CommentBadInput(f"sort must be one of {_SORTS}")
    if sort == "score":
        return (lambda n: (-n["vote_count"], n["created_at"] or "", n["id"]), False)
    if sort == "newest":
        return (lambda n: (n["created_at"] or "", n["id"]), True)
    return (lambda n: (n["created_at"] or "", n["id"]), False)


def get_thread(
    session, story_id: int, sort: str = "score", user_id: str | None = None
) -> list[dict]:
    """Return the nested comment thread for *story_id* as a list of dict nodes.

    Raises :class:`CommentNotFound` if the story does not exist and
    :class:`CommentBadInput` if *sort* is not one of "score"/"newest"/"oldest".
    Each node carries its comment fields plus a ``replies`` list of child nodes
    built recursively, a ``user_vote`` reflecting *user_id*'s own vote, an
    ``is_op`` flag, and a ``collapsed`` default for low-score nodes. Siblings at
    every level are ordered per *sort*; soft-deleted comments remain in the tree
    as ``[deleted]`` stubs so their replies stay visible.
    """
    story = session.get(Story, story_id)
    if story is None:
        raise CommentNotFound(f"story {story_id} does not exist")
    key = _sort_key(sort)  # validate before any work so a bad sort fails fast

    comments = session.scalars(
        select(Comment).where(Comment.story_id == story_id)
    ).all()

    voted: dict[int, int] = {}
    if user_id:
        ids = [c.id for c in comments]
        if ids:
            rows = session.scalars(
                select(CommentVote).where(
                    CommentVote.user_id == user_id,
                    CommentVote.comment_id.in_(ids),
                )
            ).all()
            voted = {r.comment_id: r.vote_value for r in rows}

    nodes = {c.id: _serialize(c, voted, story.submitted_by) for c in comments}
    roots: list[dict] = []
    for c in comments:
        node = nodes[c.id]
        parent = nodes.get(c.parent_comment_id) if c.parent_comment_id else None
        # A reply whose parent is missing (shouldn't happen with FK on) is
        # promoted to a root so it is never silently dropped from the thread.
        (parent["replies"] if parent is not None else roots).append(node)

    key_fn, reverse = key

    def _sort(level: list[dict]) -> None:
        level.sort(key=key_fn, reverse=reverse)
        for n in level:
            _sort(n["replies"])

    _sort(roots)
    return roots
