"""Fetch fresh postings and run them through every step before the assessment.

    python -m scripts.fetch_postings                  # search, filter, details, relevance
    python -m scripts.fetch_postings --max 50         # stop after 50 postings have passed the title filter
    python -m scripts.fetch_postings --no-relevance   # skip the paid relevance question

Steps: search (6 terms) -> title filter (keep / check / drop) -> details for keep and check ->
description check for "check" postings -> relevance question (OpenAI Decisions API) -> save.

Writes two files into data/batches/:
- batch_<date>.json: every posting that passed the title filter, with what each step decided
  (so we can check the filter and the relevance answers)
- set_<date>.json: only the postings that passed every step, in the same shape as comparison_set.json,
  ready for: python -m scripts.compare_models run --set data/batches/set_<date>.json
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

from job_fit import arbeitsagentur                                   # noqa: E402
from job_fit.title_filter import genai_signals, passes_description_check, title_status   # noqa: E402

BATCHES = ROOT / "data" / "batches"


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
    parser.add_argument("--no-relevance", action="store_true")
    args = parser.parse_args()

    BATCHES.mkdir(parents=True, exist_ok=True)
    seen = already_seen()

    found = arbeitsagentur.search(days_back=args.days)
    new = [j for j in found if j["referenznummer"] not in seen]
    print(f"Search: {len(found)} postings, {len(new)} not seen before")

    statuses = [(j, title_status(j)) for j in new]
    passed_title = [(j, s) for j, s in statuses if s != "drop"][: args.max]
    print(f"Title filter: {sum(1 for _, s in statuses if s != 'drop')} pass, taking {len(passed_title)}")

    if not args.no_relevance:
        from job_fit.relevance import check_relevance

    batch, final_set, relevance_tokens = [], [], 0
    for i, (job, status) in enumerate(passed_title, 1):
        refnr = job["referenznummer"]
        full = arbeitsagentur.details(refnr)
        text = full.get("stellenangebotsBeschreibung", "") or ""
        city, region = arbeitsagentur.location(full if full.get("stellenlokationen") else job)
        title = job.get("stellenangebotsTitel") or job.get("titel") or full.get("stellenangebotsTitel", "")
        row = {"refnr": refnr, "title": title, "firma": job.get("firma") or full.get("firma"),
               "ort": city, "region": region, "title_status": status, "signals": genai_signals(text),
               "passed_description": status == "keep" or passes_description_check(text),
               "description": text}
        if row["passed_description"] and not args.no_relevance and text:
            try:
                rel = check_relevance(title, text)
                relevance_tokens += rel.pop("input_tokens")
                row.update(rel)
            except Exception as e:                                      # noqa: BLE001
                row["relevance_error"] = f"{type(e).__name__}: {e}"[:300]
        batch.append(row)
        verdict = ("filtered out (description)" if not row["passed_description"]
                   else f"relevance {row.get('target_probability', '?')} {row.get('main_focus', '')}")
        print(f"[{i}/{len(passed_title)}] {title[:60]:60} {verdict}")
        if row["passed_description"] and (args.no_relevance or row.get("is_target_role")):
            final_set.append({k: row[k] for k in ("refnr", "title", "firma", "ort", "description")})

    today = date.today().isoformat()
    (BATCHES / f"batch_{today}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    (BATCHES / f"set_{today}.json").write_text(json.dumps(final_set, ensure_ascii=False, indent=1), encoding="utf-8")
    cost = relevance_tokens * 0.10 / 1_000_000
    print(f"\n{len(batch)} checked, {len(final_set)} passed every step. Relevance cost: ${cost:.4f}")
    print(f"Saved data/batches/batch_{today}.json and data/batches/set_{today}.json")


if __name__ == "__main__":
    main()
