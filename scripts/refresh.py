import dataclasses
import logging
import sys
from pathlib import Path

import src.db as db
import src.generate_site as generate_site
import src.scraper as scraper

logger = logging.getLogger("refresh")


@dataclasses.dataclass
class RefreshSummary:
    inserted: int
    errors: int
    per_source: dict
    out_dir: Path

    def format_lines(self) -> list[str]:
        noun = "story" if self.inserted == 1 else "stories"
        lines = [
            f"Inserted {self.inserted} new {noun}",
            f"Errors: {self.errors}",
            f"Site written to: {self.out_dir}",
        ]
        for name, count in sorted(self.per_source.items()):
            lines.append(f"  {name}: {count}")
        return lines


def refresh(engine=None, out_dir: Path | str | None = None) -> RefreshSummary:
    if engine is None:
        engine = db.get_engine()
    db.init_db(engine)

    logger.info("scraping %d sources", len(scraper.DEFAULT_SOURCES))
    result = scraper.run_scraper(engine)
    logger.info(
        "scrape done: %d inserted, %d errors", result.inserted, result.errors
    )

    if out_dir is None:
        out_path = generate_site.generate_site(engine)
    else:
        out_path = generate_site.generate_site(engine, out_dir)
    logger.info("site regenerated at %s", out_path)

    return RefreshSummary(
        inserted=result.inserted,
        errors=result.errors,
        per_source=dict(result.per_source),
        out_dir=out_path,
    )


def main(argv=None) -> int:
    # Exit non-zero only on a total outage (every source errored, nothing
    # inserted) so CI tolerates a single flaky feed but fails loudly if all die.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    summary = refresh()
    for line in summary.format_lines():
        print(line)
    return 1 if summary.errors and summary.inserted == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
