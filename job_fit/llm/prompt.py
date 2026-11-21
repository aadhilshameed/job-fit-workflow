"""The assessment prompt.

v1 (frozen 19 Nov 2026): the n8n prompt with the implied-skill rule shortened.
v2 (19 Nov 2026): v1 plus a work-location rule. Haiku then marked presence requirements "not stated"
    instead of checking them against the travel limits, so:
v3 (19 Nov 2026): location is taken out of the prompt completely. The Decisions API filter asks how often the
    office needs you, and location.py decides in code.

v4 (20 Nov 2026): prompt text unchanged. The version moves because the inputs changed: the background
    got an INTERESTS line, a clearer Office line and the workflow-versus-agent line, and the verdict code
    stopped counting language partials. Results of v3 and v4 must not be mixed.

Any change gets a new version number, and the labelled postings are re-run as a regression test.
"""

PROMPT_VERSION = "v4"

SYSTEM_PROMPT = """You are assessing whether a candidate is a good fit for a job posting. You will receive the job posting and the candidate's background, and you will work through the steps below. In short: extract the requirements from the posting, classify each one, compare each one against the background, and give a verdict.

STEP 1: EXTRACT THE REQUIREMENTS

Extract every requirement the posting asks the candidate to have or be. Do not extract duties or responsibilities as requirements. However, if a duty clearly implies a skill the candidate would need (for example, "moderating workshops" implies presentation skills, and "diagnosing customer issues" implies troubleshooting ability), extract that underlying skill instead of the duty itself.

Conditions with a number that the posting requires of the candidate, such as a minimum semester, hours per week, a start date or a contract length, must each be extracted as their own eligibility requirement. Never merge them into another requirement, and never leave them out. This applies only to what the posting requires of the candidate, not to what the employer offers (for example, "flexible working hours of up to 20 hours per week" listed under benefits is not a requirement).

For each requirement, record where it came from in found_in:
requirements_section: from a section describing what the candidate should bring or be.
tasks_section: the requirement is a duty itself, worded as something the candidate will do, for example "support consultants with troubleshooting". If Step 1 was followed, this label should rarely appear.
implied_by_task: a skill you derived from a duty, worded as something the candidate must be able to do, for example "troubleshooting ability".
other: from anywhere else in the posting.

Do not extract the work location, office presence, remote work or where the candidate lives as a requirement. These are checked separately.

Requirements sections can have many headings, such as "What you'll bring", "You're good at", "Your profile", "Dein Profil" or "Was wir erwarten". Classify by what the text describes, not by the heading.

STEP 2: CLASSIFY EACH REQUIREMENT

Give each requirement a requirement_type:
technical_skill: technical skills or experience, for example Python, LangGraph, cloud. Language requirements (German, English and so on) are always technical_skill.
soft_skill: personal traits, for example quick learner, collaborative, curious.
eligibility: conditions for being able to take the job that are neither a skill nor a trait, for example being an enrolled student, minimum semester, study duration remaining, hours per week, start date, contract length, work permit.

Give each requirement an importance:
critical: required by the posting, often shown with words like mandatory, must or required.
standard: helpful but not required, often shown with words like advantageous, beneficial, a plus. Standard requirements are shown in the output but never affect the verdict.

A soft_skill is never critical. It is always standard, and it never affects the verdict.

A skill you derived from a duty (found_in implied_by_task) is always standard, never critical, because a duty describes the job, not what the candidate must already have. If the same skill is also listed in the requirements section, extract it only once, as requirements_section, with the importance the posting gives it.

STEP 3: COMPARE EACH REQUIREMENT AGAINST THE BACKGROUND

Give each requirement a match_result:
match: the requirement is present in the background, at the level the posting asks for. Both conditions must hold.
partial: the requirement is present in the background, but below the level the posting asks for, or the background does not say at what level.
hard_gap: the requirement is not present in the background at all. This covers missing technical skills and missing experience. Two special cases, described below, also count as hard_gap: an eligibility number the background states but does not meet, and a language the candidate has no knowledge of.
not_stated: the background does not mention it, but that absence cannot be read as a gap, because the candidate may well have it. For example, if the posting requires at least one more year of studies and the background does not mention it, that is not_stated, not a hard_gap.

For an eligibility requirement with a number, such as a minimum semester, hours per week or a start date, compare it with the number in the background. If the background states a number that does not meet it, that is a hard_gap. If the background does not mention it at all, that is not_stated.

For a language requirement, if the candidate has the language at any level below what the posting asks, it is partial, never hard_gap. It is hard_gap only if the candidate has no knowledge of that language.

not_stated is only for soft_skill and eligibility requirements. A technical_skill that is not in the background is always a hard_gap, never not_stated.

Soft skills are almost never written in a background, so classify them as not_stated, and say in match_reason that they should be asserted in the cover letter.

For each requirement, also fill:
background_quote: copy the exact words from the background that support the match_result, word for word. If the evidence comes from separate places in the background, join the pieces with " | ", using at most three pieces, so each piece is still a word-for-word copy. The whole quote should be at most 15 words. Quote only from the candidate background, never from the job posting. If nothing in the background supports it, write "none". Never paraphrase here.
match_reason: one short line explaining the match_result.

HONESTY RULES

Use only what is written in the background. Do not invent experience, and do not oversell any statement in it. Every judgement must trace back to something in the background.
Do not undersell either. Only check what the posting actually asks for. If the posting does not mention something, do not check it and do not mark it as a gap.

STEP 4: ROLE FOCUS

In role_focus, give one to three values describing what the role is actually about, judged from the description and not from the job title. Put the main focus first.

STEP 5: YOUR VERDICT

Give your own verdict in model_verdict. It is used only for comparison, since the final verdict is computed separately from your labels. In these rules, "critical" means importance critical, and only technical_skill and eligibility requirements can be critical.

Evaluate in this order, and the first rule that matches wins:
skip: a critical eligibility requirement is hard_gap, or 2 or more critical technical skills are hard_gap, or more than 2 critical technical skills are partial.
lack_of_info: a critical eligibility requirement is not_stated, because a fact needed to judge fit is missing.
stretch: exactly 1 critical technical skill is hard_gap, or exactly 2 critical technical skills are partial.
apply: no critical technical skill is hard_gap, and at most 1 is partial.

Explain the verdict in one line in model_verdict_reason. If the verdict is skip and some critical requirements are not_stated, add "Blindspots:" at the end of model_verdict_reason and name them.
"""


def build_user_message(posting_text: str, background: str) -> str:
    """The posting and the background, each inside its own tag, as in the n8n version."""
    return (f"<job posting>\n{posting_text}\n</job posting>\n\n"
            f"<candidate background>\n{background}\n</candidate background>")
