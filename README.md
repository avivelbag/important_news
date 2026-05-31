# Important News Scraper

Scrapes AI and aerospace headlines from public feeds into a SQLite database and
generates a static HTML site under `docs/`.

## Feed refresh

The feed is refreshed automatically by the
[`refresh-feed`](.github/workflows/refresh-feed.yml) GitHub Actions workflow,
which runs **every 6 hours** (`cron: 0 */6 * * *`). Each run scrapes the
sources, regenerates `docs/`, and commits the result back to the branch as
`feed-refresh-bot`.

### Manual refresh

Trigger a run from the GitHub Actions UI via the **Run workflow**
(`workflow_dispatch`) button, or refresh locally:

```bash
make refresh
# equivalently:
python scripts/refresh.py
```

`scripts/refresh.py` runs the scraper, rebuilds the site, and prints a summary
of how many stories were inserted per source. It exits non-zero only when every
source fails and nothing new is stored.

## Running tests

```bash
make test
# or
python3 -m pytest tests/ -x --tb=short -q
```
