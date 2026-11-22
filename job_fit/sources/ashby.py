"""Ashby (ashbyhq.com): the applicant system many start-ups and scale-ups use. Every company has a public job board
that Ashby also serves as JSON, through its official Public Job Posting API. No key, and no search: the answer lists
all open jobs of one company. Docs: https://developers.ashbyhq.com/docs/public-job-posting-api

    fetch(board)             every listed job of one company (the board name, e.g. "DeepL" or "lio")
    to_posting(raw, board)   one job in the posting shape the daily run uses (the same keys as the Arbeitnow postings,
                             plus employment_type and workplace_type)

What the raw data looks like (checked on the DeepL board, 10 jobs, and the lio board, 30 jobs, 22 Nov 2026):
- GET https://api.ashbyhq.com/posting-api/job-board/<board>?includeCompensation=true answers {"jobs": [...],
  "apiVersion": "1"}. The board name is the one in the job page address (jobs.ashbyhq.com/<board>). It is case
  insensitive in the address, but the samples keep the spelling of the company ("DeepL", "lio")
- id is a UUID. It is unique everywhere, but source_id is still "<board>:<id>" like the other company feeds
- location is one free text place: "London", "Munich Office", "New York Office". secondaryLocations is a list of
  objects, each with a "location" text (and an "address" with postalAddress). Only the DeepL sample has entries there
- address.postalAddress has addressLocality, addressRegion and addressCountry (sometimes also streetAddress and
  postalCode). The locality can end with a space ("Austin "). It is not used for the city, because secondary
  locations have the same shape and the text of location is what the other feeds use too
- employmentType is FullTime, PartTime, Intern, Contract or Temporary. The lio sample has 6 Intern jobs
- workplaceType is OnSite, Hybrid, Remote or null, and isRemote is true, false or null. Both are null on some jobs
- publishedAt is ISO 8601 with an offset (UTC in the samples)
- descriptionPlain is the whole description as plain text, descriptionHtml is the same as HTML
- jobUrl is the public page of the job, applyUrl goes straight to the application form
- compensation is asked for with includeCompensation=true. It is not used yet (the samples hold no salaries)
- there are no coordinates, so lat and lon stay None until a city is geocoded

Decisions:
- a job with isListed false is skipped in fetch(). Ashby uses the flag for jobs that are open but meant to stay off the
  public job board, so showing them in the daily run would send people to a page that does not list the job.
  (Both samples hold only listed jobs, so this is written from the documentation and has not been seen live.)
- a 404 means the board name is wrong (or the company left Ashby). That raises LookupError naming the board.
- published_on is kept for display only. For company feeds "new" must mean "first seen by us".
- places with a remote word ("Remote", "Remote - Germany") are not cities and are left out of locations,
  but they set remote to True.
"""

import re
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

from job_fit.sources.arbeitnow import clean_text

SOURCE = "ashby"
API_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"
BERLIN = ZoneInfo("Europe/Berlin")
RETRIES = 3             # like the Arbeitnow source: 3 tries, 5 s apart (network errors only)

REMOTE_WORDS = ["remote", "homeoffice", "home office", "home-office"]
PLACE_NOISE = re.compile(r"\((hybrid|remote|onsite|on-site)[^)]*\)|\b(office|hq|headquarters)\b", re.IGNORECASE)


def _get(board: str) -> requests.Response:
    """One GET with 3 tries for network errors. A 404 is an answer, not an error, so it is returned as it is."""
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(API_URL.format(board=board), params={"includeCompensation": "true"}, timeout=30)
            if response.status_code != 404:
                response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == RETRIES:
                raise
            time.sleep(5)


def fetch(board: str) -> list[dict]:
    """Every listed job of `board` (the name in the Ashby job page address, e.g. "DeepL").
    An empty list is a valid answer ("no openings"). A 404 raises LookupError naming the board."""
    response = _get(board)
    if response.status_code == 404:
        raise LookupError(f"Ashby has no job board named '{board}' (404)")
    jobs = response.json().get("jobs") or []
    return [job for job in jobs if job.get("isListed", True)     # unlisted jobs are not on the public board
            and in_germany(job)]


GERMANY = {"germany", "deutschland", "de", "deu"}
REMOTE_REGIONS = re.compile(r"germany|deutschland|dach|europe|emea|\beu\b", re.IGNORECASE)


def in_germany(raw: dict) -> bool:
    """True if any of the job's places is in Germany. Ashby gives each place's country as its own field
    (address.postalAddress.addressCountry, also on each secondary location), so that decides first:
    DeepL's London, Austin and Tokyo jobs are left out, its Munich job with a Berlin second office is kept.
    Without any country field, the place texts decide: "Germany" or "Deutschland" in them, or a remote place
    open to Germany, DACH or Europe. A job with neither (no country, no telling text) is kept, so a gap in
    the data never hides a job."""
    def country(item: dict) -> str:
        return (((item or {}).get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
    countries = [country(raw)] + [country(other) for other in raw.get("secondaryLocations") or []
                                  if isinstance(other, dict)]
    countries = [c.strip().lower() for c in countries if c and c.strip()]
    if any(c in GERMANY for c in countries):
        return True
    texts = _places(raw)
    if any(re.search(r"germany|deutschland", t, re.IGNORECASE) for t in texts):
        return True
    if any(_has_remote_word(t) and REMOTE_REGIONS.search(t) for t in texts):
        return True
    return not countries and not texts


def published_on(raw: dict) -> date | None:
    """publishedAt (ISO 8601 with offset) as a date in Berlin time. None if it is missing."""
    published = raw.get("publishedAt")
    if not published:
        return None
    moment = datetime.fromisoformat(published.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(BERLIN).date()


def _has_remote_word(text: str) -> bool:
    return any(word in (text or "").lower() for word in REMOTE_WORDS)


def _places(raw: dict) -> list[str]:
    """The main location, then the secondary ones, as plain texts."""
    places = [raw.get("location")]
    for other in raw.get("secondaryLocations") or []:
        places.append(other.get("location") if isinstance(other, dict) else other)
    return [place.strip() for place in places if place and place.strip()]


def parse_locations(places: list[str]) -> list[dict]:
    """["Munich Office", "Remote - Germany"] -> [{"city": "Munich", ...}].
    Entries with a remote word are not cities and are left out. "New York Office" becomes "New York".
    The same city is listed once. lat and lon stay None."""
    found, seen = [], set()
    for place in places:
        if _has_remote_word(place):
            continue
        city = PLACE_NOISE.sub(" ", place).split(",")[0]
        city = re.sub(r"\s+", " ", city).strip(" -/")
        if city and city.lower() not in seen:
            seen.add(city.lower())
            found.append({"city": city, "lat": None, "lon": None, "raw": place})
    return found


def company_name(board: str) -> str:
    """"lio" -> "Lio", "my-company" -> "My Company". A name with capitals ("DeepL") is kept as it is."""
    name = board.replace("-", " ").replace("_", " ").strip()
    return name.title() if name == name.lower() else name


def to_posting(raw: dict, board: str) -> dict:
    """One Ashby job in the posting shape the daily run uses, plus employment_type and workplace_type for filtering."""
    places = _places(raw)
    locations = parse_locations(places)
    city = locations[0]["city"] if locations else None
    workplace_type = raw.get("workplaceType")
    # isRemote is also true for hybrid jobs ("remote possible"), so only a remote workplace type or place counts
    remote = workplace_type == "Remote" or any(_has_remote_word(p) for p in places)
    description = (raw.get("descriptionPlain") or "").strip() or clean_text(raw.get("descriptionHtml"))
    name = company_name(board)
    source_id = f"{board}:{raw['id']}"
    return {"source": SOURCE, "source_id": source_id, "refnr": source_id,
            "title": (raw.get("title") or "").strip(),
            "company": name, "firma": name, "city": city, "ort": city,
            "locations": locations, "location_text": (raw.get("location") or "").strip() or "; ".join(places),
            "remote": remote, "description": description, "published_on": published_on(raw),
            "url": raw.get("jobUrl"), "location_gap": None, "location_note": None,
            "employment_type": raw.get("employmentType"), "workplace_type": workplace_type}
