"""One function per provider. Each sends SYSTEM_PROMPT plus one user message and returns the same Assessment.

Copied from the AI Project (payment_tracker/llm.py) and changed only where this project differs:
the input is plain text (no PDFs or images), and the response schema is Assessment.

Every function returns (assessment, usage) with usage = {"input": tokens, "output": tokens}.
For all three providers the output count already includes the thinking tokens, which are billed as output.
effort=None means "send no thinking setting", so the model runs on its default (used for the Gemini 3.6 baseline,
because that is how it ran in n8n).
"""

from .prompt import SYSTEM_PROMPT
from .schema import Assessment

_clients = {}


def _client(provider: str):
    """Create each client on first use, so a missing key only breaks the provider that needs it."""
    if provider not in _clients:
        if provider == "google":
            from google import genai
            _clients[provider] = genai.Client()          # reads GEMINI_API_KEY
        elif provider == "anthropic":
            import anthropic
            _clients[provider] = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY
        elif provider == "openai":
            from openai import OpenAI
            _clients[provider] = OpenAI()                # reads OPENAI_API_KEY
    return _clients[provider]


def ask_gemini(user_message: str, model: str, effort: str | None):
    from google.genai import types

    config = dict(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=Assessment,
    )
    if effort:
        config["thinking_config"] = types.ThinkingConfig(thinking_level=effort.upper())
    response = _client("google").models.generate_content(
        model=model,
        contents=user_message,
        config=types.GenerateContentConfig(**config),
    )
    # response.parsed is filled by the Google SDK; if it is None we parse the text ourselves,
    # which raises an error by itself if the text is broken or cut off
    assessment = response.parsed or Assessment.model_validate_json(response.text)
    u = response.usage_metadata
    usage = {"input": u.prompt_token_count or 0,
             "output": (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)}
    return assessment, usage


def ask_claude(user_message: str, model: str, effort: str | None):
    extra = {"output_config": {"effort": effort}} if effort else {}
    response = _client("anthropic").messages.parse(
        model=model,
        # a ceiling, not a spend: thinking counts toward it (16000 was hit once, the longest answer used 15,850).
        # Above about 21,000 the Anthropic SDK refuses a non-streaming call ("may take longer than 10 minutes"),
        # so 20000 it is, with an explicit 10-minute timeout.
        max_tokens=20000,
        timeout=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        output_format=Assessment,
        **extra,
    )
    if response.parsed_output is None: #response.parsed_output checks if the parsing failed
        raise ValueError(f"No structured answer (stop_reason={response.stop_reason})")
    usage = {"input": response.usage.input_tokens, "output": response.usage.output_tokens}
    return response.parsed_output, usage


def ask_gpt(user_message: str, model: str, effort: str | None):
    extra = {"reasoning": {"effort": effort}} if effort else {}
    response = _client("openai").responses.parse(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=[{"role": "user", "content": user_message}],
        text_format=Assessment,
        **extra,
    )
    if response.output_parsed is None:
        raise ValueError(f"No structured answer (status={response.status})")
    usage = {"input": response.usage.input_tokens, "output": response.usage.output_tokens}
    return response.output_parsed, usage


PROVIDERS = {"google": ask_gemini, "anthropic": ask_claude, "openai": ask_gpt}


def ask_model(user_message: str, provider: str, model: str, effort: str | None):
    """Same call for every provider: returns (Assessment, usage)."""
    return PROVIDERS[provider](user_message, model, effort)
