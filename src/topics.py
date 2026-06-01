"""Hierarchical topic/tag system over stored stories.

A :class:`~src.models.Topic` is a named beat (e.g. "Large Language Models")
identified by a URL-safe ``slug``. Topics form a shallow hierarchy: each child
topic points at a parent domain (``artificial-intelligence`` or ``aerospace``)
via ``parent_id``. Stories are tagged with any number of topics through
:class:`~src.models.ArticleTopic` join rows, and users subscribe to topics
through :class:`~src.models.UserTopicFollow`.

Tagging is purely rule-based: :data:`TOPIC_SEEDS` carries a keyword list per
topic and :func:`suggest_topics` matches those keywords against a story's title.
No machine-learning or external calls are involved, so tagging is fully
deterministic.

Both ``Topic.follower_count`` and ``Topic.article_count`` are denormalised and
recomputed from the live join tables after every mutating operation, mirroring
the ``bookmark_count`` pattern used elsewhere in the codebase.
"""

import datetime as dt
import re

from sqlalchemy import func, select

from src.models import ArticleTopic, Story, Topic, UserTopicFollow

DOMAINS = [
    {
        "slug": "artificial-intelligence",
        "name": "Artificial Intelligence",
        "description": "Machine learning, models, and AI research.",
    },
    {
        "slug": "aerospace",
        "name": "Aerospace",
        "description": "Spaceflight, launch systems, and aviation.",
    },
]

TOPIC_SEEDS = [
    {
        "slug": "llms",
        "name": "Large Language Models",
        "parent": "artificial-intelligence",
        "description": "Transformer-based language models and chat assistants.",
        "keywords": ["llm", "language model", "gpt", "chatgpt", "transformer"],
    },
    {
        "slug": "computer-vision",
        "name": "Computer Vision",
        "parent": "artificial-intelligence",
        "description": "Image and video understanding.",
        "keywords": [
            "computer vision",
            "image recognition",
            "object detection",
        ],
    },
    {
        "slug": "nlp",
        "name": "Natural Language Processing",
        "parent": "artificial-intelligence",
        "description": "Processing and understanding human language.",
        "keywords": ["natural language", "nlp", "sentiment analysis"],
    },
    {
        "slug": "robotics",
        "name": "Robotics",
        "parent": "artificial-intelligence",
        "description": "Robots, manipulation, and embodied agents.",
        "keywords": ["robot", "robotics", "manipulation"],
    },
    {
        "slug": "reinforcement-learning",
        "name": "Reinforcement Learning",
        "parent": "artificial-intelligence",
        "description": "Learning from reward signals.",
        "keywords": ["reinforcement learning", "reward signal", "q-learning"],
    },
    {
        "slug": "generative-ai",
        "name": "Generative AI",
        "parent": "artificial-intelligence",
        "description": "Generative models for images, audio, and text.",
        "keywords": ["generative ai", "diffusion", "image generation", "gan"],
    },
    {
        "slug": "ai-safety",
        "name": "AI Safety",
        "parent": "artificial-intelligence",
        "description": "Alignment, interpretability, and safe deployment.",
        "keywords": ["ai safety", "alignment", "interpretability"],
    },
    {
        "slug": "ml-infrastructure",
        "name": "ML Infrastructure",
        "parent": "artificial-intelligence",
        "description": "Training hardware, MLOps, and inference systems.",
        "keywords": ["gpu", "mlops", "training cluster", "inference"],
    },
    {
        "slug": "ai-ethics",
        "name": "AI Ethics",
        "parent": "artificial-intelligence",
        "description": "Bias, fairness, and the societal impact of AI.",
        "keywords": ["ai ethics", "algorithmic bias", "fairness"],
    },
    {
        "slug": "speech-recognition",
        "name": "Speech Recognition",
        "parent": "artificial-intelligence",
        "description": "Voice assistants and transcription.",
        "keywords": ["speech recognition", "voice assistant", "transcription"],
    },
    {
        "slug": "satellites",
        "name": "Satellites",
        "parent": "aerospace",
        "description": "Earth-orbit satellites and constellations.",
        "keywords": ["satellite", "constellation", "starlink"],
    },
    {
        "slug": "space-exploration",
        "name": "Space Exploration",
        "parent": "aerospace",
        "description": "Missions to the Moon, Mars, and beyond.",
        "keywords": ["mars", "lunar", "deep space", "space probe"],
    },
    {
        "slug": "launch-systems",
        "name": "Launch Systems",
        "parent": "aerospace",
        "description": "Rockets and launch vehicles.",
        "keywords": ["rocket", "launch vehicle", "falcon", "starship"],
    },
    {
        "slug": "autonomous-vehicles",
        "name": "Autonomous Vehicles",
        "parent": "aerospace",
        "description": "Self-driving and autopilot systems.",
        "keywords": ["autonomous vehicle", "self-driving", "autopilot"],
    },
    {
        "slug": "drones",
        "name": "Drones",
        "parent": "aerospace",
        "description": "Uncrewed aerial vehicles.",
        "keywords": ["drone", "uav", "quadcopter"],
    },
    {
        "slug": "commercial-space",
        "name": "Commercial Space",
        "parent": "aerospace",
        "description": "Private spaceflight companies.",
        "keywords": ["spacex", "blue origin", "commercial space"],
    },
    {
        "slug": "space-policy",
        "name": "Space Policy",
        "parent": "aerospace",
        "description": "Regulation and governance of space.",
        "keywords": ["space policy", "space treaty", "faa"],
    },
    {
        "slug": "propulsion",
        "name": "Propulsion",
        "parent": "aerospace",
        "description": "Engines and propulsion technology.",
        "keywords": ["propulsion", "thruster", "ion drive"],
    },
    {
        "slug": "hypersonics",
        "name": "Hypersonics",
        "parent": "aerospace",
        "description": "Hypersonic flight and vehicles.",
        "keywords": ["hypersonic", "scramjet"],
    },
    {
        "slug": "space-stations",
        "name": "Space Stations",
        "parent": "aerospace",
        "description": "Orbital outposts and habitats.",
        "keywords": ["space station", "iss", "orbital outpost"],
    },
]

_VALID_SORTS = ("recency", "score")


class TopicError(ValueError):
    """Raised for invalid topic operations (unknown topic/story, bad input).

    ``not_found`` distinguishes a missing topic/story (maps to HTTP 404) from a
    bad-input validation failure (HTTP 400) so the API layer can pick a status
    without string-matching the message.
    """

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _clean(value: str, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise TopicError(f"{field} must not be empty")
    return cleaned


def _keyword_index() -> list[tuple[str, str]]:
    """Return ``(keyword, slug)`` pairs, longest keyword first.

    Longest-first ordering means a more specific phrase ("space station") is
    considered before a shorter token that might be a substring of it.
    """
    pairs = [
        (kw.lower(), seed["slug"])
        for seed in TOPIC_SEEDS
        for kw in seed["keywords"]
    ]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def suggest_topics(title: str, summary: str = "") -> list[str]:
    """Return topic slugs whose keywords appear in *title*/*summary*.

    Matching is case-insensitive and word-boundary aware so "gan" matches the
    standalone token but not "organ". Results are de-duplicated and returned in
    a stable order (by topic seed definition) so the same text always yields the
    same suggestions. Pure function — performs no database access. *summary* is
    accepted for callers that have extra text (stories carry only a title today).
    """
    haystack = f"{title or ''} {summary or ''}".lower()
    matched: set[str] = set()
    for keyword, slug in _keyword_index():
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, haystack):
            matched.add(slug)
    return [seed["slug"] for seed in TOPIC_SEEDS if seed["slug"] in matched]


def seed_topics(session) -> dict:
    """Insert the predefined domain + topic hierarchy if missing (idempotent).

    Domains are inserted first so child topics can resolve their ``parent_id``.
    A topic already present (matched by ``slug``) is left untouched, so repeated
    runs neither duplicate nor overwrite curated rows. Returns ``{created,
    total}`` where ``created`` counts newly inserted topics this call.
    """
    created = 0
    by_slug: dict[str, Topic] = {
        t.slug: t for t in session.scalars(select(Topic)).all()
    }
    for domain in DOMAINS:
        if domain["slug"] not in by_slug:
            topic = Topic(
                slug=domain["slug"],
                name=domain["name"],
                description=domain["description"],
                created_at=_now(),
            )
            session.add(topic)
            session.flush()
            by_slug[domain["slug"]] = topic
            created += 1
    for seed in TOPIC_SEEDS:
        if seed["slug"] in by_slug:
            continue
        parent = by_slug.get(seed["parent"])
        topic = Topic(
            slug=seed["slug"],
            name=seed["name"],
            description=seed["description"],
            parent_id=parent.id if parent else None,
            created_at=_now(),
        )
        session.add(topic)
        session.flush()
        by_slug[seed["slug"]] = topic
        created += 1
    session.commit()
    total = int(session.scalar(select(func.count()).select_from(Topic)) or 0)
    return {"created": created, "total": total}


def _get_topic(session, slug: str) -> Topic:
    name = _clean(slug, "slug")
    topic = session.scalars(select(Topic).where(Topic.slug == name)).first()
    if topic is None:
        raise TopicError(f"topic {name!r} does not exist", not_found=True)
    return topic


def _recompute_articles(session, topic: Topic) -> None:
    topic.article_count = int(
        session.scalar(
            select(func.count())
            .select_from(ArticleTopic)
            .where(ArticleTopic.topic_id == topic.id)
        )
        or 0
    )


def _recompute_followers(session, topic: Topic) -> None:
    topic.follower_count = int(
        session.scalar(
            select(func.count())
            .select_from(UserTopicFollow)
            .where(UserTopicFollow.topic_id == topic.id)
        )
        or 0
    )


def _topic_dict(topic: Topic) -> dict:
    return {
        "slug": topic.slug,
        "name": topic.name,
        "description": topic.description,
        "parent_id": topic.parent_id,
        "follower_count": topic.follower_count,
        "article_count": topic.article_count,
    }


def list_topics(session, parent: str | None = None) -> list[dict]:
    """Return all topics, optionally only the children of *parent*'s slug.

    Topics are ordered by slug for a stable response. A *parent* slug that does
    not exist raises :class:`TopicError` (``not_found``) rather than silently
    returning an empty list, so a typo surfaces instead of looking like an empty
    domain.
    """
    stmt = select(Topic).order_by(Topic.slug)
    if parent:
        parent_topic = _get_topic(session, parent)
        stmt = stmt.where(Topic.parent_id == parent_topic.id)
    return [_topic_dict(t) for t in session.scalars(stmt).all()]


def get_topic(session, slug: str) -> dict:
    """Return one topic with its description and related (sibling/child) topics.

    ``related`` lists the slugs of the topic's children (if it is a domain) or
    its siblings under the same parent (if it is a leaf), giving the "link to
    related topics" navigation. Raises :class:`TopicError` (``not_found``) for
    an unknown slug.
    """
    topic = _get_topic(session, slug)
    if topic.parent_id is None:
        related = session.scalars(
            select(Topic.slug)
            .where(Topic.parent_id == topic.id)
            .order_by(Topic.slug)
        ).all()
    else:
        related = session.scalars(
            select(Topic.slug)
            .where(Topic.parent_id == topic.parent_id, Topic.id != topic.id)
            .order_by(Topic.slug)
        ).all()
    data = _topic_dict(topic)
    data["related"] = list(related)
    return data


def _story_topic_slugs(session, story_id: int) -> list[str]:
    return list(
        session.scalars(
            select(Topic.slug)
            .join(ArticleTopic, ArticleTopic.topic_id == Topic.id)
            .where(ArticleTopic.story_id == story_id)
            .order_by(Topic.slug)
        ).all()
    )


def tag_story(session, story_id: int, slugs: list[str]) -> dict:
    """Attach the topics named by *slugs* to *story_id* (manual tagging).

    Existing tags are preserved; only missing ``(story, topic)`` pairs are
    added, so the call is idempotent. Every affected topic's ``article_count``
    is recomputed. Raises :class:`TopicError` (``not_found``) if the story or
    any slug is unknown. Returns ``{story_id, topics}`` with the story's full
    current slug list.
    """
    story = session.get(Story, story_id)
    if story is None:
        raise TopicError(f"story {story_id} does not exist", not_found=True)
    topics = [_get_topic(session, s) for s in slugs]
    existing = set(
        session.scalars(
            select(ArticleTopic.topic_id).where(
                ArticleTopic.story_id == story_id
            )
        ).all()
    )
    for topic in topics:
        if topic.id not in existing:
            session.add(
                ArticleTopic(
                    story_id=story_id, topic_id=topic.id, created_at=_now()
                )
            )
            existing.add(topic.id)
    session.flush()
    for topic in topics:
        _recompute_articles(session, topic)
    session.commit()
    return {"story_id": story_id, "topics": _story_topic_slugs(session, story_id)}


def auto_tag_story(session, story_id: int) -> dict:
    """Auto-tag *story_id* from its title via keyword matching.

    Delegates slug selection to :func:`suggest_topics` and reuses
    :func:`tag_story` so auto- and manual tagging share the same idempotent
    write path. A story whose title matches nothing is left untagged (a no-op).
    Raises :class:`TopicError` (``not_found``) for an unknown story. Returns
    ``{story_id, topics}``.
    """
    story = session.get(Story, story_id)
    if story is None:
        raise TopicError(f"story {story_id} does not exist", not_found=True)
    slugs = suggest_topics(story.title)
    if not slugs:
        return {"story_id": story_id, "topics": []}
    return tag_story(session, story_id, slugs)


def auto_tag_all(session) -> dict:
    """Auto-tag every story in the database. Returns ``{tagged}`` count.

    ``tagged`` counts stories that received at least one topic. Used to backfill
    tags for the existing corpus after the topic system is introduced.
    """
    tagged = 0
    for story_id in session.scalars(select(Story.id)).all():
        if auto_tag_story(session, story_id)["topics"]:
            tagged += 1
    return {"tagged": tagged}


def follow_topic(session, user_id: str, slug: str) -> dict:
    """Make *user_id* follow the topic *slug* (idempotent).

    Following an already-followed topic is a no-op. The topic's
    ``follower_count`` is recomputed and committed. Raises :class:`TopicError`
    for an empty user or (``not_found``) an unknown topic. Returns ``{slug,
    following: True, follower_count}``.
    """
    name = _clean(user_id, "user_id")
    topic = _get_topic(session, slug)
    existing = session.scalars(
        select(UserTopicFollow).where(
            UserTopicFollow.user_id == name,
            UserTopicFollow.topic_id == topic.id,
        )
    ).first()
    if existing is None:
        session.add(
            UserTopicFollow(
                user_id=name, topic_id=topic.id, followed_at=_now()
            )
        )
        session.flush()
        _recompute_followers(session, topic)
        session.commit()
    return {
        "slug": topic.slug,
        "following": True,
        "follower_count": topic.follower_count,
    }


def unfollow_topic(session, user_id: str, slug: str) -> dict:
    """Make *user_id* stop following *slug* (idempotent).

    Unfollowing a topic the user does not follow is a no-op that still returns
    the current state. The topic's ``follower_count`` is recomputed. Raises
    :class:`TopicError` for an empty user or (``not_found``) an unknown topic.
    Returns ``{slug, following: False, follower_count}``.
    """
    name = _clean(user_id, "user_id")
    topic = _get_topic(session, slug)
    existing = session.scalars(
        select(UserTopicFollow).where(
            UserTopicFollow.user_id == name,
            UserTopicFollow.topic_id == topic.id,
        )
    ).first()
    if existing is not None:
        session.delete(existing)
        session.flush()
        _recompute_followers(session, topic)
        session.commit()
    return {
        "slug": topic.slug,
        "following": False,
        "follower_count": topic.follower_count,
    }


def list_followed(session, user_id: str) -> list[dict]:
    """Return the topics *user_id* follows, ordered by slug.

    Raises :class:`TopicError` for an empty user. Each entry is a topic dict as
    returned by :func:`list_topics`.
    """
    name = _clean(user_id, "user_id")
    topics = session.scalars(
        select(Topic)
        .join(UserTopicFollow, UserTopicFollow.topic_id == Topic.id)
        .where(UserTopicFollow.user_id == name)
        .order_by(Topic.slug)
    ).all()
    return [_topic_dict(t) for t in topics]


def _order_stories(stmt, sort: str):
    if sort not in _VALID_SORTS:
        raise TopicError(f"sort must be one of {_VALID_SORTS}")
    if sort == "score":
        return stmt.order_by(Story.computed_score.desc(), Story.id.desc())
    return stmt.order_by(Story.published_at.desc(), Story.id.desc())


def _story_brief(story: Story) -> dict:
    return {
        "id": story.id,
        "url": story.url,
        "title": story.title,
        "topic": story.topic,
        "score": story.computed_score,
        "published_at": (
            story.published_at.isoformat() if story.published_at else None
        ),
    }


def topic_stories(
    session, slug: str, sort: str = "recency", limit: int = 50
) -> dict:
    """Return stories tagged with *slug*, sorted by recency or score.

    *sort* is ``"recency"`` (newest ``published_at`` first, the default) or
    ``"score"`` (highest ``computed_score`` first); any other value raises
    :class:`TopicError`. *limit* is clamped to ``[1, 200]``. Raises
    :class:`TopicError` (``not_found``) for an unknown topic. Returns ``{slug,
    sort, total, stories}``.
    """
    topic = _get_topic(session, slug)
    limit = max(1, min(int(limit), 200))
    base = (
        select(Story)
        .join(ArticleTopic, ArticleTopic.story_id == Story.id)
        .where(ArticleTopic.topic_id == topic.id)
    )
    total = int(
        session.scalar(select(func.count()).select_from(base.subquery())) or 0
    )
    stories = session.scalars(_order_stories(base, sort).limit(limit)).all()
    return {
        "slug": topic.slug,
        "sort": sort,
        "total": total,
        "stories": [_story_brief(s) for s in stories],
    }


def followed_feed(
    session, user_id: str, sort: str = "recency", limit: int = 50
) -> dict:
    """Return stories tagged with any topic *user_id* follows.

    This is the topic-filtered feed. A user who follows nothing gets an empty
    feed (not the global feed), since the whole point is to narrow the firehose.
    Duplicate stories (tagged with two followed topics) appear once. *sort* and
    *limit* behave as in :func:`topic_stories`. Raises :class:`TopicError` for
    an empty user. Returns ``{user_id, sort, total, stories}``.
    """
    name = _clean(user_id, "user_id")
    limit = max(1, min(int(limit), 200))
    followed = select(UserTopicFollow.topic_id).where(
        UserTopicFollow.user_id == name
    )
    base = (
        select(Story)
        .join(ArticleTopic, ArticleTopic.story_id == Story.id)
        .where(ArticleTopic.topic_id.in_(followed))
        .distinct()
    )
    total = int(
        session.scalar(select(func.count()).select_from(base.subquery())) or 0
    )
    stories = session.scalars(_order_stories(base, sort).limit(limit)).all()
    return {
        "user_id": name,
        "sort": sort,
        "total": total,
        "stories": [_story_brief(s) for s in stories],
    }


def topic_analytics(session, limit: int = 10) -> dict:
    """Return most-followed and trending topics.

    ``most_followed`` ranks topics by ``follower_count``; ``trending`` ranks by
    ``article_count`` (how many stories carry the tag), a deterministic proxy
    for momentum that needs no wall-clock window. Both lists drop topics with a
    zero count so empty beats do not pad the leaderboard, and each is limited to
    *limit* entries. Returns ``{most_followed, trending}``.
    """
    limit = max(1, min(int(limit), 100))
    most_followed = session.scalars(
        select(Topic)
        .where(Topic.follower_count > 0)
        .order_by(Topic.follower_count.desc(), Topic.slug)
        .limit(limit)
    ).all()
    trending = session.scalars(
        select(Topic)
        .where(Topic.article_count > 0)
        .order_by(Topic.article_count.desc(), Topic.slug)
        .limit(limit)
    ).all()
    return {
        "most_followed": [_topic_dict(t) for t in most_followed],
        "trending": [_topic_dict(t) for t in trending],
    }
