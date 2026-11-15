# Hawaiʻi Arts & Culture Calendar → GitHub Pages

An always-current static aggregator of Hawaiʻi arts events from **seven sources**,
merged and de-duplicated, plus an iCal feed and a machine-readable
`events.json`. It updates itself in GitHub Actions.

**Live:** https://claudiakishi.github.io/sfca-arts-calendar/

## Sources

1. **SFCA Arts & Culture Calendar** — the State Foundation on Culture and the
   Arts publishes one page per month at
   `https://sfca.hawaii.gov/arts-and-culture-calendar-<month>-<year>/`. There's
   no next-month navigation in the markup and the WordPress REST API is locked
   down (Kadence Security → 401), so the builder *guesses* the slug for every
   month from **max(August 2026, current month)** through **current month + 2**
   and fetches the plain HTML. Months not published yet return 404 and are
   skipped gracefully — SFCA typically doesn't publish the new month's page
   until a few days in, so a 404 right after rollover is expected, not a bug;
   the daily cron picks it up as soon as it's live. Months before the current
   one are dropped from the window, so the site never keeps showing a month
   that's already over (a still-open multi-month exhibit floats forward to the
   current month instead of disappearing — see `Event.month_key` in the
   script). Content is clean Elementor markup: `<h2>` category headings each
   followed by a `<ul>` of `<li>` events (`<strong>Title</strong>` +
   description). Dates are extracted from the prose best-effort.

   **As of November 2026 SFCA appears to have stopped publishing these
   pages:** October, November, and December 2026 all 404 while August still
   loads. The fetcher stays in place in case they resume; sources 6 and 7
   below were added to replace the coverage.

2. **Capitol Modern: the Hawaiʻi State Art Museum** — the upcoming-events feed at
   `https://www.capitolmodern.org/events`. A Next.js app whose events are
   streamed as a Sanity CMS JSON payload inside `self.__next_f`, carrying **exact
   UTC start/end timestamps**, titles, slugs, venue, description, and ticket
   URLs. Times are UTC and are converted to **HST (UTC−10, no DST)** to get the
   correct local date — otherwise e.g. First Friday's `2026-09-05T03:30Z` would
   wrongly land on Sept 5 instead of Sept 4.

3. **SFCA Events** (supplementary) — SFCA's own `/events/` page runs The Events
   Calendar, whose REST API at
   `https://sfca.hawaii.gov/wp-json/tribe/events/v1/events` is open. It carries
   SFCA's internal events (mostly public meetings such as board meetings) with
   exact UTC instants. Hidden/unpublished entries are skipped; pagination
   follows `next_rest_url`. Archived to `data/raw/sfca-tec-events.json`.

4. **Hawaiʻi State Public Libraries** — The Events Calendar plugin's open REST
   API at `https://www.librarieshawaii.org/wp-json/tribe/events/v1/events`.
   It carries every branch program statewide, so the builder queries only
   arts-leaning TEC categories (`culture`, `music`, `author-talk`,
   `poetry`, `photography`, `history`) for today through the end of the SFCA
   window, then drops kids' storytime, games, and similar programs by
   category and title keyword (see `LIB_*` constants and `is_arts_event`).
   Results are archived, slimmed, to `data/raw/librarieshawaii-events.json`
   for `--offline` builds. Library records are never merged with each other
   (two sessions of a class are distinct events).

5. **Volcano Art Center** — the WordPress RSS feed at
   `https://volcanoartcenter.org/feed/`. It's a plain blog feed (no Events
   Calendar plugin), so each post announces one event. Dates are parsed from
   the post title, then the description excerpt (ordinals like "October 10th"
   and "October 3 through November 15" are normalized first); the year comes
   from `pubDate`, rolled forward when a date would land well before
   publication (a January workshop posted in July). Undated posts and events
   that have already ended are dropped. Unit tests run against the offline
   fixture in `tests/fixtures/` (`python3 -m unittest discover -s tests`).

6. **The Creative City** — a curated Oʻahu arts calendar (HoMA, Bishop
   Museum, Capitol Modern, Downtown Art Center, Kumu Kahua, Manoa Valley
   Theatre, HSO, HTY, ...). It's a Squarespace events collection, so
   `https://www.thecreativecity.org/events?format=json` returns every upcoming
   event with exact start/end instants (ms epoch, converted to HST), venue,
   and categories. (`/oahu-events` is a display page over the same
   collection and has no feed of its own.) Single-day events keep their exact
   time; multi-day runs and long-running series ("HoMA Nights – every
   Friday") become a date span, which floats into the current month while
   still running. Events starting past the site window are dropped. Archived
   to `data/raw/thecreativecity-events.json`.

7. **AlohaCalendar** — a free, keyless public JSON API
   (`https://alohacalendar.com/api/v1/events`, allowed by its robots.txt and
   documented at `alohacalendar.com/api`) queried with `category=arts`. It is
   the main source of Maui, Kauaʻi, and Hawaiʻi Island events. Quirks:
   responses cap at 50 events, so the builder pages by advancing `from` to
   the last date returned; and timestamps are local wall time despite their
   `Z` suffix (the clock is sometimes a `00:00` placeholder, so `startTime`
   is the authoritative start). Repeat showings of one title on one island
   (nightly shows, a touring musical's run) collapse into one recurring
   "N dates" span; farmers/night markets are dropped. Like library records,
   AlohaCalendar listings are never merged with each other (two showtimes or
   a preview vs. an opening are distinct). Archived to
   `data/raw/alohacalendar-events.json`.

## Tests

```sh
python -m unittest discover -s tests -v   # uses tests/fixtures/, no network
python scripts/fetch_and_build.py --offline
```

## Candidate additional sources

**November 2026 search.** Two passes looked for replacements once SFCA's
monthly pages stopped: (1) venues and calendars mentioned alongside the
current sources, and (2) pages that list two or more past SFCA calendar
events together. Sources 6 and 7 came out of that. Also found, not
implemented:

- **Hawaiʻi Public Radio Community Calendar**
  (`hawaiipublicradio.org/community-calendar`) — the only large statewide
  arts-heavy list, but HTML only (Brightspot, `?f0=` filters), no feed.
- **Hawaiʻi Theatre Center** — `/wp-json/wp/v2/events` is open (31 events)
  but carries no event dates; they'd have to be scraped from each page.
- **Honolulu Pineapple Post** (Substack RSS) and **Big Island Now's weekly
  "Top 10 things to do"** (WP posts API) — both current and both listed many
  past SFCA events, but they're prose newsletters mixing arts with food and
  news; parsing would be fragile.
- **Hawaiʻi Tribune-Herald / West Hawaiʻi Today** (CitySpark widget) and
  **Maui Now / Big Island Now `/events`** (HubHawaii widget) — JSON behind
  undocumented widget APIs; not used.
- **Ka Wai Ola (OHA)** and **DLNR State Parks** run The Events Calendar but
  return 403 to scripted requests.
- **GoHawaii.com events** — Cloudflare-blocked.

Gap: no source carries Maui/Kauaʻi gallery exhibits well (e.g. Viewpoints
Gallery, Hui Noʻeau); AlohaCalendar mostly has performances there.

**Earlier (September 2026) notes:**

Researched by fetching each venue's public site and fingerprinting the
platform behind it (WordPress + The Events Calendar plugin, RSS, JSON-LD
`Event` schema, Next.js/Sanity like Capitol Modern, etc.), based on venues
that SFCA's own calendar links out to. Not yet implemented — ranked by how
promising each looked:

**Strong — structured feed confirmed working:**

- *(Hawaiʻi State Public Libraries — now implemented as source 4 above.)*

**Worth a closer look — same WordPress stack, not yet API-verified:**

- **ʻIolani Palace** (`iolanipalace.org`) fingerprints as WordPress with an
  RSS feed present (Volcano Art Center, the other such site, is now
  implemented from its RSS feed); it wasn't checked yet for a `/wp-json/tribe/events/v1/events`
  endpoint the way the library site and SFCA's own `/events/` page have. If
  it runs The Events Calendar too, it's the same easy integration as
  above.
- **SFCA's own `/events/` page** — now implemented as a supplementary source
  (`SFCA Events`) via its open TEC REST API,
  `https://sfca.hawaii.gov/wp-json/tribe/events/v1/events`. It only carries
  SFCA's internal events (e.g. board meetings), not the community calendar.
  The response is archived to `data/raw/sfca-tec-events.json` for `--offline`
  builds. Its native `?ical=1` export sits behind a Cloudflare challenge, so
  the JSON API is used instead.

**Needs a browser-render probe, not just curl:** Honolulu Museum of Art
(`honolulumuseum.org/events`), Maui Arts & Cultural Center
(`mauiarts.org/events`), Bishop Museum, and Diamond Head Theatre all returned
pages with no recognizable platform signature (Diamond Head's was only 5KB —
almost certainly a JS-rendered ticketing widget). They may run a proprietary
ticketing backend (Tessitura, AudienceView, Spektrix) with its own API, but
that requires loading the page in an actual browser to see what it fetches
client-side.

**Probably not worth a dedicated parser:** the small single-venue sites SFCA
links to (Downtown Art Center [Squarespace], Firehouse Gallery/Waimea Arts
Council, Wailoa Center, Gallery ʻIolani, Guitar Foundation of Hawaiʻi, West
Hawaiʻi County Band, First Friday Hawaii) are low-volume enough that the
ongoing parser-maintenance cost likely isn't worth it — SFCA's own monthly
roundup already captures their headline events.

## Merge & dedupe

All events from all five sources feed one list, then duplicates are clustered:

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
- Separate library records stay separate, even when another source's summary
  matches both sessions.

Library pagination must finish within its configured page limit. Incomplete or
malformed library and Volcano responses use the previous archive and report
partial status. Validated archives replace the previous file atomically, so a
failed write cannot truncate the fallback. Volcano posts with multiple class
series stay listed until the last advertised series ends; their displayed dates
retain each range.

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

**Per-island feeds.** Besides the statewide `calendar.ics` there is one feed
per county: `calendar-oahu.ics`, `calendar-hawaii-island.ics`,
`calendar-maui-county.ics` (Maui, Molokaʻi, Lānaʻi), and `calendar-kauai.ics`,
linked from the page header. Each event's `island` (also in `events.json`)
comes from, in order: a provider that only covers one island (Capitol
Modern, The Creative City → Oʻahu; Volcano Art Center → Hawaiʻi Island); the
"(Island)" suffix on library and AlohaCalendar venues; then place names in
the venue, title, or description (`_ISLAND_PLACES`; the first field naming
exactly one county wins). Ambiguous names (Waimea, Kīlauea, bare "Hawaiʻi")
are not used. Events with no island are only in the statewide feed.

## Run locally

```bash
pip install -r requirements.txt
python scripts/fetch_and_build.py            # fetch all sources + build
python scripts/fetch_and_build.py --offline  # rebuild from archived data/raw/*
python -m unittest discover -s tests -v       # unit tests (offline fixtures)
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
scripts/fetch_and_build.py   fetch (all sources) + merge/dedupe + build
data/raw/*.html              archived source pages
data/events.json             structured, merged events (canonical copy)
site/                        generated static site (index, per-month, .ics, .json)
.github/workflows/update.yml cron + dispatch + Pages deploy
```
