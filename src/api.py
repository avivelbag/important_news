"""FastAPI app exposing a JSON full-text search endpoint over stored stories.

Run with ``uvicorn src.api:app``. The static site (``docs/``) calls
``/api/search`` for its live search box.
"""

from fastapi import FastAPI, HTTPException, Query

from src.db import get_engine, get_session, init_db
from src.search import SearchError, search_stories

app = FastAPI(title="Important News Search")

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
