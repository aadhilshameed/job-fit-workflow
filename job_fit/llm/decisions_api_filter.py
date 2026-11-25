"""Is this posting the kind of job I am looking for? Asked with a "Decisions API".

Two providers, same questions, same answer shape, so they can be compared (scripts/compare_decisions_apis.py):
- "openai": OpenAI Decisions API, model gpt-6-luna (reads OPENAI_API_KEY)
- "google": Gemini Decisions API, model gemini-decision-1 (reads GEMINI_API_KEY; needs google-genai 3.2 or newer)
Both cost $0.10 per million input tokens, and output is free.

A Decisions API answers closed questions with a probability instead of writing text, so one posting
(about 1,500 tokens) costs about 0.015 cents.

Three questions share one request (the posting is billed once):
- yes_no_ai_eng_role: the probability that the role is hands-on AI building work. This one decides:
  THRESHOLD or higher goes to Haiku.
- choice_ai_work_type: what the role is mostly about. Only for reading the results, it decides nothing.
- choice_office_presence: how often the candidate must be at the office. location.py turns this,
  plus the posting's coordinates and federal state, into a location gap or none.
"""

DEFAULT_PROVIDER = "openai"
QUESTIONS_VERSION = "q3"   # q1: first test; q2: "must live in the region" counts as weekly presence;
                           # q3 (25 Nov 2026): one fixed office day a week is its own answer (one_day_weekly)
MODELS = {"openai": "gpt-6-luna", "google": "gemini-decision-1"}
THRESHOLD = 0.49  # ai_eng_probability >= 0.49 goes to Haiku (set 19 Nov 2026 from the 25-posting test)

AI_ENG_ROLE_QUESTION = (
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
    {"value": "occasional", "description": "Hybrid with office days less than once a week: a few days per month at most."},
    {"value": "one_day_weekly", "description": "Exactly one fixed office day every week, otherwise remote "
                                               "(for example 'one office day a week', '1 day on site per week')."},
    {"value": "weekly", "description": "Two or more office days every week, mostly or fully on site, "
                                       "or the person must live in the city or region."},
    {"value": "regular_unspecified", "description": "Regular presence at the office is required, "
                                                    "without saying how often."},
]

_clients = {}


def _client(provider: str):
    """Create each client on first use, like in model_calls.py."""
    if provider not in _clients:
        #client is an object that holds the api key, setting and a pool of connection it keeps open and reuses
        #it is created once and reused
        if provider == "openai":
            from openai import OpenAI
            _clients[provider] = OpenAI()                     # reads OPENAI_API_KEY
        else:
            from google import genai
            _clients[provider] = genai.Client(http_options={"api_version": "v1beta"})   # reads GEMINI_API_KEY
    return _clients[provider]

#.get for dicts 
#getattr for objects

def _ask_openai(text: str) -> dict:
    decision = _client("openai").decisions.create(
        model=MODELS["openai"],
        input=text,
        questions=[
            {"type": "predicate", "name": "yes_no_ai_eng_role", "instructions": AI_ENG_ROLE_QUESTION}, #predicate means yes/no, name-> a label we pick
            {"type": "choice", "name": "choice_ai_work_type",
             "instructions": "What is the main work of this role?", "choices": FOCUS_CHOICES},
            {"type": "choice", "name": "choice_office_presence",
             "instructions": "How often must the person in this role be physically present at the office?",
             "choices": PRESENCE_CHOICES},
        ],
    )
    answers = {a.name: a for a in decision.answers}
    ai_eng, focus, presence = (answers.get(n) for n in ("yes_no_ai_eng_role", "choice_ai_work_type",
                                                         "choice_office_presence"))
    if ai_eng is None or ai_eng.type == "refusal":
        raise ValueError("The Decisions API refused or did not answer yes_no_ai_eng_role")
    ok = lambda a: a is not None and a.type == "choice"
    return {"probability": ai_eng.probability,
            "focus": focus.choice if ok(focus) else None, "work_type_confidence": focus.confidence if ok(focus) else None,
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
            "yes_no_ai_eng_role": {"type": "noul", "instructions": AI_ENG_ROLE_QUESTION},
            "choice_ai_work_type": {"type": "choice", "instructions": "What is the main work of this role?",
                           "criteria": {c["value"]: c["description"] for c in FOCUS_CHOICES}},
            "choice_office_presence": {"type": "choice",
                                "instructions": "How often must the person in this role be physically present at the office?",
                                "criteria": {c["value"]: c["description"] for c in PRESENCE_CHOICES}},
        },
    )
    #here choices are a dict of dicts .. our choices are a list of dicts.. hence this
    ai_eng, focus, presence = (result.answers.get(n) for n in ("yes_no_ai_eng_role", "choice_ai_work_type",
                                                                "choice_office_presence"))
    if ai_eng is None or getattr(ai_eng, "noul", None) is None:
        #getattr(ai_eng, "noul", None) -> means focus.choice, or None if focus has no choice field
        raise ValueError("The Gemini Decisions API did not answer yes_no_ai_eng_role")
    usage = result.usage
    tokens = getattr(usage, "total_input_tokens", None) or getattr(usage, "input_tokens", None) or 0
    return {"probability": ai_eng.noul,
            "focus": getattr(focus, "choice", None), "work_type_confidence": getattr(focus, "confidence", None),
            "presence": getattr(presence, "choice", None),
            "presence_confidence": getattr(presence, "confidence", None),
            "input_tokens": tokens}

            # we are doing this getattr method since Gemini has no refusal thing like OpenAI


ATTEMPTS = 3          # a failed call is tried again twice, 3 seconds apart


def ask_decisions_filter(title: str, description: str, provider: str = DEFAULT_PROVIDER) -> dict:
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
        "ai_eng_probability": rnd(a["probability"]),
        "is_it_ai_eng": a["probability"] >= THRESHOLD,
        "ai_work_type": a["focus"],
        "work_type_confidence": rnd(a["work_type_confidence"]),
        "office_presence": a["presence"],
        "presence_confidence": rnd(a["presence_confidence"]),
        "input_tokens": a["input_tokens"],
    }
