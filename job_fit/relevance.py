"""Is this posting the kind of job I am looking for? Asked with the OpenAI Decisions API (gpt-6-luna).

The Decisions API answers closed questions with a probability instead of writing text. Input tokens cost
$0.10 per million and output is free, so one posting (about 1,500 tokens) costs about 0.015 cents.
It reads OPENAI_API_KEY from the environment (.env).

Two questions share one request (the posting is billed once):
- is_target_role (yes/no): the probability that the role is hands-on AI building work. This one decides.
- main_focus (choice): what the role is mostly about. Only for reading the results, it decides nothing.
"""

from openai import OpenAI

MODEL = "gpt-6-luna"
THRESHOLD = 0.5   # starting point; set from the test results, not by guessing

TARGET_ROLE_QUESTION = (
    "Is the main work of this role hands-on building with AI: LLM applications, AI agents, RAG or retrieval "
    "systems, AI-powered workflows and automations, or AI and machine learning engineering? "
    "Answer no if AI is only a side topic, or if the role is mainly consulting, coordination, community or "
    "project management, marketing or content, data analytics and BI reporting, IT security, "
    "or general software development."
)

FOCUS_CHOICES = [
    {"value": "agents_llm_apps", "description": "Building AI agents or applications on top of LLMs."},
    {"value": "rag_retrieval", "description": "Retrieval, search or RAG systems."},
    {"value": "ai_automation", "description": "Automating business processes with AI tools or workflows."},
    {"value": "ml_engineering", "description": "Training or deploying machine learning models, data science."},
    {"value": "data_analytics", "description": "Reporting, BI, dashboards, data analysis."},
    {"value": "ai_consulting_or_coordination", "description": "Advising, coordinating, introducing AI tools, workshops."},
    {"value": "software_engineering", "description": "General software development where AI is a side topic."},
    {"value": "it_security", "description": "Cyber security, SIEM, security operations."},
    {"value": "other", "description": "Marketing, content, HR, operations or anything else."},
]

_client = None


def _openai():
    global _client
    if _client is None:
        _client = OpenAI()        # reads OPENAI_API_KEY
    return _client


def check_relevance(title: str, description: str) -> dict:
    """Returns the probability, the yes/no decision, the main focus and the token count."""
    decision = _openai().decisions.create(
        model=MODEL,
        input=f"Job title: {title}\n\nJob posting:\n{description}",
        questions=[
            {"type": "predicate", "name": "is_target_role", "instructions": TARGET_ROLE_QUESTION},
            {"type": "choice", "name": "main_focus",
             "instructions": "What is the main work of this role?", "choices": FOCUS_CHOICES},
        ],
    )
    answers = {a.name: a for a in decision.answers}
    target, focus = answers.get("is_target_role"), answers.get("main_focus")
    if target is None or target.type == "refusal":
        raise ValueError("The Decisions API refused or did not answer is_target_role")
    return {
        "target_probability": round(target.probability, 3),
        "is_target_role": target.probability >= THRESHOLD,
        "main_focus": focus.choice if focus is not None and focus.type == "choice" else None,
        "focus_confidence": round(focus.confidence, 3) if focus is not None and focus.type == "choice" else None,
        "input_tokens": decision.usage.input_tokens,
    }
