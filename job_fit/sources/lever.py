"""Lever (lever.co): the public postings API of one company, no key needed.

    fetch(company)             every open posting of one company (the company slug, e.g. "xentral")
    to_posting(raw, company)   one posting in the posting shape the daily run uses (same keys as the Arbeitnow postings)

What the raw data looks like (checked on the Xentral feed, 4 postings, 22 Nov 2026):
- Lever has two hosts, api.eu.lever.co for EU accounts and api.lever.co for US accounts. A company is on one of
  them only, and the other one answers 404 or an empty list. So fetch() tries the EU host first, then the US host.
- the answer is a JSON list, one entry per open posting
- createdAt is epoch milliseconds (UTC). It can be years old for a posting that is still open (the sample has a live
  posting created in April 2022). So for company feeds "new" must mean "first seen by us", not createdAt.
  published_on is kept for display only.
- categories.location is one free text place ("Germany (Remote)"), allLocations is the list of all places
- workplaceType is "remote", "hybrid", "onsite" or "unspecified"
- descriptionPlain is the intro as plain text, lists are [{text, content}] with content as HTML <li> items,
  and additionalPlain is the closing text
- there are no coordinates, so lat and lon stay None until a city is geocoded
"""

import re
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

from job_fit.sources.arbeitnow import clean_text

SOURCE = "lever"
HOSTS = ("https://api.eu.lever.co", "https://api.lever.co")
BERLIN = ZoneInfo("Europe/Berlin")
RETRIES = 3             # like the Arbeitnow source: 3 tries, 5 s apart (network errors only)

REMOTE_WORDS = ["remote", "homeoffice", "home office", "home-office"]
PLACE_NOISE = re.compile(r"\((hybrid|remote|onsite|on-site)[^)]*\)|\b(office|hq|headquarters)\b", re.IGNORECASE)


def _get(url: str) -> requests.Response:
    """One GET with 3 tries for network errors. A 404 is an answer, not an error, so it is returned as it is."""
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(url, params={"mode": "json"}, timeout=30)
            if response.status_code != 404:
                response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == RETRIES:
                raise
            time.sleep(5)


def fetch(company: str) -> list[dict]:
    """Every open posting of `company` (the slug in the Lever job page address, e.g. "xentral").
    The EU host is tried first, then the US host, and the first one that answers 200 with a JSON list wins.
    An empty list from both hosts is a valid answer ("no openings"). A 404 from both hosts means the slug is
    wrong (or the company left Lever), so that raises an error naming the company."""
    empty_answer = False
    for host in HOSTS:
        response = _get(f"{host}/v0/postings/{company}")
        if response.status_code != 200:
            continue
        data = response.json()
        if isinstance(data, list):
            if data:
                return data
            empty_answer = True             # the company may still be on the other host, so keep looking
    if empty_answer:
        return []
    raise ValueError(f"Lever has no postings page for company '{company}' (404 on both the EU and the US host)")


def published_on(raw: dict) -> date | None:
    """createdAt (epoch milliseconds, UTC) as a date in Berlin time. Display only: it is not the "new" signal."""
    created = raw.get("createdAt")
    if not created:
        return None
    return datetime.fromtimestamp(created / 1000, timezone.utc).astimezone(BERLIN).date()


def _has_remote_word(text: str) -> bool:
    return any(word in (text or "").lower() for word in REMOTE_WORDS)


def parse_locations(places: list[str]) -> list[dict]:
    """["Berlin, Germany", "Germany (Remote)"] -> [{"city": "Berlin", ...}].
    Entries with a remote word ("Germany (Remote)", "Remote") are not cities and are left out.
    "Munich (Hybrid)" and "Berlin Office" become "Munich" and "Berlin". lat and lon stay None."""
    found = []
    for place in places:
        if not place or _has_remote_word(place):
            continue
        city = PLACE_NOISE.sub(" ", place).split(",")[0]
        city = re.sub(r"\s+", " ", city).strip(" -/")
        if city:
            found.append({"city": city, "lat": None, "lon": None, "raw": place.strip()})
    return found


def build_description(raw: dict) -> str:
    """The intro, then each list as "<list text>" and "- item" lines, then the closing text."""
    parts = [(raw.get("descriptionPlain") or "").strip()]
    for block in raw.get("lists") or []:
        heading = clean_text(block.get("text"))
        items = clean_text(block.get("content"))
        parts.append(f"{heading}\n{items}".strip())
    parts.append((raw.get("additionalPlain") or "").strip())
    return "\n\n".join(part for part in parts if part)


def to_posting(raw: dict, company: str) -> dict:
    """One Lever posting in the posting shape the daily run uses, plus commitment and workplace_type for filtering."""
    categories = raw.get("categories") or {}
    places = categories.get("allLocations") or ([categories["location"]] if categories.get("location") else [])
    locations = parse_locations(places)
    city = locations[0]["city"] if locations else None
    workplace_type = raw.get("workplaceType") or "unspecified"
    location_text = categories.get("location") or "; ".join(places)
    name = company.replace("-", " ").replace("_", " ").title()
    source_id = f"{company}:{raw['id']}"
    return {"source": SOURCE, "source_id": source_id, "refnr": source_id,
            "title": (raw.get("text") or "").strip(),
            "company": name, "firma": name, "city": city, "ort": city,
            "locations": locations, "location_text": location_text,
            "remote": workplace_type == "remote" or any(_has_remote_word(p) for p in places),
            "description": build_description(raw), "published_on": published_on(raw),
            "url": raw.get("hostedUrl"), "location_gap": None, "location_note": None,
            "commitment": categories.get("commitment"), "workplace_type": workplace_type}
