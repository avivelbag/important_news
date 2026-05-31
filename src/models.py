from datetime import datetime

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class Source(Base):
    """A content source (e.g. Hacker News, NASA feed) that stories are scraped from."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    url: Mapped[str]
    # Reliability multiplier applied to every story from this source when scoring.
    quality_weight: Mapped[float] = mapped_column(default=1.0)

    stories: Mapped[list["Story"]] = relationship(back_populates="source")
    health: Mapped["SourceHealth | None"] = relationship(
        back_populates="source", uselist=False
    )
    fetch_logs: Mapped[list["SourceFetchLog"]] = relationship(back_populates="source")


class SourceHealth(Base):
    """Rolled-up health state for a single source, one row per source."""

    __tablename__ = "source_health"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), unique=True)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    last_fetch_time: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default="healthy")

    source: Mapped["Source"] = relationship(back_populates="health")


class SourceFetchLog(Base):
    """Append-only audit trail of every fetch attempt against a source."""

    __tablename__ = "source_fetch_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    fetch_time: Mapped[datetime]
    status: Mapped[str]  # "success" | "error"
    error_message: Mapped[str | None] = mapped_column(default=None)
    article_count: Mapped[int] = mapped_column(default=0)

    source: Mapped["Source"] = relationship(back_populates="fetch_logs")


class Story(Base):
    """A news story scraped from a source, with scoring fields for ranking."""

    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    # url is the primary dedup key — scraper inserts are idempotent via UNIQUE
    url: Mapped[str] = mapped_column(unique=True)
    source_name: Mapped[str]
    topic: Mapped[str]  # "ai" | "aerospace" | "both"
    raw_score: Mapped[int] = mapped_column(default=0)
    # Denormalised count of non-deleted comments, kept in sync by the comments
    # service so listings can show "N comments" without a per-render COUNT.
    comment_count: Mapped[int] = mapped_column(default=0)
    # vote_count is the denormalised net points (sum of vote_values); downvotes
    # counts only the -1 votes, kept for the distribution display.
    vote_count: Mapped[int] = mapped_column(default=0)
    downvotes: Mapped[int] = mapped_column(default=0)
    # Denormalised count of Bookmark rows pointing at this story, kept in sync by
    # the bookmarks service so listings can show "N saved" without a COUNT.
    bookmark_count: Mapped[int] = mapped_column(default=0)
    computed_score: Mapped[float] = mapped_column(default=0.0)
    published_at: Mapped[datetime]
    fetched_at: Mapped[datetime]

    # When this story is a near-duplicate of another, canonical_id points at the
    # surviving (canonical) story; NULL means this row is itself canonical and is
    # the one the site renders. Indexed for fast "give me the dupes of X" lookups.
    canonical_id: Mapped[int | None] = mapped_column(
        ForeignKey("stories.id"), default=None, index=True
    )
    # JSON array of distinct source names that contributed to a merged story,
    # e.g. ["Hacker News", "Reddit"]. NULL until a merge happens.
    merged_sources: Mapped[str | None] = mapped_column(default=None)

    # Archived full article content fetched from the source URL. These are NULL
    # until the scraper caches the page; old stories keep NULL and render with
    # metadata only. cached_html is the raw page, cached_text the extracted
    # plaintext, and cache_timestamp records when the snapshot was taken (used
    # by the pruner to drop stale snapshots and bound database size).
    cached_html: Mapped[str | None] = mapped_column(Text, default=None)
    cached_text: Mapped[str | None] = mapped_column(Text, default=None)
    cache_timestamp: Mapped[datetime | None] = mapped_column(default=None)

    canonical: Mapped["Story | None"] = relationship(
        back_populates="duplicates", remote_side=[id]
    )
    duplicates: Mapped[list["Story"]] = relationship(back_populates="canonical")

    # Identifies the user who submitted this story (the cookie uuid / username
    # used on Vote and Comment). NULL for scraped stories with no submitter.
    submitted_by: Mapped[str | None] = mapped_column(default=None, index=True)

    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), default=None)
    source: Mapped["Source | None"] = relationship(back_populates="stories")
    votes: Mapped[list["Vote"]] = relationship(back_populates="story")
    comments: Mapped[list["Comment"]] = relationship(back_populates="story")


class Vote(Base):
    """A single vote cast by a user (or identified by IP) for a story."""

    __tablename__ = "votes"
    # SQLite treats NULL user_ids as distinct, so anonymous votes with no
    # user_id never collide on this constraint; only an identified user is
    # limited to one row per story (upsert target for vote changes/reversals).
    __table_args__ = (UniqueConstraint("user_id", "story_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"))
    # Anonymous voter id (e.g. a cookie uuid); None for legacy/IP-only votes.
    user_id: Mapped[str | None] = mapped_column(default=None)
    # -1 / 0 / +1; net of these per story is the story's vote_count.
    vote_value: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime]
    # Set when an existing vote is changed or reversed; None on first insert.
    updated_at: Mapped[datetime | None] = mapped_column(default=None)
    # Hashed IP for dedup; None when the client does not provide one
    ip_hash: Mapped[str | None] = mapped_column(default=None)

    story: Mapped["Story"] = relationship(back_populates="votes")


class Comment(Base):
    """A user comment on a story, optionally threaded under a parent comment.

    Top-level comments have ``parent_comment_id`` NULL; replies point at the
    comment they answer, so an arbitrarily deep discussion tree is stored as a
    flat adjacency list. ``deleted`` is a soft-delete flag: a removed comment
    keeps its row (so its replies stay reachable) but renders as a ``[deleted]``
    stub. ``vote_count`` ranks comments independently of the parent story's votes.
    """

    __tablename__ = "comments"
    # Threads are loaded "all comments for one story, oldest first"; this index
    # makes that scan cheap and keeps a stable tie-break order.
    __table_args__ = (Index("ix_comments_story_created", "story_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"), index=True)
    # NULL for a top-level comment; otherwise the comment this one replies to.
    parent_comment_id: Mapped[int | None] = mapped_column(
        ForeignKey("comments.id"), default=None, index=True
    )
    # Anonymous author id (e.g. a cookie uuid) or username; None for anonymous.
    user_id: Mapped[str | None] = mapped_column(default=None)
    body: Mapped[str] = mapped_column(Text)
    vote_count: Mapped[int] = mapped_column(default=0)
    # Soft delete: the row survives so child replies keep their parent, but the
    # body is suppressed behind a "[deleted]" stub on read.
    deleted: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime | None] = mapped_column(default=None)

    story: Mapped["Story"] = relationship(back_populates="comments")
    parent: Mapped["Comment | None"] = relationship(
        back_populates="replies", remote_side=[id]
    )
    replies: Mapped[list["Comment"]] = relationship(back_populates="parent")


class ExternalDiscussion(Base):
    """A discussion thread about a story found on an external platform.

    These are off-site threads (Reddit, GitHub, Hacker News) discovered by
    matching a story's topic/keywords against search results, so readers get
    "threads from the internet" context alongside the on-site comments.

    ``url`` is stored in its normalised form (see ``discussions.normalize_url``)
    so the same thread reached via http/https, with/without ``www`` or a
    trailing slash, collapses to one row. The ``(story_id, platform, url)``
    unique constraint makes re-running discovery idempotent. ``discovered_at``
    records when the link was first found (used for cache TTL) and
    ``last_verified_at`` when its metadata was last re-checked against the
    source — link-rot pruning updates the latter and deletes dead rows.
    """

    __tablename__ = "external_discussions"
    __table_args__ = (
        UniqueConstraint("story_id", "platform", "url"),
        Index("ix_external_discussions_story", "story_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"), index=True)
    # "reddit" | "github" | "hn"
    platform: Mapped[str]
    url: Mapped[str]
    title: Mapped[str]
    comment_count: Mapped[int] = mapped_column(default=0)
    # Platform-relative engagement signal (upvotes/reactions/score); used only
    # for ranking links within a story, not compared across platforms.
    engagement_score: Mapped[int] = mapped_column(default=0)
    discovered_at: Mapped[datetime]
    last_verified_at: Mapped[datetime | None] = mapped_column(default=None)

    story: Mapped["Story"] = relationship()


class Bookmark(Base):
    """A user's saved story, forming a private "read later" reading list.

    Users are identified by the same free-form ``user_id`` string used on Votes
    and Comments (a cookie uuid or chosen name). The ``(user_id, story_id)``
    unique constraint makes toggling idempotent — a user can only bookmark a
    given story once — and ``created_at`` records when it was saved so the
    bookmark list can sort and display "saved on" timestamps. The denormalised
    ``Story.bookmark_count`` is kept in sync as rows are added/removed so cards
    can show a count without a per-render COUNT.
    """

    __tablename__ = "bookmarks"
    __table_args__ = (
        UniqueConstraint("user_id", "story_id"),
        Index("ix_bookmarks_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(index=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"), index=True)
    created_at: Mapped[datetime]

    story: Mapped["Story"] = relationship()


class UserProfile(Base):
    """Public profile and cached reputation for a user identified by username.

    The rest of the schema identifies users by a free-form ``user_id`` string
    (a cookie uuid or chosen name) stored directly on Votes/Comments/Stories;
    this table is the one place that holds per-user *metadata* — a bio, the
    private-account toggle, and the denormalised counts that the profile and
    leaderboard pages read without re-aggregating the activity tables on every
    request. ``username`` matches the ``user_id`` used elsewhere.
    """

    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True, index=True)
    bio: Mapped[str | None] = mapped_column(Text, default=None)
    # When true the profile is hidden from the leaderboard and renders as a
    # minimal private stub; activity is never exposed.
    is_private: Mapped[bool] = mapped_column(default=False)
    # Cached reputation == total votes received on this user's comments. Kept in
    # sync by profiles.refresh_profile_stats so the leaderboard sorts cheaply.
    karma: Mapped[int] = mapped_column(default=0)
    # Cached activity counts, refreshed alongside karma.
    submission_count: Mapped[int] = mapped_column(default=0)
    vote_count: Mapped[int] = mapped_column(default=0)
    comment_count: Mapped[int] = mapped_column(default=0)
