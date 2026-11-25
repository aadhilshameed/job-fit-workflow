"""One function per provider. Each sends SYSTEM_PROMPT plus one user message and returns the same Assessment.

Copied from the AI Project (payment_tracker/llm.py) and changed only where this project differs:
the input is plain text (no PDFs or images), and the response schema is Assessment.

Every function returns (assessment, usage) with usage = {"input": tokens, "output": tokens}.
For all three providers the output count already includes the thinking tokens, which are billed as output.
effort=None means "send no thinking setting", so the model runs on its default (used for the Gemini 3.6 baseline,
because that is how it ran in n8n).
"""

from anthropic import transform_schema

from .prompt import SYSTEM_PROMPT
from .schema import Assessment

# Haiku's ceiling for thinking + answer together; a ceiling, not a spend (you pay only for what is used).
# 20000 cut off about 1 answer in 15 (24 Nov 2026): Haiku high uses 7,000 to 19,800 tokens, about 800 per
# requirement, and the same posting varies from run to run. A cut-off answer is half a JSON and cannot be read.
MAX_TOKENS = 32000
# the Assessment schema in the shape the API expects. Sent ourselves (instead of output_format=Assessment) so the
# SDK does not parse the answer before we have checked why it stopped
ASSESSMENT_FORMAT = {"type": "json_schema", "schema": transform_schema(Assessment)}

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

# response:        Google's response object (the envelope)
# response.text:   the model's answer, a JSON string
#                  (it follows the Assessment schema, because we passed response_schema=Assessment)
# response.parsed: that JSON string already turned into an Assessment object by the SDK,
#                  or None if it could not be (a broken or cut-off answer)
   
    assessment = response.parsed or Assessment.model_validate_json(response.text)
    u = response.usage_metadata
    usage = {"input": u.prompt_token_count or 0,
             "output": (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)}
    return assessment, usage


def ask_claude(user_message: str, model: str, effort: str | None):
    """Streamed: the answer arrives in pieces while Haiku writes it, so the connection never sits silent for the
    90 seconds a long answer takes (the SDK's advice for long answers). The result is the same as one call.
    Then three checks, in this order, so the error says what really happened:
    1. stop_reason "max_tokens": the answer was cut off at MAX_TOKENS (it cannot be complete JSON)
    2. no text at all
    3. the text must fit Assessment (pydantic raises a ValidationError if it does not)"""
    output_config = {"format": ASSESSMENT_FORMAT}
    if effort:
        output_config["effort"] = effort
    with _client("anthropic").messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        timeout=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        output_config=output_config,
    ) as stream:
        response = stream.get_final_message()       # waits until the end message has arrived, then only it is complete. 
    if response.stop_reason == "max_tokens":
        raise ValueError(f"Answer cut off: Haiku used all {MAX_TOKENS} tokens (thinking + answer) before finishing")
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text:
        raise ValueError(f"No answer text (stop_reason={response.stop_reason})")
    usage = {"input": response.usage.input_tokens, "output": response.usage.output_tokens}
    return Assessment.model_validate_json(text), usage


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
