"""Is this posting a job we already have, under another source or another spelling? Decided in code, no model.

Two kinds of duplicates, seen in the Telegram messages of 22 Nov 2026:
1. The same job on two sites, for example the Arbeitsagentur and the company's own feed. The texts are spelled
   differently ("Werkstudent (w/m/d) AI Automation" vs "Werkstudent AI Automation (m/f/d)", "Siemens AG" vs
   "Siemens", "Amberg, Oberpfalz" vs "Amberg"), so they are compared after match_parts() cleans them up.
2. The same job in two languages (Bosch posts most jobs in German and in English). The titles share nothing,
   so this is handled in the source itself: both versions carry the same reference number (smartrecruiters.py).

The rule: same cleaned title and same city, and either the companies match (one cleaned name starts with
the other: "huk coburg" and "huk coburg versicherungsgruppe") or the title is long enough (LONG_TITLE_WORDS)
that two different companies would not use it word for word in the same city.
"""

import re

LONG_TITLE_WORDS = 6

GENDER_TAG = re.compile(r"\((?:[^()]*\b(?:m|w|d|f|x|div|gn|all genders|alle geschlechter)\b[^()]*)\)", re.IGNORECASE)
GENDER_ENDING = re.compile(r"(?<=\w)(?:\*|:|/|_)in(?:nen)?\b", re.IGNORECASE)        # Werkstudent*in, Werkstudent:in
LEGAL_WORDS = {"gmbh", "ag", "se", "kg", "kgaa", "co", "mbh", "ggmbh", "ug", "ltd", "inc", "llc", "bv", "sa", "sarl",
               "group", "gruppe", "holding", "stiftung", "und", "and", "the"}
CITY_NAMES = {"munich": "münchen", "muenchen": "münchen", "cologne": "köln", "nuremberg": "nürnberg",
              "nuernberg": "nürnberg", "hanover": "hannover", "dusseldorf": "düsseldorf", "duesseldorf": "düsseldorf"}


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w]+", " ", (text or "").lower()).split()


def clean_title(title: str) -> str:
    """"Werkstudent:in (m/w/d) Softwareentwicklung & KI" -> "werkstudent softwareentwicklung ki"."""
    text = GENDER_ENDING.sub("", GENDER_TAG.sub(" ", title or ""))
    return " ".join(_words(text))


def clean_company(company: str) -> str:
    """"Siemens AG" -> "siemens", "Fiege Logistik Holding Stiftung & Co. KG" -> "fiege logistik"."""
    return " ".join(w for w in _words(company) if w not in LEGAL_WORDS)


def clean_city(city: str) -> str:
    """"Amberg, Oberpfalz" -> "amberg", "Munich" -> "münchen"."""
    first = re.split(r"[,(/]", city or "")[0]
    name = " ".join(_words(first))
    return CITY_NAMES.get(name, name)


def match_parts(company: str, title: str, city: str) -> tuple[str, str, str]:
    return clean_company(company), clean_title(title), clean_city(city)


def companies_match(a: str, b: str) -> bool:
    """Equal, or one name is the start of the other ("huk coburg" / "huk coburg versicherungsgruppe")."""
    if not a or not b:
        return False
    wa, wb = a.split(), b.split()
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return long_[: len(short)] == short


class KnownJobs:
    """Every job we already have (from the database) plus the ones met earlier in this run.
    known.has(company, title, city) answers "is this a duplicate?"; known.add(...) records a new job."""

    def __init__(self, rows=()):
        self._by_title_city: dict[tuple[str, str], list[str]] = {}
        for company, title, city in rows:
            self.add(company, title, city)

    def add(self, company: str, title: str, city: str) -> None:
        c, t, place = match_parts(company, title, city)
        self._by_title_city.setdefault((t, place), []).append(c)

    def has(self, company: str, title: str, city: str) -> bool:
        c, t, place = match_parts(company, title, city)
        companies = self._by_title_city.get((t, place))
        if not companies:
            return False
        return len(t.split()) >= LONG_TITLE_WORDS or any(companies_match(c, other) for other in companies)
