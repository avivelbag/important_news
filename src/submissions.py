import datetime as dt

from sqlalchemy import select

import src.deduplicator as deduplicator
from src.models import Story, Submission, UserProfile

SUBMISSION_KARMA = 5

DUPLICATE_THRESHOLD = deduplicator.DEFAULT_TITLE_THRESHOLD

_VALID_CATEGORIES = ("ai", "aerospace", "both", "unknown")

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
    def __init__(self, message: str, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _matches_any(text: str, keywords: frozenset) -> bool:
    # Single-token keywords match on word boundaries so "ai" does not fire on
    # "said"; multi-word phrases are matched as substrings (their spaces bound
    # them already).
    tokens = set(text.replace("/", " ").replace("-", " ").replace(".", " ").split())
    for kw in keywords:
        if " " in kw:
            if kw in text:
                return True
        elif kw in tokens:
            return True
    return False


def categorize(title: str, description: str = "", url: str = "") -> str:
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


def _title_or_url_match(
    candidate_title: str, candidate_url: str, other_title: str, other_url, threshold: float
):
    if candidate_url and other_url and deduplicator.normalize_url(other_url) == candidate_url:
        return "url", 1.0
    score = deduplicator.title_similarity(candidate_title, other_title)
    if score >= threshold:
        return "title", score
    return None, 0.0


def find_duplicates(
    session, title: str, url: str | None = None, threshold: float = DUPLICATE_THRESHOLD
) -> list[dict]:
    candidate_url = deduplicator.normalize_url(url) if url else ""
    matches: list[dict] = []

    for story in session.scalars(select(Story)).all():
        reason, similarity = _title_or_url_match(
            title, candidate_url, story.title, story.url, threshold
        )
        if reason:
            matches.append(
                {
                    "story_id": story.id,
                    "submission_id": None,
                    "title": story.title,
                    "url": story.url,
                    "similarity": round(similarity, 4),
                    "reason": reason,
                }
            )

    # Also collapse against still-pending submissions: two users submitting the
    # same URL/title must not both clear the gate and mint duplicate stories on
    # approval.
    pending = session.scalars(
        select(Submission).where(Submission.status == "pending")
    ).all()
    for sub in pending:
        reason, similarity = _title_or_url_match(
            title, candidate_url, sub.title, sub.url, threshold
        )
        if reason:
            matches.append(
                {
                    "story_id": None,
                    "submission_id": sub.id,
                    "title": sub.title,
                    "url": sub.url,
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
    clean_title = (title or "").strip()
    if not clean_title:
        raise SubmissionError("title must not be empty")

    clean_url = (url or "").strip() or None
    clean_desc = (description or "").strip() or None

    dupes = find_duplicates(session, clean_title, clean_url)
    if dupes:
        top = dupes[0]
        target = top["story_id"] if top["story_id"] is not None else f"submission {top['submission_id']}"
        raise SubmissionError(f"duplicate of existing {target} (by {top['reason']})")

    if not category or category not in _VALID_CATEGORIES or category == "unknown":
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
    profile = session.scalars(
        select(UserProfile).where(UserProfile.username == user_id)
    ).first()
    if profile is None:
        profile = UserProfile(username=user_id)
        session.add(profile)
    profile.karma = (profile.karma or 0) + points


def approve_submission(session, submission_id: int) -> Story:
    submission = session.get(Submission, submission_id)
    if submission is None:
        raise SubmissionError(f"submission {submission_id} does not exist", not_found=True)
    if submission.status == "approved":
        return session.get(Story, submission.story_id)
    if submission.status == "rejected":
        raise SubmissionError(f"submission {submission_id} was already rejected")

    now = _now()
    # A self-post has no URL; give it a synthetic one so Story's UNIQUE(url) holds.
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
