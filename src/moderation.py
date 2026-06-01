"""Content moderation: user flagging plus the moderator review queue.

Users *flag* a story or comment with one of a fixed set of preset reasons
(:data:`FLAG_REASONS`). Each flag is one :class:`~src.models.Flag` row keyed by
``(user_id, content_type, content_id)`` so a user can flag a given item at most
once, and a user can never flag content they own. Open flags are counted onto
the content's denormalised ``flag_count`` and summarised into
``flag_reason_counts`` (a JSON ``{reason: count}`` map) so the dashboard renders
without re-aggregating. When an item's open-flag count reaches
:data:`AUTO_HIDE_THRESHOLD` it is auto-hidden pending review.

Moderators act on flagged content through :func:`hide_content`,
:func:`delete_content`, and :func:`dismiss_flags`. Every action appends a
:class:`~src.models.ModerationAction` audit row; hide/delete additionally
notify the content owner via a :class:`~src.models.ModerationNotification`.
Resolving flags marks them ``upheld`` (content actioned) or ``dismissed``
(report cleared); :func:`flagger_stats` reads those statuses back to surface
repeat flaggers and likely false reporters.
"""

import datetime as dt
import json

from sqlalchemy import func, select

from src.models import (
    Comment,
    Flag,
    ModerationAction,
    ModerationNotification,
    Story,
)

# Preset reasons a user may pick when flagging. Anything else is rejected so the
# dashboard breakdown stays over a known, finite set of buckets.
FLAG_REASONS = ("spam", "off_topic", "abuse", "misinformation", "duplicate", "other")

STORY = "story"
COMMENT = "comment"
_CONTENT_TYPES = (STORY, COMMENT)

# Open-flag count at which an item is automatically hidden pending review.
AUTO_HIDE_THRESHOLD = 3


class ModerationError(ValueError):
    """Raised for invalid moderation operations (bad input, unknown content).

    ``not_found`` distinguishes a missing content item / flag (HTTP 404) from a
    bad-input validation failure (HTTP 400) so the API layer can choose a status
    without string-matching the message.
    """

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _clean(value: str | None, field: str) -> str:
    name = (value or "").strip()
    if not name:
        raise ModerationError(f"{field} must not be empty")
    return name


def _model(content_type: str):
    if content_type not in _CONTENT_TYPES:
        raise ModerationError(f"unknown content_type {content_type!r}")
    return Story if content_type == STORY else Comment


def _get_content(session, content_type: str, content_id: int):
    obj = session.get(_model(content_type), content_id)
    if obj is None:
        raise ModerationError(
            f"{content_type} {content_id} does not exist", not_found=True
        )
    return obj


def _owner(content_type: str, obj) -> str | None:
    return obj.submitted_by if content_type == STORY else obj.user_id


def _recompute(session, content_type: str, obj) -> None:
    rows = session.execute(
        select(Flag.reason, func.count())
        .where(
            Flag.content_type == content_type,
            Flag.content_id == obj.id,
            Flag.status == "open",
        )
        .group_by(Flag.reason)
    ).all()
    counts = {reason: int(n) for reason, n in rows}
    obj.flag_count = sum(counts.values())
    obj.flag_reason_counts = json.dumps(counts) if counts else None


def flag_content(
    session,
    content_type: str,
    content_id: int,
    user_id: str,
    reason: str,
    auto_hide_threshold: int = AUTO_HIDE_THRESHOLD,
) -> dict:
    """Record *user_id*'s flag on a content item and return its new state.

    Validates *content_type*/*reason* against the preset sets, requires a
    non-empty *user_id*, and refuses to let a user flag content they own
    (``submitted_by`` for a story, ``user_id`` for a comment). A repeat flag
    from the same user on the same item is a no-op — the ``(user_id,
    content_type, content_id)`` unique constraint guarantees at most one row —
    but a previously resolved flag from that user is reopened with the new
    reason so the report is reconsidered.

    After writing the flag the content's ``flag_count`` and
    ``flag_reason_counts`` are recomputed; if the open-flag count reaches
    *auto_hide_threshold* the item is auto-hidden (``is_hidden = True``) and an
    ``auto_hide`` audit action is logged. Returns ``{content_type, content_id,
    flag_count, is_hidden, reason_counts}``. Raises :class:`ModerationError`.
    """
    name = _clean(user_id, "user_id")
    if reason not in FLAG_REASONS:
        raise ModerationError(f"unknown reason {reason!r}")
    obj = _get_content(session, content_type, content_id)

    owner = _owner(content_type, obj)
    if owner is not None and owner == name:
        raise ModerationError("you cannot flag your own content")

    existing = session.scalars(
        select(Flag).where(
            Flag.user_id == name,
            Flag.content_type == content_type,
            Flag.content_id == content_id,
        )
    ).first()
    if existing is None:
        session.add(
            Flag(
                user_id=name,
                content_type=content_type,
                content_id=content_id,
                reason=reason,
                status="open",
                created_at=_now(),
            )
        )
    else:
        existing.reason = reason
        existing.status = "open"
        existing.resolved_at = None

    session.flush()
    _recompute(session, content_type, obj)

    if not obj.is_hidden and obj.flag_count >= auto_hide_threshold:
        obj.is_hidden = True
        session.add(
            ModerationAction(
                content_type=content_type,
                content_id=content_id,
                action="auto_hide",
                moderator=None,
                detail=f"auto-hidden at {obj.flag_count} flags",
                created_at=_now(),
            )
        )

    session.commit()
    return {
        "content_type": content_type,
        "content_id": content_id,
        "flag_count": obj.flag_count,
        "is_hidden": obj.is_hidden,
        "reason_counts": json.loads(obj.flag_reason_counts)
        if obj.flag_reason_counts
        else {},
    }


def _resolve_flags(session, content_type: str, content_id: int, status: str) -> int:
    flags = session.scalars(
        select(Flag).where(
            Flag.content_type == content_type,
            Flag.content_id == content_id,
            Flag.status == "open",
        )
    ).all()
    for flag in flags:
        flag.status = status
        flag.resolved_at = _now()
    return len(flags)


def _notify_owner(
    session, content_type: str, obj, action: str, message: str
) -> None:
    owner = _owner(content_type, obj)
    if not owner:
        return
    session.add(
        ModerationNotification(
            user_id=owner,
            content_type=content_type,
            content_id=obj.id,
            action=action,
            message=message,
            read=False,
            created_at=_now(),
        )
    )


def hide_content(
    session, content_type: str, content_id: int, moderator: str
) -> dict:
    """Hide a flagged item, uphold its open flags, and notify its owner.

    Sets ``is_hidden = True``, marks the item's open flags ``upheld`` (the
    reports stood), logs a ``hide`` audit action by *moderator*, and notifies
    the owner. Idempotent in effect — hiding an already-hidden item still
    records the action and resolves any new flags. Returns ``{content_type,
    content_id, is_hidden, upheld, action}``. Raises :class:`ModerationError`.
    """
    mod = _clean(moderator, "moderator")
    obj = _get_content(session, content_type, content_id)
    obj.is_hidden = True
    upheld = _resolve_flags(session, content_type, content_id, "upheld")
    session.flush()
    _recompute(session, content_type, obj)
    session.add(
        ModerationAction(
            content_type=content_type,
            content_id=content_id,
            action="hide",
            moderator=mod,
            detail=f"upheld {upheld} flag(s)",
            created_at=_now(),
        )
    )
    _notify_owner(
        session,
        content_type,
        obj,
        "hide",
        f"Your {content_type} was hidden after review of {upheld} report(s).",
    )
    session.commit()
    return {
        "content_type": content_type,
        "content_id": content_id,
        "is_hidden": True,
        "upheld": upheld,
        "action": "hide",
    }


def delete_content(
    session, content_type: str, content_id: int, moderator: str
) -> dict:
    """Delete a flagged item, uphold its open flags, and notify its owner.

    A comment is soft-deleted (``deleted = True``, preserving thread structure)
    and also hidden; a story is hidden (the schema has no hard story delete and
    its votes/comments must stay referentially intact). Open flags are marked
    ``upheld``, a ``delete`` audit action is logged, and the owner is notified.
    Returns ``{content_type, content_id, deleted, upheld, action}``. Raises
    :class:`ModerationError`.
    """
    mod = _clean(moderator, "moderator")
    obj = _get_content(session, content_type, content_id)
    if content_type == COMMENT:
        obj.deleted = True
    obj.is_hidden = True
    upheld = _resolve_flags(session, content_type, content_id, "upheld")
    session.flush()
    _recompute(session, content_type, obj)
    session.add(
        ModerationAction(
            content_type=content_type,
            content_id=content_id,
            action="delete",
            moderator=mod,
            detail=f"upheld {upheld} flag(s)",
            created_at=_now(),
        )
    )
    _notify_owner(
        session,
        content_type,
        obj,
        "delete",
        f"Your {content_type} was removed after review of {upheld} report(s).",
    )
    session.commit()
    return {
        "content_type": content_type,
        "content_id": content_id,
        "deleted": True,
        "upheld": upheld,
        "action": "delete",
    }


def dismiss_flags(
    session, content_type: str, content_id: int, moderator: str
) -> dict:
    """Clear an item's open flags as unfounded and un-hide it.

    Marks the open flags ``dismissed`` (signalling false reports), clears
    ``is_hidden`` (an auto-hidden item becomes visible again), recomputes the
    now-zero flag counts, and logs a ``dismiss`` audit action. No owner
    notification is sent — nothing was done to their content. Returns
    ``{content_type, content_id, is_hidden, dismissed, action}``. Raises
    :class:`ModerationError`.
    """
    mod = _clean(moderator, "moderator")
    obj = _get_content(session, content_type, content_id)
    dismissed = _resolve_flags(session, content_type, content_id, "dismissed")
    obj.is_hidden = False
    session.flush()
    _recompute(session, content_type, obj)
    session.add(
        ModerationAction(
            content_type=content_type,
            content_id=content_id,
            action="dismiss",
            moderator=mod,
            detail=f"dismissed {dismissed} flag(s)",
            created_at=_now(),
        )
    )
    session.commit()
    return {
        "content_type": content_type,
        "content_id": content_id,
        "is_hidden": False,
        "dismissed": dismissed,
        "action": "dismiss",
    }


def _title_for(session, content_type: str, content_id: int) -> str:
    obj = session.get(_model(content_type), content_id)
    if obj is None:
        return ""
    if content_type == STORY:
        return obj.title
    body = obj.body or ""
    return body[:80]


def list_flagged(
    session,
    content_type: str | None = None,
    reason: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return the moderation queue: items with open flags, most-flagged first.

    Aggregates open :class:`Flag` rows by content item so each entry carries the
    total ``flag_count`` and a ``reason_counts`` breakdown. Optionally filter to
    a single *content_type* (``"story"``/``"comment"``) and/or restrict to items
    carrying at least one flag of *reason*. *limit* (1..200) bounds the result.
    Each entry is ``{content_type, content_id, title, flag_count, is_hidden,
    reason_counts}``. Raises :class:`ModerationError` on invalid input.
    """
    if content_type is not None and content_type not in _CONTENT_TYPES:
        raise ModerationError(f"unknown content_type {content_type!r}")
    if reason is not None and reason not in FLAG_REASONS:
        raise ModerationError(f"unknown reason {reason!r}")
    if limit < 1:
        raise ModerationError("limit must be >= 1")
    limit = min(limit, 200)

    query = (
        select(
            Flag.content_type,
            Flag.content_id,
            Flag.reason,
            func.count().label("n"),
        )
        .where(Flag.status == "open")
        .group_by(Flag.content_type, Flag.content_id, Flag.reason)
    )
    if content_type is not None:
        query = query.where(Flag.content_type == content_type)

    grouped: dict[tuple[str, int], dict] = {}
    for ctype, cid, rsn, n in session.execute(query).all():
        entry = grouped.setdefault(
            (ctype, cid), {"reason_counts": {}, "flag_count": 0}
        )
        entry["reason_counts"][rsn] = int(n)
        entry["flag_count"] += int(n)

    items = []
    for (ctype, cid), data in grouped.items():
        if reason is not None and reason not in data["reason_counts"]:
            continue
        obj = session.get(_model(ctype), cid)
        items.append(
            {
                "content_type": ctype,
                "content_id": cid,
                "title": _title_for(session, ctype, cid),
                "flag_count": data["flag_count"],
                "is_hidden": bool(obj.is_hidden) if obj is not None else False,
                "reason_counts": data["reason_counts"],
            }
        )

    items.sort(key=lambda it: (-it["flag_count"], it["content_type"], it["content_id"]))
    return items[:limit]


def flagger_stats(session, limit: int = 50) -> list[dict]:
    """Return per-user flagging behaviour, busiest flaggers first.

    For every user who has ever flagged something, reports how many flags they
    raised in total, how many were ``upheld`` (good reports), and how many were
    ``dismissed`` (false reports), plus a ``false_rate`` over the *resolved*
    flags. This surfaces repeat flaggers and likely false reporters for
    moderators. *limit* (1..500) bounds the result. Each entry is ``{user_id,
    total, upheld, dismissed, open, false_rate}``.
    """
    if limit < 1:
        raise ModerationError("limit must be >= 1")
    limit = min(limit, 500)

    rows = session.execute(
        select(Flag.user_id, Flag.status, func.count())
        .group_by(Flag.user_id, Flag.status)
    ).all()
    stats: dict[str, dict] = {}
    for user_id, status, n in rows:
        entry = stats.setdefault(
            user_id, {"total": 0, "upheld": 0, "dismissed": 0, "open": 0}
        )
        entry["total"] += int(n)
        if status in entry:
            entry[status] += int(n)

    result = []
    for user_id, entry in stats.items():
        resolved = entry["upheld"] + entry["dismissed"]
        false_rate = entry["dismissed"] / resolved if resolved else 0.0
        result.append(
            {
                "user_id": user_id,
                "total": entry["total"],
                "upheld": entry["upheld"],
                "dismissed": entry["dismissed"],
                "open": entry["open"],
                "false_rate": round(false_rate, 3),
            }
        )

    result.sort(key=lambda it: (-it["total"], it["user_id"]))
    return result[:limit]


def list_actions(
    session, content_type: str, content_id: int, limit: int = 100
) -> list[dict]:
    """Return the audit trail for one content item, newest action first.

    Reads back the :class:`ModerationAction` rows for *(content_type,
    content_id)* so a moderator can see the full history of decisions on an
    item. Each entry is ``{action, moderator, detail, created_at}`` with an ISO
    timestamp. *limit* (1..500) bounds the result.
    """
    if content_type not in _CONTENT_TYPES:
        raise ModerationError(f"unknown content_type {content_type!r}")
    if limit < 1:
        raise ModerationError("limit must be >= 1")
    limit = min(limit, 500)
    rows = session.scalars(
        select(ModerationAction)
        .where(
            ModerationAction.content_type == content_type,
            ModerationAction.content_id == content_id,
        )
        .order_by(ModerationAction.created_at.desc(), ModerationAction.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "action": a.action,
            "moderator": a.moderator,
            "detail": a.detail,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


def list_notifications(
    session, user_id: str, unread_only: bool = False, limit: int = 50
) -> list[dict]:
    """Return *user_id*'s moderation notifications, newest first.

    These tell the owner their content was hidden/deleted after review. Set
    *unread_only* to return just the unseen ones. *limit* (1..200) bounds the
    result. Each entry is ``{id, content_type, content_id, action, message,
    read, created_at}``. Raises :class:`ModerationError` on empty *user_id*.
    """
    name = _clean(user_id, "user_id")
    if limit < 1:
        raise ModerationError("limit must be >= 1")
    limit = min(limit, 200)
    query = select(ModerationNotification).where(
        ModerationNotification.user_id == name
    )
    if unread_only:
        query = query.where(ModerationNotification.read.is_(False))
    rows = session.scalars(
        query.order_by(
            ModerationNotification.created_at.desc(),
            ModerationNotification.id.desc(),
        ).limit(limit)
    ).all()
    return [
        {
            "id": n.id,
            "content_type": n.content_type,
            "content_id": n.content_id,
            "action": n.action,
            "message": n.message,
            "read": n.read,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in rows
    ]
