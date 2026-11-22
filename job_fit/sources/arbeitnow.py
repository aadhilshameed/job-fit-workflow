"""Arbeitnow (arbeitnow.com): an open job board API. No key, and no search: it only hands out the newest jobs,
300 per page.

    fetch(days)        the jobs in Germany published in the last `days` days (page 1, page 2, ... until the dates run out)
    to_posting(job)    one job in the posting shape the daily run uses (the same keys as the Arbeitsagentur postings)

What the raw data looks like (checked on 300 jobs, 22 Nov 2026):
- created_at is a Unix timestamp (seconds, UTC); the order is newest first, but not strictly
- description is HTML, and 135 of 300 store the HTML a second time as text ("&lt;p&gt;" instead of "<p>")
- location is free text: "München", "Munich", "Berlin (Hybrid)", "Homeoffice", "London; Manchester", or empty
- there are no coordinates, so lat and lon stay None until a city is geocoded
- each page is built from blocks by country: 100 jobs in Germany first, then 50 each for the United Kingdom,
  France, Switzerland and the Netherlands. The board marks the country of every non-German job with a last line
  "Find (more English Speaking) Jobs in <country> on Arbeitnow"; German jobs have no such line.
  So that line is the Germany filter: no line means Germany (including remote and empty locations).
  The German block covers only about one day, so whether to read the next page is decided by the German jobs alone.
"""

import html
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

SOURCE = "arbeitnow"
API_URL = "https://www.arbeitnow.com/api/job-board-api"
BERLIN = ZoneInfo("Europe/Berlin")
MAX_PAGES = 10          # one page holds about one day of German jobs; 10 pages covers a missed weekend
PAUSE_SECONDS = 1       # between page calls
RETRIES = 3             # like the Arbeitsagentur source: 3 tries, 5 s apart

REMOTE_WORDS = ["remote", "homeoffice", "home office", "home-office", "werken op afstand", "télétravail"]
COUNTRY_LINE = re.compile(r"\s*Find (?:more )?(?:English Speaking )?Jobs in (.+?) on Arbeitnow\s*$")
MIN_TEXT_CHARS = 200    # shorter than this after cleaning means the posting is an image or a stub: treated as no text
PLACE_NOISE = re.compile(r"\((hybrid|remote|onsite|on-site)[^)]*\)|\b(office|hq|headquarters)\b", re.IGNORECASE)


def _get(url: str) -> dict:
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == RETRIES:
                raise
            time.sleep(5)


def published_on(job: dict) -> date:
    """created_at (Unix seconds, UTC) as a date in Berlin time, the same day the Telegram message shows."""
    return datetime.fromtimestamp(job["created_at"], timezone.utc).astimezone(BERLIN).date()


def country(job: dict) -> str:
    """"Germany", or the country named in the board's last line ("France", "United Kingdom", ...)."""
    match = COUNTRY_LINE.search(clean_text(job.get("description"), keep_country_line=True))
    return match.group(1) if match else "Germany"


def fetch(days: int = 1) -> list[dict]:
    """Every job in Germany published in the last `days` days (today counts as day 0, so days=1 is today and
    yesterday). Reads page after page and stops once a page's German jobs reach older dates."""
    cutoff = datetime.now(BERLIN).date() - timedelta(days=days)
    jobs, url = [], API_URL
    for _ in range(MAX_PAGES):
        data = _get(url)
        german = [job for job in data.get("data") or [] if country(job) == "Germany"]
        jobs += [job for job in german if published_on(job) >= cutoff]
        if not german or min(published_on(job) for job in german) < cutoff:
            break                                   # the German jobs on this page already reach older dates
        url = (data.get("links") or {}).get("next")
        if not url:
            break
        time.sleep(PAUSE_SECONDS)
    return jobs


def clean_text(raw: str, keep_country_line: bool = False) -> str:
    """HTML to plain text, whether the HTML is plain, stored once as text ("&lt;p&gt;") or twice.

    1. decode until nothing changes, so "&amp;lt;p&amp;gt;" and "&lt;p&gt;" both become "<p>"
    2. turn line-ending tags into line breaks and list items into "- ", then drop every other tag
       (a tag must start with a letter, so text such as "<5 years" survives)
    3. tidy the spaces and empty lines, and drop the board's own last line ("Find Jobs in France on Arbeitnow")
    """
    text = raw or ""
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = re.sub(r"<\s*br\s*/?>|</\s*(p|div|h[1-6]|ul|ol|tr)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*li[^<>]*>", "\n- ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[a-zA-Z][^<>]*>", " ", text)
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if keep_country_line else COUNTRY_LINE.sub("", text).strip()


def is_remote(job: dict) -> bool:
    """The remote flag, or a remote word in the location or the title (the flag alone misses most of them)."""
    words = f"{job.get('location') or ''} {job.get('title') or ''}".lower()
    return bool(job.get("remote")) or any(w in words for w in REMOTE_WORDS)


def parse_locations(location: str) -> list[dict]:
    """"Düsseldorf, Nordrhein-Westfalen, Deutschland" -> [{"city": "Düsseldorf", ...}].
    Several places are split on ";". Pure remote words ("Remote", "Homeoffice") are not places and are left out;
    "Berlin (Hybrid)" and "Berlin Office" become "Berlin". lat and lon stay None (no coordinates in this source)."""
    places = []
    for part in (location or "").split(";"):
        part = html.unescape(part)
        city = PLACE_NOISE.sub(" ", part).split(",")[0]
        city = re.split(r"\s+[-/]\s+", city)[0]                  # "Paris - Full Remote" -> "Paris"
        city = re.sub(r"[^\w\s'.-]", " ", city)                   # flags and other symbols ("Lyon 🇫🇷")
        city = re.sub(r"\s+", " ", city).strip(" -/")
        if city and not any(w in city.lower() for w in REMOTE_WORDS):
            places.append({"city": city, "lat": None, "lon": None, "raw": part.strip()})
    return places


def to_posting(job: dict) -> dict:
    """One Arbeitnow job in the posting shape the daily run uses."""
    locations = parse_locations(job.get("location"))
    city = locations[0]["city"] if locations else None
    company = job.get("company_name") or ""
    text = clean_text(job.get("description"))
    return {"source": SOURCE, "source_id": job["slug"], "refnr": job["slug"],
            "title": html.unescape(job.get("title") or "").strip(),
            "company": company, "firma": company, "city": city, "ort": city,
            "locations": locations, "location_text": job.get("location") or "", "remote": is_remote(job),
            "description": text if len(text) >= MIN_TEXT_CHARS else "", "published_on": published_on(job),
            "url": job.get("url"), "location_gap": None, "location_note": None}
