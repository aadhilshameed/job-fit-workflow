# job-fit-workflow

An LLM workflow that assesses Werkstudent job postings against my background and returns a structured verdict
(apply, stretch, skip or lack_of_info), with automated intake from the Arbeitsagentur API.
It is a fixed workflow, not an agent: the steps always run in the same order, and the verdict is computed in code.

Status (19 Nov 2026): moved from n8n to Python. Step 1 is a model comparison with the frozen prompt.
Language rule (kept from n8n on purpose): a language at any lower level is partial, so German alone never blocks an application.

## Layout

| Path | What it is |
| --- | --- |
| `job_fit/prompt.py` | The frozen assessment prompt (`SYSTEM_PROMPT`, version v1) |
| `job_fit/schema.py` | The response schema every model must fill (Pydantic) |
| `job_fit/llm.py` | One function per provider (Gemini, OpenAI, Anthropic), same answer shape for all |
| `job_fit/verdict.py` | Quote check and the verdict rules, in code |
| `job_fit/arbeitsagentur.py` | Search and detail calls to the Arbeitsagentur API |
| `job_fit/title_filter.py` | The free two-stage keyword filter from n8n (title, then description) |
| `job_fit/relevance.py` | The relevance question (OpenAI Decisions API): is this hands-on AI building work? |
| `scripts/fetch_postings.py` | Fresh postings through search, filter, details and relevance, saved in `data/batches/` |
| `scripts/compare_models.py` | Runs every config on a set of postings and scores it (`--set` picks the set) |
| `scripts/report.py` | Builds the side-by-side comparison page |
| `data/comparison_set.json` | 25 real postings from the Arbeitsagentur API (Sept to Nov 2026) that pass today's filter |
| `data/background.txt` | My real background. **Not in Git** (see `.gitignore`); `background.example.txt` shows the format |
| `data/labels.json` | The correct verdict per posting, drafted by Claude and checked by me |

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      (then put your keys in .env)
```

## Model comparison

Configs: Gemini 3.6 Flash (baseline, default thinking), Gemini 3.8 Flash high, GPT-6.1 Terra medium and high,
Claude Sonnet 5.5 medium, Claude Haiku 5.5 high. Each one assesses all 30 postings 3 times.

Decision rule, fixed on 19 Nov 2026 before any run:
- Gates: no failed call (one retry allowed), every labelled eligibility gap caught in at least 2 of 3 runs,
  at least 95% of quotes verified, p90 under 60 s.
- Floor: majority verdict correct on at least 24 of 30 postings, and the same verdict in all 3 runs on at least 24.
- Pick: the cheapest config that passes, at 2027 prices. Configs within 10% on cost are a tie, broken by
  correct postings and then p90.

Labelling: the models run first. Postings where the configs disagree get a label drafted by Claude and checked by me.
Postings where all configs agree are checked by Claude against the rules, and I spot-check five of them.

```
python -m scripts.compare_models run --only haiku-high --runs 1    # smoke test first
python -m scripts.compare_models run                               # everything (resumes if stopped)
python -m scripts.compare_models report                            # results/comparison_v1.html
```

## Changes after the first Haiku run (19 Nov 2026)

- Prompt v2: a work location only counts when the posting demands regular presence; the travel limits are in the background.
- Haiku max_tokens 16000 -> 32000 (one posting ran out of room). It is a ceiling, not a spend.
- Terra high and Sonnet dropped: too expensive to ever be the pick.
- 5 postings removed from the set because today's filter would not let them through.
- New: fresh intake and a relevance check before the assessment, so off-target roles never reach Haiku.
