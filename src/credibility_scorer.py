"""Source credibility scoring: blend observed signals into a 0-100 score.

A source's credibility is a single 0-100 number used by ranking and search to
weight that source's stories. It blends three observable signals:

* **Vote ratio** — upvotes / total votes across the source's stories. A source
  whose stories the community consistently upvotes is more trustworthy; heavy
  downvoting is a negative signal.
* **Domain authority** — whether the source's domain is in a curated list of
  established tech publications, research institutions, and official outlets.
* **Content freshness** — sources that keep publishing recent content score
  higher than ones that have gone quiet.

A moderator can pin the score with a manual override that wins over the
computed value; the override and its reason are persisted on the
``SourceCredibility`` row and audited via a ``ModerationAction`` entry.
"""

import datetime as dt
from urllib.parse import urlparse

from sqlalchemy import func, select

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
    """Return the lowercased registrable host of *url*, ``www.`` stripped.

    Falls back to treating the whole string as a host when it has no scheme, so
    both ``"https://www.nasa.gov/x"`` and ``"nasa.gov"`` resolve to
    ``"nasa.gov"``. Returns ``""`` for empty/None input.
    """
    if not url:
        return ""
    parsed = urlparse(url if "//" in url else f"//{url}")
    host = (parsed.netloc or parsed.path).strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0]


def domain_authority(url: str | None) -> float:
    """Return the curated authority (0-100) for *url*'s domain.

    Matching is suffix-based so a subdomain inherits its parent's authority.
    Unknown domains get ``_UNKNOWN_DOMAIN_AUTHORITY``.
    """
    host = extract_domain(url)
    if not host:
        return _UNKNOWN_DOMAIN_AUTHORITY
    for domain, score in _DOMAIN_AUTHORITY.items():
        if host == domain or host.endswith(f".{domain}"):
            return score
    return _UNKNOWN_DOMAIN_AUTHORITY


def vote_ratio(upvotes: int, total_votes: int) -> float:
    """Return upvotes / total votes in [0, 1]; neutral 0.5 when no votes cast.

    A source with no votes yet is neither endorsed nor rejected, so it gets the
    neutral midpoint rather than 0 (which would unfairly punish new sources).
    """
    if total_votes <= 0:
        return 0.5
    ratio = upvotes / total_votes
    return min(1.0, max(0.0, ratio))


def freshness_score(
    last_published: dt.datetime | None, now: dt.datetime
) -> float:
    """Return a 0-100 freshness score that decays linearly with staleness.

    A source whose newest story is ``now`` scores 100; one whose newest story is
    ``_FRESHNESS_HORIZON_DAYS`` old (or older, or has no stories) scores 0.
    """
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
    """Blend the three signals into a single 0-100 credibility score.

    The vote ratio is scaled to 0-100 and combined with domain authority and
    freshness via the fixed ``_W_*`` weights. The result is clamped to [0, 100].
    """
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
    """Map a 0-100 credibility score to a ranking multiplier in [0.5, 1.5].

    A neutral score of 50 yields 1.0 (no effect), a fully verified source 1.5,
    and a zero-credibility source 0.5 — so high-credibility stories surface
    earlier and low-credibility ones are demoted without being hidden.
    """
    clamped = min(100.0, max(0.0, score))
    return 0.5 + clamped / 100.0


def weight_by_credibility(base_score: float, cred_score: float) -> float:
    """Scale a story's base ranking score by its source credibility multiplier."""
    return base_score * credibility_multiplier(cred_score)


def credibility_tier(score: float) -> str:
    """Map an effective credibility score to its human-facing tier bucket."""
    if score >= VERIFIED_THRESHOLD:
        return TIER_VERIFIED
    if score >= COMMUNITY_THRESHOLD:
        return TIER_COMMUNITY
    return TIER_UNVERIFIED


def credibility_badge(score: float) -> str:
    """Return the badge label ("Verified Source", etc.) for a score."""
    return _TIER_BADGES[credibility_tier(score)]


def effective_score(cred: models.SourceCredibility) -> float:
    """Return the score in effect: the manual override if set, else computed."""
    if cred.manual_override is not None:
        return cred.manual_override
    return cred.score


def _source_vote_totals(session, source_id: int) -> tuple[int, int]:
    # Derived from denormalised Story.vote_count (net = up - down) and
    # Story.downvotes (count of -1): upvotes = net + down, total = up + down.
    stories = session.scalars(
        select(models.Story).filter_by(source_id=source_id)
    ).all()
    downvotes = sum(s.downvotes or 0 for s in stories)
    net = sum(s.vote_count or 0 for s in stories)
    upvotes = net + downvotes
    total = upvotes + downvotes
    return max(0, upvotes), max(0, total)


def _source_last_published(session, source_id: int) -> dt.datetime | None:
    return (
        session.query(func.max(models.Story.published_at))
        .filter_by(source_id=source_id)
        .scalar()
    )


def get_or_create_credibility(session, source_id: int) -> models.SourceCredibility:
    """Return the source's credibility row, creating a default one if missing."""
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


def recompute_source(
    session, source: models.Source, now: dt.datetime
) -> models.SourceCredibility:
    """Recompute and persist one source's credibility from its observed signals.

    A manual override is preserved (the computed ``score`` is still refreshed so
    the override can be cleared later and reveal an up-to-date value), but the
    persisted ``tier``/``is_verified`` reflect the *effective* score so badges
    honour the override.
    """
    upvotes, total = _source_vote_totals(session, source.id)
    last_published = _source_last_published(session, source.id)
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
    """Pin (or clear) a source's credibility score and write an audit row.

    Passing ``score=None`` clears the override and reverts to the computed
    value. Any other value is clamped to [0, 100]. Every change appends a
    ``ModerationAction`` (content_type ``"source"``) so the override history is
    permanently reconstructable, mirroring the content-moderation audit trail.
    """
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
