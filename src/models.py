from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class Source(Base):
    """A content source (e.g. Hacker News, NASA feed) that stories are scraped from."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    url: Mapped[str]

    stories: Mapped[list["Story"]] = relationship(back_populates="source")


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
    vote_count: Mapped[int] = mapped_column(default=0)
    computed_score: Mapped[float] = mapped_column(default=0.0)
    published_at: Mapped[datetime]
    fetched_at: Mapped[datetime]

    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), default=None)
    source: Mapped["Source | None"] = relationship(back_populates="stories")
    votes: Mapped[list["Vote"]] = relationship(back_populates="story")


class Vote(Base):
    """A single upvote cast by a user (or identified by IP) for a story."""

    __tablename__ = "votes"

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"))
    created_at: Mapped[datetime]
    # Hashed IP for dedup; None when the client does not provide one
    ip_hash: Mapped[str | None] = mapped_column(default=None)

    story: Mapped["Story"] = relationship(back_populates="votes")
