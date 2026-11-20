"""Fetch fresh postings and run them through every step before the assessment.

    python -m scripts.fetch_postings                  # search, title filter, details, relevance
    python -m scripts.fetch_postings --max 50         # stop after 50 postings have passed the title filter
    python -m scripts.fetch_postings --days 7         # only postings published in the last 7 days

Steps: search (6 terms) -> title filter (keep / check / drop) -> details for everything not dropped ->
relevance call (OpenAI Decisions API: target role? main focus? office presence?) -> location rule in code -> save.

Writes two files into data/batches/:
- batch_<date>.json: EVERY search result, with what each step decided: the ones the title filter dropped,
  the duplicates, and the full checks for the rest (so we can audit every step, including the filter)
- set_<date>.json: only the postings that are a target role, in the shape compare_models expects:
      python -m scripts.compare_models run --set data/batches/set_<date>.json
  Postings with a location gap stay in the set: the gap turns their verdict into skip, and the report shows why.
Postings already in comparison_set.json or in an earlier batch are skipped, so every batch is new.
Duplicates: the same company, title and city under a different reference number count as one posting
(the same job is often listed twice); the extra reference numbers are kept in "duplicate_refnrs".
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))
load_dotenv(ROOT / ".env")

from job_fit import arbeitsagentur                                  # noqa: E402
from job_fit.location import location_gap, location_note, locations_from_api   # noqa: E402
from job_fit.relevance import check_relevance                        # noqa: E402
from job_fit.title_filter import genai_signals, title_status         # noqa: E402

BATCHES = ROOT / "data" / "batches"
SET_FIELDS = ("refnr", "title", "firma", "ort", "description", "locations",
              "target_probability", "main_focus", "office_presence", "presence_confidence", "relevance_error")


def duplicate_key(firma, title, city) -> str:
    """Company + title + city, in lower case with spaces tidied: the same job listed twice gets the same key."""
    tidy = lambda x: " ".join((x or "").lower().split())
    return f"{tidy(firma)}|{tidy(title)}|{tidy(city)}"


def already_seen() -> tuple[set[str], set[str]]:
    """Reference numbers and duplicate keys of every posting seen before."""
    refnrs, keys = set(), set()
    files = [ROOT / "data" / "comparison_set.json"] + list(BATCHES.glob("batch_*.json"))
    for f in files:
        if not f.exists():
            continue
        for p in json.loads(f.read_text(encoding="utf-8")):
            refnrs.add(p["refnr"])
            refnrs.update(p.get("duplicate_refnrs") or [])
            keys.add(duplicate_key(p.get("firma"), p.get("title"), p.get("ort")))
    return refnrs, keys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=60, help="stop after this many postings pass the title filter")
    parser.add_argument("--days", type=int, default=None, help="only postings published in the last N days")
    args = parser.parse_args()

    BATCHES.mkdir(parents=True, exist_ok=True)
    seen_refnrs, seen_keys = already_seen()

    found = arbeitsagentur.search(days_back=args.days)
    new, extra_refnrs, skipped_seen = [], {}, 0
    for j in found:
        key = duplicate_key(j.get("firma"), j.get("stellenangebotsTitel"), arbeitsagentur.location(j)[0])
        if j["referenznummer"] in seen_refnrs or key in seen_keys:
            skipped_seen += 1
            continue
        if key in extra_refnrs:                       # a duplicate within this search
            extra_refnrs[key].append(j["referenznummer"])
            continue
        extra_refnrs[key] = []
        new.append((j, key))
    n_dupes = sum(len(v) for v in extra_refnrs.values())
    print(f"Search: {len(found)} postings, {skipped_seen} seen before, {n_dupes} duplicates, {len(new)} new")

    statuses = [(j, title_status(j), key) for j, key in new]
    dropped = [{"refnr": j["referenznummer"], "title": j.get("stellenangebotsTitel"), "firma": j.get("firma"),
                "ort": arbeitsagentur.location(j)[0], "region": arbeitsagentur.location(j)[1],
                "hauptberuf": j.get("hauptberuf"), "title_status": "drop",
                "duplicate_refnrs": extra_refnrs[key]} for j, s, key in statuses if s == "drop"]
    passed = [(j, s, key) for j, s, key in statuses if s != "drop"]
    print(f"Title filter: {len(passed)} pass ({sum(s == 'keep' for _, s, _ in passed)} keep, "
          f"{sum(s == 'check' for _, s, _ in passed)} check), taking {min(len(passed), args.max)}")

    batch, final_set, relevance_tokens = [], [], 0
    for i, (job, status, key) in enumerate(passed[: args.max], 1):
        refnr = job["referenznummer"]
        try:
            full = arbeitsagentur.details(refnr)
        except Exception as e:                                          # noqa: BLE001
            print(f"[{i}] {refnr}: detail call failed ({type(e).__name__}), skipped")
            continue
        text = full.get("stellenangebotsBeschreibung", "") or ""
        title = job.get("stellenangebotsTitel") or full.get("stellenangebotsTitel", "")
        locations = locations_from_api(full if full.get("stellenlokationen") else job)
        row = {"refnr": refnr, "title": title, "firma": job.get("firma") or full.get("firma"),
               "ort": locations[0]["city"] if locations else "", "title_status": status,
               "signals": genai_signals(text), "locations": locations, "description": text,
               "duplicate_refnrs": extra_refnrs[key]}
        if text:
            try:
                rel = check_relevance(title, text)
                relevance_tokens += rel.pop("input_tokens")
                row.update(rel)
                row["location_gap"] = location_gap(rel["office_presence"], locations, rel["presence_confidence"])
                row["location_note"] = location_note(rel["office_presence"], locations)
            except Exception as e:                                      # noqa: BLE001
                row["relevance_error"] = f"{type(e).__name__}: {e}"[:300]
        batch.append(row)
        if "relevance_error" in row:
            note = "RELEVANCE ERROR " + row["relevance_error"][:60]
        elif not text:
            note = "no posting text"
        else:
            note = (f"target {row['target_probability']:.2f} {str(row['main_focus']):15} "
                    f"presence {row['office_presence']}{('  LOCATION ' + row['location_gap']['kind'].upper()) if row['location_gap'] else ''}")
        print(f"[{i}/{min(len(passed), args.max)}] {title[:55]:55} {note}")
        # a posting whose relevance call failed goes through anyway: Haiku costs less than a missed job
        if row.get("is_target_role") or "relevance_error" in row:
            final_set.append({k: row.get(k) for k in SET_FIELDS})

    today = datetime.now().strftime("%Y-%m-%d_%H%M")   # with the time, so a second run on one day never overwrites
    batch_all = batch + dropped        # dropped postings too, so the title filter can be audited
    (BATCHES / f"batch_{today}.json").write_text(json.dumps(batch_all, ensure_ascii=False, indent=1), encoding="utf-8")
    (BATCHES / f"set_{today}.json").write_text(json.dumps(final_set, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(batch)} checked, {len(final_set)} are target roles "
          f"({sum(1 for r in batch if r.get('is_target_role') and r.get('location_gap'))} of them with a location gap). "
          f"Relevance cost: ${relevance_tokens * 0.10 / 1_000_000:.4f}")
    # the same set under a fixed name, so an overnight command can find it after midnight
    (BATCHES / "set_latest.json").write_text(json.dumps(final_set, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Saved data/batches/batch_{today}.json, data/batches/set_{today}.json and data/batches/set_latest.json")


if __name__ == "__main__":
    main()
