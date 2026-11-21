-- One row per job posting. Filled by the daily run.
-- Comments in SQL start with two dashes (--). Postgres ignores everything after them on that line.
CREATE TABLE postings ( 
    -- from the job site (Arbeitsagentur search and details)
    id  SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    duplicate_ids TEXT[],
    title TEXT NOT NULL,
    company  TEXT NOT NULL,
    city TEXT,
    locations JSONB,
    description TEXT NOT NULL,
    published_on TIMESTAMPTZ,
    found_on TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- from the API decisions filter (relevance.py, OpenAI Decisions API).
    -- All can be empty: if that call fails, the posting still goes on to Haiku (fail-open).
    relevance_error TEXT,            -- shows error message: if the call fails
    ai_eng_probability NUMERIC,      -- 0 to 1: how likely this is an AI engineering related job
    is_it_ai_eng BOOLEAN,            -- yes or no: is the probability at least 0.49 (the threshold)
    ai_work_type TEXT,               -- agentic_ai, ai_automation, ai_engineering or other
    work_type_confidence NUMERIC,    -- 0 to 1: how confident the model is in ai_work_type
    office_presence TEXT,            -- not_mentioned, remote, occasional, weekly or regular_unspecified
    presence_confidence NUMERIC,     -- 0 to 1: confidence on office_presence response
    

    -- from the location code (location.py). All empty when the location is fine.
    location_note TEXT,
    location_gap_type TEXT,          -- skip (too far for the office days) or check (look up the train time)
    location_gap_reason TEXT,        -- one sentence on why: skip or check

    -- rules for the whole table
    UNIQUE (source, source_id)   
    -- source + source id needs to be unique for every row
    -- no comma on the last statement (syntax)
    );


-- One row per Haiku run on one posting. Filled step by step during the daily run:
-- inserted after the assessment, then updated with the summary and the Telegram time.
CREATE TABLE assessments (
    id SERIAL PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id),   -- the posting this run assessed
    assessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),        -- filled in when the row is inserted

    -- the setup: kept so results can be compared when the model or prompt changes
    model TEXT NOT NULL,                 -- for example claude-haiku-5-5
    effort TEXT NOT NULL,                -- low, medium or high
    prompt_version TEXT NOT NULL,        -- PROMPT_VERSION in prompt.py, for example v4

    -- the result: all empty if the call failed (then only the error is filled)
    model_extraction_error TEXT,         -- the error message when the Haiku call failed
    model_verdict TEXT,                  -- the verdict Haiku gave (kept only to compare)
    verdict_reason TEXT,                 -- Haiku's one-sentence reason
    code_verdict TEXT,                   -- the real verdict from verdict.py: apply, stretch, lack_of_info or skip

    -- what the assessment cost
    input_tokens INTEGER,
    output_tokens INTEGER,               -- thinking tokens count here too
    cost_usd NUMERIC,                    -- in US dollars
    time_seconds NUMERIC,                -- how long the call took

    -- the summary for Telegram (summary.py): empty for skips, which get no summary
    summary_model TEXT,
    summary_effort TEXT,
    summary_prompt_version TEXT,
    summary_error TEXT,                  -- the error message when the summary call failed twice
    summary JSONB,                       -- role line, about lines, ranked requirement labels
    summary_input_tokens INTEGER,
    summary_output_tokens INTEGER,
    summary_cost_usd NUMERIC,
    summary_time_seconds NUMERIC,

    sent_to_telegram_at TIMESTAMPTZ      -- empty means never sent (a skip, or a crash before sending)
);