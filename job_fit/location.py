"""Can I get to this job's office as often as it needs? Decided in code, the same way every time.

Inputs:
- office_presence: from the relevance call (relevance.py): not_mentioned, remote, occasional, weekly,
  regular_unspecified.
- the posting's locations from the Arbeitsagentur API: coordinates (breite, laenge) and federal state (region).

The rule (agreed 19 Nov 2026), with three zones for weekly presence so nothing reachable is lost silently:
- weekly or regular_unspecified:
    within WEEKLY_RADIUS_KM (115 km)        -> fine
    up to CHECK_RADIUS_KM (170 km)           -> "check": you look up the train time (verdict lack_of_info)
    further                                  -> "skip"
- occasional: at least one location in TWICE_A_MONTH_STATES (or within the weekly radius), else "skip".
- remote or not_mentioned: fine. "Not mentioned" counts as fine, so a posting is never dropped over
  something it does not say.

The radius is a straight line, not train time. 115 km covers Hof, Bayreuth, Bamberg, Coburg, Plauen,
Zwickau, Erlangen, Nuremberg, Fürth and Chemnitz (107 km), and leaves out Dresden (168 km),
Leipzig (134 km), Regensburg (133 km) and Gunzenhausen (141 km).
"""

from math import asin, cos, radians, sin, sqrt

HOME = (50.1917, 11.7856)            # Münchberg
WEEKLY_RADIUS_KM = 115
CHECK_RADIUS_KM = 170       # 115 to 170 km: Leipzig, Regensburg, Würzburg, Ansbach, Gunzenhausen, Dresden
TWICE_A_MONTH_STATES = {"BAYERN", "THUERINGEN", "SACHSEN", "SACHSEN_ANHALT", "HESSEN",
                        "BADEN_WUERTTEMBERG", "BERLIN"}   # the API's own spelling of the federal states

NEEDS_WEEKLY = {"weekly", "regular_unspecified"}


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line distance between two (latitude, longitude) points (the haversine formula)."""
    lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


def location_gap(office_presence: str | None, locations: list[dict]) -> dict | None:
    """None if the job is reachable often enough. Otherwise {"kind": "skip" or "check", "reason": one line}.

    locations: [{"city": ..., "region": ..., "lat": ..., "lon": ...}, ...]; a job is fine if ANY of its
    locations works (for example "Chemnitz or Dresden").
    """
    if office_presence not in NEEDS_WEEKLY and office_presence != "occasional":
        return None                          # remote, not_mentioned, or the question was not answered
    with_coords = [loc for loc in locations if loc.get("lat") is not None and loc.get("lon") is not None]
    if not locations:
        return None                          # no location given: nothing to judge

    nearest = min((distance_km(HOME, (loc["lat"], loc["lon"])), loc.get("city") or "?") for loc in with_coords) \
        if with_coords else None
    if nearest and nearest[0] <= WEEKLY_RADIUS_KM:
        return None
    if office_presence == "occasional":
        if any(loc.get("region") in TWICE_A_MONTH_STATES for loc in locations):
            return None
        where = ", ".join(sorted({loc.get("city") or "?" for loc in locations}))
        return {"kind": "skip", "reason": f"Occasional office presence in {where}, outside the states reachable twice a month"}

    if nearest is None:
        return {"kind": "check", "reason": "Office presence every week, but the posting has no coordinates: check where it is"}
    km, city = nearest
    if km <= CHECK_RADIUS_KM:
        return {"kind": "check", "reason": f"Office presence every week ({office_presence}) in {city}, {km:.0f} km away: "
                                           f"check the train time"}
    return {"kind": "skip", "reason": f"Office presence every week ({office_presence}) in {city}, {km:.0f} km away"}


def locations_from_api(job: dict) -> list[dict]:
    """The posting's locations in the shape location_gap() expects."""
    out = []
    for place in job.get("stellenlokationen") or []:
        address = place.get("adresse") or {}
        out.append({"city": address.get("ort"), "region": address.get("region"),
                    "lat": place.get("breite"), "lon": place.get("laenge")})
    return out
