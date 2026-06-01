"""Source credibility scoring: blend vote ratio, domain authority, and freshness."""

import datetime as dt
from urllib.parse import urlparse

from sqlalchemy import select

import src.db as db
import src.models as models

# Effective-score thresholds for the human-facing tier/badge buckets.
VERIFIED_THRESHOLD = 70.0
COMMUNITY_THRESHOLD = 40.0

TIER_VERIFIED = "verified"
TIER_COMMUNITY = "community"
TIER_UNVERIFIED = "unverified"

_TIER_BADGES = {
    TIER_VERIFIED: "Verified Source",
    TIER_COMMUNITY: "Community Submitted",
    TIER_UNVERIFIED: "Unverified",
}

# Curated domains that are treated as high-authority. Matching is suffix-based
# so "blog.openai.com" still resolves to "openai.com". Scores are 0-100.
_DOMAIN_AUTHORITY = {
    "nasa.gov": 100.0,
    "esa.int": 100.0,
    "arxiv.org": 95.0,
    "nature.com": 95.0,
    "science.org": 95.0,
    "deepmind.com": 90.0,
    "openai.com": 90.0,
    "anthropic.com": 90.0,
    "spacex.com": 90.0,
    "mit.edu": 90.0,
    "stanford.edu": 90.0,
    "arstechnica.com": 80.0,
    "technologyreview.com": 85.0,
    "spacenews.com": 80.0,
    "theverge.com": 70.0,
    "wired.com": 75.0,
    "techcrunch.com": 65.0,
}

# Authority assigned to a source whose domain is not in the curated list. Mid-low
# so an unknown blog starts "unverified" but can climb on strong vote ratios.
_UNKNOWN_DOMAIN_AUTHORITY = 35.0

# Blend weights for the three signals; they sum to 1.0.
_W_VOTE = 0.4
_W_DOMAIN = 0.4
_W_FRESHNESS = 0.2

# A source with no recent content decays to zero freshness over this window.
_FRESHNESS_HORIZON_DAYS = 30.0


def _naive_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def extract_domain(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "//" in url else f"//{url}")
    host = (parsed.netloc or parsed.path).strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0]


def domain_authority(url: str | None) -> float:
    host = extract_domain(url)
    if not host:
        return _UNKNOWN_DOMAIN_AUTHORITY
    for domain, score in _DOMAIN_AUTHORITY.items():
        if host == domain or host.endswith(f".{domain}"):
            return score
    return _UNKNOWN_DOMAIN_AUTHORITY


def vote_ratio(upvotes: int, total_votes: int) -> float:
    # A source with no votes yet is neither endorsed nor rejected, so it gets the
    # neutral midpoint rather than 0 (which would unfairly punish new sources).
    if total_votes <= 0:
        return 0.5
    ratio = upvotes / total_votes
    return min(1.0, max(0.0, ratio))


def freshness_score(
    last_published: dt.datetime | None, now: dt.datetime
) -> float:
    if last_published is None:
        return 0.0
    age_days = (_naive_utc(now) - _naive_utc(last_published)).total_seconds() / 86400.0
    if age_days <= 0:
        return 100.0
    if age_days >= _FRESHNESS_HORIZON_DAYS:
        return 0.0
    return 100.0 * (1.0 - age_days / _FRESHNESS_HORIZON_DAYS)


def compute_credibility(
    *,
    upvotes: int,
    total_votes: int,
    url: str | None,
    last_published: dt.datetime | None,
    now: dt.datetime,
) -> float:
    vote_component = vote_ratio(upvotes, total_votes) * 100.0
    domain_component = domain_authority(url)
    fresh_component = freshness_score(last_published, now)
    score = (
        _W_VOTE * vote_component
        + _W_DOMAIN * domain_component
        + _W_FRESHNESS * fresh_component
    )
    return min(100.0, max(0.0, score))


def credibility_multiplier(score: float) -> float:
    # 50 -> 1.0 (no effect), 100 -> 1.5, 0 -> 0.5: high credibility surfaces
    # earlier, low credibility is demoted without being hidden.
    clamped = min(100.0, max(0.0, score))
    return 0.5 + clamped / 100.0


def weight_by_credibility(base_score: float, cred_score: float) -> float:
    return base_score * credibility_multiplier(cred_score)


def credibility_tier(score: float) -> str:
    if score >= VERIFIED_THRESHOLD:
        return TIER_VERIFIED
    if score >= COMMUNITY_THRESHOLD:
        return TIER_COMMUNITY
    return TIER_UNVERIFIED


def credibility_badge(score: float) -> str:
    return _TIER_BADGES[credibility_tier(score)]


def effective_score(cred: models.SourceCredibility) -> float:
    if cred.manual_override is not None:
        return cred.manual_override
    return cred.score


def _vote_totals(stories) -> tuple[int, int]:
    # Derived from denormalised Story.vote_count (net = up - down) and
    # Story.downvotes (count of -1): upvotes = net + down, total = up + down.
    downvotes = sum(s.downvotes or 0 for s in stories)
    net = sum(s.vote_count or 0 for s in stories)
    upvotes = net + downvotes
    total = upvotes + downvotes
    return max(0, upvotes), max(0, total)


def get_or_create_credibility(session, source_id: int) -> models.SourceCredibility:
    cred = (
        session.query(models.SourceCredibility)
        .filter_by(source_id=source_id)
        .one_or_none()
    )
    if cred is None:
        cred = models.SourceCredibility(source_id=source_id)
        session.add(cred)
        session.flush()
    return cred


def get_or_create_stats(session, source_id: int) -> models.SourceStats:
    stats = (
        session.query(models.SourceStats)
        .filter_by(source_id=source_id)
        .one_or_none()
    )
    if stats is None:
        stats = models.SourceStats(source_id=source_id)
        session.add(stats)
        session.flush()
    return stats


def _refresh_stats(session, source_id: int, stories, now: dt.datetime) -> None:
    stats = get_or_create_stats(session, source_id)
    count = len(stories)
    stats.article_count = count
    stats.avg_votes = (
        sum(s.vote_count or 0 for s in stories) / count if count else 0.0
    )
    stats.avg_comments = (
        sum(s.comment_count or 0 for s in stories) / count if count else 0.0
    )
    published = [s.published_at for s in stories if s.published_at is not None]
    stats.established_date = min(published) if published else None
    stats.updated_at = _naive_utc(now)


def recompute_source(
    session, source: models.Source, now: dt.datetime
) -> models.SourceCredibility:
    # The manual override is preserved (the computed ``score`` is still refreshed
    # so a later clear reveals an up-to-date value), but the persisted
    # tier/is_verified and the denormalised Story.credibility_score reflect the
    # *effective* score so badges and ranking honour the override.
    stories = session.scalars(
        select(models.Story).filter_by(source_id=source.id)
    ).all()
    upvotes, total = _vote_totals(stories)
    last_published = max(
        (s.published_at for s in stories if s.published_at is not None),
        default=None,
    )
    score = compute_credibility(
        upvotes=upvotes,
        total_votes=total,
        url=source.url,
        last_published=last_published,
        now=now,
    )
    cred = get_or_create_credibility(session, source.id)
    cred.score = score
    cred.vote_ratio = vote_ratio(upvotes, total)
    cred.updated_at = _naive_utc(now)
    eff = effective_score(cred)
    cred.tier = credibility_tier(eff)
    cred.is_verified = cred.tier == TIER_VERIFIED
    for story in stories:
        story.credibility_score = eff
    _refresh_stats(session, source.id, stories, now)
    session.flush()
    return cred


def recompute_all(engine, now: dt.datetime | None = None) -> int:
    """Recompute credibility for every source; return the number processed."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    session = db.get_session(engine)
    try:
        sources = session.scalars(select(models.Source)).all()
        for source in sources:
            recompute_source(session, source, now)
        session.commit()
        return len(sources)
    finally:
        session.close()


def set_manual_override(
    session,
    source_id: int,
    score: float | None,
    *,
    moderator: str,
    reason: str,
    now: dt.datetime,
) -> models.SourceCredibility:
    """Pin (``score``) or clear (``score=None``) a source's score and audit it."""
    cred = get_or_create_credibility(session, source_id)
    if score is None:
        cred.manual_override = None
    else:
        cred.manual_override = min(100.0, max(0.0, float(score)))
    cred.reason = reason
    cred.updated_at = _naive_utc(now)
    eff = effective_score(cred)
    cred.tier = credibility_tier(eff)
    cred.is_verified = cred.tier == TIER_VERIFIED
    session.add(
        models.ModerationAction(
            content_type="source",
            content_id=source_id,
            action="credibility_override",
            moderator=moderator,
            detail=(
                f"cleared override; reason: {reason}"
                if score is None
                else f"set credibility to {cred.manual_override:.1f}; reason: {reason}"
            ),
            created_at=_naive_utc(now),
        )
    )
    session.flush()
    return cred


def credibility_report(session, source_id: int) -> dict | None:
    """Return a source's credibility + stats summary, or None if it doesn't exist."""
    source = session.get(models.Source, source_id)
    if source is None:
        return None
    cred = get_or_create_credibility(session, source_id)
    stats = get_or_create_stats(session, source_id)
    eff = effective_score(cred)
    return {
        "source_id": source_id,
        "name": source.name,
        "url": source.url,
        "score": cred.score,
        "effective_score": eff,
        "manual_override": cred.manual_override,
        "vote_ratio": cred.vote_ratio,
        "is_verified": cred.is_verified,
        "tier": credibility_tier(eff),
        "badge": credibility_badge(eff),
        "reason": cred.reason,
        "updated_at": cred.updated_at.isoformat() if cred.updated_at else None,
        "stats": {
            "article_count": stats.article_count,
            "avg_votes": stats.avg_votes,
            "avg_comments": stats.avg_comments,
            "established_date": (
                stats.established_date.isoformat()
                if stats.established_date
                else None
            ),
        },
    }


def list_source_credibility(session, limit: int = 200) -> list[dict]:
    """Return every source's credibility report, highest effective score first."""
    sources = session.scalars(select(models.Source)).all()
    rows = [credibility_report(session, source.id) for source in sources]
    rows.sort(key=lambda r: r["effective_score"], reverse=True)
    return rows[:limit]
