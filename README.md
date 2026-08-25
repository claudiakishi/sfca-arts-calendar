# SFCA Arts & Culture Calendar → GitHub Pages

An always-current static mirror of the Hawai‘i State Foundation on Culture and
the Arts (SFCA) monthly **Arts & Culture Calendar**, plus an iCal feed and a
machine-readable `events.json`. It updates itself in GitHub Actions.

## What it does

1. **Discovers months.** SFCA publishes one page per month at
   `https://sfca.hawaii.gov/arts-and-culture-calendar-<month>-<year>/`. There is
   no next-month navigation in the markup and the WordPress REST API is locked
   down (Kadence Security → 401), so the builder *guesses* the slug for every
   month from **August 2026** through **current month + 2** and fetches the
   plain HTML. Months that aren't published yet return 404 and are skipped
   gracefully.
2. **Archives** each fetched page to `data/raw/<slug>.html` so the parser can be
   fixed and re-run offline later.
3. **Parses** events into structured data. The content is clean Elementor
   markup: `<h2>` category headings ("Family-friendly events", "Performances and
   Concerts", "Exhibits") each followed by a `<ul>` whose `<li>` items are
   `<strong>Title</strong>` + description + links. Dates are extracted from the
   prose best-effort.
4. **Builds** `site/`: a combined `index.html`, per-month pages, `events.json`,
   and `calendar.ics`. Fully static — no client-side fetching (avoids CORS and
   flakiness).
5. **Deploys** to GitHub Pages.

If a month's event-level parsing fails, that month is still listed with a link
to the source page rather than crashing the build.

## Run locally

```bash
pip install -r requirements.txt
python scripts/fetch_and_build.py            # fetch live + build
python scripts/fetch_and_build.py --offline  # rebuild from archived data/raw/*.html
python scripts/fetch_and_build.py --today 2026-10-01   # test the discovery window
```

Outputs land in `site/`. Open `site/index.html` in a browser.

## Automation

`.github/workflows/update.yml` runs daily (cron), on `workflow_dispatch`, and on
pushes that touch the script. It fetches, commits refreshed `data/` + `site/`
back to the repo (so the raw-HTML archive accumulates), and deploys `site/` to
Pages.

### One-time setup after pushing to GitHub

1. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. Settings → Actions → General → **Workflow permissions: Read and write**.
3. Actions tab → **Update SFCA calendar → Run workflow** to trigger the first
   build, or just wait for the daily cron.

## Data & attribution

`events.json` and the site present **facts** (title, date, venue, link) with
attribution and a link back to each source page. SFCA content is not
automatically public domain, so their HTML is not mirrored wholesale. This is an
unofficial mirror; always confirm details on the source page. Inclusion is not
an endorsement.

## Layout

```
scripts/fetch_and_build.py   fetch + parse + build (stdlib + requests + bs4)
data/raw/<slug>.html         archived source pages
data/events.json             structured events (canonical copy)
site/                        generated static site (index, per-month, .ics, .json)
.github/workflows/update.yml cron + dispatch + Pages deploy
```
