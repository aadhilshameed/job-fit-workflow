# Model comparison and version history

This is how Claude Haiku 5.5 was picked for the requirement check, and how the prompt and rules changed on the way.
The main README describes how the pipeline works today.

## Model comparison

Configs: Gemini 3.6 Flash (baseline, default thinking), Gemini 3.8 Flash high, GPT-6.1 Terra medium and high,
Claude Sonnet 5.5 medium, Claude Haiku 5.5 high. Each one assesses all 30 postings 3 times.

Decision rule, fixed on 19 Nov 2026 before any run:
- Gates: no failed call (one retry allowed), every labelled eligibility gap caught in at least 2 of 3 runs,
  at least 95% of quotes verified, p90 under 60 s.
- Floor: majority verdict correct on at least 24 of 30 postings, and the same verdict in all 3 runs on at least 24.
- Pick: the cheapest config that passes, at 2027 prices. Configs within 10% on cost are a tie, broken by
  correct postings and then p90.

Labelling: the models run first, then every posting gets a label checked against the rules.

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
- New: fresh intake and a Decisions API filter before the assessment, so roles that are not AI engineering never reach Haiku.

## Version log

| Version | Date | Prompt text | Background | Verdict code |
| --- | --- | --- | --- | --- |
| v1 | 19 Nov 2026 | n8n prompt, implied-skill rule shortened | as in n8n | as in n8n |
| v2 | 19 Nov 2026 | + location rule | travel limits added | same |
| v3 | 19 Nov 2026 | location rule removed, "do not extract location" | travel lines removed | + location decided in code, empty posting = lack_of_info |
| v4 | 20 Nov 2026 | **unchanged** | + INTERESTS, Office line, workflow-versus-agent line | language partials do not count towards the two-partial limit |

Results of different versions are saved in separate files (`results/<set>_<version>.jsonl`) and never mixed.

From v4 on, every result row also carries a snapshot id. The snapshot (`results/snapshots/<id>.json`) holds the full
prompt, the full background, the verdict code and the git commit the row was made with. The id changes whenever any
of them changes, and the report warns if one results file holds more than one id. To see what changed between two runs:

```
python -m job_fit.snapshot list
python -m job_fit.snapshot diff <old id> <new id>
```

Snapshots stay out of Git, like the background they contain. The prompts of v1 to v3 can be read in the Git history;
the backgrounds of v1 to v3 were not saved, so the table above is the only record of how they differed.
