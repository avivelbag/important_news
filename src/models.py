from datetime import datetime

from sqlalchemy import ForeignKey, UniqueConstraint
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
    # vote_count is the denormalised net points (sum of vote_values); downvotes
    # counts only the -1 votes, kept for the distribution display.
    vote_count: Mapped[int] = mapped_column(default=0)
    downvotes: Mapped[int] = mapped_column(default=0)
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

    canonical: Mapped["Story | None"] = relationship(
        back_populates="duplicates", remote_side=[id]
    )
    duplicates: Mapped[list["Story"]] = relationship(back_populates="canonical")

    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), default=None)
    source: Mapped["Source | None"] = relationship(back_populates="stories")
    votes: Mapped[list["Vote"]] = relationship(back_populates="story")


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
