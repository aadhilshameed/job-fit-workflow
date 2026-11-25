"""Turns the model's per-requirement labels into the final verdict, in code.

Ported from the n8n "Parse and verdict" node. Same rules, same order:
skip > lack_of_info > stretch > apply.
Two additions (19 Nov 2026): a location result from location.py ("skip": the office is out of reach;
"check": you need to look up the train time, so lack_of_info), and a posting with no requirements at all
is lack_of_info, never apply.
"""

import re

from job_fit.llm.schema import Assessment

# a requirement about a spoken language (German, English, ...). Its partial never counts towards the
# two-partial limit, because a lower language level alone should never stop an application (19 Nov 2026)
LANGUAGE = re.compile(r"deutsch|german|englisch|english|französisch|french|spanisch|spanish|"
                      r"sprachkenntnis|language skill|\b(a1|a2|b1|b2|c1|c2)\b", re.IGNORECASE)
#| means OR


def is_language(requirement_text: str) -> bool:
    return LANGUAGE.search(requirement_text) is not None


def _clean(text: str) -> str:
    """Lower case, and every run of spaces, tabs or line breaks is replaced by one space."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def quote_verification(quote: str, background: str) -> str:
    """no_quote, verified or not_verified.

    The quote may have up to three pieces joined with " | ". Every piece must appear
    word for word in the background (ignoring case, extra spaces and punctuation at its edges).
    """
    if quote.strip().lower() in ("", "none"):
        return "no_quote"   # the prompt tells Haiku to write the word "none" when nothing in the background supports a
                            # requirement (it did so all 217 times by 24 Nov 2026); an empty quote counts the same
    clean_background = _clean(background)   # the whole background, cleaned the same way as the quotes

    different_quote_broken_into_list = re.split(r"\s*\|\s*", quote)
    pieces = [_clean(p).strip(".,;: ") for p in different_quote_broken_into_list]
    pieces = [p for p in pieces if p]
    if pieces and all(p in clean_background for p in pieces):
        return "verified"
    return "not_verified"


def compute_verdict(assessment: Assessment, background: str, commute_problem: dict | None = None) -> dict:
    """Returns the code verdict plus everything needed to explain it.

    commute_problem: None, or {"kind": "skip" or "check", "reason": ...} from location.commute_problem().
    """
    requirements = [r.model_copy() for r in assessment.requirements if r.found_in != "tasks_section"]
    #.model_copy() makes a copy of the each requirements from the response of the model.
    #because we are changing "importance" of the requiremt based on the "found_in" and if were to do it without the model_copy(), it would change the original response of the model.
    #we want to the keep the original response of the model: to evaluate its responses later.
    tasks = [r.requirement_text for r in assessment.requirements if r.found_in == "tasks_section"]
    #we just need the requirement_text and not anything else

    for r in requirements:
        if r.found_in == "implied_by_task":       # a duty never makes a skill critical
            r.importance = "standard"

    quote_statuses = {id(r): quote_verification(r.background_quote, background) for r in requirements}
    #quote_verification returns either "verified", "not_verified" or "no_quote"
    #every object has an id and id(x) gives x's id
    #the content of 2 objects can be same, but the ids will be different.
    #id(r) is used since r is a pydatic object and not actually a dict, so cant just say r 
    
    critical_skills = [r for r in requirements
                       if r.requirement_type == "technical_skill" and r.importance == "critical"]
    # a critical technical skill marked not_stated is treated as a gap (the prompt forbids that label for skills)
    hard_gaps = [r for r in critical_skills if r.match_result in ("hard_gap", "not_stated")]
    partials = [r for r in critical_skills if r.match_result == "partial" and not is_language(r.requirement_text)]
    language_partials = [r for r in critical_skills if r.match_result == "partial" and is_language(r.requirement_text)]
    critical_eligibility = [r for r in requirements
                            if r.requirement_type == "eligibility" and r.importance == "critical"]
    eligibility_gaps = [r for r in critical_eligibility if r.match_result == "hard_gap"]
    # a half-met eligibility (start date, full-time hours, duration) is never apply: at least stretch (25 Nov 2026)
    eligibility_partials = [r for r in critical_eligibility if r.match_result == "partial"]
    missing_eligibility = [r for r in critical_eligibility if r.match_result == "not_stated"]

    location_skip = bool(commute_problem) and commute_problem["kind"] == "skip"
    location_check = bool(commute_problem) and commute_problem["kind"] == "check"

    too_many_gaps = (len(hard_gaps) >= 2 or len(partials) > 2
                     or (len(hard_gaps) == 1 and len(partials) >= 2))   # one missing skill plus two half-met is too much (25 Nov 2026)
    if location_skip or eligibility_gaps or too_many_gaps:
        verdict = "skip"
    elif location_check or missing_eligibility or not requirements:      # nothing to judge is not a fit
        verdict = "lack_of_info"
    elif len(hard_gaps) == 1 or len(partials) == 2 or eligibility_partials:
        verdict = "stretch"
    else:
        verdict = "apply"

    return {
        "code_verdict": verdict,
        "model_verdict": assessment.model_verdict,
        "verdicts_agree": verdict == assessment.model_verdict,
        "model_verdict_reason": assessment.model_verdict_reason,
        "role_focus": assessment.role_focus[:3],
        "critical_gaps": ([f"location ({commute_problem['kind']}): {commute_problem['reason']}"] if commute_problem else [])
                         + [f"{r.match_result}: {r.requirement_text}" for r in eligibility_gaps + hard_gaps + partials]
                         + [f"partial (eligibility): {r.requirement_text}" for r in eligibility_partials]
                         + [f"partial (language, not counted): {r.requirement_text}" for r in language_partials],
        "commute_problem": commute_problem,
        "missing_eligibility": [r.requirement_text for r in missing_eligibility],
        "unverified_quotes": [r.requirement_text for r in requirements
                              if quote_statuses[id(r)] == "not_verified"],
        "quotes_checked": sum(1 for s in quote_statuses.values() if s != "no_quote"),
        "tasks": tasks,
        "requirements": [r.model_dump() | {"quote_status": quote_statuses[id(r)]} for r in requirements],
    }
