"""Send saved results to Telegram, to see how the alerts look. The assessment model is not called again.

    python -m scripts.preview_telegram                        # the summary and the first posting
    python -m scripts.preview_telegram --limit 5              # the summary and the first 5 postings
    python -m scripts.preview_telegram --set data/batches/set_<date>.json --config haiku-high

It reads the results file that compare_models wrote for that set (results/<set name>_<prompt version>.jsonl).
The verdicts are not recomputed, but each sent posting gets its short summary (summary.py, about 0.05 cents).
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

from job_fit.bot.telegram import send_alerts  # noqa: E402
from job_fit.prompt import PROMPT_VERSION     # noqa: E402
from job_fit.summary import summarize         # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", type=Path, default=ROOT / "data" / "batches" / "set_2026-11-19.json")
    parser.add_argument("--config", default="haiku-high")
    parser.add_argument("--limit", type=int, default=1, help="how many posting messages to send after the summary")
    args = parser.parse_args()

    postings = {p["refnr"]: p for p in json.loads(args.set.read_text(encoding="utf-8"))}
    results_file = ROOT / "results" / f"{args.set.stem}_{PROMPT_VERSION}.jsonl"
    rows = [json.loads(line) for line in results_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    latest = {}
    for r in rows:                                    # the last successful answer per posting
        if r["config"] == args.config and not r["error"] and r["refnr"] in postings:
            latest[r["refnr"]] = r["result"]
    items = [(postings[ref], result, None) for ref, result in latest.items()]
    sent = send_alerts(items, note=f"Preview from {results_file.name}", limit=args.limit,
                       summarize=lambda title, text, rows: summarize(title, text, rows)[0])
    print(f"Sent the summary and {sent} posting messages ({len(items)} results in the file).")


if __name__ == "__main__":
    main()
