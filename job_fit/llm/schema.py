"""What the LLM is asked to fill for one job posting.

The same Pydantic models are sent to every provider as the response schema, so every model
must answer in exactly this shape. The verdict is NOT taken from here: code_nodes/code_verdict.py computes it
from the per-requirement labels. model_verdict is kept only to compare with the code's verdict.
"""

from typing import Literal

from pydantic import BaseModel

FoundIn = Literal["requirements_section", "tasks_section", "implied_by_task", "other"]
RequirementType = Literal["technical_skill", "soft_skill", "eligibility"]
Importance = Literal["critical", "standard"]
MatchResult = Literal["match", "partial", "hard_gap", "not_stated"]
RoleFocus = Literal[
    "agentic_ai", "llm_applications", "rag", "ml_engineering", "data_science",
    "robotics", "automation", "software_engineering", "marketing_or_business", "other",
]
Verdict = Literal["apply", "stretch", "skip", "lack_of_info"]


class Requirement(BaseModel):
    requirement_text: str
    found_in: FoundIn
    requirement_type: RequirementType
    importance: Importance
    background_quote: str
    match_reason: str
    match_result: MatchResult


class Assessment(BaseModel):
    # "one to three values" is asked for in the prompt, not enforced in the schema,
    # because not every provider accepts a maximum list length in its schema
    role_focus: list[RoleFocus]
    requirements: list[Requirement]
    model_verdict_reason: str
    #model_verdict comes after the model_verdict_reason --> so the model actually reasons to that answer
    model_verdict: Verdict
