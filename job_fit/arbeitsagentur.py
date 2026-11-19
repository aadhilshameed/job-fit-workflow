"""Intake from the Arbeitsagentur Jobsuche REST API (the same calls the n8n workflow made).

Search: GET .../pc/v6/jobs          (v4 answers 403)
Details: GET .../pc/v4/jobdetails/{reference number in base64}
Both need the header X-API-Key: jobboerse-jobsuche (a public key, not a secret).
"""

import base64
import time

import requests

SEARCH_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
DETAILS_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{}"
HEADERS = {"X-API-Key": "jobboerse-jobsuche"}

SEARCH_TERMS = [
    "Werkstudent KI", "Werkstudent AI", "Werkstudent Künstliche Intelligenz",
    "Werkstudent Generative KI", "Werkstudent LLM", "Werkstudent Agent",
]

PAUSE_SECONDS = 2      # between calls, like the n8n batching (1 item every 2000 ms)
RETRIES = 3            # like n8n "Retry On Fail": 3 tries, 5 s apart


def _get(url, params=None):
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(url, headers=HEADERS, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == RETRIES:
                raise
            time.sleep(5)


def search(days_back: int | None = None, size: int = 100) -> list[dict]:
    """All search results for every term, without duplicates (the same posting often matches several terms)."""
    seen, postings = set(), []
    for term in SEARCH_TERMS:
        params = {"was": term, "size": size, "page": 1}
        if days_back:
            params["veroeffentlichtseit"] = days_back
        result = _get(SEARCH_URL, params)
        for job in result.get("ergebnisliste", []) or []:
            if job["referenznummer"] not in seen:
                seen.add(job["referenznummer"])
                postings.append(job)
        time.sleep(PAUSE_SECONDS)
    return postings


def details(refnr: str) -> dict:
    """The full posting, including the description text (stellenangebotsBeschreibung)."""
    encoded = base64.b64encode(refnr.encode("utf-8")).decode("ascii")
    data = _get(DETAILS_URL.format(encoded))
    time.sleep(PAUSE_SECONDS)
    return data


def location(job: dict) -> tuple[str, str]:
    """City and federal state of the first listed location ("", "" if none)."""
    places = job.get("stellenlokationen") or [{}]
    address = places[0].get("adresse", {}) or {}
    return address.get("ort", ""), address.get("region", "")
