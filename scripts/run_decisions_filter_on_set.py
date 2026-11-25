"""Ask the Decisions API filter's questions for a set of postings that does not have the answers yet.

    python -m scripts.run_decisions_filter_on_set                                  # data/comparison_set.json
    python -m scripts.run_decisions_filter_on_set --set data/batches/set_<date>.json

fetch_postings.py already does this for fresh postings. This script is for older sets, such as the
25-posting comparison set: it adds ai_eng_probability, ai_work_type and office_presence to each posting
and writes the file back. Postings that already have office_presence are left alone, so it is safe to rerun.
Cost: about 0.015 cents per posting.
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))
load_dotenv(ROOT / ".env")

from job_fit.code_nodes.location import commute_problem           # noqa: E402
from job_fit.llm.decisions_api_filter import ask_decisions_filter        # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", type=Path, default=ROOT / "data" / "comparison_set.json")
    args = parser.parse_args()

    postings = json.loads(args.set.read_text(encoding="utf-8"))
    tokens = 0
    for i, p in enumerate(postings, 1):
        if p.get("office_presence"):
            continue
        decisions = ask_decisions_filter(p["title"], p["description"])
        tokens += decisions.pop("input_tokens")
        for key in ("ai_eng_probability", "ai_work_type", "office_presence", "presence_confidence"):
            p[key] = decisions[key]
        gap = commute_problem(p["office_presence"], p.get("locations") or [], p.get("presence_confidence"))
        print(f"[{i}/{len(postings)}] {p['title'][:55]:55} ai_eng {p['ai_eng_probability']:.2f} "
              f"{str(p['ai_work_type']):15} presence {p['office_presence']}{('  LOCATION ' + gap['kind'].upper()) if gap else ''}")
        args.set.write_text(json.dumps(postings, ensure_ascii=False, indent=1), encoding="utf-8")   # save as we go
    print(f"Done. Cost: ${tokens * 0.10 / 1_000_000:.4f}")


if __name__ == "__main__":
    main()
