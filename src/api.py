"""FastAPI app exposing a JSON full-text search endpoint over stored stories.

Run with ``uvicorn src.api:app``. The static site (``docs/``) calls
``/api/search`` for its live search box.
"""

import uuid
from pathlib import Path

from fastapi import Body, Cookie, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.comments import (
    CommentError,
    delete_comment,
    get_thread,
    post_comment,
    vote_comment,
)
from src.db import get_engine, get_session, init_db
from src.profiles import (
    ProfileError,
    get_profile,
    get_user_articles,
    get_user_comments,
    leaderboard,
    set_private,
)
from src.search import SearchError, search_stories
from src.source_health import health_dashboard
from src.submissions import (
    SubmissionError,
    approve_submission,
    create_submission,
    find_duplicates,
    list_pending,
    reject_submission,
)
from src.voting import VoteError, cast_vote, get_distribution

app = FastAPI(title="Important News Search")

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "ui" / "templates")
)

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
) -> list[dict]:
    """Return search results for *q*, optionally filtered by *category*.

    Responds 400 when the query fails length validation; otherwise a JSON array
    of ``{id, title, description, url, source, date, score}`` objects ordered by
    relevance then recency.
    """
    session = _session()
    try:
        return search_stories(session, q, category)
    except SearchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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


def _comment_status(exc: CommentError) -> int:
    """Map a CommentError to 404 for unknown ids, else 400 for bad input."""
    return 404 if exc.not_found else 400


@app.get("/api/articles/{article_id}/comments")
def api_get_comments(article_id: int) -> list[dict]:
    """Return the nested comment thread for *article_id*; 404 if it is unknown."""
    session = _session()
    try:
        return get_thread(session, article_id)
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
    """Map a SubmissionError to 404 for unknown ids, else 400 for bad input."""
    return 404 if exc.not_found else 400


@app.post("/api/submissions")
def api_create_submission(
    payload: dict = Body(...),
    voter_id: str | None = Cookie(default=None),
) -> JSONResponse:
    """Create a pending user submission and return it.

    Accepts ``title`` (required), optional ``url`` (omit for a self-post),
    ``description``, and ``category`` in the JSON body. The submitter id defaults
    to the ``voter_id`` cookie (a fresh uuid is minted and set when absent).
    Responds 400 for an empty title or when the candidate duplicates an existing
    story; 201 with the created submission otherwise.
    """
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
    """Return the pending moderation queue (oldest first)."""
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
    """Return existing stories that would duplicate a candidate (live preview)."""
    session = _session()
    try:
        return find_duplicates(session, title, url)
    finally:
        session.close()


@app.post("/api/submissions/{submission_id}/approve")
def api_approve_submission(submission_id: int) -> dict:
    """Approve a submission, promoting it to a story; 404 if unknown."""
    session = _session()
    try:
        story = approve_submission(session, submission_id)
        return {"submission_id": submission_id, "story_id": story.id, "status": "approved"}
    except SubmissionError as exc:
        raise HTTPException(status_code=_submission_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.post("/api/submissions/{submission_id}/reject")
def api_reject_submission(submission_id: int) -> dict:
    """Reject a pending submission; 404 if unknown."""
    session = _session()
    try:
        submission = reject_submission(session, submission_id)
        return {"submission_id": submission.id, "status": submission.status}
    except SubmissionError as exc:
        raise HTTPException(status_code=_submission_status(exc), detail=str(exc)) from exc
    finally:
        session.close()


@app.get("/submit", response_class=HTMLResponse)
def submit_page(request: Request) -> HTMLResponse:
    """Render the submission form."""
    return _TEMPLATES.TemplateResponse(request, "submit.html", {})


@app.get("/moderation", response_class=HTMLResponse)
def moderation_page(request: Request) -> HTMLResponse:
    """Render the moderation queue of pending submissions."""
    session = _session()
    try:
        pending = list_pending(session, 200)
    finally:
        session.close()
    return _TEMPLATES.TemplateResponse(
        request, "moderation.html", {"submissions": pending}
    )


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
