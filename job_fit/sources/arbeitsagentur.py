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

SEARCH_TERMS = [         # every role a student can take next to the MSc: Werkstudent, internship (widened 22 Nov 2026)
    "Werkstudent KI", "Werkstudent AI", "Werkstudent Künstliche Intelligenz",
    "Werkstudent Generative KI", "Werkstudent LLM", "Werkstudent Agent", "Working Student AI",
    "Praktikum KI", "Praktikum AI", "Praktikum Generative KI", "Praktikum LLM", "Praktikum Agent",
    "Internship AI",
    # added 22 Nov 2026: agentic AI is the target, and English titles starting with "Intern" were missed
    # (BMW's "Intern Artificial Intelligence and Agentic AI in Quality Management"). These also bring full-time roles.
    "Agentic AI", "KI Agent", "AI Agent", "Generative AI", "GenAI", "LLM", "Intern AI", "Intern KI",
    "Werkstudent Agentic AI", "Praktikum Agentic AI",
]

PAUSE_SECONDS = 2      # between calls, like the n8n batching (1 item every 2000 ms)
RETRIES = 3            # like n8n "Retry On Fail": 3 tries, 5 s apart
MAX_PAGES = 5          # per search term: at most 500 results, far more than one day brings


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
    """All search results for every term, without duplicates among them (the same posting often matches several terms).
    wihin the said number of pages"""
    seen, postings = set(), []
    for term in SEARCH_TERMS:
        for page in range(1, MAX_PAGES + 1):      # a full page means there may be more: fetch the next one
            params = {"was": term, "size": size, "page": page}
            if days_back:
                params["veroeffentlichtseit"] = days_back   # ignored by the v6 search (21 Nov 2026), kept in case it returns
            page_jobs = _get(SEARCH_URL, params).get("ergebnisliste", []) or []
            for job in page_jobs:
                if job["referenznummer"] not in seen:
                    seen.add(job["referenznummer"])
                    postings.append(job)
            time.sleep(PAUSE_SECONDS)
            if len(page_jobs) < size:
                break
    return postings
    #if there are 500 postings in AI which are unique, it would return all of them.
    #of these 500 postings, 95% will be the postings that we have seen before


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
