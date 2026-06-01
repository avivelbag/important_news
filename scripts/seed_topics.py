#!/usr/bin/env python3
"""Seed the predefined topic hierarchy and optionally auto-tag stories."""

import argparse

from src.db import session_scope
from src.topics import auto_tag_all, seed_topics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        action="store_true",
        help="auto-tag existing stories after seeding",
    )
    args = parser.parse_args()
    with session_scope() as session:
        result = seed_topics(session)
        print(f"topics: created={result['created']} total={result['total']}")
        if args.tag:
            tagged = auto_tag_all(session)
            print(f"auto-tagged stories: {tagged['tagged']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
