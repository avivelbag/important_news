"""Drop archived article content older than N days to bound database growth.

Usage:
    python -m scripts.prune_cache [--days N] [--db sqlite:///news.db]

Cached HTML can dominate database size; this command nulls out the cache
columns (cached_html, cached_text, cache_timestamp) for any story whose snapshot
predates the cutoff while leaving the story row itself intact.
"""

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from src.article_cache import prune_cache
from src.db import init_db


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
        default="sqlite:///news.db",
        help="SQLAlchemy database URL (default: sqlite:///news.db).",
    )
    args = parser.parse_args(argv)

    engine = init_db(args.db)
    session = sessionmaker(bind=engine)()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    pruned = prune_cache(session, cutoff)
    print(f"Pruned cached content from {pruned} stories older than {args.days} days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
