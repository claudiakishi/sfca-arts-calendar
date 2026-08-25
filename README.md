# Hawaiʻi Arts & Culture Calendar → GitHub Pages

An always-current static aggregator of Hawaiʻi arts events from **two sources**,
merged and de-duplicated, plus an iCal feed and a machine-readable
`events.json`. It updates itself in GitHub Actions.

**Live:** https://claudiakishi.github.io/sfca-arts-calendar/

## Sources

1. **SFCA Arts & Culture Calendar** — the State Foundation on Culture and the
   Arts publishes one page per month at
   `https://sfca.hawaii.gov/arts-and-culture-calendar-<month>-<year>/`. There's
   no next-month navigation in the markup and the WordPress REST API is locked
   down (Kadence Security → 401), so the builder *guesses* the slug for every
   month from **August 2026** through **current month + 2** and fetches the
   plain HTML. Months not published yet return 404 and are skipped gracefully.
   Content is clean Elementor markup: `<h2>` category headings each followed by a
   `<ul>` of `<li>` events (`<strong>Title</strong>` + description). Dates are
   extracted from the prose best-effort.

2. **Capitol Modern: the Hawaiʻi State Art Museum** — the upcoming-events feed at
   `https://www.capitolmodern.org/events`. A Next.js app whose events are
   streamed as a Sanity CMS JSON payload inside `self.__next_f`, carrying **exact
   UTC start/end timestamps**, titles, slugs, venue, description, and ticket
   URLs. Times are UTC and are converted to **HST (UTC−10, no DST)** to get the
   correct local date — otherwise e.g. First Friday's `2026-09-05T03:30Z` would
   wrongly land on Sept 5 instead of Sept 4.

## Merge & dedupe

All events from both sources feed one list, then duplicates are clustered:

- **Two concrete instances** (both have a specific date) merge only when they
  share the **same local start date** *and* a strong title match. This keeps
  distinct instances of a recurring event apart — e.g. SFCA's "Super Saturday"
  (Aug 15) is **not** merged with Capitol Modern's "Super Saturday: Creative
  Micronesia" (Aug 29).
- **A recurring/dateless listing** (e.g. SFCA's generic "First Friday") is
  **never** collapsed into a single dated instance — that would lose the
  recurrence and misplace the event.
- Merged events keep **every** contributing source with a link back, and prefer
  the most precise record (a timed Capitol Modern event over an all-day SFCA
  one) for the displayed date/time.

SFCA also lists some events under two categories (e.g. "Drawn to Music"); those
are de-duplicated to a single event too.

Events are grouped by month. An event whose date range spans its SFCA source
month stays in that month (a July–August exhibit listed on the August page stays
in August); a future-dated event flagged ahead of time (e.g. a September event
on the August page) moves to its real month.

## Output

`site/` contains a combined `index.html`, per-month pages (`YYYY-MM.html`),
`events.json`, and `calendar.ics`. Fully static — no client-side fetching
(avoids CORS and flakiness). All-day SFCA events become `VALUE=DATE` VEVENTs;
timed Capitol Modern events become UTC-instant VEVENTs.

## Run locally

```bash
pip install -r requirements.txt
python scripts/fetch_and_build.py            # fetch both sources + build
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
3. Actions tab → **Update SFCA calendar → Run workflow**, or wait for the cron.

## Data & attribution

`events.json` and the site present **facts** (title, date, venue, link) with
attribution and a link back to each source. SFCA and Capitol Modern content is
not automatically public domain, so their HTML is not mirrored wholesale. This
is an unofficial aggregator; always confirm details on the source page.
Inclusion is not an endorsement.

## Layout

```
scripts/fetch_and_build.py   fetch (both sources) + merge/dedupe + build
data/raw/*.html              archived source pages
data/events.json             structured, merged events (canonical copy)
site/                        generated static site (index, per-month, .ics, .json)
.github/workflows/update.yml cron + dispatch + Pages deploy
```
