"""The two-stage keyword filter from n8n ("Filter AI roles" and "Check ambiguous roles"), ported line by line.

Stage 1 (title and occupation, before the detail call): keep, check or drop.
Stage 2 (description, after the detail call): a "check" posting stays only if its text has at least
two different GenAI signals. "keep" postings skip stage 2.

This is the free first sieve. The paid relevance question (relevance.py) comes after it.
"""

import re

BLOCKED_OCCUPATIONS = ["betriebswirt", "personal", "operations", "marketing", "kaufmann", "kauffrau",
                       "verkäufer", "controller", "designer", "vertrieb", "sales"]
HARD_BLOCKED_TITLE_WORDS = ["content", "marketing", "social media", "grafik", "webdesign", "seo", "sales",
                            "e-commerce", "hubspot", "machine learning"]
HARD_BLOCKED_TITLE_WHOLE_WORDS = ["cad"]
CHECK_TITLE_WORDS = ["qualitätsmanagement", "produktmanagement", "projektmanagement", "innovationsmanagement",
                     "organisationsentwicklung", "unternehmensentwicklung", "wissenstransfer",
                     "innovationsscouting", "arbeitsschutz", "umweltschutz", "datenbank", "support", "sysadmin",
                     "data analytics", "datenanalyse", "business intelligence", "kommunikation", "community",
                     "security", "people & culture"]
CHECK_TITLE_WHOLE_WORDS = ["data", "hr"]
GENAI_WHOLE_WORDS = ["llm", "genai"]
GENAI_WORD_STARTS = ["generativ", "agent", "chatbot", "prompt"]
AI_WHOLE_WORDS = ["ki", "ai"]
AI_WORD_STARTS = ["künstliche intelligenz"]

LETTERS = "a-z0-9äöüß"


def has_whole(text: str, word: str) -> bool:
    """The word stands on its own: "ki" matches "KI-Agent" but not "Kiel"."""
    return re.search(rf"(^|[^{LETTERS}]){word}([^{LETTERS}]|$)", text) is not None


def has_start(text: str, word: str) -> bool:
    """A word starts with this: "agent" matches "Agenten" and "agentic"."""
    return re.search(rf"(^|[^{LETTERS}]){word}", text) is not None


def title_status(job: dict) -> str:
    """Stage 1: "keep", "check" or "drop", from the title and the occupation (hauptberuf)."""
    title = (job.get("stellenangebotsTitel") or job.get("titel") or "").lower()
    occupation = (job.get("hauptberuf") or job.get("beruf") or "").lower()

    genai_title = (any(has_whole(title, w) for w in GENAI_WHOLE_WORDS)
                   or any(has_start(title, w) for w in GENAI_WORD_STARTS))
    ai_title = (genai_title or any(has_whole(title, w) for w in AI_WHOLE_WORDS)
                or any(has_start(title, w) for w in AI_WORD_STARTS))
    if not ai_title:
        return "drop"
    if any(w in occupation for w in BLOCKED_OCCUPATIONS):
        return "drop"
    if any(w in title for w in HARD_BLOCKED_TITLE_WORDS):
        return "drop"
    if any(has_whole(title, w) for w in HARD_BLOCKED_TITLE_WHOLE_WORDS):
        return "drop"
    if genai_title:
        return "keep"
    ambiguous = (any(w in title for w in CHECK_TITLE_WORDS)
                 or any(has_whole(title, w) for w in CHECK_TITLE_WHOLE_WORDS))
    return "check" if ambiguous else "keep"


# Stage 2: each signal counts once, however often it appears
SIGNALS = {
    "llm": lambda t: has_whole(t, "llms?"),
    "rag": lambda t: has_whole(t, "rag"),
    "gpt": lambda t: has_whole(t, "gpt"),
    "genai": lambda t: has_whole(t, "genai"),
    "language model": lambda t: has_start(t, "large language") or has_start(t, "sprachmodell"),
    "generative": lambda t: has_start(t, "generativ"),
    "agents": lambda t: has_start(t, "agent(?!ur)"),          # "Agentur" (agency) is not an agent
    "prompts": lambda t: has_start(t, "prompt"),
    "chatgpt": lambda t: has_start(t, "chatgpt"),
    "copilot": lambda t: has_start(t, "copilot"),
    "openai": lambda t: has_start(t, "openai"),
    "gemini": lambda t: has_start(t, "gemini"),
    "langchain": lambda t: has_start(t, "langchain") or has_start(t, "langgraph"),
    "chatbot": lambda t: has_start(t, "chatbot"),
}


def genai_signals(description: str) -> list[str]:
    text = (description or "").lower()
    return [name for name, test in SIGNALS.items() if test(text)]


def passes_description_check(description: str) -> bool:
    """Stage 2 for "check" postings: at least two different GenAI signals in the text."""
    return len(genai_signals(description)) >= 2
