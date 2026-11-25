"""Personio (personio.de): the applicant system many German companies use. Every company has its own public job
feed at https://<company>.jobs.personio.de/xml. No key, and no search: the feed lists all open jobs of one company.

    fetch(company)             every open job of one company, one plain dict per job
    to_posting(raw, company)   one job in the posting shape the daily run uses (the same keys as the Arbeitnow postings,
                               plus employment_type and seniority)

What the raw data looks like (checked on the appliedAI feed, 9 jobs, 22 Nov 2026):
- XML: <workzag-jobs> holds one <position> per job
- the feed has no link to the job: the link is built from the id (https://<company>.jobs.personio.de/job/<id>)
- id is a number that is unique inside one company only, so source_id is "<company>:<id>"
- subcompany is the legal entity ("appliedAI Initiative GmbH"); it is the better company name when it is there
- office is the main place, additionalOffices/office the other places. Some offices carry a postal code in
  brackets: "München (81671)". There are no coordinates, so lat and lon stay None until a city is geocoded
- jobDescriptions/jobDescription are the text sections ("About us", "Your mission", "Your profile", ...),
  each with a name and a value that is HTML inside CDATA (the HTML may still hold "&amp;" and similar)
- employmentType (permanent, intern, ...) and seniority (student, entry-level, experienced, ...) are fixed words;
  a working student job is seniority "student" with employmentType "intern"
- schedule is full-time or part-time; createdAt is ISO 8601 with an offset (UTC in the sample)
- a company without a Personio page answers 404
"""

import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

from job_fit.sources.arbeitnow import clean_text

SOURCE = "personio"
FEED_URLS = ("https://{company}.jobs.personio.de/xml?language=en",     # older accounts
             "https://{company}.jobs.personio.com/xml?language=en")    # newer accounts: tried when .de answers 404
JOB_URL = "https://{company}.jobs.personio.de/job/{id}"
BERLIN = ZoneInfo("Europe/Berlin")
RETRIES = 3             # like the Arbeitnow source: 3 tries, 5 s apart

REMOTE_WORDS = ["remote", "homeoffice", "home office", "home-office"]
# In the descriptions "remote" is also part of hybrid jobs ("work remotely a few days each week"), so a bare
# "remote" in the text proves nothing. Only phrases that describe the whole work model count.
REMOTE_PHRASES = ["fully remote", "full remote", "full-remote", "100% remote", "remote-first", "remote first",
                  "remote only", "remote-only", "completely remote", "vollständig remote", "komplett remote",
                  "voll remote", "100% homeoffice", "100 % homeoffice"]
POSTAL_CODE = re.compile(r"\s*\(\s*\d{4,5}\s*\)|^\s*\d{5}\s+")      # "München (81671)" and "81671 München"


def _get_feed(company: str) -> bytes:
    """The feed from the first address that has it (.de, then .com), as raw bytes.

    Bytes, not text: the feed is UTF-8, but Personio's answer does not say so in its Content-Type header, and for
    text without a stated charset requests guesses ISO-8859-1. Then "München" became "MÃ¼nchen" (found 25 Nov 2026:
    5 appliedAI postings, one Munich job wrongly skipped because Geoapify could not place "MÃ¼nchen").
    The XML parser reads the encoding from the feed's own first line (<?xml ... encoding="UTF-8"?>) when it gets bytes."""
    for url in FEED_URLS:
        try:
            return _get_one(url.format(company=company), company)
        except LookupError:
            continue
    raise LookupError(f"Personio has no job feed for company '{company}' (404 on .de and .com)")


def _get_one(url: str, company: str) -> bytes:
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 404:
                raise LookupError(f"Personio has no job feed for company '{company}' (404 at {url})")
            response.raise_for_status()
            return response.content                  # bytes: see _get_feed
        except LookupError:
            raise                                    # a missing company will not appear on the next try
        except requests.RequestException:
            if attempt == RETRIES:
                raise
            time.sleep(5)


def _text(node: ET.Element, tag: str) -> str:
    """The stripped text of a child tag, or "" when the tag is missing or empty."""
    child = node.find(tag)
    return (child.text or "").strip() if child is not None else ""


def parse_position(position: ET.Element) -> dict:
    """One <position> as a plain dict. Nothing is cleaned here, the values stay as the feed has them."""
    sections = [(_text(section, "name"), _text(section, "value"))
                for section in position.findall("jobDescriptions/jobDescription")]
    return {"id": _text(position, "id"), "subcompany": _text(position, "subcompany"),
            "office": _text(position, "office"),
            "additional_offices": [(office.text or "").strip() for office in position.findall("additionalOffices/office")
                                   if (office.text or "").strip()],
            "department": _text(position, "department"), "recruiting_category": _text(position, "recruitingCategory"),
            "title": _text(position, "name"), "descriptions": sections,
            "employment_type": _text(position, "employmentType"), "seniority": _text(position, "seniority"),
            "schedule": _text(position, "schedule"), "created_at": _text(position, "createdAt")}


def fetch(company: str) -> list[dict]:
    """Every open job of one company (company is the first part of the address: "appliedai" for
    appliedai.jobs.personio.de). Raises a LookupError that names the company when the feed does not exist."""
    try:
        root = ET.fromstring(_get_feed(company))
    except ET.ParseError as error:
        raise ValueError(f"The Personio feed of company '{company}' is not valid XML: {error}") from error
    return [parse_position(position) for position in root.findall("position")]


def published_on(raw: dict) -> date | None:
    """createdAt (ISO 8601 with an offset) as a date in Berlin time, the same day the Telegram message shows."""
    if not raw.get("created_at"):
        return None
    created = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(BERLIN).date()


def parse_locations(raw: dict) -> list[dict]:
    """Main office first, then the additional offices. "München (81671)" becomes city "München" and keeps the
    text as written in raw. Offices that only say "Remote" or "Homeoffice" are not places and are left out.
    The same city twice is kept once. lat and lon stay None (no coordinates in this source)."""
    places = []
    for office in [raw.get("office") or ""] + list(raw.get("additional_offices") or []):
        office = html.unescape(office).strip()
        city = re.sub(r"\s+", " ", POSTAL_CODE.sub("", office)).strip(" ,-/")
        if city and not any(word in city.lower() for word in REMOTE_WORDS) \
                and city.lower() not in [place["city"].lower() for place in places]:
            places.append({"city": city, "lat": None, "lon": None, "raw": office})
    return places


def is_remote(raw: dict, description: str) -> bool:
    """Simple on purpose. True when an office or the title has a remote word ("Remote", "Homeoffice"), or the
    description names the whole work model as remote ("fully remote", "100% remote", "remote-first", ...).
    The plain word "remote" in the description is not enough: hybrid jobs say "work remotely a few days a week"."""
    short = " ".join([raw.get("office") or "", *(raw.get("additional_offices") or []), raw.get("title") or ""]).lower()
    return any(word in short for word in REMOTE_WORDS) or any(phrase in description.lower() for phrase in REMOTE_PHRASES)


def to_posting(raw: dict, company: str) -> dict:
    """One Personio job in the posting shape the daily run uses."""
    locations = parse_locations(raw)
    city = locations[0]["city"] if locations else None
    name = raw.get("subcompany") or company
    source_id = f"{company}:{raw['id']}"
    blocks = [f"{section}\n{clean_text(value)}".strip() for section, value in raw.get("descriptions") or []]
    description = "\n\n".join(block for block in blocks if block)
    return {"source": SOURCE, "source_id": source_id, "refnr": source_id,
            "title": html.unescape(raw.get("title") or "").strip(),
            "company": name, "firma": name, "city": city, "ort": city,
            "locations": locations, "location_text": "; ".join(place["raw"] for place in locations),
            "remote": is_remote(raw, description),
            "description": description, "published_on": published_on(raw),
            "url": JOB_URL.format(company=company, id=raw["id"]), "location_gap": None, "location_note": None,
            "employment_type": raw.get("employment_type") or "", "seniority": raw.get("seniority") or ""}
