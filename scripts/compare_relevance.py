"""Ask both Decisions APIs (OpenAI and Gemini) the same three questions about the same postings.

    python -m scripts.compare_relevance                                    # the 25 comparison postings
    python -m scripts.compare_relevance --set data/batches/set_<date>.json

Nothing is written into the set. Every answer goes to results/relevance_<set name>.jsonl, so a rerun only
asks what is missing, and Claude checks each answer against the posting text before we pick a provider.
Cost: about 0.015 cents per posting per provider, so under 1 cent for 25 postings.
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))
load_dotenv(ROOT / ".env")

from job_fit.location import location_gap           # noqa: E402
from job_fit.relevance import QUESTIONS_VERSION, check_relevance        # noqa: E402

PROVIDERS = ["openai", "google"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", type=Path, default=ROOT / "data" / "comparison_set.json")
    parser.add_argument("--only", choices=PROVIDERS, help="ask one provider only")
    args = parser.parse_args()

    postings = json.loads(args.set.read_text(encoding="utf-8"))
    name = "comparison" if args.set.stem == "comparison_set" else args.set.stem
    out = ROOT / "results" / f"relevance_{name}_{QUESTIONS_VERSION}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not r.get("error"):
                done.add((r["provider"], r["refnr"]))

    tokens = {p: 0 for p in PROVIDERS}
    for provider in ([args.only] if args.only else PROVIDERS):
        for i, p in enumerate(postings, 1):
            if (provider, p["refnr"]) in done:
                continue
            start = time.perf_counter()
            try:
                rel = check_relevance(p["title"], p["description"], provider)
                tokens[provider] += rel["input_tokens"] or 0
                rel["location_gap"] = location_gap(rel["office_presence"], p.get("locations") or [])
                row, status = rel, (f"target {rel['target_probability']:.2f} {str(rel['main_focus']):15} "
                                    f"presence {rel['office_presence']}{('  LOCATION ' + rel['location_gap']['kind'].upper()) if rel['location_gap'] else ''}")
            except Exception as e:                                       # noqa: BLE001
                row, status = {"error": f"{type(e).__name__}: {e}"[:400]}, "ERROR " + f"{type(e).__name__}: {e}"[:100]
            row |= {"provider": provider, "refnr": p["refnr"], "seconds": round(time.perf_counter() - start, 2)}
            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[{provider:6} {i}/{len(postings)}] {p['title'][:50]:50} {status}")
    print("Cost: " + ", ".join(f"{p} ${t * 0.10 / 1_000_000:.4f}" for p, t in tokens.items()))
    print(f"Saved {out.relative_to(ROOT)}. Tell Claude, who checks the answers.")


if __name__ == "__main__":
    main()
