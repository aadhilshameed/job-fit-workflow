"""SmartRecruiters (smartrecruiters.com): the job platform many large employers use for their career pages
(for example Bosch). The Posting API is public, needs no key and is read per company.

    fetch(company, country)               all postings of one company in one country (the list call, paged)
    fetch_description(company, id)        the full text of one posting (the detail call)
    to_posting(raw, company)              one posting in the shape the daily run uses

Two calls per job, on purpose:
- The list call (100 postings per page) has the title, place, coordinates, dates and levels, but NO description.
- The detail call (one per posting) has the description, split into sections.
So the description is not read for every posting. The daily run first filters on the title and the other list
fields, and only then calls fetch_description for the postings that passed. This keeps the number of calls low
(Bosch has about 800 postings in Germany: 8 list calls, then only a few detail calls).

Things to know about the data:
- location.region is a short code ("BW", "BY"). to_posting maps it to the federal state in the spelling of the
  Arbeitsagentur ("BADEN_WUERTTEMBERG"), because the location rule of the pipeline uses that spelling.
- latitude and longitude are strings and can be missing, so they become float or None.
- releasedDate is the date of the latest release of the posting. A posting that is released again gets a new date,
  so published_on can be newer than the first time the job was online.
- The detail response is read defensively (jobAd.sections.<name>.title and .text, as HTML). Missing sections
  are skipped.
"""

import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from job_fit.sources.arbeitnow import clean_text

SOURCE = "smartrecruiters"
LIST_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"
PUBLIC_URL = "https://jobs.smartrecruiters.com/{company}/{posting_id}"
BERLIN = ZoneInfo("Europe/Berlin")
PAGE_SIZE = 100
MAX_PAGES = 20          # 2000 postings; Bosch in Germany needs about 8
PAUSE_SECONDS = 1       # between page calls
RETRIES = 3             # like the other sources: 3 tries, 5 s apart
RETRY_WAIT_SECONDS = 5

SECTION_ORDER = ["companyDescription", "jobDescription", "qualifications", "additionalInformation"]

STATE_BY_REGION_CODE = {
    "BW": "BADEN_WUERTTEMBERG", "BY": "BAYERN", "BE": "BERLIN", "BB": "BRANDENBURG",
    "HB": "BREMEN", "HH": "HAMBURG", "HE": "HESSEN", "MV": "MECKLENBURG_VORPOMMERN",
    "NI": "NIEDERSACHSEN", "NW": "NORDRHEIN_WESTFALEN", "RP": "RHEINLAND_PFALZ", "SL": "SAARLAND",
    "SN": "SACHSEN", "ST": "SACHSEN_ANHALT", "SH": "SCHLESWIG_HOLSTEIN", "TH": "THUERINGEN",
}


def _get(url: str, params: dict | None = None) -> dict:
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == RETRIES:
                raise
            time.sleep(RETRY_WAIT_SECONDS)


def fetch(company: str, country: str = "de") -> list[dict]:
    """Every posting of `company` in `country`, as the raw list entries (no description).
    Reads page after page (100 each) until totalFound postings are read, with at most MAX_PAGES pages."""
    postings, offset = [], 0
    for page in range(MAX_PAGES):
        data = _get(LIST_URL.format(company=company),
                    {"country": country, "limit": PAGE_SIZE, "offset": offset})
        content = data.get("content") or []
        postings += content
        offset += PAGE_SIZE
        if not content or offset >= (data.get("totalFound") or 0):
            break
        time.sleep(PAUSE_SECONDS)
    return postings


def fetch_description(company: str, posting_id: str) -> tuple[str, str | None]:
    """The detail call for ONE posting: (description text, public link or None).

    The text is every section of the job ad as "Section title\\n<clean text>", sections separated by an empty
    line. Call this only for postings that already passed the title filter, to keep the number of calls low."""
    data = _get(DETAIL_URL.format(company=company, posting_id=posting_id))
    sections = (data.get("jobAd") or {}).get("sections") or {}
    names = SECTION_ORDER + [name for name in sections if name not in SECTION_ORDER]
    parts = []
    for name in names:
        section = sections.get(name)
        if not isinstance(section, dict):
            continue
        text = clean_text(section.get("text"))
        if not text:
            continue
        title = clean_text(section.get("title")) or name
        parts.append(f"{title}\n{text}")
    return "\n\n".join(parts), data.get("postingUrl") or data.get("applyUrl") or None


def published_on(raw: dict) -> date | None:
    """releasedDate (ISO, UTC) as a date in Berlin time. It can be a re-release date."""
    released = raw.get("releasedDate")
    if not released:
        return None
    moment = datetime.fromisoformat(released.replace("Z", "+00:00"))
    return moment.astimezone(BERLIN).date()


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def state_name(region_code: str | None) -> str | None:
    """"BW" -> "BADEN_WUERTTEMBERG". Unknown or empty -> None."""
    return STATE_BY_REGION_CODE.get((region_code or "").strip().upper())


def to_posting(raw: dict, company: str) -> dict:
    """One SmartRecruiters list entry in the posting shape the daily run uses.
    The description stays empty here and is filled later with fetch_description."""
    place = raw.get("location") or {}
    city = place.get("city") or None
    posting_id = raw["id"]
    source_id = f"{company}:{posting_id}"
    firm = (raw.get("company") or {}).get("name") or ""
    locations = [{"city": city, "lat": _to_float(place.get("latitude")), "lon": _to_float(place.get("longitude")),
                  "raw": place.get("fullLocation") or "", "region": state_name(place.get("region"))}] if city else []
    return {"source": SOURCE, "source_id": source_id, "refnr": source_id,
            "title": (raw.get("name") or "").strip(),
            "company": firm, "firma": firm, "city": city, "ort": city,
            "locations": locations, "location_text": place.get("fullLocation") or "",
            "remote": bool(place.get("remote")),
            "description": "", "published_on": published_on(raw),
            "url": PUBLIC_URL.format(company=company, posting_id=posting_id),
            "location_gap": None, "location_note": None,
            "experience_level": (raw.get("experienceLevel") or {}).get("label"),
            "employment_type": (raw.get("typeOfEmployment") or {}).get("label")}
