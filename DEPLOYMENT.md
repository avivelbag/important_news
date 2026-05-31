# Deployment & contribution guide

This guide covers deploying the static site and extending the scraper with new
data sources.

## Deployment overview

1. The [`refresh-feed`](.github/workflows/refresh-feed.yml) workflow scrapes the
   sources and regenerates `docs/` on a schedule, committing the result.
2. GitHub Pages serves the committed `docs/` directory (see the **Enabling
   GitHub Pages** section of the [README](README.md)).

There is no separate build/deploy step — committing the regenerated `docs/`
*is* the deploy.

## Adding a new data source

Sources are declared in `DEFAULT_SOURCES` in `src/scraper.py`. Each entry is a
`SourceSpec` with these fields:

- `name` — human-readable label shown in the per-source summary.
- `url` — the feed URL to fetch.
- `kind` — `"hn"` for the Hacker News Firebase API, or `"rss"` for an
  RSS/Atom feed.
- `category` — `"ai"` or `"aerospace"`; used for keyword filtering and grouping.
- `limit` — maximum number of items to consider per run.

To add a new RSS feed:

1. Append a `SourceSpec` to `DEFAULT_SOURCES` in `src/scraper.py`, e.g.:

   ```python
   SourceSpec(
       name="ESA Top News",
       url="https://www.esa.int/rssfeed/Our_Activities/Space_News",
       kind="rss",
       category="aerospace",
       limit=25,
   ),
   ```

2. If the feed uses a category not yet covered by the keyword lists, extend
   `AI_KEYWORDS` or `AEROSPACE_KEYWORDS` (also in `src/scraper.py`) so relevant
   items pass the filter.
3. Add a test under `tests/` that feeds canned feed XML/JSON through the
   scraper's injectable `fetch` function (see `tests/test_scraper.py` for the
   pattern — do not hit the network in tests).
4. Run `make test` and confirm the suite passes.
5. Document the source in the **Data sources** table in the [README](README.md).

### Sources that need credentials

If a source requires an API key:

1. Add the secret under **Settings → Secrets and variables → Actions** in the
   GitHub repository.
2. Reference it in `.github/workflows/refresh-feed.yml` via an `env:` block
   (e.g. `MY_API_KEY: ${{ secrets.MY_API_KEY }}`).
3. Read it with `os.environ["MY_API_KEY"]` inside your fetcher.

Never commit credentials to the repository.

## Adjusting the refresh frequency

The schedule is the `cron` expression in
`.github/workflows/refresh-feed.yml`. The default `0 */6 * * *` runs every six
hours. For example, to run hourly use `0 * * * *`; to run once a day at 08:00
UTC use `0 8 * * *`.

## Changing styling

The site's HTML and inline CSS are produced by `src/generate_site.py`. Edit the
templates/strings there and run `make site` to regenerate `docs/` locally before
committing.
