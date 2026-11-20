"""A record of exactly what went into a model run, so every result can be traced back to its inputs.

Each distinct setup is saved once, as results/snapshots/<id>.json, with:
- the full system prompt and its version
- the full background text (data/background.txt is not in git, so this is the only record of old versions)
- the code that turns an answer into a verdict (schema.py, verdict.py, location.py, llm.py)
- the relevance questions version, the git commit, and whether there were uncommitted changes
Every result row stores the id under "snapshot".

The id is a hash of the prompt, the background and that code. Change any of them, even by one character,
and the id changes, so a forgotten PROMPT_VERSION bump can no longer mix two setups without a trace
(the report warns when one results file holds more than one id).

The snapshots live in results/, which is gitignored, because they contain the background.

See what changed between two snapshots:
    python -m job_fit.snapshot diff <id> <id>
    python -m job_fit.snapshot list
"""

import difflib
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from job_fit.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from job_fit.relevance import QUESTIONS_VERSION

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "results" / "snapshots"
CODE_FILES = ("schema.py", "verdict.py", "location.py", "llm.py")   # in job_fit/


def _git(*args) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def save_snapshot(background: str) -> str:
    """Save the current setup (once) and return its id."""
    code = {name: (ROOT / "job_fit" / name).read_text(encoding="utf-8") for name in CODE_FILES}
    hashed = "\n\0\n".join([SYSTEM_PROMPT, background, *code.values()])
    snap_id = hashlib.sha256(hashed.encode("utf-8")).hexdigest()[:10]
    path = FOLDER / f"{snap_id}.json"
    if not path.exists():
        FOLDER.mkdir(parents=True, exist_ok=True)
        status = _git("status", "--porcelain", "--", "job_fit")
        snapshot = {"id": snap_id, "saved": datetime.now().isoformat(timespec="seconds"),
                    "prompt_version": PROMPT_VERSION, "questions_version": QUESTIONS_VERSION,
                    "git_commit": _git("rev-parse", "--short", "HEAD"),
                    "uncommitted_changes": bool(status) if status is not None else None,
                    "system_prompt": SYSTEM_PROMPT, "background": background, "code": code}
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    return snap_id


def load(snap_id: str) -> dict:
    return json.loads((FOLDER / f"{snap_id}.json").read_text(encoding="utf-8"))


def diff(a: str, b: str) -> str:
    """What changed from snapshot a to snapshot b, part by part."""
    old, new = load(a), load(b)
    parts = [("system prompt", old["system_prompt"], new["system_prompt"]),
             ("background", old["background"], new["background"])]
    parts += [(f"job_fit/{n}", old["code"].get(n, ""), new["code"].get(n, "")) for n in CODE_FILES]
    out = [f"{a} ({old['prompt_version']}, saved {old['saved']}) -> {b} ({new['prompt_version']}, saved {new['saved']})"]
    for name, x, y in parts:
        lines = list(difflib.unified_diff(x.splitlines(), y.splitlines(), name, name, lineterm="", n=1))
        out.append(f"\n=== {name}: " + ("no change" if not lines else "changed"))
        out.extend(lines[2:])
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "diff":
        print(diff(sys.argv[2], sys.argv[3]))
    elif len(sys.argv) == 2 and sys.argv[1] == "list":
        for f in sorted(FOLDER.glob("*.json"), key=lambda f: json.loads(f.read_text(encoding="utf-8"))["saved"]):
            s = json.loads(f.read_text(encoding="utf-8"))
            print(f"{s['id']}  {s['prompt_version']}  saved {s['saved']}  commit {s['git_commit']}"
                  f"{'  (uncommitted changes)' if s['uncommitted_changes'] else ''}")
    else:
        print("python -m job_fit.snapshot list | diff <id> <id>")
