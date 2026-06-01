"""Saved-search service: persist and recall a user's advanced-filter presets.

A saved search is keyed by ``(user_id, name)`` so a user can keep several named
presets but not two with the same name. ``query_params`` stores the raw search
query string (everything after ``?`` on ``/api/search``) so recalling a preset
yields a directly shareable URL and survives new filters being added without a
schema change. A user's saved searches are private — only ever returned for the
requesting ``user_id``.
"""

import datetime as dt

from sqlalchemy import select

from src.models import SavedSearch

_MAX_NAME_LEN = 100
_MAX_PARAMS_LEN = 2000


class SavedSearchError(ValueError):
    """Raised for invalid saved-search operations (bad input, name clash, 404).

    ``not_found`` distinguishes a missing saved search (HTTP 404) from a
    bad-input or duplicate-name validation failure (HTTP 400) so the API layer
    can pick a status without string-matching the message.
    """

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _clean_user(user_id: str) -> str:
    name = (user_id or "").strip()
    if not name:
        raise SavedSearchError("user_id must not be empty")
    return name


def _serialize(saved: SavedSearch) -> dict:
    return {
        "id": saved.id,
        "name": saved.name,
        "query_params": saved.query_params,
        "created_at": saved.created_at.isoformat() if saved.created_at else None,
    }


def create_saved_search(
    session, user_id: str, name: str, query_params: str
) -> dict:
    """Persist a named filter preset for *user_id* and return its serialised form.

    *name* and *query_params* are required and length-bounded; a leading ``?`` on
    *query_params* is stripped so callers may pass a full URL suffix or a bare
    query string interchangeably. Raises :class:`SavedSearchError` for empty or
    over-long input, or when the user already has a preset with the same *name*.
    """
    user = _clean_user(user_id)
    clean_name = (name or "").strip()
    if not clean_name:
        raise SavedSearchError("name must not be empty")
    if len(clean_name) > _MAX_NAME_LEN:
        raise SavedSearchError(f"name must be at most {_MAX_NAME_LEN} characters")

    params = (query_params or "").strip().lstrip("?")
    if not params:
        raise SavedSearchError("query_params must not be empty")
    if len(params) > _MAX_PARAMS_LEN:
        raise SavedSearchError(
            f"query_params must be at most {_MAX_PARAMS_LEN} characters"
        )

    existing = session.scalars(
        select(SavedSearch).where(
            SavedSearch.user_id == user, SavedSearch.name == clean_name
        )
    ).first()
    if existing is not None:
        raise SavedSearchError(f"a saved search named {clean_name!r} already exists")

    saved = SavedSearch(
        user_id=user, name=clean_name, query_params=params, created_at=_now()
    )
    session.add(saved)
    session.commit()
    session.refresh(saved)
    return _serialize(saved)


def list_saved_searches(session, user_id: str) -> list[dict]:
    """Return *user_id*'s saved searches, newest first.

    Returns an empty list for a user with no presets. Raises
    :class:`SavedSearchError` only for an empty *user_id*.
    """
    user = _clean_user(user_id)
    rows = session.scalars(
        select(SavedSearch)
        .where(SavedSearch.user_id == user)
        .order_by(SavedSearch.created_at.desc(), SavedSearch.id.desc())
    ).all()
    return [_serialize(row) for row in rows]


def delete_saved_search(session, user_id: str, saved_id: int) -> None:
    """Delete saved search *saved_id* owned by *user_id*.

    Raises :class:`SavedSearchError` with ``not_found=True`` when no such row
    exists for this user (a missing id or one owned by someone else), so a user
    can never delete another user's preset.
    """
    user = _clean_user(user_id)
    saved = session.scalars(
        select(SavedSearch).where(
            SavedSearch.id == saved_id, SavedSearch.user_id == user
        )
    ).first()
    if saved is None:
        raise SavedSearchError("saved search not found", not_found=True)
    session.delete(saved)
    session.commit()
