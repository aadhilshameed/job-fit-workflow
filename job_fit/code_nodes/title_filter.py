"""The free keyword filter on the title, ported from n8n ("Filter AI roles"), made more lenient on 19 Nov 2026.

It sorts every search result into keep, check or drop, before any detail call. Everything that is not
dropped goes on to the Decisions API filter (llm/decisions_api_filter.py), which reads the whole posting. Because that
question now does the careful work, this filter only throws out what is clearly off-target, and
lets doubtful titles through:
- a GenAI word in the title (LLM, LLMs, GenAI, agent, chatbot, prompt, generative) is never dropped:
  if a blocked word or occupation is also there, it becomes "check" instead;
- "Artificial Intelligence" counts as an AI word.

The old stage 2 (a "check" posting needed two GenAI words in its text) is gone: the Decisions API filter
replaced it. genai_signals() is kept only so the batch file shows which words a posting contains.
"""

import re

BLOCKED_OCCUPATIONS = ["betriebswirt", "personal", "operations", "marketing", "kaufmann", "kauffrau",
                       "verkäufer", "controller", "designer", "vertrieb", "sales"]
HARD_BLOCKED_TITLE_WORDS = ["content", "marketing", "social media", "grafik", "webdesign", "seo", "sales",
                            "e-commerce", "hubspot"]
# "machine learning" was blocked until 22 Nov 2026; removed after "Werkstudent Python & KI (LLMs / Machine Learning)"
# (HUK-COBURG) turned out to be an LLM role. The Decisions API filter now judges such titles.
HARD_BLOCKED_TITLE_WHOLE_WORDS = ["cad"]
CHECK_TITLE_WORDS = ["qualitätsmanagement", "produktmanagement", "projektmanagement", "innovationsmanagement",
                     "organisationsentwicklung", "unternehmensentwicklung", "wissenstransfer",
                     "innovationsscouting", "arbeitsschutz", "umweltschutz", "datenbank", "support", "sysadmin",
                     "data analytics", "datenanalyse", "business intelligence", "kommunikation", "community",
                     "security", "people & culture"]
CHECK_TITLE_WHOLE_WORDS = ["data", "hr"]
GENAI_WHOLE_WORDS = ["llms?", "genai"]
GENAI_WORD_STARTS = ["generativ", "agent", "chatbot", "prompt"]
AI_WHOLE_WORDS = ["ki", "ai"]
AI_WORD_STARTS = ["künstliche intelligenz", "artificial intelligence", "machine learning", "maschinelles lernen"]

LETTERS = "a-z0-9äöüß"


def has_whole(text: str, word: str) -> bool:
    """The word stands on its own: "ki" matches "KI-Agent" but not "Kiel"."""
    return re.search(rf"(^|[^{LETTERS}]){word}([^{LETTERS}]|$)", text) is not None


def has_start(text: str, word: str) -> bool:
    """A word starts with this: "agent" matches "Agenten" and "agentic"."""
    return re.search(rf"(^|[^{LETTERS}]){word}", text) is not None


def title_status(title: str, occupation: str = "") -> str:
    """Stage 1: "keep", "check" or "drop", from the title text and, where the source has one, the occupation
    (the Arbeitsagentur's hauptberuf). Every source passes its own field, for example
    title_status(job.get("stellenangebotsTitel"), job.get("hauptberuf")) or title_status(posting["title"])."""
    title = (title or "").lower()
    occupation = (occupation or "").lower()

    genai_title = (any(has_whole(title, w) for w in GENAI_WHOLE_WORDS)
                   or any(has_start(title, w) for w in GENAI_WORD_STARTS))
    ai_title = (genai_title or any(has_whole(title, w) for w in AI_WHOLE_WORDS)
                or any(has_start(title, w) for w in AI_WORD_STARTS))
    if not ai_title:
        return "drop"
    blocked = (any(w in occupation for w in BLOCKED_OCCUPATIONS)
               or any(w in title for w in HARD_BLOCKED_TITLE_WORDS)
               or any(has_whole(title, w) for w in HARD_BLOCKED_TITLE_WHOLE_WORDS))
    if genai_title:
        return "check" if blocked else "keep"     # a GenAI word is never thrown away on a title alone
    if blocked:
        return "drop"
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


STUDENT_WORD_STARTS = ["werkstudent", "working student", "studentische", "praktikum", "praktikant", "pflichtpraktikum",
                       "internship"]
STUDENT_WHOLE_WORDS = ["intern", "interns"]       # whole word: not "international" or "internal"


def is_student_role(title: str) -> bool:
    """A role a student can take next to the MSc: Werkstudent, working student, Praktikum or internship.
    Used by run_daily --students-only; thesis and PhD positions are not counted (yet)."""
    text = (title or "").lower()
    return (any(has_start(text, w) for w in STUDENT_WORD_STARTS)
            or any(has_whole(text, w) for w in STUDENT_WHOLE_WORDS))


def genai_signals(description: str) -> list[str]:
    text = (description or "").lower()
    return [name for name, test in SIGNALS.items() if test(text)]


