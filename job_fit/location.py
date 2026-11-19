"""Can I get to this job's office as often as it needs? Decided in code, the same way every time.

Inputs:
- office_presence: from the relevance call (relevance.py): not_mentioned, remote, occasional, weekly,
  regular_unspecified.
- the posting's locations from the Arbeitsagentur API: coordinates (breite, laenge) and federal state (region).

The rule (agreed 19 Nov 2026):
- weekly or regular_unspecified: at least one location within WEEKLY_RADIUS_KM of Münchberg.
- occasional: at least one location in TWICE_A_MONTH_STATES (or within the weekly radius).
- remote or not_mentioned: no location gap. "Not mentioned" counts as fine, so a posting is never
  dropped over something it does not say.

The radius is a straight line, not train time. 115 km covers Hof, Bayreuth, Bamberg, Coburg, Plauen,
Zwickau, Erlangen, Nuremberg, Fürth and Chemnitz (107 km), and leaves out Dresden (168 km),
Leipzig (134 km), Regensburg (133 km) and Gunzenhausen (141 km).
"""

from math import asin, cos, radians, sin, sqrt

HOME = (50.1917, 11.7856)            # Münchberg
WEEKLY_RADIUS_KM = 115
TWICE_A_MONTH_STATES = {"BAYERN", "THUERINGEN", "SACHSEN", "SACHSEN_ANHALT", "HESSEN",
                        "BADEN_WUERTTEMBERG", "BERLIN"}   # the API's own spelling of the federal states

NEEDS_WEEKLY = {"weekly", "regular_unspecified"}


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line distance between two (latitude, longitude) points (the haversine formula)."""
    lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


def location_gap(office_presence: str | None, locations: list[dict]) -> str | None:
    """None if the job is reachable often enough, otherwise one line saying why not.

    locations: [{"city": ..., "region": ..., "lat": ..., "lon": ...}, ...]; a job is fine if ANY of its
    locations works (for example "Chemnitz or Dresden").
    """
    if office_presence not in NEEDS_WEEKLY and office_presence != "occasional":
        return None                          # remote, not_mentioned, or the question was not answered
    if not locations:
        return None                          # no location given: nothing to judge

    def within_weekly(loc):
        return loc.get("lat") is not None and distance_km(HOME, (loc["lat"], loc["lon"])) <= WEEKLY_RADIUS_KM

    if any(within_weekly(loc) for loc in locations):
        return None
    if office_presence == "occasional" and any(loc.get("region") in TWICE_A_MONTH_STATES for loc in locations):
        return None

    where = ", ".join(sorted({loc.get("city") or "?" for loc in locations}))
    if office_presence in NEEDS_WEEKLY:
        return f"Office presence every week ({office_presence}) in {where}, more than {WEEKLY_RADIUS_KM} km away"
    return f"Occasional office presence in {where}, outside the states reachable twice a month"


def locations_from_api(job: dict) -> list[dict]:
    """The posting's locations in the shape location_gap() expects."""
    out = []
    for place in job.get("stellenlokationen") or []:
        address = place.get("adresse") or {}
        out.append({"city": address.get("ort"), "region": address.get("region"),
                    "lat": place.get("breite"), "lon": place.get("laenge")})
    return out
