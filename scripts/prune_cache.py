"""Drop archived article content older than N days to bound database growth.

Usage:
    python scripts/prune_cache.py [--days N] [--db sqlite:///data/stories.db]

Cached HTML can dominate database size; this command nulls out the cache
columns (``cached_html``, ``cached_text``, ``cache_timestamp``) for any story
whose snapshot predates the cutoff while leaving the story row itself intact.
"""

import argparse
import datetime as dt

import src.db as db
from src.article_cache import prune_cache


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Prune old cached article content.")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Remove cached content older than this many days (default: 30).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLAlchemy database URL (defaults to the project's data/stories.db).",
    )
    args = parser.parse_args(argv)

    engine = db.get_engine(args.db)
    db.init_db(engine)
    session = db.get_session(engine)
    try:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
        pruned = prune_cache(session, cutoff)
    finally:
        session.close()
    print(f"Pruned cached content from {pruned} stories older than {args.days} days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
