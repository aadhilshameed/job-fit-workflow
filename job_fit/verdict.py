"""Turns the model's per-requirement labels into the final verdict, in code.

Ported from the n8n "Parse and verdict" node. Same rules, same order:
skip > lack_of_info > stretch > apply.
"""

import re

from .schema import Assessment


def _clean(text: str) -> str:
    """Lower case, and every run of spaces or line breaks becomes one space."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def quote_status(quote: str, background: str) -> str:
    """no_quote, verified or not_verified.

    The quote may have up to three pieces joined with " | ". Every piece must appear
    word for word in the background (ignoring case, extra spaces and punctuation at its edges).
    """
    if quote.strip().lower() == "none":
        return "no_quote"
    clean_background = _clean(background)
    pieces = [_clean(p).strip(".,;: ") for p in re.split(r"\s*\|\s*", quote)]
    pieces = [p for p in pieces if p]
    if pieces and all(p in clean_background for p in pieces):
        return "verified"
    return "not_verified"


def compute_verdict(assessment: Assessment, background: str) -> dict:
    """Returns the code verdict plus everything needed to explain it."""
    requirements = [r.model_copy() for r in assessment.requirements if r.found_in != "tasks_section"]
    tasks = [r.requirement_text for r in assessment.requirements if r.found_in == "tasks_section"]

    for r in requirements:
        if r.found_in == "implied_by_task":       # a duty never makes a skill critical
            r.importance = "standard"

    statuses = {id(r): quote_status(r.background_quote, background) for r in requirements}

    critical_skills = [r for r in requirements
                       if r.requirement_type == "technical_skill" and r.importance == "critical"]
    # a critical technical skill marked not_stated is treated as a gap (the prompt forbids that label for skills)
    hard_gaps = [r for r in critical_skills if r.match_result in ("hard_gap", "not_stated")]
    partials = [r for r in critical_skills if r.match_result == "partial"]
    critical_eligibility = [r for r in requirements
                            if r.requirement_type == "eligibility" and r.importance == "critical"]
    eligibility_gaps = [r for r in critical_eligibility if r.match_result == "hard_gap"]
    missing_eligibility = [r for r in critical_eligibility if r.match_result == "not_stated"]

    if eligibility_gaps or len(hard_gaps) >= 2 or len(partials) > 2:
        verdict = "skip"
    elif missing_eligibility:
        verdict = "lack_of_info"
    elif len(hard_gaps) == 1 or len(partials) == 2:
        verdict = "stretch"
    else:
        verdict = "apply"

    return {
        "code_verdict": verdict,
        "model_verdict": assessment.model_verdict,
        "verdicts_agree": verdict == assessment.model_verdict,
        "model_verdict_reason": assessment.model_verdict_reason,
        "role_focus": assessment.role_focus[:3],
        "critical_gaps": [f"{r.match_result}: {r.requirement_text}"
                          for r in eligibility_gaps + hard_gaps + partials],
        "missing_eligibility": [r.requirement_text for r in missing_eligibility],
        "unverified_quotes": [r.requirement_text for r in requirements
                              if statuses[id(r)] == "not_verified"],
        "quotes_checked": sum(1 for s in statuses.values() if s != "no_quote"),
        "tasks": tasks,
        "requirements": [r.model_dump() | {"quote_status": statuses[id(r)]} for r in requirements],
    }
