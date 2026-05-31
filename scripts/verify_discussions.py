"""Scheduled job: re-verify stored external discussion links, pruning dead ones."""

import logging
import sys

import src.db as db
from src.discussions import verify_discussions

logger = logging.getLogger("verify_discussions")


def _keep_unchanged(discussion) -> dict:
    return {
        "title": discussion.title,
        "comment_count": discussion.comment_count,
        "engagement_score": discussion.engagement_score,
    }


def verify(engine=None, verify_fn=_keep_unchanged) -> dict:
    if engine is None:
        engine = db.get_engine()
    db.init_db(engine)
    session = db.get_session(engine)
    try:
        summary = verify_discussions(session, verify_fn)
    finally:
        session.close()
    logger.info(
        "verified=%d removed=%d errors=%d",
        summary["verified"],
        summary["removed"],
        summary["errors"],
    )
    return summary


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    summary = verify()
    print(
        f"Verified {summary['verified']}, "
        f"removed {summary['removed']}, errors {summary['errors']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
