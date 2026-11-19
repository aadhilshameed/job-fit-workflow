"""Model comparison: every configuration assesses every posting in the comparison set, several times.

Run it from the repo folder:
    python -m scripts.compare_models run            # all configs, 3 runs each (resumes where it stopped)
    python -m scripts.compare_models run --only haiku-high --runs 1   # a smoke test on one config
    python -m scripts.compare_models report         # summary in the terminal + an HTML page in results/
    Add --set data/batches/set_<date>.json to either command to use another set of postings
    (its labels, if any, go in a file next to it named labels_<date>.json).

Same idea as the AI Project screening notebook: every answer is appended to a .jsonl file the moment it
arrives, so a crash or Ctrl+C loses nothing, and running again only does what is missing.
"""

try:
    import truststore
    truststore.inject_into_ssl()   # only needed on Windows (fixes the SSL certificate error)
except ImportError:
    pass

import argparse
import json
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))
load_dotenv(ROOT / ".env")

from job_fit.llm import ask_model                      # noqa: E402
from job_fit.location import location_gap              # noqa: E402
from job_fit.prompt import PROMPT_VERSION, build_user_message   # noqa: E402
from job_fit.verdict import compute_verdict            # noqa: E402

# ---------------------------------------------------------------- the frozen setup

CONFIGS = {
    "gemini36-default": {"provider": "google",    "model": "gemini-3.6-flash",  "effort": None},   # baseline, as in n8n
    "gemini38-high":    {"provider": "google",    "model": "gemini-3.8-flash",  "effort": "high"},
    "terra-medium":     {"provider": "openai",    "model": "gpt-6.1-terra",     "effort": "medium"},
    # dropped on 19 Nov 2026 as too expensive to ever be the pick:
    # "terra-high":       {"provider": "openai",    "model": "gpt-6.1-terra",     "effort": "high"},
    # "sonnet-medium":    {"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "medium"},
    "haiku-high":       {"provider": "anthropic", "model": "claude-haiku-5-5",  "effort": "high"},
}

# dollars per 1 million tokens: (input, output). Thinking tokens are billed as output.
# From the AI Project notebook (checked 9 Nov 2026); Gemini 3.6 Flash checked on OpenRouter on 19 Nov 2026.
PRICES = {
    "gemini-3.6-flash":  (0.75, 3.75),
    "gemini-3.8-flash":  (0.75, 3.75),
    "gpt-6.1-terra":     (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5":  (0.10, 0.50),
}
PRICES_FROM_2027 = PRICES | {"gemini-3.8-flash": (1.50, 7.50)}   # Gemini 3.8 doubles on 1 Jan 2027

# decision rule, fixed before any run (19 Nov 2026)
MIN_QUOTES_VERIFIED = 95      # percent of quotes found word for word in the background
MAX_P90_SECONDS = 60
MIN_CORRECT = 24              # of 30 postings: majority verdict equals the label
MIN_STABLE = 24               # of 30 postings: the same verdict in every run
COST_TIE = 0.10               # configs within 10% of each other on cost count as a tie
RULE = (f"Gates: no failed call, every labelled eligibility gap caught (skip) in at least 2 of 3 runs, "
        f"at least {MIN_QUOTES_VERIFIED}% of quotes verified, p90 under {MAX_P90_SECONDS} s. "
        f"Floor: majority verdict correct on at least {MIN_CORRECT} postings and the same verdict in every run "
        f"on at least {MIN_STABLE}. Pick: the cheapest config that passes (2027 prices); configs within "
        f"{int(COST_TIE * 100)}% on cost are a tie, broken by correct postings, then p90.")

DATA = ROOT / "data"
DEFAULT_SET = DATA / "comparison_set.json"
RESULTS = None     # set in main(): results/<set name>_<prompt version>.jsonl
MAX_ATTEMPTS = 2              # one retry, as the gate allows

_write_lock = threading.Lock()


def load_inputs(set_path=DEFAULT_SET):
    postings = json.loads(set_path.read_text(encoding="utf-8"))
    background = (DATA / "background.txt").read_text(encoding="utf-8")
    labels_path = (DATA / "labels.json" if set_path.resolve() == DEFAULT_SET.resolve()
                   else set_path.with_name(set_path.name.replace("set_", "labels_", 1)))
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.exists() else {}
    return postings, background, labels


def load_rows():
    if not RESULTS.exists():
        return []
    return [json.loads(line) for line in RESULTS.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- running

def assess_once(posting, background, name, run):
    cfg = CONFIGS[name]
    user_message = build_user_message(posting["description"], background)
    error, result, usage, attempts = None, None, {"input": 0, "output": 0}, 0
    start = time.perf_counter()
    for attempts in range(1, MAX_ATTEMPTS + 1):
        start = time.perf_counter()               # time only the attempt that counts
        try:
            assessment, usage = ask_model(user_message, cfg["provider"], cfg["model"], cfg["effort"])
            gap = location_gap(posting.get("office_presence"), posting.get("locations") or [])
            result, error = compute_verdict(assessment, background, gap), None
            break
        except Exception as e:                    # noqa: BLE001  (any failure is recorded, not hidden)
            error = f"{type(e).__name__}: {e}"[:500]
            time.sleep(10)
    seconds = time.perf_counter() - start
    return {"config": name, "model": cfg["model"], "effort": cfg["effort"], "prompt": PROMPT_VERSION,
            "refnr": posting["refnr"], "run": run, "attempts": attempts, "error": error,
            "result": result, "input_tokens": usage["input"], "output_tokens": usage["output"],
            "seconds": round(seconds, 2)}


def run_config(name, postings, background, runs, done):
    todo = [(run, p) for run in range(1, runs + 1) for p in postings if (name, p["refnr"], run) not in done]
    for i, (run, posting) in enumerate(todo, 1):
        row = assess_once(posting, background, name, run)
        with _write_lock:
            with open(RESULTS, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            status = "ERROR " + row["error"][:80] if row["error"] else row["result"]["code_verdict"]
            print(f"[{name:16} {i}/{len(todo)}] run {run} {posting['refnr']:28} {status}  {row['seconds']:.1f} s")


def cmd_run(args):
    postings, background, _ = load_inputs(args.set)
    missing = [p["refnr"] for p in postings if not p.get("office_presence")]
    if missing:
        print(f"WARNING: {len(missing)} postings have no office_presence, so no location check for them. "
              f"Run first: python -m scripts.check_relevance --set {args.set}")
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    done = {(r["config"], r["refnr"], r["run"]) for r in load_rows() if not r["error"]}
    names = args.only or list(CONFIGS)
    print(f"{len(postings)} postings, {len(names)} configs, {args.runs} runs, prompt {PROMPT_VERSION}")
    # one thread per config: configs run side by side, each one goes through its postings in order
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = [pool.submit(run_config, n, postings, background, args.runs, done) for n in names]
        for f in futures:
            f.result()
    print("Done. Now run: python -m scripts.compare_models report")


# ---------------------------------------------------------------- scoring

def cost(row, prices):
    p_in, p_out = prices[row["model"]]
    return (row["input_tokens"] * p_in + row["output_tokens"] * p_out) / 1_000_000


def latest_rows(rows):
    """If a call was repeated (an error first, then a success), keep the last answer per config, posting and run."""
    latest = {}
    for r in rows:
        latest[(r["config"], r["refnr"], r["run"])] = r
    return list(latest.values())


def summarize(name, rows, postings, labels):
    mine = [r for r in rows if r["config"] == name]
    if not mine:
        return None
    ok = [r for r in mine if not r["error"]]
    verdicts = {p["refnr"]: [r["result"]["code_verdict"] if not r["error"] else "error"
                             for r in sorted(mine, key=lambda r: r["run"]) if r["refnr"] == p["refnr"]]
                for p in postings}
    majority = {}
    for refnr, vs in verdicts.items():
        top = Counter(vs).most_common()
        majority[refnr] = top[0][0] if top and (len(top) == 1 or top[0][1] > top[1][1]) else "no majority"

    labelled = [p["refnr"] for p in postings if p["refnr"] in labels]
    correct = sum(1 for ref in labelled if majority[ref] == labels[ref]["verdict"])
    stable = sum(1 for vs in verdicts.values() if len(vs) > 1 and len(set(vs)) == 1 and "error" not in vs)
    elig = [ref for ref in labelled if labels[ref].get("eligibility_gap")]
    # caught = skip in most runs (2 of 3, or 1 of 1 while only run 1 exists)
    elig_caught = sum(1 for ref in elig if verdicts[ref].count("skip") * 2 > len(verdicts[ref]))
    quotes = sum(r["result"]["quotes_checked"] for r in ok)
    unverified = sum(len(r["result"]["unverified_quotes"]) for r in ok)
    seconds = sorted(r["seconds"] for r in ok) or [0.0]
    p90 = statistics.quantiles(seconds, n=10, method="inclusive")[-1] if len(seconds) > 1 else seconds[0]

    s = {
        "config": name, "calls": len(mine), "errors": len(mine) - len(ok),
        "retried": sum(1 for r in mine if r["attempts"] > 1),
        "correct": correct, "labelled": len(labelled),
        "stable": stable, "postings": len(postings),
        "elig_caught": f"{elig_caught}/{len(elig)}", "elig_ok": elig_caught == len(elig),
        "quotes_verified_pct": 100 * (quotes - unverified) / quotes if quotes else 100.0,
        "agree_pct": 100 * sum(r["result"]["verdicts_agree"] for r in ok) / len(ok) if ok else 0.0,
        "cost_now": statistics.mean(cost(r, PRICES) for r in mine),
        "total_spend": sum(cost(r, PRICES) for r in mine),
        "cost_2027": statistics.mean(cost(r, PRICES_FROM_2027) for r in mine),
        "median_s": statistics.median(seconds), "p90_s": p90,
        "verdicts": verdicts, "majority": majority,
    }
    s["gates"] = (s["errors"] == 0 and s["elig_ok"] and s["quotes_verified_pct"] >= MIN_QUOTES_VERIFIED
                  and s["p90_s"] < MAX_P90_SECONDS)
    s["floor"] = correct >= MIN_CORRECT and stable >= MIN_STABLE
    s["passes"] = s["gates"] and s["floor"] and len(labelled) == len(postings)
    return s


def pick(summary):
    passing = sorted((s for s in summary if s["passes"]), key=lambda s: s["cost_2027"])
    if not passing:
        return None
    cheapest = passing[0]["cost_2027"]
    tied = [s for s in passing if s["cost_2027"] <= cheapest * (1 + COST_TIE)]
    return sorted(tied, key=lambda s: (-s["correct"], s["p90_s"]))[0]["config"]


def cmd_report(args):
    from scripts.report import build_report

    postings, _, labels = load_inputs(args.set)
    rows = [r for r in latest_rows(load_rows()) if r["refnr"] in {p["refnr"] for p in postings}]
    summary = [s for s in (summarize(n, rows, postings, labels) for n in CONFIGS) if s]
    chosen = pick(summary)

    print(RULE, "\n")
    print(f"{'config':17} {'calls':>5} {'err':>4} {'correct':>8} {'stable':>7} {'elig':>5} {'quotes':>7} "
          f"{'agree':>6} {'$/posting':>10} {'$ 2027':>9} {'median':>7} {'p90':>6}  rule")
    for s in summary:
        print(f"{s['config']:17} {s['calls']:5} {s['errors']:4} {s['correct']:>3}/{s['labelled']:<4} "
              f"{s['stable']:>3}/{s['postings']:<3} {s['elig_caught']:>5} {s['quotes_verified_pct']:6.1f}% "
              f"{s['agree_pct']:5.0f}% {s['cost_now']:10.5f} {s['cost_2027']:9.5f} {s['median_s']:6.1f}s "
              f"{s['p90_s']:5.1f}s  {'pass' if s['passes'] else 'fail'}")
    if len(labels) < len(postings):
        print(f"\nOnly {len(labels)} of {len(postings)} postings are labelled, so no config can pass yet.")
    print(f"\nSpend: " + ", ".join(f"{s['config']} ${s['total_spend']:.2f}" for s in summary)
          + f"; total ${sum(s['total_spend'] for s in summary):.2f}")
    failed = [s for s in summary if s["errors"]]
    if failed:
        print("WARNING: failed calls: " + ", ".join(f"{s['config']} {s['errors']}" for s in failed))
    print("\nRule picks:", chosen or "nothing passes")

    page = build_report(summary, postings, rows, labels, list(CONFIGS), RULE, chosen,
                        RESULTS.with_suffix(".html"))
    print("Open this file in your browser:", page)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--only", nargs="*", choices=list(CONFIGS))
    report = sub.add_parser("report")
    for p in (run, report):
        p.add_argument("--set", type=Path, default=DEFAULT_SET, help="which postings (default: data/comparison_set.json)")
    args = parser.parse_args()
    global RESULTS
    set_name = "comparison" if args.set.resolve() == DEFAULT_SET.resolve() else args.set.stem
    RESULTS = ROOT / "results" / f"{set_name}_{PROMPT_VERSION}.jsonl"
    {"run": cmd_run, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    main()
