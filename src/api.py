"""FastAPI app exposing a JSON full-text search endpoint over stored stories.

Run with ``uvicorn src.api:app``. The static site (``docs/``) calls
``/api/search`` for its live search box.
"""

import os
import uuid
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.bookmarks import (
    BookmarkError,
    bulk_remove_bookmarks,
    list_bookmarks,
    remove_bookmark,
    toggle_bookmark,
)
from src.comments import (
    CommentError,
    cast_comment_vote,
    delete_comment,
    get_comment_votes,
    get_thread,
    post_comment,
    vote_comment,
)
from src.db import get_engine, get_session, init_db
from src.moderation import (
    ModerationError,
    delete_content,
    dismiss_flags,
    flag_content,
    flagger_stats,
    hide_content,
    list_actions,
    list_flagged,
    list_notifications,
)
from src.merge_service import (
    MergeError,
    list_duplicate_flags,
    list_merges,
    merge_articles,
    merged_into,
    potential_duplicates,
    rollback_merge,
)
from src.profiles import (
    ProfileError,
    get_profile,
    get_user_articles,
    get_user_comments,
    leaderboard,
    set_private,
)
from src.recommendation import (
    RecommendationError,
    get_preferences,
    personalized_feed,
    set_preferences,
)
from src.saved_searches import (
    SavedSearchError,
    create_saved_search,
    delete_saved_search,
    list_saved_searches,
)
from src.search import SearchError, build_filters, search_stories
from src.source_health import health_dashboard
from src.submissions import (
    SubmissionError,
    approve_submission,
    create_submission,
    find_duplicates,
    list_pending,
    reject_submission,
)
from src.topics import (
    TopicError,
    auto_tag_story,
    follow_topic,
    followed_feed,
    get_topic,
    list_followed,
    list_topics,
    suggest_topics,
    tag_story,
    topic_analytics,
    topic_stories,
    unfollow_topic,
)
from src.voting import VoteError, cast_vote, get_distribution

app = FastAPI(title="Important News Search")

_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
_TEMPLATES = Jinja2Templates(directory=str(_UI_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(_UI_DIR / "static")), name="static")

_engine = None


def _session():
    """Return a session bound to a lazily-created, schema-initialised engine."""
    global _engine
    if _engine is None:
        _engine = get_engine()
        init_db(_engine)
    return get_session(_engine)


@app.get("/api/search")
def api_search(
    q: str = Query(..., description="search query, 2-100 chars"),
    category: str | None = Query(None, description="optional topic filter"),
    sources: str | None = Query(None, description="comma-separated source names"),
    topics: str | None = Query(None, description="comma-separated topic slugs"),
    min_score: int | None = Query(None, description="minimum story score"),
    max_score: int | None = Query(None, description="maximum story score"),
    min_comments: int | None = Query(None, description="minimum comment count"),
    date_from: str | None = Query(None, description="ISO 8601 earliest date"),
    date_to: str | None = Query(None, description="ISO 8601 latest date"),
    sort: str = Query("relevance", description="relevance | recent | score"),
) -> list[dict]:
    """Return search results for *q*, refined by the advanced filter params.

    The optional filters (``sources``, ``topics``, ``min_score``/``max_score``,
    ``min_comments``, ``date_from``/``date_to``, ``sort``) are AND-combined and
    encoded entirely in the query string, so a filtered search is shareable by
    URL. Responds 400 when the query fails length validation or any filter value
    is malformed; otherwise a JSON array of ``{id, title, description, url,
    source, date, score}`` objects ordered per ``sort``.
    """
    session = _session()
    try:
        filters = build_filters(
            sources=sources,
            topics=topics,
            min_score=min_score,
            max_score=max_score,
            min_comments=min_comments,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
        )
        return search_stories(session, q, category, filters=filters)
    except SearchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/user/saved-searches")
def api_create_saved_search(
    payload: dict = Body(...),
    voter_id: str | None = Cookie(default=None),
) -> JSONResponse:
    """Store the caller's named search-filter preset and return it.

    Identifies the user by the ``voter_id`` cookie (minting one on first use,
    set on the response). Expects ``name`` and ``query_params`` in the JSON body;
    responds 400 for missing/duplicate/over-long input.
    """
    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())

    session = _session()
    try:
        saved = create_saved_search(
            session,
            voter_id,
            payload.get("name"),
            payload.get("query_params"),
        )
    except SavedSearchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()

    response = JSONResponse(saved, status_code=201)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.get("/api/user/saved-searches")
def api_list_saved_searches(
    voter_id: str | None = Cookie(default=None),
) -> list[dict]:
    """Return the caller's saved searches (newest first); empty list if none."""
    if voter_id is None:
        return []
    session = _session()
    try:
        return list_saved_searches(session, voter_id)
    except SavedSearchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.delete("/api/user/saved-searches/{saved_id}")
def api_delete_saved_search(
    saved_id: int,
    voter_id: str | None = Cookie(default=None),
) -> dict:
    """Delete one of the caller's saved searches; 404 if it is not theirs."""
    if voter_id is None:
        raise HTTPException(status_code=404, detail="saved search not found")
    session = _session()
    try:
        delete_saved_search(session, voter_id, saved_id)
        return {"deleted": saved_id}
    except SavedSearchError as exc:
        status = 404 if exc.not_found else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/vote")
def api_vote(
    payload: dict = Body(...),
    voter_id: str | None = Cookie(default=None),
) -> JSONResponse:
    """Cast/update the caller's vote on a story and return its distribution.

    Reads the anonymous voter id from the ``voter_id`` cookie, minting a fresh
    uuid (set on the response) when absent. Accepts ``story_id`` or
    ``article_id`` plus ``vote_value`` (-1/0/+1) in the JSON body. Responds 400
    for an invalid value and 404 when the story does not exist.
    """
    story_id = payload.get("story_id", payload.get("article_id"))
    vote_value = payload.get("vote_value")
    if story_id is None or vote_value is None:
        raise HTTPException(status_code=400, detail="story_id and vote_value required")

    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())

    session = _session()
    try:
        distribution = cast_vote(session, int(story_id), voter_id, int(vote_value))
    except VoteError as exc:
        # An unknown story is a 404; any other validation failure is a 400.
        status = 404 if "does not exist" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()

    response = JSONResponse(distribution)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.get("/api/articles/{article_id}/votes")
def api_article_votes(article_id: int) -> dict:
    """Return the vote distribution for *article_id*; 404 if it does not exist."""
    session = _session()
    try:
        return get_distribution(session, article_id)
    except VoteError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        session.close()


def _bookmark_status(exc: BookmarkError) -> int:
    """Map a BookmarkError to 404 for unknown story/bookmark, else 400."""
    return 404 if exc.not_found else 400


@app.post("/api/articles/{article_id}/bookmark")
def api_toggle_bookmark(
    article_id: int,
    voter_id: str | None = Cookie(default=None),
) -> JSONResponse:
    """Toggle the caller's bookmark on *article_id* and return its new state.

    Reads the anonymous user id from the ``voter_id`` cookie, minting a fresh
    uuid (set on the response) when absent. Responds 404 when the story does not
    exist. The body carries ``{story_id, bookmarked, bookmark_count}``.
    """
    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())

    session = _session()
    try:
        result = toggle_bookmark(session, article_id, voter_id)
    except BookmarkError as exc:
        raise HTTPException(
            status_code=_bookmark_status(exc), detail=str(exc)
        ) from exc
    finally:
        session.close()

    response = JSONResponse(result)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.delete("/api/articles/{article_id}/bookmark")
def api_remove_bookmark(
    article_id: int,
    voter_id: str | None = Cookie(default=None),
) -> dict:
    """Explicitly remove the caller's bookmark on *article_id* (idempotent).

    Responds 400 when no ``voter_id`` cookie is present (nothing to remove for an
    unidentified caller) and 404 when the story does not exist.
    """
    if voter_id is None:
        raise HTTPException(status_code=400, detail="voter_id cookie required")
    session = _session()
    try:
        return remove_bookmark(session, article_id, voter_id)
    except BookmarkError as exc:
        raise HTTPException(
            status_code=_bookmark_status(exc), detail=str(exc)
        ) from exc
    finally:
        session.close()


@app.get("/api/user/bookmarks")
def api_list_bookmarks(
    voter_id: str | None = Cookie(default=None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    category: str | None = Query(None, description="optional topic filter"),
) -> dict:
    """Return the caller's private, paginated bookmark list (newest first).

    The list is scoped to the ``voter_id`` cookie, so a caller only ever sees
    their own saves. Without the cookie the caller has saved nothing, so an empty
    page is returned rather than an error. Supports a ``category`` topic filter.
    """
    if voter_id is None:
        return {
            "user_id": None,
            "page": page,
            "per_page": per_page,
            "total": 0,
            "items": [],
        }
    session = _session()
    try:
        return list_bookmarks(session, voter_id, page, per_page, category)
    except BookmarkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/user/bookmarks/bulk-delete")
def api_bulk_delete_bookmarks(
    payload: dict = Body(...),
    voter_id: str | None = Cookie(default=None),
) -> dict:
    """Bulk-remove the caller's bookmarks for the ``story_ids`` in the body.

    Responds 400 when no ``voter_id`` cookie is present or ``story_ids`` is not a
    list. Ids the caller has not bookmarked are skipped. Returns ``{removed}``.
    """
    if voter_id is None:
        raise HTTPException(status_code=400, detail="voter_id cookie required")
    story_ids = payload.get("story_ids")
    if not isinstance(story_ids, list):
        raise HTTPException(status_code=400, detail="story_ids (list) required")
    session = _session()
    try:
        return bulk_remove_bookmarks(session, [int(s) for s in story_ids], voter_id)
    except BookmarkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


def _comment_status(exc: CommentError) -> int:
    """Map a CommentError to 404 for unknown ids, else 400 for bad input."""
    return 404 if exc.not_found else 400


@app.get("/api/articles/{article_id}/comments")
def api_get_comments(
    article_id: int,
    sort: str = Query("score", description="score | newest | oldest"),
    voter_id: str | None = Cookie(default=None),
) -> list[dict]:
    """Return the nested comment thread for *article_id*; 404 if it is unknown.

    ``sort`` orders sibling comments by ``score`` (default), ``newest``, or
    ``oldest``; an unknown value is a 400. Each node reports the caller's own
    ``user_vote`` (read from the ``voter_id`` cookie) so the UI can restore vote
    state, plus a ``collapsed`` flag for low-score comments.
    """
    session = _session()
    try:
        return get_thread(session, article_id, sort=sort, user_id=voter_id)
    except CommentError as exc:
        raise HTTPException(status_code=_comment_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/comments")
def api_post_comment(
    payload: dict = Body(...),
    voter_id: str | None = Cookie(default=None),
) -> JSONResponse:
    """Post a new comment (or reply) and return the created comment.

    Accepts ``story_id`` or ``article_id``, ``body``, and optional
    ``parent_comment_id`` in the JSON body. The author id defaults to the
    ``voter_id`` cookie (a fresh uuid is minted and set when absent). Responds
    400 for missing/empty body or a cross-story parent, 404 for an unknown
    story or parent comment.
    """
    story_id = payload.get("story_id", payload.get("article_id"))
    body = payload.get("body")
    if story_id is None or body is None:
        raise HTTPException(status_code=400, detail="story_id and body required")

    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())

    session = _session()
    try:
        comment = post_comment(
            session,
            int(story_id),
            body,
            user_id=voter_id,
            parent_comment_id=payload.get("parent_comment_id"),
        )
        result = {
            "id": comment.id,
            "story_id": comment.story_id,
            "parent_comment_id": comment.parent_comment_id,
            "user_id": comment.user_id,
            "body": comment.body,
            "vote_count": comment.vote_count,
        }
    except CommentError as exc:
        raise HTTPException(status_code=_comment_status(exc), detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()

    response = JSONResponse(result, status_code=201)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.delete("/api/comments/{comment_id}")
def api_delete_comment(comment_id: int) -> dict:
    """Soft-delete *comment_id* (keeps the row); 404 if it does not exist."""
    session = _session()
    try:
        comment = delete_comment(session, comment_id)
        return {"id": comment.id, "deleted": comment.deleted}
    except CommentError as exc:
        raise HTTPException(status_code=_comment_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/comments/{comment_id}/vote")
def api_vote_comment(comment_id: int, payload: dict = Body(...)) -> dict:
    """Apply a -1/+1 vote to *comment_id* and return its new vote_count.

    Responds 400 for a missing/invalid ``vote_value`` and 404 when the comment
    does not exist.
    """
    vote_value = payload.get("vote_value")
    if vote_value is None:
        raise HTTPException(status_code=400, detail="vote_value required")
    session = _session()
    try:
        new_count = vote_comment(session, comment_id, int(vote_value))
        return {"id": comment_id, "vote_count": new_count}
    except CommentError as exc:
        raise HTTPException(status_code=_comment_status(exc), detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


def _comment_vote(comment_id: int, direction: int, voter_id: str | None) -> JSONResponse:
    """Toggle *voter_id*'s vote on *comment_id* in *direction* (+1/-1).

    Mints a voter_id cookie when absent. Voting the same direction twice removes
    the vote (toggle); voting the other direction flips it. Returns the comment's
    fresh vote state. 404 for an unknown comment, 400 when the caller is the
    comment's own author or otherwise votes invalidly.
    """
    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())

    session = _session()
    try:
        current = get_comment_votes(session, comment_id, voter_id)["user_vote"]
        # Re-clicking the active arrow clears the vote; otherwise apply direction.
        value = 0 if current == direction else direction
        result = cast_comment_vote(session, comment_id, voter_id, value)
    except CommentError as exc:
        raise HTTPException(status_code=_comment_status(exc), detail=str(exc)) from exc
    finally:
        session.close()

    response = JSONResponse(result)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.post("/api/comments/{comment_id}/upvote")
def api_comment_upvote(
    comment_id: int, voter_id: str | None = Cookie(default=None)
) -> JSONResponse:
    """Upvote *comment_id* for the cookie's voter (toggles off on repeat)."""
    return _comment_vote(comment_id, 1, voter_id)


@app.post("/api/comments/{comment_id}/downvote")
def api_comment_downvote(
    comment_id: int, voter_id: str | None = Cookie(default=None)
) -> JSONResponse:
    """Downvote *comment_id* for the cookie's voter (toggles off on repeat)."""
    return _comment_vote(comment_id, -1, voter_id)


@app.get("/api/comments/{comment_id}/votes")
def api_comment_votes(
    comment_id: int, voter_id: str | None = Cookie(default=None)
) -> dict:
    """Return *comment_id*'s vote counts and the caller's vote state; 404 if unknown."""
    session = _session()
    try:
        return get_comment_votes(session, comment_id, voter_id)
    except CommentError as exc:
        raise HTTPException(status_code=_comment_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


def _profile_status(exc: ProfileError) -> int:
    """Map a ProfileError to 404 for unknown/private users, else 400."""
    msg = str(exc)
    return 404 if ("does not exist" in msg or "is private" in msg) else 400


# Declared before the /{username} route so the literal path wins the match.
@app.get("/api/users/leaderboard")
def api_leaderboard(
    limit: int = Query(10, ge=1, le=100, description="top N users to return"),
) -> list[dict]:
    """Return the top users ranked by karma (private profiles excluded)."""
    session = _session()
    try:
        return leaderboard(session, limit)
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/users/{username}")
def api_user_profile(username: str) -> dict:
    """Return *username*'s public profile; 404 if unknown, stub if private."""
    session = _session()
    try:
        return get_profile(session, username)
    except ProfileError as exc:
        raise HTTPException(status_code=_profile_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/users/{username}/articles")
def api_user_articles(
    username: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> dict:
    """Return *username*'s paginated submitted/upvoted article activity."""
    session = _session()
    try:
        return get_user_articles(session, username, page, per_page)
    except ProfileError as exc:
        raise HTTPException(status_code=_profile_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/users/{username}/comments")
def api_user_comments(
    username: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> dict:
    """Return *username*'s paginated, timestamped comment history."""
    session = _session()
    try:
        return get_user_comments(session, username, page, per_page)
    except ProfileError as exc:
        raise HTTPException(status_code=_profile_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/users/{username}/privacy")
def api_set_privacy(username: str, payload: dict = Body(...)) -> dict:
    """Set *username*'s private-account toggle from the JSON ``is_private`` flag."""
    is_private = payload.get("is_private")
    if not isinstance(is_private, bool):
        raise HTTPException(status_code=400, detail="is_private (bool) required")
    session = _session()
    try:
        profile = set_private(session, username, is_private)
        return {"username": profile.username, "is_private": profile.is_private}
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


def _submission_status(exc: SubmissionError) -> int:
    return 404 if exc.not_found else 400


# Shared moderation secret. Approve/reject are privileged actions, so they are
# gated behind a token (X-Admin-Token header) rather than the open cookie
# identity the rest of the app uses for voting/commenting.
ADMIN_TOKEN = os.environ.get("SUBMISSIONS_ADMIN_TOKEN", "swarm-admin")


def _require_admin(
    x_admin_token: str | None = Header(default=None),
    x_admin_user: str | None = Header(default=None),
) -> str:
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="admin token required")
    # The acting admin's identity, recorded on audited actions (merges, etc.).
    # All admins share one token, so the client names itself via X-Admin-User;
    # absent that we fall back to the generic "admin".
    return (x_admin_user or "").strip() or "admin"


@app.post("/api/submissions")
def api_create_submission(
    payload: dict = Body(...),
    voter_id: str | None = Cookie(default=None),
) -> JSONResponse:
    title = payload.get("title")
    if title is None:
        raise HTTPException(status_code=400, detail="title required")

    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())

    session = _session()
    try:
        submission = create_submission(
            session,
            title,
            url=payload.get("url"),
            description=payload.get("description"),
            user_id=voter_id,
            category=payload.get("category"),
        )
        result = {
            "id": submission.id,
            "user_id": submission.user_id,
            "title": submission.title,
            "url": submission.url,
            "description": submission.description,
            "category": submission.category,
            "status": submission.status,
        }
    except SubmissionError as exc:
        raise HTTPException(status_code=_submission_status(exc), detail=str(exc)) from exc
    finally:
        session.close()

    response = JSONResponse(result, status_code=201)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.get("/api/submissions")
def api_list_submissions(
    limit: int = Query(50, ge=1, le=200, description="max pending rows"),
) -> list[dict]:
    session = _session()
    try:
        return list_pending(session, limit)
    except SubmissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/submissions/duplicates")
def api_submission_duplicates(
    title: str = Query(..., description="candidate title"),
    url: str | None = Query(None, description="candidate url"),
) -> list[dict]:
    session = _session()
    try:
        return find_duplicates(session, title, url)
    finally:
        session.close()


@app.post("/api/submissions/{submission_id}/approve")
def api_approve_submission(
    submission_id: int, _: None = Depends(_require_admin)
) -> dict:
    session = _session()
    try:
        story = approve_submission(session, submission_id)
        return {"submission_id": submission_id, "story_id": story.id, "status": "approved"}
    except SubmissionError as exc:
        raise HTTPException(status_code=_submission_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/submissions/{submission_id}/reject")
def api_reject_submission(
    submission_id: int, _: None = Depends(_require_admin)
) -> dict:
    session = _session()
    try:
        submission = reject_submission(session, submission_id)
        return {"submission_id": submission.id, "status": submission.status}
    except SubmissionError as exc:
        raise HTTPException(status_code=_submission_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


def _moderation_status(exc: ModerationError) -> int:
    return 404 if exc.not_found else 400


def _flag(content_type: str, content_id: int, payload: dict, voter_id: str | None):
    reason = payload.get("reason")
    if reason is None:
        raise HTTPException(status_code=400, detail="reason required")

    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())

    session = _session()
    try:
        result = flag_content(session, content_type, content_id, voter_id, reason)
    except ModerationError as exc:
        raise HTTPException(status_code=_moderation_status(exc), detail=str(exc)) from exc
    finally:
        session.close()
    return result, new_cookie, voter_id


@app.post("/api/stories/{story_id}/flag")
def api_flag_story(
    story_id: int,
    payload: dict = Body(...),
    voter_id: str | None = Cookie(default=None),
) -> JSONResponse:
    result, new_cookie, voter_id = _flag("story", story_id, payload, voter_id)
    response = JSONResponse(result, status_code=201)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.post("/api/comments/{comment_id}/flag")
def api_flag_comment(
    comment_id: int,
    payload: dict = Body(...),
    voter_id: str | None = Cookie(default=None),
) -> JSONResponse:
    result, new_cookie, voter_id = _flag("comment", comment_id, payload, voter_id)
    response = JSONResponse(result, status_code=201)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.get("/api/flags")
def api_list_flags(
    content_type: str | None = Query(None),
    reason: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _: None = Depends(_require_admin),
) -> list[dict]:
    session = _session()
    try:
        return list_flagged(session, content_type, reason, limit)
    except ModerationError as exc:
        raise HTTPException(status_code=_moderation_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/flags/flaggers")
def api_flagger_stats(
    limit: int = Query(50, ge=1, le=500),
    _: None = Depends(_require_admin),
) -> list[dict]:
    session = _session()
    try:
        return flagger_stats(session, limit)
    except ModerationError as exc:
        raise HTTPException(status_code=_moderation_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/flags/{content_type}/{content_id}/hide")
def api_hide_content(
    content_type: str,
    content_id: int,
    _: None = Depends(_require_admin),
) -> dict:
    session = _session()
    try:
        return hide_content(session, content_type, content_id, "admin")
    except ModerationError as exc:
        raise HTTPException(status_code=_moderation_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/flags/{content_type}/{content_id}/delete-content")
def api_delete_content(
    content_type: str,
    content_id: int,
    _: None = Depends(_require_admin),
) -> dict:
    session = _session()
    try:
        return delete_content(session, content_type, content_id, "admin")
    except ModerationError as exc:
        raise HTTPException(status_code=_moderation_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/flags/{content_type}/{content_id}/dismiss")
def api_dismiss_flags(
    content_type: str,
    content_id: int,
    _: None = Depends(_require_admin),
) -> dict:
    session = _session()
    try:
        return dismiss_flags(session, content_type, content_id, "admin")
    except ModerationError as exc:
        raise HTTPException(status_code=_moderation_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/flags/{content_type}/{content_id}/actions")
def api_list_actions(
    content_type: str,
    content_id: int,
    limit: int = Query(100, ge=1, le=500),
    _: None = Depends(_require_admin),
) -> list[dict]:
    session = _session()
    try:
        return list_actions(session, content_type, content_id, limit)
    except ModerationError as exc:
        raise HTTPException(status_code=_moderation_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/user/notifications")
def api_list_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    voter_id: str | None = Cookie(default=None),
) -> list[dict]:
    if not voter_id:
        return []
    session = _session()
    try:
        return list_notifications(session, voter_id, unread_only, limit)
    except ModerationError as exc:
        raise HTTPException(status_code=_moderation_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


def _topic_status(exc: TopicError) -> int:
    """Map a TopicError to 404 for unknown topic/story, else 400."""
    return 404 if exc.not_found else 400


@app.get("/api/topics")
def api_list_topics(parent: str | None = Query(None)) -> dict:
    """List topics, optionally only the children of the ``parent`` slug.

    Responds 404 when ``parent`` is given but unknown. Returns ``{topics}``.
    """
    session = _session()
    try:
        return {"topics": list_topics(session, parent=parent)}
    except TopicError as exc:
        raise HTTPException(status_code=_topic_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


# Declared before /api/topics/{slug} so these literal paths win the match.
@app.get("/api/topics/analytics")
def api_topic_analytics(limit: int = Query(10, ge=1, le=100)) -> dict:
    """Return the most-followed and trending topics."""
    session = _session()
    try:
        return topic_analytics(session, limit)
    finally:
        session.close()


@app.get("/api/topics/suggest")
def api_suggest_topics(
    title: str = Query(..., min_length=1),
    summary: str = Query(""),
) -> dict:
    """Return keyword-suggested topic slugs for a candidate title/summary."""
    return {"slugs": suggest_topics(title, summary)}


@app.get("/api/topics/{slug}")
def api_get_topic(slug: str) -> dict:
    """Return one topic with its description and related topic slugs; 404 if unknown."""
    session = _session()
    try:
        return get_topic(session, slug)
    except TopicError as exc:
        raise HTTPException(status_code=_topic_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/topics/{slug}/stories")
def api_topic_stories(
    slug: str,
    sort: str = Query("recency", description="recency | score"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """Return stories tagged with ``slug`` sorted by recency/score; 404 if unknown."""
    session = _session()
    try:
        return topic_stories(session, slug, sort=sort, limit=limit)
    except TopicError as exc:
        raise HTTPException(status_code=_topic_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/articles/{article_id}/topics")
def api_tag_article(article_id: int, payload: dict = Body(...)) -> dict:
    """Tag ``article_id`` with the ``slugs`` list in the body (idempotent).

    Responds 400 when ``slugs`` is not a list and 404 for an unknown story or
    slug. Returns ``{story_id, topics}`` with the story's full current slug list.
    """
    slugs = payload.get("slugs")
    if not isinstance(slugs, list):
        raise HTTPException(status_code=400, detail="slugs (list) required")
    session = _session()
    try:
        return tag_story(session, article_id, [str(s) for s in slugs])
    except TopicError as exc:
        raise HTTPException(status_code=_topic_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/articles/{article_id}/topics/auto")
def api_auto_tag_article(article_id: int) -> dict:
    """Auto-tag ``article_id`` from its title via keyword matching; 404 if unknown."""
    session = _session()
    try:
        return auto_tag_story(session, article_id)
    except TopicError as exc:
        raise HTTPException(status_code=_topic_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/user/topics")
def api_list_followed_topics(voter_id: str | None = Cookie(default=None)) -> dict:
    """Return the topics the caller (``voter_id`` cookie) follows.

    Without the cookie the caller follows nothing, so an empty list is returned
    rather than an error.
    """
    if voter_id is None:
        return {"topics": []}
    session = _session()
    try:
        return {"topics": list_followed(session, voter_id)}
    except TopicError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/user/topics/feed")
def api_followed_feed(
    voter_id: str | None = Cookie(default=None),
    sort: str = Query("recency", description="recency | score"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """Return the caller's topic-filtered feed (stories from followed topics).

    Without the ``voter_id`` cookie the caller follows nothing, so an empty feed
    is returned rather than an error.
    """
    if voter_id is None:
        return {"user_id": None, "sort": sort, "total": 0, "stories": []}
    session = _session()
    try:
        return followed_feed(session, voter_id, sort=sort, limit=limit)
    except TopicError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/user/topics/{slug}/follow")
def api_follow_topic(
    slug: str, voter_id: str | None = Cookie(default=None)
) -> JSONResponse:
    """Follow ``slug`` for the caller, minting a ``voter_id`` cookie if absent.

    Responds 404 for an unknown topic. Returns ``{slug, following, follower_count}``.
    """
    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())
    session = _session()
    try:
        result = follow_topic(session, voter_id, slug)
    except TopicError as exc:
        raise HTTPException(status_code=_topic_status(exc), detail=str(exc)) from exc
    finally:
        session.close()
    response = JSONResponse(result)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.delete("/api/user/topics/{slug}/follow")
def api_unfollow_topic(
    slug: str, voter_id: str | None = Cookie(default=None)
) -> dict:
    """Unfollow ``slug`` for the caller (idempotent).

    Responds 400 when no ``voter_id`` cookie is present (nothing to unfollow for
    an unidentified caller) and 404 for an unknown topic.
    """
    if voter_id is None:
        raise HTTPException(status_code=400, detail="voter_id cookie required")
    session = _session()
    try:
        return unfollow_topic(session, voter_id, slug)
    except TopicError as exc:
        raise HTTPException(status_code=_topic_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/user/feed")
def api_user_feed(
    voter_id: str | None = Cookie(default=None),
    algorithm: str | None = Query(
        None, description="balanced | trending | recent | followed"
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    # Anonymous callers get user_id None so the homepage falls back to global.
    if voter_id is None:
        return {
            "user_id": None,
            "algorithm": algorithm or "balanced",
            "limit": limit,
            "offset": offset,
            "total": 0,
            "stories": [],
        }
    session = _session()
    try:
        return personalized_feed(
            session, voter_id, algorithm=algorithm, limit=limit, offset=offset
        )
    except RecommendationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/api/user/preferences")
def api_get_preferences(voter_id: str | None = Cookie(default=None)) -> JSONResponse:
    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())
    session = _session()
    try:
        result = get_preferences(session, voter_id)
    except RecommendationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()
    response = JSONResponse(result)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


@app.post("/api/user/preferences")
def api_set_preferences(
    payload: dict = Body(...),
    voter_id: str | None = Cookie(default=None),
) -> JSONResponse:
    new_cookie = voter_id is None
    if new_cookie:
        voter_id = str(uuid.uuid4())
    session = _session()
    try:
        result = set_preferences(
            session,
            voter_id,
            algorithm=payload.get("algorithm"),
            min_score_threshold=payload.get("min_score_threshold"),
            topic_weight=payload.get("topic_weight"),
            source_weight=payload.get("source_weight"),
            recency_weight=payload.get("recency_weight"),
        )
    except RecommendationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.close()
    response = JSONResponse(result)
    if new_cookie:
        response.set_cookie("voter_id", voter_id, httponly=True, samesite="lax")
    return response


def _merge_status_code(exc: MergeError) -> int:
    return 404 if exc.not_found else 400


@app.get("/api/admin/articles/{article_id}/potential-duplicates")
def api_potential_duplicates(
    article_id: int,
    _: str = Depends(_require_admin),
) -> dict:
    session = _session()
    try:
        candidates = potential_duplicates(session, article_id)
        return {"article_id": article_id, "candidates": candidates}
    except MergeError as exc:
        raise HTTPException(
            status_code=_merge_status_code(exc), detail=str(exc)
        ) from exc
    finally:
        session.close()


@app.get("/api/admin/duplicate-flags")
def api_duplicate_flags(
    unresolved_only: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
    _: str = Depends(_require_admin),
) -> dict:
    session = _session()
    try:
        return {"flags": list_duplicate_flags(
            session, unresolved_only=unresolved_only, limit=limit
        )}
    finally:
        session.close()


@app.post("/api/admin/articles/{source_id}/merge-into/{target_id}")
def api_merge_articles(
    source_id: int,
    target_id: int,
    admin: str = Depends(_require_admin),
) -> dict:
    session = _session()
    try:
        return merge_articles(session, source_id, target_id, merged_by=admin)
    except MergeError as exc:
        raise HTTPException(
            status_code=_merge_status_code(exc), detail=str(exc)
        ) from exc
    finally:
        session.close()


@app.post("/api/admin/merges/{merge_id}/rollback")
def api_rollback_merge(
    merge_id: int,
    admin: str = Depends(_require_admin),
) -> dict:
    session = _session()
    try:
        return rollback_merge(session, merge_id, rolled_back_by=admin)
    except MergeError as exc:
        raise HTTPException(
            status_code=_merge_status_code(exc), detail=str(exc)
        ) from exc
    finally:
        session.close()


@app.get("/api/admin/merges")
def api_list_merges(
    active_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    _: str = Depends(_require_admin),
) -> list[dict]:
    session = _session()
    try:
        return list_merges(session, active_only=active_only, limit=limit)
    except MergeError as exc:
        raise HTTPException(
            status_code=_merge_status_code(exc), detail=str(exc)
        ) from exc
    finally:
        session.close()


@app.get("/api/articles/{article_id}/merged-into")
def api_merged_into(article_id: int) -> dict:
    session = _session()
    try:
        return {"article_id": article_id, "merged_into": merged_into(session, article_id)}
    except MergeError as exc:
        raise HTTPException(
            status_code=_merge_status_code(exc), detail=str(exc)
        ) from exc
    finally:
        session.close()


@app.get("/admin/merges", response_class=HTMLResponse)
def admin_merge_page(request: Request) -> HTMLResponse:
    session = _session()
    try:
        merges = list_merges(session, limit=100)
    finally:
        session.close()
    return _TEMPLATES.TemplateResponse(
        request, "admin_merge.html", {"merges": merges}
    )


@app.get("/submit", response_class=HTMLResponse)
def submit_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "submit.html", {})


@app.get("/moderation", response_class=HTMLResponse)
def moderation_page(request: Request) -> HTMLResponse:
    session = _session()
    try:
        pending = list_pending(session, 200)
    finally:
        session.close()
    return _TEMPLATES.TemplateResponse(
        request, "moderation.html", {"submissions": pending}
    )


@app.get("/moderation/flags", response_class=HTMLResponse)
def flag_moderation_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "flag_queue.html", {})


@app.get("/api/sources/health")
def api_sources_health() -> dict:
    """Return the source health dashboard payload as JSON."""
    global _engine
    if _engine is None:
        _engine = get_engine()
        init_db(_engine)
    return health_dashboard(_engine)


@app.get("/health", response_class=HTMLResponse)
def health_page(request: Request) -> HTMLResponse:
    """Render the source health dashboard as an HTML page with status badges."""
    global _engine
    if _engine is None:
        _engine = get_engine()
        init_db(_engine)
    data = health_dashboard(_engine)
    return _TEMPLATES.TemplateResponse(
        request,
        "health.html",
        {"metrics": data["metrics"], "sources": data["sources"]},
    )
