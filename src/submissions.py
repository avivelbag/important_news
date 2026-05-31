"""User story submission service: validation, duplicate detection, lifecycle.

A *submission* is a community-contributed story that enters a moderation queue
before becoming a real :class:`~src.models.Story`. This module covers the whole
lifecycle:

* **auto-categorisation** — :func:`categorize` keyword-matches the title,
  description, and URL host against AI and Aerospace vocabularies and returns
  ``"ai"``, ``"aerospace"``, ``"both"``, or ``"unknown"``.
* **duplicate detection** — :func:`find_duplicates` compares a candidate against
  existing stories by normalised URL (domain + path match) and by fuzzy title
  similarity, reusing :mod:`src.deduplicator` so submissions are deduped exactly
  like scraped stories. :func:`create_submission` rejects a candidate that clears
  the duplicate threshold so the same article cannot be submitted twice.
* **moderation** — :func:`list_pending` returns the FIFO queue;
  :func:`approve_submission` mints a Story, links it back, and awards the
  submitter karma (the visibility threshold); :func:`reject_submission` closes
  the row without minting anything. Both decisions are idempotent.
"""

import datetime as dt

from sqlalchemy import select

import src.deduplicator as deduplicator
from src.models import Story, Submission, UserProfile

# Karma granted to a submitter when their submission is approved (i.e. crosses
# the visibility threshold and becomes a live article).
SUBMISSION_KARMA = 5

# A candidate at or above this title similarity to an existing story is treated
# as a duplicate. Matches the deduplicator's default so submissions and scraped
# stories collapse on the same rule.
DUPLICATE_THRESHOLD = deduplicator.DEFAULT_TITLE_THRESHOLD

_VALID_CATEGORIES = ("ai", "aerospace", "both", "unknown")

# Lower-cased keyword vocabularies for auto-categorisation. Kept deliberately
# small and high-precision: a stray match should not misfile a submission.
_AI_KEYWORDS = frozenset(
    {
        "ai",
        "artificial intelligence",
        "machine learning",
        "deep learning",
        "neural network",
        "neural",
        "llm",
        "language model",
        "gpt",
        "transformer",
        "openai",
        "anthropic",
        "deepmind",
        "chatbot",
        "agentic",
        "inference",
    }
)
_AEROSPACE_KEYWORDS = frozenset(
    {
        "aerospace",
        "rocket",
        "spacecraft",
        "satellite",
        "orbit",
        "orbital",
        "launch",
        "nasa",
        "spacex",
        "blue origin",
        "starship",
        "falcon",
        "aviation",
        "aircraft",
        "propulsion",
        "lunar",
        "mars",
        "astronaut",
    }
)


class SubmissionError(ValueError):
    """Raised for invalid submissions (bad input, duplicate, unknown id)."""

    def __init__(self, message: str, not_found: bool = False) -> None:
        super().__init__(message)
        # Lets the API layer map unknown-id errors to 404 and the rest to 400.
        self.not_found = not_found


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _matches_any(text: str, keywords: frozenset) -> bool:
    """True if any whole-word/phrase in *keywords* occurs in *text*.

    Single-token keywords match on word boundaries (so "ai" does not fire on
    "said"); multi-word phrases match as a substring (their internal spaces
    already bound them). *text* is expected pre-lowercased.
    """
    tokens = set(text.replace("/", " ").replace("-", " ").replace(".", " ").split())
    for kw in keywords:
        if " " in kw:
            if kw in text:
                return True
        elif kw in tokens:
            return True
    return False


def categorize(title: str, description: str = "", url: str = "") -> str:
    """Return the topic category for a submission from its text and URL.

    Keyword-matches the combined lower-cased *title*, *description*, and *url*
    host/path against the AI and Aerospace vocabularies. Returns ``"both"`` when
    both topics match, the single matching topic when only one does, and
    ``"unknown"`` when neither matches (so the moderator can categorise by hand).
    """
    blob = " ".join(part for part in (title, description, url) if part).lower()
    is_ai = _matches_any(blob, _AI_KEYWORDS)
    is_aero = _matches_any(blob, _AEROSPACE_KEYWORDS)
    if is_ai and is_aero:
        return "both"
    if is_ai:
        return "ai"
    if is_aero:
        return "aerospace"
    return "unknown"


def find_duplicates(
    session, title: str, url: str | None = None, threshold: float = DUPLICATE_THRESHOLD
) -> list[dict]:
    """Return existing stories that duplicate the candidate, most similar first.

    A stored story is a duplicate when its normalised URL equals the candidate's
    (domain + path match, ignoring scheme/``www``/query/trailing slash) or when
    its title clears *threshold* under :func:`deduplicator.title_similarity`. A
    URL match scores ``1.0``. Each result is ``{story_id, title, url, similarity,
    reason}`` where ``reason`` is ``"url"`` or ``"title"``. Reads only — used both
    by the form's live preview and by :func:`create_submission`'s guard.
    """
    candidate_url = deduplicator.normalize_url(url) if url else ""
    stories = session.scalars(select(Story)).all()
    matches: list[dict] = []
    for story in stories:
        reason = None
        similarity = 0.0
        if candidate_url and deduplicator.normalize_url(story.url) == candidate_url:
            reason, similarity = "url", 1.0
        else:
            score = deduplicator.title_similarity(title, story.title)
            if score >= threshold:
                reason, similarity = "title", score
        if reason:
            matches.append(
                {
                    "story_id": story.id,
                    "title": story.title,
                    "url": story.url,
                    "similarity": round(similarity, 4),
                    "reason": reason,
                }
            )
    matches.sort(key=lambda m: m["similarity"], reverse=True)
    return matches


def create_submission(
    session,
    title: str,
    url: str | None = None,
    description: str | None = None,
    user_id: str | None = None,
    category: str | None = None,
) -> Submission:
    """Create and persist a pending submission after validating it.

    *title* must be non-empty after stripping. *url* is optional — a missing or
    empty URL makes this a self-post. The candidate is checked against existing
    stories via :func:`find_duplicates`; a match raises :class:`SubmissionError`
    so the same article cannot be submitted twice. *category* is auto-assigned
    via :func:`categorize` when not supplied (or supplied as a falsy/invalid
    value); an explicit valid category from the form is honoured. The row is
    committed with ``status == "pending"`` and returned.
    """
    clean_title = (title or "").strip()
    if not clean_title:
        raise SubmissionError("title must not be empty")

    clean_url = (url or "").strip() or None
    clean_desc = (description or "").strip() or None

    dupes = find_duplicates(session, clean_title, clean_url)
    if dupes:
        top = dupes[0]
        raise SubmissionError(
            f"duplicate of existing story {top['story_id']} (by {top['reason']})"
        )

    if category not in _VALID_CATEGORIES or category in (None, "unknown"):
        category = categorize(clean_title, clean_desc or "", clean_url or "")

    submission = Submission(
        user_id=(user_id or None),
        title=clean_title,
        url=clean_url,
        description=clean_desc,
        category=category,
        status="pending",
        created_at=_now(),
    )
    session.add(submission)
    session.commit()
    return submission


def list_pending(session, limit: int = 50) -> list[dict]:
    """Return the pending moderation queue, oldest first (FIFO), capped at *limit*.

    Each entry carries ``id``, ``user_id``, ``title``, ``url``, ``description``,
    ``category``, and ``created_at`` (ISO). Raises :class:`SubmissionError` if
    *limit* < 1.
    """
    if limit < 1:
        raise SubmissionError("limit must be >= 1")
    rows = session.scalars(
        select(Submission)
        .where(Submission.status == "pending")
        .order_by(Submission.created_at.asc(), Submission.id.asc())
        .limit(limit)
    ).all()
    return [
        {
            "id": s.id,
            "user_id": s.user_id,
            "title": s.title,
            "url": s.url,
            "description": s.description,
            "category": s.category,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in rows
    ]


def _award_karma(session, user_id: str, points: int) -> None:
    """Add *points* karma to *user_id*'s profile, creating the row if needed."""
    profile = session.scalars(
        select(UserProfile).where(UserProfile.username == user_id)
    ).first()
    if profile is None:
        profile = UserProfile(username=user_id)
        session.add(profile)
    profile.karma = (profile.karma or 0) + points


def approve_submission(session, submission_id: int) -> Story:
    """Approve a pending submission: mint a Story, link it, award karma.

    Raises :class:`SubmissionError` (``not_found``) for an unknown id, or a plain
    :class:`SubmissionError` if the submission was already rejected. Idempotent
    on an already-approved row: the existing Story is returned and no second
    karma award is made. On first approval a :class:`Story` is created with the
    submission's title/url/category (``submitted_by`` = the submitter,
    timestamps = now), the submission is marked ``approved`` and linked via
    ``story_id``, and the submitter is granted :data:`SUBMISSION_KARMA` (the
    visibility threshold reward). A self-post with no URL gets a synthetic
    ``submission:<id>`` URL so the Story's UNIQUE(url) constraint holds.
    """
    submission = session.get(Submission, submission_id)
    if submission is None:
        raise SubmissionError(f"submission {submission_id} does not exist", not_found=True)
    if submission.status == "approved":
        return session.get(Story, submission.story_id)
    if submission.status == "rejected":
        raise SubmissionError(f"submission {submission_id} was already rejected")

    now = _now()
    story = Story(
        title=submission.title,
        url=submission.url or f"submission:{submission.id}",
        source_name="user-submission",
        topic=submission.category,
        submitted_by=submission.user_id,
        published_at=now,
        fetched_at=now,
    )
    session.add(story)
    session.flush()

    submission.status = "approved"
    submission.decided_at = now
    submission.story_id = story.id
    submission.points = SUBMISSION_KARMA
    if submission.user_id:
        _award_karma(session, submission.user_id, SUBMISSION_KARMA)

    session.commit()
    return story


def reject_submission(session, submission_id: int) -> Submission:
    """Reject a pending submission without minting a story or awarding karma.

    Raises :class:`SubmissionError` (``not_found``) for an unknown id, or a plain
    :class:`SubmissionError` if the submission was already approved. Idempotent on
    an already-rejected row. Marks the row ``rejected`` with ``decided_at`` and
    returns it.
    """
    submission = session.get(Submission, submission_id)
    if submission is None:
        raise SubmissionError(f"submission {submission_id} does not exist", not_found=True)
    if submission.status == "approved":
        raise SubmissionError(f"submission {submission_id} was already approved")
    if submission.status == "rejected":
        return submission

    submission.status = "rejected"
    submission.decided_at = _now()
    session.commit()
    return submission
