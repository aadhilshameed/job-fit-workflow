"""Is this posting the kind of job I am looking for? Asked with a "Decisions API".

Two providers, same questions, same answer shape, so they can be compared (scripts/compare_relevance.py):
- "openai": OpenAI Decisions API, model gpt-6-luna (reads OPENAI_API_KEY)
- "google": Gemini Decisions API, model gemini-decision-1 (reads GEMINI_API_KEY; needs google-genai 3.2 or newer)
Both cost $0.10 per million input tokens, and output is free.

A Decisions API answers closed questions with a probability instead of writing text, so one posting
(about 1,500 tokens) costs about 0.015 cents.

Three questions share one request (the posting is billed once):
- yes_no_target_role: the probability that the role is hands-on AI building work. This one decides:
  THRESHOLD or higher goes to Haiku.
- choice_main_focus: what the role is mostly about. Only for reading the results, it decides nothing.
- choice_office_presence: how often the candidate must be at the office. location.py turns this,
  plus the posting's coordinates and federal state, into a location gap or none.
"""

DEFAULT_PROVIDER = "openai"
QUESTIONS_VERSION = "q2"   # q1: first test; q2: "must live in the region" counts as weekly presence
MODELS = {"openai": "gpt-6-luna", "google": "gemini-decision-1"}
THRESHOLD = 0.49  # target_probability >= 0.49 goes to Haiku (set 19 Nov 2026 from the 25-posting test)

TARGET_ROLE_QUESTION = (
    "Is the main work of this role hands-on building with AI: AI agents (agentic AI), LLM applications, "
    "RAG or retrieval systems, AI-powered workflows and automations, or AI engineering (building software "
    "around AI models: integrating, deploying and evaluating them)? "
    "Answer no if the role is mainly training classic machine learning models or data science, data analytics "
    "or BI reporting, consulting, coordination or workshops, marketing or content, IT security, "
    "or software development where AI is only a side topic."
)

# Only for reading the results: it shows WHY a posting was let through or not. It decides nothing.
FOCUS_CHOICES = [
    {"value": "agentic_ai", "description": "Building AI agents: models that plan and use tools in several steps."},
    {"value": "ai_automation", "description": "Automating business processes with AI tools or workflows (for example n8n)."},
    {"value": "ai_engineering", "description": "Building software around AI models: LLM applications, RAG and "
                                               "retrieval, integration, deployment, evaluation."},
    {"value": "other", "description": "Anything else: classic ML or data science, analytics, consulting, "
                                      "marketing, IT security, general software development."},
]

# The values are what location.py understands; change both together.
PRESENCE_CHOICES = [
    {"value": "not_mentioned", "description": "The posting says nothing about remote work or office days."},
    {"value": "remote", "description": "Fully remote, or office visits are only optional."},
    {"value": "occasional", "description": "Hybrid or mobile work with some office days, a few per month at most, "
                                           "or mostly remote."},
    {"value": "weekly", "description": "Fixed office days every week, mostly on site, no remote work, "
                                       "or the person must live in the city or region."},
    {"value": "regular_unspecified", "description": "Regular presence at the office is required, "
                                                    "without saying how often."},
]

_clients = {}


def _client(provider: str):
    """Create each client on first use, like in llm.py."""
    if provider not in _clients:
        if provider == "openai":
            from openai import OpenAI
            _clients[provider] = OpenAI()                     # reads OPENAI_API_KEY
        else:
            from google import genai
            _clients[provider] = genai.Client(http_options={"api_version": "v1beta"})   # reads GEMINI_API_KEY
    return _clients[provider]


def _ask_openai(text: str) -> dict:
    decision = _client("openai").decisions.create(
        model=MODELS["openai"],
        input=text,
        questions=[
            {"type": "predicate", "name": "yes_no_target_role", "instructions": TARGET_ROLE_QUESTION}, #predicate means yes/no, name-> a label we pick
            {"type": "choice", "name": "choice_main_focus",
             "instructions": "What is the main work of this role?", "choices": FOCUS_CHOICES},
            {"type": "choice", "name": "choice_office_presence",
             "instructions": "How often must the person in this role be physically present at the office?",
             "choices": PRESENCE_CHOICES},
        ],
    )
    answers = {a.name: a for a in decision.answers}      # a list of answers -> a dictionary by name
    target, focus, presence = (answers.get(n) for n in ("yes_no_target_role", "choice_main_focus",
                                                         "choice_office_presence"))
    if target is None or target.type == "refusal":
        raise ValueError("The Decisions API refused or did not answer yes_no_target_role")
    ok = lambda a: a is not None and a.type == "choice"
    return {"probability": target.probability,
            "focus": focus.choice if ok(focus) else None, "focus_confidence": focus.confidence if ok(focus) else None,
            "presence": presence.choice if ok(presence) else None,
            "presence_confidence": presence.confidence if ok(presence) else None,
            "input_tokens": decision.usage.input_tokens}


def _ask_gemini(text: str) -> dict:
    # Gemini takes the questions as a dictionary (the key is the name), the yes/no type is called "noul",
    # and choices are a dictionary of value -> description called "criteria"
    result = _client("google").decisions.create(
        model=MODELS["google"],
        input=text,
        questions={
            "yes_no_target_role": {"type": "noul", "instructions": TARGET_ROLE_QUESTION},
            "choice_main_focus": {"type": "choice", "instructions": "What is the main work of this role?",
                           "criteria": {c["value"]: c["description"] for c in FOCUS_CHOICES}},
            "choice_office_presence": {"type": "choice",
                                "instructions": "How often must the person in this role be physically present at the office?",
                                "criteria": {c["value"]: c["description"] for c in PRESENCE_CHOICES}},
        },
    )
    target, focus, presence = (result.answers.get(n) for n in ("yes_no_target_role", "choice_main_focus",
                                                                "choice_office_presence"))
    if target is None or getattr(target, "noul", None) is None:
        raise ValueError("The Gemini Decisions API did not answer yes_no_target_role")
    usage = result.usage
    tokens = getattr(usage, "total_input_tokens", None) or getattr(usage, "input_tokens", None) or 0
    return {"probability": target.noul,
            "focus": getattr(focus, "choice", None), "focus_confidence": getattr(focus, "confidence", None),
            "presence": getattr(presence, "choice", None),
            "presence_confidence": getattr(presence, "confidence", None),
            "input_tokens": tokens}


ATTEMPTS = 3          # a failed call is tried again twice, 3 seconds apart


def check_relevance(title: str, description: str, provider: str = DEFAULT_PROVIDER) -> dict:
    """Returns the probability, the yes/no decision, the main focus, the office presence and the token count.

    Raises the last error if all attempts fail; the caller decides what to do then (fetch_postings lets
    the posting through, so an API hiccup never drops a job).
    """
    import time
    text = f"Job title: {title}\n\nJob posting:\n{description}"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            a = _ask_openai(text) if provider == "openai" else _ask_gemini(text)
            break
        except Exception:
            if attempt == ATTEMPTS:
                raise
            time.sleep(3)
    rnd = lambda x: round(x, 3) if x is not None else None
    return {
        "target_probability": rnd(a["probability"]),
        "is_target_role": a["probability"] >= THRESHOLD,
        "main_focus": a["focus"],
        "focus_confidence": rnd(a["focus_confidence"]),
        "office_presence": a["presence"],
        "presence_confidence": rnd(a["presence_confidence"]),
        "input_tokens": a["input_tokens"],
    }
