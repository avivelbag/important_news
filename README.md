# Important News Scraper

A Hacker-News-style static site that scrapes **AI** and **aerospace** headlines
from public feeds into a SQLite database and generates a browsable static site
under `docs/`. The generated site is served directly by GitHub Pages.

## Folder structure

```
src/                Python source (scraper, scorer, deduplicator, site generator, RSS, search API)
scripts/refresh.py  One-shot refresh entry point (scrape + regenerate site)
docs/               Generated static site — this is what GitHub Pages serves (NOT gitignored)
tests/              pytest suite
.github/workflows/  refresh-feed.yml — scheduled refresh automation
.github/settings.yml  Declarative repo settings, incl. the GitHub Pages source
```

`docs/` is intentionally **not** listed in `.gitignore`: GitHub Pages serves the
committed contents of that directory, so the generated HTML must be checked in.

## Enabling GitHub Pages

The site is served from the `docs/` directory on the default branch.

1. Push this repository to GitHub.
2. Go to **Settings → Pages**.
3. Under **Build and deployment**, set **Source** to **Deploy from a branch**.
4. Select branch **`main`** (or your default branch) and folder **`/docs`**,
   then click **Save**.
5. After a minute the site is published at
   `https://<owner>.github.io/<repo>/`.

These settings are also captured declaratively in
[`.github/settings.yml`](.github/settings.yml) so that, if the
[Probot Settings app](https://github.com/apps/settings) is installed, Pages is
configured automatically from `docs/` on the default branch. Installing the app
is optional — the manual steps above are sufficient.

## Data sources

The scraper pulls from these public feeds (see `DEFAULT_SOURCES` in
`src/scraper.py`):

| Source | Kind | Category |
| ------ | ---- | -------- |
| Hacker News (top stories) | Firebase JSON API | AI |
| NASA Breaking News | RSS | aerospace |
| MIT Technology Review — AI | RSS | AI |

Stories are filtered by AI/aerospace keywords, scored, and de-duplicated before
being stored.

## Local development

```bash
python3 -m pip install sqlalchemy
make scrape   # scrape sources into the SQLite database
make site     # regenerate docs/ from the database
```

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

### Secrets and tokens

None of the default sources require an API key — they are all public,
unauthenticated feeds, so no GitHub Actions secrets need to be configured. The
refresh workflow only needs the built-in `GITHUB_TOKEN` (granted via
`permissions: contents: write`) to commit the regenerated site. If you add a
source that needs credentials, store them under **Settings → Secrets and
variables → Actions** and read them via `os.environ` in your fetcher.

## Adding new data sources

See [DEPLOYMENT.md](DEPLOYMENT.md) for a step-by-step guide to adding a new feed
and adjusting the refresh frequency.

## Running tests

```bash
make test
# or
python3 -m pytest tests/ -x --tb=short -q
```
