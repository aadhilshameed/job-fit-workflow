"""Place names to coordinates, for sources that give only a city name (Arbeitnow, later the company feeds).
The Arbeitsagentur sends coordinates itself, so its postings never come here.

    add_coordinates(conn, locations)   fills in lat, lon and region for every location that has none

Each place text is looked up once: the answer goes into the place_coordinates table (db/schema.sql), and every
later run reads it from there. A place Geoapify cannot find is stored too (lat and lon NULL), so it is not
asked about again. A failed call (network, quota) is not stored, so the next run tries again.

Geoapify (free plan: 3,000 calls a day, 5 a second) needs GEOAPIFY_API_KEY in .env and in the Lambda's
environment variables. Its data comes from OpenStreetMap: "Powered by Geoapify, © OpenStreetMap contributors".
"""

import os

import requests
from psycopg.rows import dict_row

GEOCODE_URL = "https://api.geoapify.com/v1/geocode/search"
WRONG_KIND_OF_PLACE = {"street", "amenity", "building"}   # Geoapify result types that are never a job's city

# Geoapify's state codes -> the Arbeitsagentur's spelling, which the location rule (code_nodes/location.py) uses
STATE_NAMES = {"BW": "BADEN_WUERTTEMBERG", "BY": "BAYERN", "BE": "BERLIN", "BB": "BRANDENBURG", "HB": "BREMEN",
               "HH": "HAMBURG", "HE": "HESSEN", "MV": "MECKLENBURG_VORPOMMERN", "NI": "NIEDERSACHSEN",
               "NW": "NORDRHEIN_WESTFALEN", "RP": "RHEINLAND_PFALZ", "SL": "SAARLAND", "SN": "SACHSEN",
               "ST": "SACHSEN_ANHALT", "SH": "SCHLESWIG_HOLSTEIN", "TH": "THUERINGEN"}


def place_key(text: str | None) -> str:
    """"  Düsseldorf,  Nordrhein-Westfalen " -> "düsseldorf, nordrhein-westfalen": one spelling per place."""
    return " ".join((text or "").lower().split())


def ask_geoapify(text: str) -> dict:
    """One Geoapify call. Returns the place_coordinates columns; lat and lon are None if nothing was found.
    Germany is preferred (bias), so "Frankfurt" means Frankfurt am Main, but places abroad are still found."""
    response = requests.get(GEOCODE_URL, timeout=20, params={
        "text": text, "lang": "en", "limit": 1, "format": "json", "bias": "countrycode:de",
        "apiKey": os.environ["GEOAPIFY_API_KEY"]})
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        return {"lat": None, "lon": None, "country_code": None, "state_code": None, "result_type": None,
                "confidence": None, "found_name": None}
    best = results[0]
    if best.get("result_type") in WRONG_KIND_OF_PLACE:
        # "heidelberg, ger" came back as a street called Heidelberg in Ilmenau (Thuringia), and
        # "london, england, spain" as a building called London in Spain: a street or a building is never
        # what a job posting means, so treat it as not found (25 Nov 2026)
        return {"lat": None, "lon": None, "country_code": None, "state_code": None,
                "result_type": best.get("result_type"), "confidence": None, "found_name": best.get("formatted")}
    return {"lat": best.get("lat"), "lon": best.get("lon"), "country_code": best.get("country_code"),
            "state_code": best.get("state_code"), "result_type": best.get("result_type"),
            "confidence": (best.get("rank") or {}).get("confidence"), "found_name": best.get("formatted")}


def lookup(conn, text: str | None) -> dict | None:
    """The stored answer for this place, asking Geoapify only the first time. None for an empty text,
    or when the call failed (then nothing is stored, so the next run tries again)."""
    key = place_key(text)
    if not key:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT * FROM place_coordinates WHERE place_text = %s", (key,)).fetchone()
    if row:
        return row
    try:
        found = ask_geoapify(key)
    except Exception as e:                                                  # noqa: BLE001
        print(f"Geoapify failed for {key!r} ({type(e).__name__}): left without coordinates for this run")
        return None
    conn.execute(
        """
        INSERT INTO place_coordinates
            (place_text, lat, lon, country_code, state_code, result_type, confidence, found_name)
        VALUES (%(place_text)s, %(lat)s, %(lon)s, %(country_code)s, %(state_code)s, %(result_type)s,
                %(confidence)s, %(found_name)s)
        ON CONFLICT (place_text) DO NOTHING
        """, {"place_text": key, **found})
    return {"place_text": key, **found}


def add_coordinates(conn, locations: list[dict]) -> list[dict]:
    """Fills in lat, lon and region (the Arbeitsagentur's state spelling, for the location rule) for every
    location without coordinates. Uses the place as the source wrote it ("raw") when there is one, since
    "Düsseldorf, Nordrhein-Westfalen" is more exact than "Düsseldorf". Changes the dicts in place and returns them."""
    for loc in locations:
        if loc.get("lat") is not None and loc.get("lon") is not None:
            continue
        found = lookup(conn, loc.get("raw") or loc.get("city"))
        if found and found.get("lat") is not None:
            loc["lat"], loc["lon"] = found["lat"], found["lon"]
            loc["region"] = loc.get("region") or STATE_NAMES.get((found.get("state_code") or "").upper())
            loc["country_code"] = found.get("country_code")
    return locations
