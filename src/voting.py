"""Vote casting and aggregation over stored stories.

A vote is keyed by ``(user_id, story_id)``: a user casting a second vote on the
same story updates their existing row rather than adding a new one, so changing
a vote (+1 to -1) or reversing it (to 0) mutates one row. After every change the
story's denormalised ``vote_count`` (net points) and ``downvotes`` count are
recomputed from the live Vote rows.
"""

import datetime as dt

from sqlalchemy import select

from src.models import Story, Vote

_VALID_VALUES = (-1, 0, 1)


class VoteError(ValueError):
    """Raised when a vote is invalid (bad value or unknown story)."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _recompute(session, story: Story) -> None:
    votes = session.scalars(
        select(Vote).where(Vote.story_id == story.id)
    ).all()
    story.vote_count = sum(v.vote_value for v in votes)
    story.downvotes = sum(1 for v in votes if v.vote_value == -1)


def cast_vote(session, story_id: int, user_id: str, vote_value: int) -> dict:
    """Cast or update *user_id*'s vote of *vote_value* on *story_id*.

    *vote_value* must be one of -1, 0, +1, else :class:`VoteError` is raised.
    Raises :class:`VoteError` if the story does not exist. If the user already
    voted on this story the existing row is updated (recording the change or
    reversal) and ``updated_at`` is stamped; otherwise a new row is inserted
    with ``created_at`` set. The story's ``vote_count`` and ``downvotes`` are
    recomputed and committed. Returns the distribution dict.
    """
    if vote_value not in _VALID_VALUES:
        raise VoteError(f"vote_value must be one of {_VALID_VALUES}")

    story = session.get(Story, story_id)
    if story is None:
        raise VoteError(f"story {story_id} does not exist")

    existing = session.scalars(
        select(Vote).where(Vote.user_id == user_id, Vote.story_id == story_id)
    ).first()
    if existing is None:
        session.add(
            Vote(
                story_id=story_id,
                user_id=user_id,
                vote_value=vote_value,
                created_at=_now(),
            )
        )
    else:
        existing.vote_value = vote_value
        existing.updated_at = _now()

    session.flush()
    _recompute(session, story)
    session.commit()
    return get_distribution(session, story_id)


def get_distribution(session, story_id: int) -> dict:
    """Return the vote distribution for *story_id*.

    Raises :class:`VoteError` if the story does not exist. The dict carries the
    net ``points`` (== ``vote_count``), ``upvotes`` and ``downvotes`` counts.
    """
    story = session.get(Story, story_id)
    if story is None:
        raise VoteError(f"story {story_id} does not exist")

    votes = session.scalars(
        select(Vote).where(Vote.story_id == story_id)
    ).all()
    upvotes = sum(1 for v in votes if v.vote_value == 1)
    downvotes = sum(1 for v in votes if v.vote_value == -1)
    return {
        "story_id": story_id,
        "points": story.vote_count,
        "upvotes": upvotes,
        "downvotes": downvotes,
        "vote_count": story.vote_count,
    }
