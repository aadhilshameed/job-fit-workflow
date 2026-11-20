"""A short summary of a posting for the alert: what the role is about in one short line, and every requirement
with a short label, ranked by how much the role depends on it, so the message stays small.

A separate small call, so the frozen assessment prompt (prompt.py) stays untouched. It does not judge the fit:
it gets each requirement's status (met, partial, missing) from the assessment and only shortens and ranks.
Only postings that get sent are summarised (not skips). Cost: about 0.05 cents per posting.
"""

from pydantic import BaseModel

from job_fit.llm import _client

SUMMARY_MODEL = "claude-haiku-5-5"

SUMMARY_PROMPT = """You shorten a job posting for a student who decides whether to open it.
The posting may be in German or English. Always answer in plain English. No full stops.

role: what the role is about, in the form "<kind of work> → <field>", 3 to 8 words in total.
Leave out "working student" and verbs such as supporting, building or developing. Name the field in
1 to 3 words, not a list. Examples: "AI agent systems → aero-engine testing",
"AI automation → safety and compliance", "LLM chatbots → customer service".

requirements: you get a numbered list of requirements from the posting, each with its status
(met, partial, missing, not stated) and, for partial and missing ones, the reason. Return every number
exactly once, ordered from the most to the least important for this role (what the posting stresses
most comes first).
- label: 1 to 4 words in English. Keep combined items together ("Power BI and Power Automate" stays one item).
- short_reason: for partial and missing items only, at most 8 words on what is lacking, taken from the
  given reason. Empty for the others.

Only shorten and rank. Do not change any status and do not judge the fit."""

STATUS_WORDS = {"match": "met", "partial": "partial", "hard_gap": "missing", "not_stated": "not stated"}


class RankedRequirement(BaseModel):
    number: int
    label: str
    short_reason: str


class PostingSummary(BaseModel):
    role: str
    requirements: list[RankedRequirement]


def summarize(title: str, description: str, requirements: list[dict]) -> tuple[PostingSummary, dict]:
    """Returns (summary, usage).
    requirements: [{"text", "match_result", "match_reason"}, ...] from the assessment, numbered from 1 in this order."""
    lines = []
    for i, r in enumerate(requirements, 1):
        status = STATUS_WORDS.get(r["match_result"], r["match_result"])
        reason = f" | reason: {r['match_reason']}" if r["match_result"] in ("partial", "hard_gap") else ""
        lines.append(f"{i}. [{status}] {r['text']}{reason}")
    response = _client("anthropic").messages.parse(
        model=SUMMARY_MODEL,
        max_tokens=1500,
        timeout=120,
        system=SUMMARY_PROMPT,
        messages=[{"role": "user", "content": f"<title>{title}</title>\n<job posting>\n{description}\n</job posting>\n"
                                              f"<requirements>\n{chr(10).join(lines) or '(none)'}\n</requirements>"}],
        output_format=PostingSummary,
        output_config={"effort": "low"},     # a summary needs no deep thinking
    )
    if response.parsed_output is None:
        raise ValueError(f"No structured summary (stop_reason={response.stop_reason})")
    return response.parsed_output, {"input": response.usage.input_tokens, "output": response.usage.output_tokens}
