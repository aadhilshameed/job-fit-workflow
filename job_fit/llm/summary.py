"""A short summary of a posting for the alert: what the role is about in one short line, 2 or 3 lines on what
you would do, and every requirement
with a short label, ranked by how much the role depends on it, so the message stays small.

A separate small call, so the frozen assessment prompt (prompt.py) stays untouched. It does not judge the fit:
it gets each requirement's status (met, partial, missing) from the assessment and only shortens and ranks.
Only postings that get sent are summarised (not skips). The preview prints what the summaries cost.
"""

from pydantic import BaseModel

from job_fit.llm.model_calls import _client

SUMMARY_MODEL = "claude-haiku-5-5"
SUMMARY_EFFORT = "high"    # low was cheaper, but failed on a long posting (20 Nov); high costs about 0.1 cent more
SUMMARY_PROMPT_VERSION = "s2"
# s1 (20 Nov): role line, about lines, ranked labels of 1 to 4 words with groups
# s2 (21 Nov): labels more specific, with the posting's examples in brackets, up to 10 words

SUMMARY_PROMPT = """You shorten a job posting for a student who decides whether to open it.
The posting may be in German or English. Always answer in plain English. No full stops.

role: what the role is about, in the form "<kind of work> → <field>", 3 to 8 words in total.
Leave out "working student" and verbs such as supporting, building or developing. Name the field in
1 to 3 words, not a list. Examples: "AI agent systems → aero-engine testing",
"AI automation → safety and compliance", "LLM chatbots → customer service".

about: 2 or 3 short lines, each at most 12 words, on what you would actually do in this job day to day.
Concrete tasks, not company marketing.

requirements: you get a numbered list of requirements from the posting, each with its status
(met, partial, missing, not stated) and, for partial and missing ones, the reason. Return every number
exactly once, ordered from the most to the least important for this role (what the posting stresses
most comes first).
- label: always in English, even when the posting is German. Name the thing itself, without the heading
  words ("Idealerweise praktische Erfahrung mit LangGraph" becomes "LangGraph"). Be specific rather than
  short: when the posting names examples, keep them in brackets ("AI frameworks" becomes "AI frameworks
  (LangChain, LlamaIndex)"). Usually 2 to 6 words; use up to 10 when fewer would lose the meaning.
  Keep combined items together ("Power BI and Power Automate" stays one item).
- group: when several requirements share a heading, give them the same short English heading of 1 to 3
  words, for example "Practical experience" or "Languages". Empty for a requirement that stands alone.
- short_reason: for partial and missing items only, at most 8 words in English on what is lacking,
  taken from the given reason. Empty for the others.

Every label and reason must be complete words and a complete thought: shorten by choosing fewer words,
never by cutting a sentence off.

Only shorten and rank. Do not change any status and do not judge the fit."""

STATUS_WORDS = {"match": "met", "partial": "partial", "hard_gap": "missing", "not_stated": "not stated"}


class RankedRequirement(BaseModel):
    number: int
    label: str
    group: str
    short_reason: str


class PostingSummary(BaseModel):
    role: str
    about: list[str]
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
        max_tokens=16000,        # thinking counts toward this ceiling; it is not a spend
        timeout=300,
        system=SUMMARY_PROMPT,
        messages=[{"role": "user", "content": f"<title>{title}</title>\n<job posting>\n{description}\n</job posting>\n"
                                              f"<requirements>\n{chr(10).join(lines) or '(none)'}\n</requirements>"}],
        output_format=PostingSummary,
        output_config={"effort": SUMMARY_EFFORT},
    )
    if response.parsed_output is None:
        raise ValueError(f"No structured summary (stop_reason={response.stop_reason})")
    return response.parsed_output, {"input": response.usage.input_tokens, "output": response.usage.output_tokens}
