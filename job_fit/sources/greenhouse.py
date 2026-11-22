"""Greenhouse (greenhouse.io): the public Job Board API of one company, no key needed.

    fetch(board)             every open job of one company that is in Germany (the board token, e.g. "celonis")
    in_germany(raw)          True when a job is in Germany, or remote and open to Germany
    to_posting(raw, board)   one job in the posting shape the daily run uses (same keys as the Arbeitnow postings)

What the raw data looks like (checked on the Celonis board, 262 jobs, 22 Nov 2026):
- GET https://boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true answers {"jobs": [...], "meta": {"total": N}}.
  Without content=true the jobs have no description. The answer is big (4.5 MB for Celonis), so the timeout is 60 s
- the board lists the jobs of every country, so Celonis has 262 jobs and only a part of them is in Germany.
  fetch() applies the Germany filter, and in_germany() holds the rule
- id is a number (sent as text) that is unique inside one board, so source_id is "<board>:<id>"
- location.name is one free text place: "Munich, Germany", "Remote, Germany", "New York, US, New York"
- offices is a list of {name, location, ...}; name is the same kind of text. The office list and location.name
  often disagree (a job with location "Munich, Germany" can have the office "Madrid, Spain"), so both are read
- content is HTML stored once as text ("&lt;p&gt;" instead of "<p>"), so it is decoded before the tags are removed
- first_published and updated_at are ISO 8601 with an offset (-05:00 in the sample); first_published is the better
  date, because updated_at changes with every edit
- language is a short code ("en"); requisition_id is the company's own job number ("R15632")
- company_name is the display name of the company ("Celonis")
- there are no coordinates, so lat and lon stay None until a city is geocoded
- boards hosted on Greenhouse's EU servers (job-boards.eu.greenhouse.io) are not served by this API and are not supported
"""

import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from job_fit.sources.arbeitnow import clean_text

SOURCE = "greenhouse"
API_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
BERLIN = ZoneInfo("Europe/Berlin")
RETRIES = 3             # like the Arbeitnow source: 3 tries, 5 s apart (network errors only)
TIMEOUT_SECONDS = 60    # the answer with content=true is several MB

REMOTE_WORDS = ["remote", "homeoffice", "home office", "home-office"]
PLACE_NOISE = re.compile(r"\((hybrid|remote|onsite|on-site)[^)]*\)|\b(office|hq|headquarters)\b", re.IGNORECASE)
PLACE_SEPARATORS = re.compile(r"\s*;\s*|\s+\|\s+")

GERMANY_WORDS = ["germany", "deutschland"]
GERMAN_CITIES = [
    "Berlin", "München", "Munich", "Hamburg", "Frankfurt", "Köln", "Cologne", "Stuttgart", "Düsseldorf",
    "Nürnberg", "Nuremberg", "Leipzig", "Dresden", "Karlsruhe", "Heidelberg",
    "Hannover", "Hanover", "Bremen", "Essen", "Dortmund", "Bonn", "Mannheim", "Münster", "Augsburg", "Walldorf",
]
# remote roles count when the text names a region that includes Germany
REMOTE_REGION_WORDS = ["dach", "europe", "emea", "eu"]

GERMANY_PATTERN = re.compile(r"\b(" + "|".join(GERMANY_WORDS + [c.lower() for c in GERMAN_CITIES]) + r")\b")
REGION_PATTERN = re.compile(r"\b(" + "|".join(REMOTE_REGION_WORDS) + r")\b")


def _get(url: str) -> requests.Response:
    """One GET with 3 tries for network errors. A 404 is an answer, not an error, so it is returned as it is."""
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(url, params={"content": "true"}, timeout=TIMEOUT_SECONDS)
            if response.status_code != 404:
                response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == RETRIES:
                raise
            time.sleep(5)


def _has_remote_word(text: str) -> bool:
    return any(word in (text or "").lower() for word in REMOTE_WORDS)


def split_places(text: str) -> list[str]:
    """"Berlin, Germany; Munich, Germany" -> two places. Splits on ";" and on " | " only, because a comma is
    part of one place ("Munich, Germany")."""
    return [part.strip() for part in PLACE_SEPARATORS.split(text or "") if part.strip()]


def location_texts(raw: dict) -> list[str]:
    """Every place text of a job: location.name and the name and location of each office, split into single places,
    without duplicates, in this order."""
    texts = [(raw.get("location") or {}).get("name")]
    for office in raw.get("offices") or []:
        texts += [office.get("name"), office.get("location")]
    found = []
    for text in texts:
        for place in split_places(text):
            if place not in found:
                found.append(place)
    return found


def place_in_germany(place: str) -> bool:
    """One place text. "Munich, Germany" and "Berlin" count, "Madrid, Spain" does not.
    A remote text ("Remote, Germany") counts when it names Germany, DACH, Europe, EMEA or the EU.
    "Remote, Austria" and "Remote, US, Texas" do not count."""
    lowered = place.lower()
    if _has_remote_word(lowered):
        return bool(GERMANY_PATTERN.search(lowered) or REGION_PATTERN.search(lowered))
    return bool(GERMANY_PATTERN.search(lowered))


def in_germany(raw: dict) -> bool:
    """True when a job is in Germany or is a remote role open to Germany.

    The job's place texts are location.name and the name and location of every office. The job is kept when ANY of
    them passes place_in_germany(): it names Germany or Deutschland, or one of the German cities in GERMAN_CITIES
    (the ones seen in the data plus the major ones), or it says remote together with Germany, DACH, Europe, EMEA or EU.
    A text that is only remote with another country ("Remote, Austria") does not pass. A job whose location is
    Madrid but whose office is Munich is kept, because one of its texts names Germany."""
    return any(place_in_germany(place) for place in location_texts(raw))


def fetch(board: str) -> list[dict]:
    """Every open job of `board` (the board token in the Greenhouse job page address, e.g. "celonis") that passes
    in_germany(). Boards on Greenhouse's EU servers (job-boards.eu.greenhouse.io) are not served by this API and are
    not supported. A 404 means the board token is wrong (or the company left Greenhouse), so that raises an error
    naming the board."""
    response = _get(API_URL.format(board=board))
    if response.status_code == 404:
        raise LookupError(f"Greenhouse has no job board '{board}' (404)")
    jobs = response.json().get("jobs") or []
    return [raw for raw in jobs if in_germany(raw)]


def published_on(raw: dict) -> date | None:
    """first_published as a date in Berlin time (updated_at when there is no first_published, None when neither)."""
    stamp = raw.get("first_published") or raw.get("updated_at")
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(BERLIN).date()


def parse_locations(raw: dict) -> list[dict]:
    """Every non-remote place of a job as {"city", "lat", "lon", "raw"}: "Munich, Germany" -> city "Munich".
    Pure remote texts ("Remote, Germany") are not cities and are left out. Places in Germany come first, so the
    first city is a German one. lat and lon stay None."""
    found = []
    for place in location_texts(raw):
        if _has_remote_word(place):
            continue
        city = re.sub(r"\s+", " ", PLACE_NOISE.sub(" ", place).split(",")[0]).strip(" -/")
        if city:
            found.append({"city": city, "lat": None, "lon": None, "raw": place})
    return sorted(found, key=lambda entry: not place_in_germany(entry["raw"]))   # stable: German places first


def to_posting(raw: dict, board: str) -> dict:
    """One Greenhouse job in the posting shape the daily run uses, plus language and requisition_id."""
    places = location_texts(raw)
    locations = parse_locations(raw)
    city = locations[0]["city"] if locations else None
    name = raw.get("company_name") or board.replace("-", " ").replace("_", " ").title()
    source_id = f"{board}:{raw['id']}"
    return {"source": SOURCE, "source_id": source_id, "refnr": source_id,
            "title": clean_text(raw.get("title")),
            "company": name, "firma": name, "city": city, "ort": city,
            "locations": locations,
            "location_text": (raw.get("location") or {}).get("name") or "; ".join(places),
            "remote": any(_has_remote_word(place) and place_in_germany(place) for place in places),
            "description": clean_text(raw.get("content")), "published_on": published_on(raw),
            "url": raw.get("absolute_url"), "location_gap": None, "location_note": None,
            "language": raw.get("language"), "requisition_id": raw.get("requisition_id")}
