#!/usr/bin/env python3
"""Fetch Hawai'i arts events from multiple sources, merge + dedupe, build a site.

Sources
-------
1. SFCA Arts & Culture Calendar (monthly pages)
     https://sfca.hawaii.gov/arts-and-culture-calendar-<month>-<year>/
   No next-month navigation and the WordPress REST API is locked down (Kadence
   Security -> 401), so month discovery guesses the slug for every month from
   max(August 2026, current month) through current month + 2 and fetches plain
   HTML. Missing months 404 gracefully -- expected right after a month rolls
   over, since SFCA usually publishes a few days in. Months before the current
   one are dropped from the window entirely, so the site never shows a month
   that's already over (a still-open multi-month exhibit floats forward to the
   current month instead of disappearing; see Event.month_key). Content is
   clean Elementor markup: <h2> category headings each followed by a <ul> of
   <li> events (<strong>Title</strong> + prose).

2. Capitol Modern: the Hawai'i State Art Museum (upcoming events feed)
     https://www.capitolmodern.org/events
   A Next.js app. Events are rendered as cards AND streamed as a Sanity CMS JSON
   payload inside self.__next_f, which carries exact UTC start/end timestamps,
   titles, slugs, venue, description, and ticket URLs. Times are UTC and are
   converted to HST (UTC-10, no DST) to get the correct local date.

3. SFCA /events/ (The Events Calendar REST API) -- supplementary
     https://sfca.hawaii.gov/wp-json/tribe/events/v1/events
   SFCA's own events page runs The Events Calendar, whose REST API is open even
   though the core WP API is locked down. It carries SFCA's internal events
   (mostly public meetings), not the community calendar. JSON with exact UTC
   instants; archived to data/raw/sfca-tec-events.json for --offline.

4. Hawai'i State Public Libraries (The Events Calendar REST API)
     https://www.librarieshawaii.org/wp-json/tribe/events/v1/events
   Open JSON API covering every branch program statewide. The query asks for
   arts-leaning TEC categories (culture, music, poetry, ...) and the parser
   then drops kids' storytime, games, and similar non-arts programs. Results
   are archived slimmed to data/raw/librarieshawaii-events.json for --offline.

5. Volcano Art Center (WordPress RSS feed)
     https://volcanoartcenter.org/feed/
   A plain blog feed (no Events Calendar plugin), so each <item> is a post
   announcing an event. Dates are parsed from the post title first, then the
   description excerpt ("October 10th", "October 3 through November 15"); the
   year comes from the post's pubDate, rolled forward when the date would
   otherwise land well before publication. Undated posts and events that have
   already ended are dropped.

Pipeline
--------
    fetch each source -> parse to a common Event model -> merge + dedupe across
    sources -> group by month -> write data/events.json + site/ (index,
    per-month pages, calendar.ics, events.json).

Dedupe requires the SAME local start date AND a strong title match, so distinct
instances of a recurring event (e.g. two different "Super Saturday" dates) are
never collapsed. Merged events keep every contributing source with a link back.

Only facts (title, date, venue, link) are re-presented, with attribution. SFCA
and Capitol Modern content is not automatically public domain, so their markup
is not mirrored wholesale.

Runs on stdlib + requests + beautifulsoup4. Built to run in GitHub Actions
(unrestricted network) on a cron, but fully runnable locally.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SFCA_BASE = "https://sfca.hawaii.gov"
SFCA_SLUG_FMT = "arts-and-culture-calendar-{month}-{year}"
START_YEAR, START_MONTH = 2026, 8  # August 2026 is the first published month.
MONTHS_AHEAD = 2  # SFCA discovery reaches current month + this many.

CAPMOD_BASE = "https://www.capitolmodern.org"
CAPMOD_EVENTS_URL = f"{CAPMOD_BASE}/events"
CAPMOD_SLUG = "capitolmodern-events"

# SFCA's own /events/ page runs The Events Calendar; its REST API is open
# (unlike the core WP API). Mostly internal public meetings -> supplementary.
SFCA_TEC_API = f"{SFCA_BASE}/wp-json/tribe/events/v1/events"
SFCA_TEC_SLUG = "sfca-tec-events"
SFCA_TEC_NAME = "SFCA Events"
SFCA_TEC_MAX_PAGES = 10

LIB_BASE = "https://www.librarieshawaii.org"
LIB_API_URL = f"{LIB_BASE}/wp-json/tribe/events/v1/events"
LIB_SLUG = "librarieshawaii-events"
LIB_PROVIDER = "Libraries"
LIB_PROVIDER_FULL = "Hawaiʻi State Public Libraries"
LIB_PER_PAGE = 50
LIB_MAX_PAGES = 20
LIB_DESC_MAX = 400
# TEC category slugs that signal arts/culture programming (queried server-side).
LIB_ARTS_CATEGORIES = ("culture", "music", "author-reading", "poetry",
                       "photography", "history")
# Any of these disqualifies an event: kids' programs, games, tech help, etc.
LIB_EXCLUDE_CATEGORIES = {"storytime-for-kids", "games", "computer-classes",
                          "health-and-wellness"}
LIB_EXCLUDE_TITLE_WORDS = ("keiki", "storytime", "story time", "mahjong",
                           "chess", "puzzle", "lego", "karaoke", "preschool",
                           "toddler", "baby", "babies", "tiny tunes")
# Accessibility notice appended to every event description.
LIB_BOILERPLATE = "auxiliary aid"

VAC_BASE = "https://volcanoartcenter.org"
VAC_FEED_URL = f"{VAC_BASE}/feed/"
VAC_SLUG = "volcanoartcenter-feed"
# A post whose first parsed date falls this far before its publish date is
# about the following year (e.g. a January workshop announced in July).
VAC_YEAR_ROLL_DAYS = 60

HST = dt.timezone(dt.timedelta(hours=-10))  # Hawai'i has no daylight saving.

# In the iCal feed, an all-day run this long or longer is emitted as two
# one-day "opens"/"closes" markers instead of one multi-week spanning bar.
LONG_RUN_DAYS = 6

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTH_NUM = {name: i + 1 for i, name in enumerate(MONTH_NAMES)}

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
DATA_DIR = REPO_ROOT / "data"
SITE_DIR = REPO_ROOT / "site"

# SFCA section headings that are not event categories.
SKIP_HEADINGS = ("about the state foundation",)

USER_AGENT = (
    "Mozilla/5.0 (compatible; sfca-calendar-bot/2.0; static-site builder; "
    "contact via repo issues)"
)


# --------------------------------------------------------------------------- #
# Common data model
# --------------------------------------------------------------------------- #

@dataclass
class Source:
    name: str          # "SFCA" | "Capitol Modern"
    url: str           # link back to the source listing / detail page
    label: str = ""    # provenance label, e.g. "August 2026" or a domain


@dataclass
class Event:
    title: str
    description: str
    category: str
    provider: str                       # originating source name
    sources: list[Source] = field(default_factory=list)
    venue: str | None = None
    date_start: str | None = None       # ISO local date (YYYY-MM-DD)
    date_end: str | None = None         # ISO local date
    date_display: str | None = None
    time_display: str | None = None     # e.g. "11:00 AM - 3:00 PM HST"
    dt_start_utc: str | None = None      # ISO instant, for timed ICS entries
    dt_end_utc: str | None = None
    recurring: bool = False
    links: list[dict] = field(default_factory=list)
    fallback_month: str | None = None   # 'YYYY-MM' when no date_start (grouping)
    uid_seed: str = ""                  # stable identity for a deterministic UID

    def uid(self, suffix: str = "") -> str:
        # Deterministic across builds so calendar clients update in place rather
        # than duplicating. (Python's hash() is per-process salted -- never use
        # it for identities that must persist.)
        seed = self.uid_seed or f"{self.title}|{self.date_start or ''}"
        h = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
        return f"{h}{suffix}@hawaii-arts-calendar"

    def month_key(self, current_month: str | None = None) -> str | None:
        # An event belongs to its source-page month when its date range actually
        # spans that month (e.g. a July-Aug exhibit listed on the August page
        # stays in August). Otherwise it groups under its real start month
        # (e.g. a September event flagged ahead of time on the August page).
        if self.date_start:
            s = self.date_start[:7]
            e = (self.date_end or self.date_start)[:7]
            if self.fallback_month and s <= self.fallback_month <= e:
                key = self.fallback_month
            else:
                key = s
            # Old months roll off the site (see sfca_month_window), which would
            # otherwise make a still-open exhibit vanish the moment its origin
            # month is dropped. Float it forward to the current month instead,
            # as long as it's still running.
            if current_month and key < current_month <= e:
                return current_month
            return key
        return self.fallback_month

    def sort_key(self) -> tuple:
        # Dated events first, in date order; undated events sort last by title.
        return (0, self.date_start) if self.date_start else (1, self.title.lower())


@dataclass
class ProviderStatus:
    name: str
    status: str        # "ok" | "partial" | "error"
    detail: str = ""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def fetch(url: str, session: requests.Session) -> tuple[int, str]:
    resp = session.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    return resp.status_code, resp.text


# --------------------------------------------------------------------------- #
# Source 1: SFCA
# --------------------------------------------------------------------------- #

def sfca_month_window(today: dt.date) -> list[tuple[int, int]]:
    """Every (year, month) from max(Aug 2026, current month) through current
    month + MONTHS_AHEAD. Months before the current one roll off so the site
    (and the daily fetch) never re-shows or re-requests a month that's over."""
    start = max(dt.date(START_YEAR, START_MONTH, 1),
                dt.date(today.year, today.month, 1))
    end_m = today.month + MONTHS_AHEAD
    end_y = today.year + (end_m - 1) // 12
    end_m = (end_m - 1) % 12 + 1
    end = dt.date(end_y, end_m, 1)
    if end < start:
        end = start
    out, cur = [], start
    while cur <= end:
        out.append((cur.year, cur.month))
        cur = dt.date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
    return out


def sfca_slug(year: int, month: int) -> str:
    return SFCA_SLUG_FMT.format(month=MONTH_NAMES[month - 1], year=year)


_MONTH_ALT = "|".join(MONTH_NAMES)
_RANGE_RE = re.compile(
    rf"\b({_MONTH_ALT})\s+(\d{{1,2}})\s*[\u2013\u2014-]\s*"
    rf"(?:({_MONTH_ALT})\s+)?(\d{{1,2}})(?:,?\s*(\d{{4}}))?",
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(
    rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?:,?\s*(\d{{4}}))?", re.IGNORECASE)
_RECUR_RE = re.compile(
    r"\b(every|monthly|weekly|each (?:month|week|first|second|third)|"
    r"first friday|second friday|ongoing)\b", re.IGNORECASE)


def _mk_date(month: int, day: int, year: int) -> str | None:
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def extract_dates(text: str, default_year: int) -> dict:
    """Pull the first date (or range) out of an event's prose, best-effort."""
    recurring = bool(_RECUR_RE.search(text))
    out = {"date_start": None, "date_end": None,
           "date_display": None, "recurring": recurring}
    m = _RANGE_RE.search(text)
    if m:
        m1, d1, m2, d2, yr = m.groups()
        year = int(yr) if yr else default_year
        sm = MONTH_NUM[m1.lower()]
        em = MONTH_NUM[m2.lower()] if m2 else sm
        ey = year if em >= sm else year + 1
        out["date_start"] = _mk_date(sm, int(d1), year)
        out["date_end"] = _mk_date(em, int(d2), ey)
        out["date_display"] = m.group(0).strip()
        return out
    m = _SINGLE_RE.search(text)
    if m:
        mon, day, yr = m.groups()
        year = int(yr) if yr else default_year
        iso = _mk_date(MONTH_NUM[mon.lower()], int(day), year)
        out["date_start"] = iso
        out["date_end"] = iso
        out["date_display"] = m.group(0).strip()
    return out


def parse_sfca(page_html: str, url: str, label: str, month_key: str,
               default_year: int) -> list[Event]:
    soup = BeautifulSoup(page_html, "html.parser")
    content = (soup.select_one(".elementor-widget-theme-post-content")
               or soup.select_one(".entry-content") or soup.body or soup)
    for tag in content(["script", "style"]):
        tag.decompose()

    events: list[Event] = []
    category = "General"
    for node in content.find_all(["h2", "h3", "ul"]):
        if node.name in ("h2", "h3"):
            category = node.get_text(" ", strip=True)
            continue
        if any(skip in category.lower() for skip in SKIP_HEADINGS):
            continue
        for li in node.find_all("li", recursive=False):
            ev = _parse_sfca_li(li, category, url, label, month_key,
                                default_year)
            if ev:
                events.append(ev)
    return events


def _parse_sfca_li(li, category: str, url: str, label: str, month_key: str,
                   default_year: int) -> Event | None:
    strong = li.find("strong") or li.find("b")
    title = (strong.get_text(" ", strip=True).strip(" .") if strong
             else li.get_text(" ", strip=True).split(".")[0].strip())
    if not title:
        return None
    full = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
    desc = full
    if full.lower().startswith(title.lower()):
        desc = full[len(title):].lstrip(" .,\u2013\u2014-").strip()

    links, seen = [], set()
    for a in li.find_all("a"):
        href = a.get("href")
        if href and href not in seen:
            seen.add(href)
            links.append({"text": a.get_text(" ", strip=True) or href,
                          "href": href})

    d = extract_dates(full, default_year)
    # Keyed by source month + normalized title so the identity survives minor
    # prose edits and re-runs.
    seed = f"sfca|{month_key}|{_norm_title(title)}"
    return Event(
        title=title, description=desc, category=category, provider="SFCA",
        sources=[Source("SFCA", url, label)], links=links,
        fallback_month=month_key, uid_seed=seed, **d,
    )


def collect_sfca(today: dt.date, offline: bool
                 ) -> tuple[list[Event], list[dict], ProviderStatus]:
    """Return (events, month_status_rows, provider_status)."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    events: list[Event] = []
    rows: list[dict] = []
    ok_any = False

    for year, month in sfca_month_window(today):
        slug = sfca_slug(year, month)
        url = f"{SFCA_BASE}/{slug}/"
        label = f"{MONTH_NAMES[month - 1].capitalize()} {year}"
        mkey = f"{year:04d}-{month:02d}"
        raw_path = RAW_DIR / f"{slug}.html"
        page_html, status, note = None, "missing", ""

        if offline:
            if raw_path.exists():
                page_html, status = raw_path.read_text(encoding="utf-8"), "ok"
            else:
                note = "offline: no archived HTML"
        else:
            try:
                code, text = fetch(url, session)
                if code == 200 and "post-content" in text:
                    page_html = text
                    raw_path.write_text(text, encoding="utf-8")
                    status = "ok"
                elif code == 404:
                    note = "not published yet (404)"
                else:
                    status, note = "error", f"unexpected HTTP {code}"
            except requests.RequestException as exc:
                status, note = "error", f"fetch failed: {exc}"
                if raw_path.exists():
                    page_html, status = raw_path.read_text(encoding="utf-8"), "ok"
                    note += " (used archived copy)"

        n = 0
        if page_html:
            try:
                evs = parse_sfca(page_html, url, label, mkey, year)
                events.extend(evs)
                n = len(evs)
                ok_any = True
            except Exception as exc:  # never crash the build on one month
                status, note = "error", f"parse failed: {exc}"

        rows.append({"slug": slug, "label": label, "url": url,
                     "month_key": mkey, "status": status, "note": note,
                     "count": n})
        print(f"  SFCA {label:<16} {status:<8} events={n:<3} {note}",
              file=sys.stderr)

    ps = ProviderStatus(
        "SFCA", "ok" if ok_any else "error",
        "; ".join(f"{r['label']}: {r['note'] or r['count']}" for r in rows))
    return events, rows, ps


# --------------------------------------------------------------------------- #
# Source 2: Capitol Modern
# --------------------------------------------------------------------------- #

def _unescape_next(payload: str) -> str:
    """Undo the RSC string escaping used inside self.__next_f pushes."""
    return payload.replace('\\"', '"').replace("\\\\", "\\").replace("\\/", "/")


def _clean_json_str(s: str) -> str:
    """Decode remaining \\uXXXX / \\n / \\t escapes in a captured JSON value."""
    try:
        return json.loads('"' + s.replace('"', '\\"') + '"')
    except Exception:
        return s


def _fmt_time_range(start_utc: str, end_utc: str | None) -> tuple[str, str, str]:
    """Return (date_display, time_display, iso_local_date) in HST."""
    su = dt.datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    sl = su.astimezone(HST)
    date_display = sl.strftime("%B %-d, %Y")
    t = sl.strftime("%-I:%M %p")
    if end_utc:
        try:
            el = dt.datetime.fromisoformat(
                end_utc.replace("Z", "+00:00")).astimezone(HST)
            if el.date() == sl.date():
                t = f"{t} - {el.strftime('%-I:%M %p')}"
        except ValueError:
            pass
    return date_display, f"{t} HST", sl.date().isoformat()


def parse_capitol_modern(page_html: str) -> list[Event]:
    u = _unescape_next(page_html)
    events: list[Event] = []
    seen_slugs: set[str] = set()
    # Each Sanity event object begins with "_createdAt".
    for chunk in re.split(r'(?="_createdAt")', u):
        m_slug = re.search(r'"current":"(/events/[^"]+?)/?"', chunk)
        m_start = re.search(r'"startDate":"([^"]+)"', chunk)
        m_title = re.search(r'"title":"([^"]*)"', chunk)
        if not (m_slug and m_start and m_title):
            continue
        slug = m_slug.group(1)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        m_end = re.search(r'"endDate":"([^"]+)"', chunk)
        m_desc = re.search(r'"description":"([^"]*)"', chunk)
        m_loc = re.search(r'"location":"([^"]*)"', chunk)
        m_tix = re.search(r'"externalTicketingUrl":"([^"]*)"', chunk)
        recurring = ('"recurrence"' in chunk or '"frequency"' in chunk
                     or "first-fridays" in slug)

        start_utc = m_start.group(1)
        end_utc = m_end.group(1) if m_end else None
        # endDate may be a plain YYYY-MM-DD recurrence-end; only treat as an
        # instant when it carries a time component.
        end_instant = end_utc if (end_utc and "T" in end_utc) else None
        date_display, time_display, iso_date = _fmt_time_range(
            start_utc, end_instant)

        detail = f"{CAPMOD_BASE}{slug}"
        links = [{"text": "Event details (Capitol Modern)", "href": detail}]
        if m_tix and m_tix.group(1):
            links.append({"text": "Tickets / registration",
                          "href": _clean_json_str(m_tix.group(1))})

        title = _clean_json_str(m_title.group(1)).strip()
        events.append(Event(
            title=title,
            description=_clean_json_str(m_desc.group(1)) if m_desc else "",
            category="Capitol Modern",
            provider="Capitol Modern",
            sources=[Source("Capitol Modern", detail, "capitolmodern.org")],
            venue=_clean_json_str(m_loc.group(1)) if m_loc else
                  "Capitol Modern: the Hawai\u02bbi State Art Museum",
            date_start=iso_date,
            date_end=(_fmt_time_range(end_instant, None)[2]
                      if end_instant else iso_date),
            date_display=date_display,
            time_display=time_display,
            dt_start_utc=start_utc,
            dt_end_utc=end_instant,
            recurring=recurring,
            links=links,
            uid_seed=f"capmod|{slug}",  # slug is stable even if the title edits
        ))
    events.sort(key=lambda e: e.date_start or "")
    return events


def collect_capitol_modern(offline: bool
                           ) -> tuple[list[Event], ProviderStatus]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"{CAPMOD_SLUG}.html"
    page_html = None
    if offline:
        if raw_path.exists():
            page_html = raw_path.read_text(encoding="utf-8")
        else:
            return [], ProviderStatus("Capitol Modern", "error",
                                      "offline: no archived HTML")
    else:
        try:
            code, text = fetch(CAPMOD_EVENTS_URL, requests.Session())
            if code == 200 and "__next_f" in text:
                page_html = text
                raw_path.write_text(text, encoding="utf-8")
            else:
                if raw_path.exists():
                    page_html = raw_path.read_text(encoding="utf-8")
                    note = f"HTTP {code}; used archived copy"
                    events = parse_capitol_modern(page_html)
                    return events, ProviderStatus("Capitol Modern", "partial", note)
                return [], ProviderStatus("Capitol Modern", "error",
                                          f"unexpected HTTP {code}")
        except requests.RequestException as exc:
            if raw_path.exists():
                page_html = raw_path.read_text(encoding="utf-8")
            else:
                return [], ProviderStatus("Capitol Modern", "error",
                                          f"fetch failed: {exc}")
    try:
        events = parse_capitol_modern(page_html)
    except Exception as exc:
        return [], ProviderStatus("Capitol Modern", "error",
                                  f"parse failed: {exc}")
    print(f"  Capitol Modern   ok       events={len(events)}", file=sys.stderr)
    return events, ProviderStatus("Capitol Modern", "ok",
                                  f"{len(events)} upcoming events")


# --------------------------------------------------------------------------- #
# Source 3: SFCA /events/ (The Events Calendar REST API)
# --------------------------------------------------------------------------- #

def _tec_utc_iso(value: str | None) -> str | None:
    """'2026-10-16 21:00:00' (TEC utc_*_date) -> '2026-10-16T21:00:00Z'."""
    if not value or len(value) < 19:
        return None
    try:
        dt.datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return value[:10] + "T" + value[11:19] + "Z"


def _fmt_date_span(start: dt.date, end: dt.date) -> str:
    if end <= start:
        return start.strftime("%B %-d, %Y")
    if start.year != end.year:
        return f"{start.strftime('%B %-d, %Y')} – {end.strftime('%B %-d, %Y')}"
    if start.month != end.month:
        return (f"{start.strftime('%B %-d')} – "
                f"{end.strftime('%B %-d')}, {start.year}")
    return f"{start.strftime('%B %-d')} – {end.day}, {start.year}"


def _tec_text(value) -> str:
    if not isinstance(value, str) or not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", html.unescape(text).replace("\xa0", " ")).strip()


def _parse_tec_event(raw: dict) -> Event | None:
    if raw.get("hide_from_listings") or raw.get("status", "publish") != "publish":
        return None
    title = _tec_text(raw.get("title"))
    url = raw.get("url")
    start_local = raw.get("start_date") or ""
    if not (title and url and len(start_local) >= 10):
        return None
    try:
        d_start = dt.date.fromisoformat(start_local[:10])
        d_end = dt.date.fromisoformat((raw.get("end_date") or start_local)[:10])
    except ValueError:
        return None
    d_end = max(d_end, d_start)

    start_utc = end_utc = time_display = None
    if not raw.get("all_day"):
        start_utc = _tec_utc_iso(raw.get("utc_start_date"))
        end_utc = _tec_utc_iso(raw.get("utc_end_date"))
    if start_utc:
        _, time_display, _ = _fmt_time_range(start_utc, end_utc)

    venue = raw.get("venue")
    venue_name = (_tec_text(venue.get("venue")) or None
                  if isinstance(venue, dict) else None)
    cats = [c.get("name") for c in raw.get("categories") or []
            if isinstance(c, dict) and c.get("name")]

    links = [{"text": "Event details (SFCA)", "href": url}]
    website = raw.get("website")
    if isinstance(website, str) and website.startswith("http") and website != url:
        links.append({"text": "Event website", "href": website})

    return Event(
        title=title,
        description=_tec_text(raw.get("description")),
        category=html.unescape(cats[0]) if cats else SFCA_TEC_NAME,
        provider=SFCA_TEC_NAME,
        sources=[Source(SFCA_TEC_NAME, url, "sfca.hawaii.gov/events")],
        venue=venue_name,
        date_start=d_start.isoformat(),
        date_end=d_end.isoformat(),
        date_display=_fmt_date_span(d_start, d_end),
        time_display=time_display,
        dt_start_utc=start_utc,
        dt_end_utc=end_utc if start_utc else None,
        links=links,
        uid_seed=f"sfca-tec|{raw.get('id') or url}",
    )


def parse_sfca_tec(payload: dict) -> list[Event]:
    """Parse a TEC /wp-json/tribe/events/v1/events response into Events."""
    events = []
    for raw in (payload or {}).get("events") or []:
        if isinstance(raw, dict):
            ev = _parse_tec_event(raw)
            if ev:
                events.append(ev)
    events.sort(key=lambda e: e.date_start or "")
    return events


# --------------------------------------------------------------------------- #
# Source 5: Volcano Art Center (WordPress RSS)
# --------------------------------------------------------------------------- #

_ORDINAL_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_THROUGH_RE = re.compile(r"\s+(?:through|thru|until|to)\s+(?=[A-Za-z]+\s+\d)",
                         re.IGNORECASE)
_WP_BOILERPLATE_RE = re.compile(r"\s*The post .*? appeared first on .*$",
                                re.DOTALL)


def _vac_normalize_dates(text: str) -> str:
    """Make VAC prose digestible by extract_dates: drop ordinals, 'through'."""
    text = _ORDINAL_RE.sub(r"\1", text)
    return _THROUGH_RE.sub(" \u2013 ", text)


def _vac_venue(text: str) -> str:
    low = text.lower()
    if "niaulani" in low:
        return "Volcano Art Center Niaulani Campus, Volcano Village"
    if "golf course" in low:
        return "Volcano Golf Course, Volcano"
    if "national park" in low:
        return ("Volcano Art Center Gallery, "
                "Hawai\u02bbi Volcanoes National Park")
    return "Volcano Art Center"


def _vac_dates(title: str, desc: str, published: dt.date) -> dict | None:
    for text in (title, desc):
        norm = _vac_normalize_dates(text)
        d = extract_dates(norm, published.year)
        if not d["date_start"]:
            continue
        roll = published - dt.timedelta(days=VAC_YEAR_ROLL_DAYS)
        if dt.date.fromisoformat(d["date_start"]) < roll:
            d = extract_dates(norm, published.year + 1)
        if d["date_start"]:
            return d
    return None


def parse_volcano_art_center(feed_xml: str, today: dt.date) -> list[Event]:
    """Parse the VAC RSS feed into upcoming/ongoing Events.

    Raises on XML that isn't an RSS feed, so callers can report a parse error.
    """
    root = ET.fromstring(feed_xml.encode("utf-8"))
    if root.tag != "rss":
        raise ValueError(f"not an RSS feed (root <{root.tag}>)")
    events: list[Event] = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        raw_desc = item.findtext("description") or ""
        desc = BeautifulSoup(raw_desc, "html.parser").get_text(" ")
        desc = _WP_BOILERPLATE_RE.sub("", html.unescape(desc))
        desc = re.sub(r"\s+", " ", desc).replace("[\u2026]", "\u2026").strip()

        pub = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub).date() if pub else today
        except (TypeError, ValueError):
            published = today
        d = _vac_dates(title, desc, published)
        if not d or (d["date_end"] or d["date_start"]) < today.isoformat():
            continue

        cats = [c.text.strip() for c in item.findall("category")
                if c.text and c.text.strip().lower() != "news"]
        post_id = item.findtext("{com-wordpress:feed-additions:1}post-id")
        guid = item.findtext("guid") or link
        events.append(Event(
            title=title,
            description=desc,
            category=cats[0] if cats else "Volcano Art Center",
            provider="Volcano Art Center",
            sources=[Source("Volcano Art Center", link, "volcanoartcenter.org")],
            venue=_vac_venue(f"{title} {desc}"),
            date_start=d["date_start"],
            date_end=d["date_end"],
            date_display=d["date_display"],
            recurring=d["recurring"],
            links=[{"text": "Event details (Volcano Art Center)",
                    "href": link}],
            uid_seed=f"vac|{post_id.strip() if post_id else guid}",
        ))
    events.sort(key=lambda e: e.date_start or "")
    return events


def _fetch_json(url: str) -> tuple[int, dict | None]:
    resp = requests.get(url, timeout=30, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        return resp.status_code, resp.json()
    except ValueError:  # e.g. a Cloudflare HTML challenge page
        return resp.status_code, None


def collect_sfca_tec(offline: bool, fetch_json=_fetch_json
                     ) -> tuple[list[Event], ProviderStatus]:
    """Supplementary feed: SFCA's own events (mostly public meetings)."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"{SFCA_TEC_SLUG}.json"

    def from_archive(status: str, note: str):
        if not raw_path.exists():
            return [], ProviderStatus(SFCA_TEC_NAME, "error", note)
        try:
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
            events = parse_sfca_tec(payload)
        except Exception as exc:
            return [], ProviderStatus(SFCA_TEC_NAME, "error",
                                      f"archive parse failed: {exc}")
        detail = f"{len(events)} events" + (f"; {note}" if note else "")
        print(f"  SFCA Events      {status:<8} events={len(events)}",
              file=sys.stderr)
        return events, ProviderStatus(SFCA_TEC_NAME, status, detail)

    if offline:
        if not raw_path.exists():
            return [], ProviderStatus(SFCA_TEC_NAME, "error",
                                      "offline: no archived JSON")
        return from_archive("ok", "")

    collected: list[dict] = []
    first: dict | None = None
    url: str | None = f"{SFCA_TEC_API}?per_page=50"
    try:
        for _ in range(SFCA_TEC_MAX_PAGES):
            code, payload = fetch_json(url)
            if code != 200 or not isinstance(payload, dict):
                return from_archive("partial",
                                    f"HTTP {code}; used archived copy")
            first = first or payload
            collected.extend(payload.get("events") or [])
            url = payload.get("next_rest_url")
            if not url:
                break
    except requests.RequestException as exc:
        return from_archive("partial", f"fetch failed ({exc}); used archived copy")

    archive = {k: v for k, v in first.items()
               if k not in ("events", "next_rest_url")}
    archive["events"] = collected
    raw_path.write_text(json.dumps(archive, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    events = parse_sfca_tec(archive)
    print(f"  SFCA Events      ok       events={len(events)}", file=sys.stderr)
    return events, ProviderStatus(SFCA_TEC_NAME, "ok", f"{len(events)} events")


# --------------------------------------------------------------------------- #
# Source 4: Hawai'i State Public Libraries (The Events Calendar REST API)
# --------------------------------------------------------------------------- #

def librarieshawaii_window(today: dt.date) -> tuple[dt.date, dt.date]:
    """Today through the last day of the SFCA horizon (current month + N)."""
    y, m = today.year, today.month + MONTHS_AHEAD + 1
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return today, dt.date(y, m, 1) - dt.timedelta(days=1)


def tec_events_url(start: dt.date, end: dt.date, page: int = 1) -> str:
    query = urlencode({
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "categories": ",".join(LIB_ARTS_CATEGORIES),
        "per_page": LIB_PER_PAGE,
        "page": page,
    })
    return f"{LIB_API_URL}?{query}"


def is_arts_event(ev: dict) -> bool:
    """True for arts/culture programs; drops storytime, games, keiki, etc."""
    cats = {c.get("slug") for c in ev.get("categories") or []}
    if not cats & set(LIB_ARTS_CATEGORIES) or cats & LIB_EXCLUDE_CATEGORIES:
        return False
    title = html.unescape(ev.get("title") or "").lower()
    return not any(word in title for word in LIB_EXCLUDE_TITLE_WORDS)


def _lib_description(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    if not paras:
        paras = [soup.get_text(" ", strip=True)]
    keep = [p for p in paras if p and LIB_BOILERPLATE not in p.lower()]
    text = re.sub(r"\s+", " ", " ".join(keep)).strip()
    if len(text) > LIB_DESC_MAX:
        text = text[:LIB_DESC_MAX].rsplit(" ", 1)[0] + "…"
    return text


def _lib_venue(venue: dict | list | None) -> str:
    # TEC venue names look like "Hawai'i &#8211; Mountain View" (island - branch).
    name = html.unescape(venue.get("venue") or "") if isinstance(venue, dict) else ""
    if " – " in name:
        island, branch = name.split(" – ", 1)
        return f"{branch.strip()} Public Library ({island.strip()})"
    return name or LIB_PROVIDER_FULL


def _tec_utc(value: str | None) -> str | None:
    # "2026-10-06 02:00:00" (UTC) -> "2026-10-06T02:00:00Z"
    return value.replace(" ", "T") + "Z" if value else None


def _lib_date_display(start: dt.date, end: dt.date) -> str:
    if start == end:
        return f"{start.strftime('%B')} {start.day}, {start.year}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.strftime('%B')} {start.day} – {end.day}, {end.year}"
    return (f"{start.strftime('%B')} {start.day} – "
            f"{end.strftime('%B')} {end.day}, {end.year}")


def parse_librarieshawaii(raw_events: list[dict]) -> list[Event]:
    events: list[Event] = []
    for ev in raw_events:
        if not is_arts_event(ev):
            continue
        start = dt.date.fromisoformat(ev["start_date"][:10])
        end = dt.date.fromisoformat((ev.get("end_date") or ev["start_date"])[:10])
        if ev.get("all_day"):
            time_display = start_utc = end_utc = None
        else:
            start_utc = _tec_utc(ev.get("utc_start_date"))
            end_utc = _tec_utc(ev.get("utc_end_date"))
            _, time_display, _ = _fmt_time_range(start_utc, end_utc)
        url = ev.get("url") or LIB_BASE
        links = [{"text": "Event details (Hawaiʻi State Public Libraries)",
                  "href": url}]
        events.append(Event(
            title=html.unescape(ev.get("title") or "").strip(),
            description=_lib_description(ev.get("description") or ""),
            category="Library programs",
            provider=LIB_PROVIDER,
            sources=[Source(LIB_PROVIDER, url, "librarieshawaii.org")],
            venue=_lib_venue(ev.get("venue")),
            date_start=start.isoformat(),
            date_end=end.isoformat(),
            date_display=_lib_date_display(start, end),
            time_display=time_display,
            dt_start_utc=start_utc,
            dt_end_utc=end_utc,
            recurring=bool(re.search(r"/\d{4}-\d{2}-\d{2}/?$", url)),
            links=links,
            uid_seed=f"hspl|{ev['id']}|{start.isoformat()}",
        ))
    events.sort(key=lambda e: (e.date_start or "", e.dt_start_utc or "", e.title))
    return events


def slim_tec_event(ev: dict) -> dict:
    """Keep only the TEC fields the parser reads, so the archive stays small."""
    out = {k: ev.get(k) for k in (
        "id", "url", "title", "description", "excerpt", "all_day", "start_date",
        "end_date", "utc_start_date", "utc_end_date", "timezone", "cost",
        "website")}
    out["categories"] = [{"slug": c.get("slug"), "name": c.get("name")}
                         for c in ev.get("categories") or []]
    out["tags"] = [{"slug": t.get("slug"), "name": t.get("name")}
                   for t in ev.get("tags") or []]
    venue = ev.get("venue")
    out["venue"] = ({k: venue.get(k) for k in ("venue", "city", "url")}
                    if isinstance(venue, dict) else {})
    return out


def _fetch_tec_pages(start: dt.date, end: dt.date) -> list[dict]:
    session = requests.Session()
    raw: list[dict] = []
    for page in range(1, LIB_MAX_PAGES + 1):
        code, text = fetch(tec_events_url(start, end, page), session)
        if code != 200:
            # TEC answers 400 for a page past the end; treat that as done.
            if page > 1 and code == 400:
                break
            raise requests.RequestException(f"HTTP {code} on page {page}")
        data = json.loads(text)
        raw.extend(slim_tec_event(e) for e in data.get("events") or [])
        if page >= int(data.get("total_pages") or 1):
            break
    return raw


def collect_librarieshawaii(today: dt.date, offline: bool
                            ) -> tuple[list[Event], ProviderStatus]:
    raw_path = RAW_DIR / f"{LIB_SLUG}.json"
    start, end = librarieshawaii_window(today)
    status, note = "ok", ""
    raw: list[dict] | None = None
    if not offline:
        try:
            raw = _fetch_tec_pages(start, end)
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps({"events": raw}, ensure_ascii=False,
                                           indent=1), encoding="utf-8")
        except (requests.RequestException, ValueError) as exc:
            status, note = "partial", f"fetch failed ({exc}); used archived copy"
    if raw is None:
        if not raw_path.exists():
            detail = "offline: no archived JSON" if offline else note
            return [], ProviderStatus(LIB_PROVIDER, "error", detail)
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))["events"]
        except (ValueError, KeyError) as exc:
            return [], ProviderStatus(LIB_PROVIDER, "error",
                                      f"bad archive: {exc}")
    try:
        events = parse_librarieshawaii(raw)
    except Exception as exc:
        return [], ProviderStatus(LIB_PROVIDER, "error", f"parse failed: {exc}")
    today_iso = today.isoformat()
    events = [e for e in events if (e.date_end or e.date_start) >= today_iso]
    print(f"  Libraries        {status:<8} events={len(events)} "
          f"(of {len(raw)} raw)", file=sys.stderr)
    detail = note or f"{len(events)} arts/culture programs"
    return events, ProviderStatus(LIB_PROVIDER, status, detail)

def collect_volcano_art_center(today: dt.date, offline: bool
                               ) -> tuple[list[Event], ProviderStatus]:
    name = "Volcano Art Center"
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"{VAC_SLUG}.xml"
    archived = (raw_path.read_text(encoding="utf-8")
                if raw_path.exists() else None)
    feed_xml, status, note = None, "ok", ""
    if offline:
        if archived is None:
            return [], ProviderStatus(name, "error", "offline: no archived feed")
        feed_xml = archived
    else:
        try:
            code, text = fetch(VAC_FEED_URL, requests.Session())
            if code == 200 and "<rss" in text:
                feed_xml = text
                raw_path.write_text(text, encoding="utf-8")
            else:
                note = f"HTTP {code}"
        except requests.RequestException as exc:
            note = f"fetch failed: {exc}"
        if feed_xml is None:
            if archived is None:
                return [], ProviderStatus(name, "error", note)
            feed_xml, status, note = archived, "partial", f"{note}; used archived copy"
    try:
        events = parse_volcano_art_center(feed_xml, today)
    except Exception as exc:
        return [], ProviderStatus(name, "error", f"parse failed: {exc}")
    print(f"  Volcano Art Ctr  {status:<8} events={len(events)}", file=sys.stderr)
    return events, ProviderStatus(
        name, status, note or f"{len(events)} upcoming events")


# --------------------------------------------------------------------------- #
# Merge + dedupe
# --------------------------------------------------------------------------- #

_VENUE_TAIL = re.compile(
    r"\bat (the )?capitol modern.*$|\bat the hawai.*$|\bat capitol modern.*$",
    re.IGNORECASE)


def _norm_title(t: str) -> str:
    t = t.lower()
    t = _VENUE_TAIL.sub("", t)
    t = re.sub(r"[\u2018\u2019\u201c\u201d\"']", "", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _same_event(a: Event, b: Event) -> bool:
    # Only ever merge records at the same granularity. A recurring/dateless
    # listing (e.g. SFCA "First Friday") must NOT be collapsed into a single
    # dated instance (e.g. Capitol Modern's Sept 4 First Friday) -- that would
    # lose the recurrence and misplace the event.
    if bool(a.date_start) != bool(b.date_start):
        return False
    # Library records are distinct TEC event instances (another session, or
    # another branch), never duplicates of each other.
    if a.provider == b.provider == LIB_PROVIDER:
        return False
    # Two concrete instances: same day required.
    if a.date_start and b.date_start:
        if a.date_start != b.date_start:
            return False
        na, nb = _norm_title(a.title), _norm_title(b.title)
        sim = SequenceMatcher(None, na, nb).ratio()
        shared = na and nb and (na in nb or nb in na)
        return sim > 0.55 or shared          # same day: lenient title
    # Two dateless listings: require a near-exact title match.
    return SequenceMatcher(None, _norm_title(a.title),
                           _norm_title(b.title)).ratio() > 0.87


def _merge_cluster(cluster: list[Event]) -> Event:
    # Primary = the most precise/informative record: a timed event beats an
    # all-day one; otherwise the longest description wins.
    def score(e: Event) -> tuple:
        return (1 if e.dt_start_utc else 0,
                1 if e.date_start else 0,
                len(e.description or ""))
    primary = max(cluster, key=score)

    sources: list[Source] = []
    links: list[dict] = []
    seen_src, seen_link = set(), set()
    providers = []
    for e in cluster:
        if e.provider not in providers:
            providers.append(e.provider)
        for s in e.sources:
            k = (s.name, s.url)
            if k not in seen_src:
                seen_src.add(k)
                sources.append(s)
        for l in e.links:
            if l["href"] not in seen_link:
                seen_link.add(l["href"])
                links.append(l)

    merged = Event(
        title=primary.title,
        description=primary.description,
        category=primary.category,
        provider=primary.provider,
        sources=sources,
        venue=next((e.venue for e in cluster if e.venue), primary.venue),
        date_start=primary.date_start,
        date_end=primary.date_end,
        date_display=primary.date_display,
        time_display=next((e.time_display for e in cluster if e.time_display),
                          None),
        dt_start_utc=primary.dt_start_utc,
        dt_end_utc=primary.dt_end_utc,
        recurring=any(e.recurring for e in cluster),
        links=links,
        fallback_month=next((e.fallback_month for e in cluster
                             if e.fallback_month), primary.fallback_month),
        uid_seed=primary.uid_seed,
    )
    return merged


def merge_events(events: list[Event]) -> tuple[list[Event], int]:
    """Cluster duplicate events across sources; return (merged, num_merged)."""
    clusters: list[list[Event]] = []
    for ev in events:
        for cl in clusters:
            if _same_event(ev, cl[0]):
                cl.append(ev)
                break
        else:
            clusters.append([ev])
    merged = [_merge_cluster(cl) for cl in clusters]
    num_merged = sum(1 for cl in clusters if len(cl) > 1)
    return merged, num_merged


def group_by_month(events: list[Event], current_month: str | None = None
                   ) -> tuple[list[tuple[str, str, list[Event]]], list[Event]]:
    """Return ([(month_key, label, sorted_events)...], undated_events)."""
    buckets: dict[str, list[Event]] = {}
    undated: list[Event] = []
    for e in events:
        key = e.month_key(current_month)
        if key:
            buckets.setdefault(key, []).append(e)
        else:
            undated.append(e)
    out = []
    for key in sorted(buckets):
        label = dt.datetime.strptime(key + "-01", "%Y-%m-%d").strftime("%B %Y")
        evs = sorted(buckets[key], key=lambda e: e.sort_key())
        out.append((key, label, evs))
    undated.sort(key=lambda e: e.title.lower())
    return out, undated


# --------------------------------------------------------------------------- #
# Output: events.json
# --------------------------------------------------------------------------- #

def write_events_json(months, undated, providers, generated) -> None:
    def dump(e: Event) -> dict:
        d = asdict(e)
        d["sources"] = [asdict(s) for s in e.sources]
        return d
    payload = {
        "generated": generated,
        "sources": [{"name": p.name, "status": p.status, "detail": p.detail}
                    for p in providers],
        "attribution": (
            "Event information aggregated from the Hawai\u02bbi State Foundation "
            "on Culture and the Arts (SFCA), Capitol Modern: the Hawai\u02bbi "
            "State Art Museum, and Volcano Art Center. Unofficial mirror; confirm details at the source."
        ),
        "months": [
            {"month": key, "label": label, "events": [dump(e) for e in evs]}
            for key, label, evs in months
        ],
        "undated": [dump(e) for e in undated],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    (DATA_DIR / "events.json").write_text(text, encoding="utf-8")
    (SITE_DIR / "events.json").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Output: calendar.ics
# --------------------------------------------------------------------------- #

def _ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line: str) -> str:
    out, b = [], line.encode("utf-8")
    while len(b) > 73:
        cut = 73
        while (b[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def write_ics(all_events: list[Event], generated: str) -> int:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//hawaii-arts-calendar//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", "X-WR-CALNAME:Hawai\u02bbi Arts & Culture Calendar",
        "X-WR-TIMEZONE:Pacific/Honolulu",
        # Hawai'i observes no daylight saving, so a single fixed -10:00 offset.
        "BEGIN:VTIMEZONE", "TZID:Pacific/Honolulu",
        "BEGIN:STANDARD", "DTSTART:19700101T000000",
        "TZOFFSETFROM:-1000", "TZOFFSETTO:-1000", "TZNAME:HST",
        "END:STANDARD", "END:VTIMEZONE",
    ]
    def base_desc(ev: Event, prefix: str = "") -> str:
        desc = prefix + (ev.description or "")
        for l in ev.links:
            desc += f"  {l['href']}"
        return desc + "  Sources: " + "; ".join(
            f"{s.name} {s.url}" for s in ev.sources)

    def all_day_block(ev: Event, uid: str, summary: str, day: dt.date,
                      desc: str) -> list[str]:
        return [
            "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(day + dt.timedelta(days=1)).strftime('%Y%m%d')}",
            _fold(f"SUMMARY:{_ics_escape(summary)}"),
            _fold(f"DESCRIPTION:{_ics_escape(desc)}"),
            _fold(f"LOCATION:{_ics_escape(ev.venue or '')}"),
            _fold(f"URL:{ev.sources[0].url if ev.sources else ''}"),
            f"CATEGORIES:{_ics_escape(ev.category)}",
            "END:VEVENT",
        ]

    count = 0
    for ev in all_events:
        if not ev.date_start:
            continue
        uid = ev.uid()

        if ev.dt_start_utc:  # timed event (Capitol Modern) -> local HST + TZID
            su = dt.datetime.fromisoformat(
                ev.dt_start_utc.replace("Z", "+00:00")).astimezone(HST)
            block = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                     f"DTSTART;TZID=Pacific/Honolulu:{su.strftime('%Y%m%dT%H%M%S')}"]
            if ev.dt_end_utc:
                eu = dt.datetime.fromisoformat(
                    ev.dt_end_utc.replace("Z", "+00:00")).astimezone(HST)
                block.append(
                    f"DTEND;TZID=Pacific/Honolulu:{eu.strftime('%Y%m%dT%H%M%S')}")
            block += [
                _fold(f"SUMMARY:{_ics_escape(ev.title)}"),
                _fold(f"DESCRIPTION:{_ics_escape(base_desc(ev))}"),
                _fold(f"LOCATION:{_ics_escape(ev.venue or '')}"),
                _fold(f"URL:{ev.sources[0].url if ev.sources else ''}"),
                f"CATEGORIES:{_ics_escape(ev.category)}",
                "END:VEVENT",
            ]
            lines += block
            count += 1
            continue

        # All-day (SFCA) event.
        start = dt.date.fromisoformat(ev.date_start)
        end = dt.date.fromisoformat(ev.date_end) if ev.date_end else start
        span = (end - start).days + 1
        run = ev.date_display or f"{start.isoformat()} – {end.isoformat()}"

        if span >= LONG_RUN_DAYS and end > start:
            # A long-running exhibit: bookend the run with two one-day markers
            # instead of a multi-week bar across the whole calendar.
            note = f"Runs {run}. "
            lines += all_day_block(
                ev, ev.uid("-opens"), f"{ev.title} — opens", start,
                base_desc(ev, note))
            lines += all_day_block(
                ev, ev.uid("-closes"), f"{ev.title} — closes (last day)", end,
                base_desc(ev, note))
            count += 2
        else:
            # Single-day or short multi-day event: one entry (a short bar is
            # fine and informative).
            block = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                     f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
                     f"DTEND;VALUE=DATE:{(end + dt.timedelta(days=1)).strftime('%Y%m%d')}",
                     _fold(f"SUMMARY:{_ics_escape(ev.title)}"),
                     _fold(f"DESCRIPTION:{_ics_escape(base_desc(ev))}"),
                     _fold(f"LOCATION:{_ics_escape(ev.venue or '')}"),
                     _fold(f"URL:{ev.sources[0].url if ev.sources else ''}"),
                     f"CATEGORIES:{_ics_escape(ev.category)}",
                     "END:VEVENT"]
            lines += block
            count += 1
    lines.append("END:VCALENDAR")
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "calendar.ics").write_text("\r\n".join(lines) + "\r\n",
                                           encoding="utf-8")
    return count


# --------------------------------------------------------------------------- #
# Output: HTML site
# --------------------------------------------------------------------------- #

def _e(text: str) -> str:
    return html.escape(text or "", quote=True)


_SRC_CLASS = {"SFCA": "sfca", "SFCA Events": "sfca",
              "Capitol Modern": "capmod", LIB_PROVIDER: "lib",
              "Volcano Art Center": "vac"}


def _fmt_when(ev: Event) -> str:
    if ev.date_display and ev.time_display:
        return f"{_e(ev.date_display)} \u00b7 {_e(ev.time_display)}"
    if ev.date_display:
        return _e(ev.date_display)
    if ev.recurring:
        return "Recurring \u2014 see details"
    return "See details"


def _render_event(ev: Event) -> str:
    badges = "".join(
        f'<span class="badge {_SRC_CLASS.get(s.name, "")}">{_e(s.name)}</span>'
        for s in ev.sources)
    if len(ev.sources) > 1:
        badges = '<span class="badge both">merged</span>' + badges
    venue = f'<div class="venue">{_e(ev.venue)}</div>' if ev.venue else ""
    links = ""
    if ev.links:
        items = " \u00b7 ".join(
            f'<a href="{_e(l["href"])}" rel="noopener nofollow">{_e(l["text"])}</a>'
            for l in ev.links)
        links = f'<div class="links">{items}</div>'
    src = " \u00b7 ".join(
        f'<a href="{_e(s.url)}" rel="noopener">{_e(s.name)}'
        + (f' ({_e(s.label)})' if s.label else "") + "</a>"
        for s in ev.sources)
    return f"""      <article class="event">
        <div class="when">{_fmt_when(ev)}</div>
        <h3>{_e(ev.title)} {badges}</h3>
        {venue}
        <p>{_e(ev.description)}</p>
        {links}
        <div class="src">Source: {src}</div>
      </article>"""


def _page_shell(title: str, body: str, generated: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1c1a17; --muted:#6b6560; --card:#fff; --accent:#0b6b5f; --line:#e7e2d9; --sfca:#0b6b5f; --capmod:#8a4b96; --lib:#2f5d9e; --vac:#a5402d; --both:#b4641c; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161513; --fg:#ece8e1; --muted:#a49e94; --card:#201e1b; --accent:#5fd3c0; --line:#33302b; --sfca:#5fd3c0; --capmod:#d6a3e0; --lib:#8fb4ee; --vac:#f0957f; --both:#e3a869; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
header, main, footer {{ max-width:820px; margin:0 auto; padding:0 20px; }}
header {{ padding-top:36px; padding-bottom:8px; }}
h1 {{ font-size:1.8rem; margin:0 0 6px; }}
.tag {{ color:var(--accent); font-weight:600; letter-spacing:.02em; text-transform:uppercase; font-size:.72rem; }}
.lede {{ color:var(--muted); margin:.4rem 0 1rem; }}
nav.months {{ display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 6px; }}
nav.months a {{ display:inline-block; padding:6px 12px; border:1px solid var(--line);
  border-radius:999px; text-decoration:none; color:var(--fg); background:var(--card); font-size:.9rem; }}
nav.months a[aria-current] {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
.actions {{ display:flex; gap:14px; flex-wrap:wrap; margin:8px 0 4px; font-size:.9rem; }}
.actions a {{ color:var(--accent); }}
.legend {{ display:flex; gap:14px; flex-wrap:wrap; font-size:.8rem; color:var(--muted); margin:6px 0 2px; }}
h2.month {{ margin:30px 0 4px; padding-bottom:6px; border-bottom:2px solid var(--line); font-size:1.3rem; }}
.event {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:14px 16px; margin:10px 0; }}
.event h3 {{ margin:.15rem 0 .35rem; font-size:1.08rem; }}
.event p {{ margin:.2rem 0; }}
.when {{ font-weight:600; color:var(--accent); font-size:.9rem; }}
.venue {{ font-size:.85rem; color:var(--muted); margin:.1rem 0 .3rem; }}
.badge {{ font-size:.66rem; font-weight:700; text-transform:uppercase; letter-spacing:.03em;
  border-radius:6px; padding:1px 6px; vertical-align:middle; white-space:nowrap; border:1px solid currentColor; }}
.badge.sfca {{ color:var(--sfca); }}
.badge.capmod {{ color:var(--capmod); }}
.badge.lib {{ color:var(--lib); }}

.badge.vac {{ color:var(--vac); }}
.badge.both {{ color:var(--both); }}
.links {{ font-size:.9rem; margin-top:.35rem; }}
.src {{ font-size:.78rem; margin-top:.4rem; color:var(--muted); }}
.src a {{ color:var(--muted); }}
.missing {{ color:var(--muted); font-style:italic; padding:8px 0; }}
footer {{ color:var(--muted); font-size:.82rem; margin:40px auto; border-top:1px solid var(--line); padding-top:16px; }}
a {{ color:var(--accent); }}
</style>
</head>
<body>
{body}
<footer>
  <p>Generated {_e(generated)}. Aggregated from the
  <a href="{SFCA_BASE}/" rel="noopener">Hawai\u02bbi State Foundation on Culture and the Arts</a>,
  <a href="{CAPMOD_BASE}/events" rel="noopener">Capitol Modern: the Hawai\u02bbi State Art Museum</a>,
  and <a href="{VAC_BASE}/" rel="noopener">Volcano Art Center</a>.
  This is an unofficial, automatically updated aggregator; always confirm details on the
  source page. Inclusion is not an endorsement.</p>
</footer>
</body>
</html>
"""


def _month_nav(months, sfca_rows, current=None) -> str:
    # Include SFCA months that are known-but-empty so users see they exist.
    known = {k for k, _, _ in months}
    items = []
    for key, label, _ in months:
        cur = ' aria-current="page"' if key == current else ""
        items.append(f'<a href="#m-{key}"{cur}>{_e(label)}</a>')
    return '<nav class="months">' + "".join(items) + "</nav>"


def _render_month(key, label, evs, sfca_rows) -> str:
    parts = [f'<h2 class="month" id="m-{key}">{_e(label)}</h2>']
    row = next((r for r in sfca_rows if r["month_key"] == key), None)
    if row and row["status"] != "ok":
        parts.append(f'<p class="missing">SFCA calendar for {_e(label)} '
                     f'{_e(row["note"] or "not available")} '
                     f'(<a href="{_e(row["url"])}" rel="noopener">source</a>).</p>')
    parts.extend(_render_event(e) for e in evs)
    return "\n".join(parts)


def render_site(months, undated, sfca_rows, providers, num_merged,
                generated) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    total = sum(len(evs) for _, _, evs in months) + len(undated)

    legend = ('<div class="legend">'
              '<span><span class="badge sfca">SFCA</span> state calendar</span>'
              '<span><span class="badge capmod">Capitol Modern</span> museum feed</span>'
              '<span><span class="badge lib">Libraries</span> public library arts programs</span>'

              '<span><span class="badge vac">Volcano Art Center</span> RSS feed</span>'
              '<span><span class="badge both">merged</span> same event, multiple sources</span>'
              '</div>')
    actions = ('<div class="actions">'
               '<a href="calendar.ics">\U0001f4c5 Subscribe (iCal)</a>'
               '<a href="events.json">events.json</a></div>')
    src_line = " \u00b7 ".join(f"{p.name}: {p.status}" for p in providers)

    header = f"""<header>
  <div class="tag">Hawai\u02bbi \u00b7 unofficial aggregator</div>
  <h1>Hawai\u02bbi Arts &amp; Culture Calendar</h1>
  <p class="lede">{total} events from {len(providers)} sources
  ({_e(src_line)}), merged and de-duplicated
  ({num_merged} overlap{'s' if num_merged != 1 else ''} combined). Updated automatically.</p>
  {legend}
  {_month_nav(months, sfca_rows)}
  {actions}
</header>"""

    body_parts = [header, "<main>"]
    for key, label, evs in months:
        body_parts.append(_render_month(key, label, evs, sfca_rows))
    if undated:
        body_parts.append('<h2 class="month" id="m-recurring">Recurring &amp; ongoing</h2>')
        body_parts.extend(_render_event(e) for e in undated)
    body_parts.append("</main>")
    (SITE_DIR / "index.html").write_text(
        _page_shell("Hawai\u02bbi Arts & Culture Calendar",
                    "\n".join(body_parts), generated), encoding="utf-8")

    # Per-month standalone pages.
    for key, label, evs in months:
        mnav = ('<nav class="months"><a href="index.html">\u2190 All</a>'
                + "".join(f'<a href="{k}.html"'
                          + (' aria-current="page"' if k == key else "")
                          + f'>{_e(lbl)}</a>' for k, lbl, _ in months)
                + "</nav>")
        mbody = (f'<header><div class="tag">Hawai\u02bbi \u00b7 unofficial aggregator</div>'
                 f'<h1>{_e(label)}</h1>{legend}{mnav}{actions}</header>\n<main>\n'
                 + _render_month(key, label, evs, sfca_rows) + "\n</main>")
        (SITE_DIR / f"{key}.html").write_text(
            _page_shell(f"{label} \u2014 Hawai\u02bbi Arts Calendar", mbody,
                        generated), encoding="utf-8")

    # Remove stale per-month pages for months that have rolled off the site
    # (see sfca_month_window) so an old URL doesn't linger with frozen content.
    current_keys = {key for key, _, _ in months}
    for f in SITE_DIR.glob("????-??.html"):
        if f.stem not in current_keys:
            f.unlink()

    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true",
                    help="parse archived data/raw/* instead of fetching")
    ap.add_argument("--today", help="override 'today' as YYYY-MM-DD (testing)")
    args = ap.parse_args()

    today = (dt.date.fromisoformat(args.today) if args.today
             else dt.date.today())
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"Collecting (today={today}, offline={args.offline})", file=sys.stderr)
    sfca_events, sfca_rows, sfca_ps = collect_sfca(today, args.offline)
    capmod_events, capmod_ps = collect_capitol_modern(args.offline)
    tec_events, tec_ps = collect_sfca_tec(args.offline)
    lib_events, lib_ps = collect_librarieshawaii(today, args.offline)
    vac_events, vac_ps = collect_volcano_art_center(today, args.offline)
    providers = [sfca_ps, capmod_ps, tec_ps, lib_ps, vac_ps]

    all_events = sfca_events + capmod_events + tec_events + lib_events + vac_events
    merged, num_merged = merge_events(all_events)
    current_month = f"{today.year:04d}-{today.month:02d}"
    months, undated = group_by_month(merged, current_month)

    write_events_json(months, undated, providers, generated)
    n_ics = write_ics(merged, generated)
    render_site(months, undated, sfca_rows, providers, num_merged, generated)

    total = len(merged)
    print(f"Done: {len(all_events)} raw -> {total} merged events "
          f"({num_merged} overlaps combined), {len(months)} month(s), "
          f"{n_ics} ICS entries -> {SITE_DIR}", file=sys.stderr)
    if all(p.status == "error" for p in providers):
        print("WARNING: all sources failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
