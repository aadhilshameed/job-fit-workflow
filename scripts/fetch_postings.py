"""Fetch fresh postings and run them through every step before the assessment.

    python -m scripts.fetch_postings                  # search, title filter, details, relevance
    python -m scripts.fetch_postings --max 50         # stop after 50 postings have passed the title filter
    python -m scripts.fetch_postings --days 7         # only postings published in the last 7 days

Steps: search (6 terms) -> title filter (keep / check / drop) -> details for everything not dropped ->
relevance call (OpenAI Decisions API: target role? main focus? office presence?) -> location rule in code -> save.

Writes two files into data/batches/:
- batch_<date>.json: every posting that passed the title filter, with what each step decided
  (so we can check the filter, the relevance answers and the location rule)
- set_<date>.json: only the postings that are a target role, in the shape compare_models expects:
      python -m scripts.compare_models run --set data/batches/set_<date>.json
  Postings with a location gap stay in the set: the gap turns their verdict into skip, and the report shows why.
Postings already in comparison_set.json or in an earlier batch are skipped, so every batch is new.
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import json
import sys
from datetime import date
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


def already_seen() -> set[str]:
    seen = set()
    comparison = ROOT / "data" / "comparison_set.json"
    if comparison.exists():
        seen |= {p["refnr"] for p in json.loads(comparison.read_text(encoding="utf-8"))}
    for f in BATCHES.glob("batch_*.json"):
        seen |= {p["refnr"] for p in json.loads(f.read_text(encoding="utf-8"))}
    return seen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=60, help="stop after this many postings pass the title filter")
    parser.add_argument("--days", type=int, default=None, help="only postings published in the last N days")
    args = parser.parse_args()

    BATCHES.mkdir(parents=True, exist_ok=True)
    seen = already_seen()

    found = arbeitsagentur.search(days_back=args.days)
    new = [j for j in found if j["referenznummer"] not in seen]
    print(f"Search: {len(found)} postings, {len(new)} not seen before")

    statuses = [(j, title_status(j)) for j in new]
    passed = [(j, s) for j, s in statuses if s != "drop"]
    print(f"Title filter: {len(passed)} pass ({sum(s == 'keep' for _, s in passed)} keep, "
          f"{sum(s == 'check' for _, s in passed)} check), taking {min(len(passed), args.max)}")

    batch, final_set, relevance_tokens = [], [], 0
    for i, (job, status) in enumerate(passed[: args.max], 1):
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
               "signals": genai_signals(text), "locations": locations, "description": text}
        if text:
            try:
                rel = check_relevance(title, text)
                relevance_tokens += rel.pop("input_tokens")
                row.update(rel)
                row["location_gap"] = location_gap(rel["office_presence"], locations)
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

    today = date.today().isoformat()
    (BATCHES / f"batch_{today}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    (BATCHES / f"set_{today}.json").write_text(json.dumps(final_set, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(batch)} checked, {len(final_set)} are target roles "
          f"({sum(1 for r in batch if r.get('is_target_role') and r.get('location_gap'))} of them with a location gap). "
          f"Relevance cost: ${relevance_tokens * 0.10 / 1_000_000:.4f}")
    print(f"Saved data/batches/batch_{today}.json and data/batches/set_{today}.json")


if __name__ == "__main__":
    main()
