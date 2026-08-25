#!/usr/bin/env python3
"""Fetch the SFCA Arts & Culture monthly calendars and build a static site.

The State Foundation on Culture and the Arts (SFCA) publishes a new page each
month at:

    https://sfca.hawaii.gov/arts-and-culture-calendar-<month>-<year>/

There is no month-to-month navigation in the page markup and the WordPress REST
API is locked down (Kadence Security -> 401), so discovery works by *guessing*
the slug for each month in a window and fetching the plain HTML. Months that do
not exist yet return 404 and are skipped gracefully.

Pipeline:
    discover months -> fetch + archive raw HTML -> parse events ->
    write data/events.json -> render site/ (index, per-month, ics, json)

Only facts (title, date, venue, link) are extracted and re-presented with
attribution and a link back to the source page. SFCA content is not
automatically public domain, so we do not mirror their HTML wholesale.

Runs on stdlib + requests + beautifulsoup4. Designed to run in GitHub Actions
(unrestricted network) on a cron, but is fully runnable locally too.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BASE = "https://sfca.hawaii.gov"
SLUG_FMT = "arts-and-culture-calendar-{month}-{year}"
START_YEAR, START_MONTH = 2026, 8  # August 2026 is the first published month.
MONTHS_AHEAD = 2  # discover through (current month + this many).

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTH_NUM = {name: i + 1 for i, name in enumerate(MONTH_NAMES)}

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
DATA_DIR = REPO_ROOT / "data"
SITE_DIR = REPO_ROOT / "site"

# Section headings we do NOT treat as event categories.
SKIP_HEADINGS = ("about the state foundation",)

USER_AGENT = (
    "sfca-calendar-bot/1.0 (+https://github.com/) "
    "static-site builder; contact via repo issues"
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Event:
    title: str
    description: str
    category: str
    month_slug: str
    month_label: str
    source_url: str
    links: list[dict] = field(default_factory=list)
    date_start: str | None = None  # ISO date
    date_end: str | None = None    # ISO date
    date_display: str | None = None
    recurring: bool = False


@dataclass
class MonthResult:
    slug: str
    label: str
    url: str
    year: int
    month: int
    status: str          # "ok" | "missing" | "error"
    events: list[Event] = field(default_factory=list)
    note: str = ""


# --------------------------------------------------------------------------- #
# Discovery + fetch
# --------------------------------------------------------------------------- #

def month_window(today: dt.date) -> list[tuple[int, int]]:
    """Every (year, month) from Aug 2026 through current month + MONTHS_AHEAD."""
    start = dt.date(START_YEAR, START_MONTH, 1)
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


def slug_for(year: int, month: int) -> str:
    return SLUG_FMT.format(month=MONTH_NAMES[month - 1], year=year)


def fetch(url: str, session: requests.Session) -> tuple[int, str]:
    resp = session.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    return resp.status_code, resp.text


# --------------------------------------------------------------------------- #
# Date extraction (best effort)
# --------------------------------------------------------------------------- #

_MONTH_ALT = "|".join(MONTH_NAMES)
_RANGE_RE = re.compile(
    rf"\b({_MONTH_ALT})\s+(\d{{1,2}})\s*[\u2013\u2014-]\s*"
    rf"(?:({_MONTH_ALT})\s+)?(\d{{1,2}})(?:,?\s*(\d{{4}}))?",
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(
    rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?:,?\s*(\d{{4}}))?",
    re.IGNORECASE,
)
_RECUR_RE = re.compile(
    r"\b(every|monthly|weekly|each (?:month|week|first|second|third)|"
    r"first friday|second friday|ongoing)\b",
    re.IGNORECASE,
)


def _mk_date(month: int, day: int, year: int) -> str | None:
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def extract_dates(text: str, default_year: int) -> dict:
    """Pull the first date (or date range) out of an event's prose."""
    recurring = bool(_RECUR_RE.search(text))
    result = {"date_start": None, "date_end": None,
              "date_display": None, "recurring": recurring}

    m = _RANGE_RE.search(text)
    if m:
        m1, d1, m2, d2, yr = m.groups()
        year = int(yr) if yr else default_year
        sm = MONTH_NUM[m1.lower()]
        em = MONTH_NUM[m2.lower()] if m2 else sm
        # If the end month is earlier than the start month, it rolls to next yr.
        ey = year if em >= sm else year + 1
        result["date_start"] = _mk_date(sm, int(d1), year)
        result["date_end"] = _mk_date(em, int(d2), ey)
        result["date_display"] = m.group(0).strip()
        return result

    m = _SINGLE_RE.search(text)
    if m:
        mon, day, yr = m.groups()
        year = int(yr) if yr else default_year
        iso = _mk_date(MONTH_NUM[mon.lower()], int(day), year)
        result["date_start"] = iso
        result["date_end"] = iso
        result["date_display"] = m.group(0).strip()
    return result


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def parse_events(page_html: str, slug: str, label: str, url: str,
                 default_year: int) -> list[Event]:
    soup = BeautifulSoup(page_html, "html.parser")
    content = (soup.select_one(".elementor-widget-theme-post-content")
               or soup.select_one(".entry-content")
               or soup.body
               or soup)
    for tag in content(["script", "style"]):
        tag.decompose()

    events: list[Event] = []
    current_category = "General"
    # Walk h2 headings and the ul that follows each, in document order.
    for node in content.find_all(["h2", "h3", "ul"]):
        if node.name in ("h2", "h3"):
            current_category = node.get_text(" ", strip=True)
            continue
        # node is a <ul>
        heading_l = current_category.lower()
        if any(skip in heading_l for skip in SKIP_HEADINGS):
            continue
        for li in node.find_all("li", recursive=False):
            ev = _parse_li(li, current_category, slug, label, url, default_year)
            if ev:
                events.append(ev)
    return events


def _parse_li(li, category: str, slug: str, label: str, url: str,
              default_year: int) -> Event | None:
    strong = li.find("strong") or li.find("b")
    if strong:
        title = strong.get_text(" ", strip=True).strip(" .")
    else:
        # Fallback: first sentence as the title.
        title = li.get_text(" ", strip=True).split(".")[0].strip()
    if not title:
        return None

    full = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
    # Description = full text minus the leading title.
    desc = full
    if full.lower().startswith(title.lower()):
        desc = full[len(title):].lstrip(" .,–—-").strip()

    links = []
    seen = set()
    for a in li.find_all("a"):
        href = a.get("href")
        if href and href not in seen:
            seen.add(href)
            links.append({"text": a.get_text(" ", strip=True) or href,
                          "href": href})

    dates = extract_dates(full, default_year)
    return Event(
        title=title,
        description=desc,
        category=category,
        month_slug=slug,
        month_label=label,
        source_url=url,
        links=links,
        **dates,
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def collect(today: dt.date, offline: bool = False) -> list[MonthResult]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    results: list[MonthResult] = []

    for year, month in month_window(today):
        slug = slug_for(year, month)
        url = f"{BASE}/{slug}/"
        label = f"{MONTH_NAMES[month - 1].capitalize()} {year}"
        raw_path = RAW_DIR / f"{slug}.html"
        mr = MonthResult(slug=slug, label=label, url=url,
                         year=year, month=month, status="missing")

        page_html = None
        if offline:
            if raw_path.exists():
                page_html = raw_path.read_text(encoding="utf-8")
                mr.status = "ok"
            else:
                mr.note = "offline: no archived HTML"
        else:
            try:
                code, text = fetch(url, session)
                if code == 200 and "post-content" in text:
                    page_html = text
                    raw_path.write_text(text, encoding="utf-8")
                    mr.status = "ok"
                elif code == 404:
                    mr.status = "missing"
                    mr.note = "not published yet (404)"
                else:
                    mr.status = "error"
                    mr.note = f"unexpected HTTP {code}"
            except requests.RequestException as exc:
                mr.status = "error"
                mr.note = f"fetch failed: {exc}"
                # Fall back to a previously archived copy if we have one.
                if raw_path.exists():
                    page_html = raw_path.read_text(encoding="utf-8")
                    mr.status = "ok"
                    mr.note += " (used archived copy)"

        if page_html:
            try:
                mr.events = parse_events(page_html, slug, label, url, year)
            except Exception as exc:  # parser must never crash the build
                mr.status = "error"
                mr.note = f"parse failed: {exc}"
        results.append(mr)
        print(f"  {label:<16} {mr.status:<8} events={len(mr.events):<3} {mr.note}",
              file=sys.stderr)
    return results


# --------------------------------------------------------------------------- #
# Output: events.json
# --------------------------------------------------------------------------- #

def write_events_json(results: list[MonthResult], generated: str) -> dict:
    payload = {
        "generated": generated,
        "source": BASE,
        "attribution": (
            "Event information from the Hawai\u02bbi State Foundation on "
            "Culture and the Arts (SFCA) monthly Arts & Culture Calendar."
        ),
        "months": [],
    }
    for mr in results:
        payload["months"].append({
            "slug": mr.slug,
            "label": mr.label,
            "url": mr.url,
            "status": mr.status,
            "note": mr.note,
            "events": [asdict(e) for e in mr.events],
        })
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "events.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    (SITE_DIR).mkdir(parents=True, exist_ok=True)
    (SITE_DIR / "events.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


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
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 sequence
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def write_ics(results: list[MonthResult], generated: str) -> int:
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//sfca-calendar//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", "X-WR-CALNAME:SFCA Arts & Culture Calendar",
    ]
    count = 0
    for mr in results:
        for i, ev in enumerate(ev for ev in mr.events if ev.date_start):
            start = dt.date.fromisoformat(ev.date_start)
            end = (dt.date.fromisoformat(ev.date_end)
                   if ev.date_end else start)
            dtend = end + dt.timedelta(days=1)  # DTEND is exclusive for all-day
            uid = f"{mr.slug}-{count}@sfca-calendar"
            desc = ev.description
            if ev.links:
                desc += "  " + " ".join(l["href"] for l in ev.links)
            desc += f"  Source: {ev.source_url}"
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{dtend.strftime('%Y%m%d')}",
                _fold(f"SUMMARY:{_ics_escape(ev.title)}"),
                _fold(f"DESCRIPTION:{_ics_escape(desc)}"),
                _fold(f"URL:{ev.source_url}"),
                f"CATEGORIES:{_ics_escape(ev.category)}",
                "END:VEVENT",
            ]
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
    return html.escape(text, quote=True)


def _fmt_date(ev: Event) -> str:
    if ev.date_display:
        return _e(ev.date_display)
    if ev.recurring:
        return "Recurring \u2014 see details"
    return "See details"


def _render_event(ev: Event) -> str:
    links = ""
    if ev.links:
        items = " \u00b7 ".join(
            f'<a href="{_e(l["href"])}" rel="noopener nofollow">{_e(l["text"])}</a>'
            for l in ev.links)
        links = f'<div class="links">{items}</div>'
    badge = ' <span class="recur">recurring</span>' if ev.recurring else ""
    return f"""      <article class="event">
        <div class="when">{_fmt_date(ev)}{badge}</div>
        <h3>{_e(ev.title)}</h3>
        <p>{_e(ev.description)}</p>
        {links}
        <div class="src"><a href="{_e(ev.source_url)}" rel="noopener">Source: SFCA {_e(ev.month_label)}</a></div>
      </article>"""


def _page_shell(title: str, body: str, generated: str, rel: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1c1a17; --muted:#6b6560; --card:#fff; --accent:#0b6b5f; --line:#e7e2d9; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161513; --fg:#ece8e1; --muted:#a49e94; --card:#201e1b; --accent:#5fd3c0; --line:#33302b; }}
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
h2.month {{ margin:30px 0 4px; padding-bottom:6px; border-bottom:2px solid var(--line); font-size:1.3rem; }}
h2.cat {{ margin:22px 0 6px; font-size:.8rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }}
.event {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:14px 16px; margin:10px 0; }}
.event h3 {{ margin:.15rem 0 .35rem; font-size:1.08rem; }}
.event p {{ margin:.2rem 0; }}
.when {{ font-weight:600; color:var(--accent); font-size:.9rem; }}
.recur {{ background:var(--line); color:var(--muted); border-radius:6px; padding:1px 6px;
  font-size:.72rem; font-weight:600; text-transform:uppercase; }}
.links {{ font-size:.9rem; margin-top:.35rem; }}
.src {{ font-size:.78rem; margin-top:.4rem; }}
.src a {{ color:var(--muted); }}
.missing {{ color:var(--muted); font-style:italic; padding:8px 0; }}
footer {{ color:var(--muted); font-size:.82rem; margin:40px auto; border-top:1px solid var(--line); padding-top:16px; }}
a {{ color:var(--accent); }}
</style>
</head>
<body>
{body}
<footer>
  <p>Generated {_e(generated)}. Event information is aggregated from the
  <a href="{BASE}/" rel="noopener">Hawai\u02bbi State Foundation on Culture and the Arts</a>
  monthly Arts &amp; Culture Calendar. This is an unofficial, automatically
  updated mirror; always confirm details on the source page. Inclusion is not an endorsement.</p>
</body>
</html>
"""


def _render_month_section(mr: MonthResult) -> str:
    if mr.status != "ok" or not mr.events:
        note = mr.note or "not available"
        return (f'<h2 class="month" id="{_e(mr.slug)}">{_e(mr.label)}</h2>\n'
                f'<p class="missing">Not published yet '
                f'(<a href="{_e(mr.url)}" rel="noopener">check source</a>). {_e(note)}</p>')
    parts = [f'<h2 class="month" id="{_e(mr.slug)}">{_e(mr.label)}</h2>']
    by_cat: dict[str, list[Event]] = {}
    for ev in mr.events:
        by_cat.setdefault(ev.category, []).append(ev)
    for cat, evs in by_cat.items():
        parts.append(f'<h2 class="cat">{_e(cat)}</h2>')
        parts.extend(_render_event(e) for e in evs)
    return "\n".join(parts)


def render_site(results: list[MonthResult], generated: str) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    ok_months = [m for m in results if m.status == "ok" and m.events]

    nav = '<nav class="months">' + "".join(
        f'<a href="#{_e(m.slug)}">{_e(m.label)}</a>' for m in results
    ) + "</nav>"
    actions = ('<div class="actions">'
               '<a href="calendar.ics">\U0001f4c5 Subscribe (iCal)</a>'
               '<a href="events.json">events.json</a></div>')

    total = sum(len(m.events) for m in ok_months)
    header = f"""<header>
  <div class="tag">Hawai\u02bbi \u00b7 unofficial mirror</div>
  <h1>SFCA Arts &amp; Culture Calendar</h1>
  <p class="lede">{total} events across {len(ok_months)} month(s), updated automatically from the
  State Foundation on Culture and the Arts.</p>
  {nav}
  {actions}
</header>"""

    body = header + "\n<main>\n" + "\n".join(
        _render_month_section(m) for m in results) + "\n</main>"
    (SITE_DIR / "index.html").write_text(
        _page_shell("SFCA Arts & Culture Calendar", body, generated),
        encoding="utf-8")

    # Per-month standalone pages.
    for mr in results:
        if mr.status != "ok" or not mr.events:
            continue
        mnav = ('<nav class="months"><a href="index.html">\u2190 All months</a>'
                + "".join(f'<a href="{_e(m.slug)}.html"'
                          + (' aria-current="page"' if m.slug == mr.slug else '')
                          + f'>{_e(m.label)}</a>'
                          for m in results if m.status == "ok" and m.events)
                + "</nav>")
        mbody = (f'<header><div class="tag">Hawai\u02bbi \u00b7 unofficial mirror</div>'
                 f'<h1>{_e(mr.label)}</h1>{mnav}{actions}</header>\n<main>\n'
                 + _render_month_section(mr) + "\n</main>")
        (SITE_DIR / f"{mr.slug}.html").write_text(
            _page_shell(f"{mr.label} \u2014 SFCA Calendar", mbody, generated),
            encoding="utf-8")

    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true",
                    help="parse archived data/raw/*.html instead of fetching")
    ap.add_argument("--today", help="override 'today' as YYYY-MM-DD (testing)")
    args = ap.parse_args()

    today = (dt.date.fromisoformat(args.today) if args.today
             else dt.date.today())
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"Discovering months (today={today}, offline={args.offline})",
          file=sys.stderr)
    results = collect(today, offline=args.offline)

    write_events_json(results, generated)
    n_ics = write_ics(results, generated)
    render_site(results, generated)

    ok = sum(1 for m in results if m.status == "ok" and m.events)
    total = sum(len(m.events) for m in results)
    print(f"Done: {ok} month(s) with events, {total} events, "
          f"{n_ics} dated ICS entries -> {SITE_DIR}", file=sys.stderr)
    if not any(m.status == "ok" for m in results):
        print("WARNING: no months fetched successfully", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
