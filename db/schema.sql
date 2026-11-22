-- One row per job posting. Filled by the daily run.
-- Comments in SQL start with two dashes (--). Postgres ignores everything after them on that line.
CREATE TABLE screened_postings ( 
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

    -- from the API decisions filter (llm/decisions_api_filter.py, OpenAI Decisions API).
    -- All can be empty: if that call fails, the posting still goes on to Haiku (fail-open).
    decisions_call_error TEXT,            -- shows error message: if the call fails
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

    -- added 22 Nov 2026, when a second source arrived
    url TEXT,                        -- the posting's own page (the Telegram button); empty for older Arbeitsagentur rows

    -- rules for the whole table
    UNIQUE (source, source_id)   
    -- source + source id needs to be unique for every row
    -- no comma on the last statement (syntax)
    );


-- One row per Haiku run on one posting. Filled step by step during the daily run:
-- inserted after the assessment, then updated with the summary and the Telegram time.
CREATE TABLE model_verdicts (
    id SERIAL PRIMARY KEY,
    screened_posting_id INTEGER NOT NULL REFERENCES screened_postings(id),   -- the posting this verdict is about
    assessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),        -- filled in when the row is inserted

    -- the setup: kept so results can be compared when the model or prompt changes
    model TEXT NOT NULL,                 -- for example claude-haiku-5-5
    effort TEXT NOT NULL,                -- low, medium or high
    prompt_version TEXT NOT NULL,        -- PROMPT_VERSION in prompt.py, for example v4

    -- the result: all empty if the call failed (then only the error is filled)
    model_extraction_error TEXT,         -- the error message when the Haiku call failed
    model_verdict TEXT,                  -- the verdict Haiku gave (kept only to compare)
    verdict_reason TEXT,                 -- Haiku's one-sentence reason
    code_verdict TEXT,                   -- the real verdict from code_verdict.py: apply, stretch, lack_of_info or skip

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

    sent_to_telegram_at TIMESTAMPTZ,     -- empty means never sent (a skip, or a crash before sending)

    -- filled in by Postgres itself, always the sum of the two calls (the Decisions API's tiny cost is not included).
    -- Last, because ALTER TABLE ... ADD COLUMN always adds at the end, so this matches the live table.
    total_cost_usd NUMERIC GENERATED ALWAYS AS (coalesce(cost_usd, 0) + coalesce(summary_cost_usd, 0)) STORED,
    total_seconds NUMERIC GENERATED ALWAYS AS (coalesce(time_seconds, 0) + coalesce(summary_time_seconds, 0)) STORED
);


-- One row per requirement Haiku extracted for one verdict. A failed Haiku call has no rows here.
CREATE TABLE verdict_requirements (
    id SERIAL PRIMARY KEY,
    model_verdict_id INTEGER NOT NULL REFERENCES model_verdicts(id),   -- the verdict this requirement belongs to
    position_in_req_list INTEGER NOT NULL,           -- 1, 2, 3... the order Haiku listed them in, within this verdict
    requirement_text TEXT NOT NULL,      -- word for word from the posting
    found_in TEXT NOT NULL,              -- requirements_section, tasks_section, implied_by_task or other
    requirement_type TEXT NOT NULL,      -- technical_skill, soft_skill or eligibility
    critical_or_standard TEXT NOT NULL,  -- critical or standard (implied_by_task is always standard, set by code_verdict.py)
    background_quote TEXT,               -- the line from my background that proves it; empty when Haiku said "none"
    quote_status TEXT NOT NULL,          -- verified (found word for word), not_verified or no_quote (set by code_verdict.py)
    match_reason TEXT NOT NULL,          -- Haiku's one sentence on why it is a match or a gap
    match_result TEXT NOT NULL           -- match, partial, hard_gap or not_stated
);


-- One row per button press under a Telegram message. Several per verdict are fine
-- (for example "right" first, "applied" a week later).
CREATE TABLE verdict_feedback (
    id SERIAL PRIMARY KEY,
    model_verdict_id INTEGER NOT NULL REFERENCES model_verdicts(id),   -- the verdict whose Telegram message the button was under
    action TEXT NOT NULL,                -- right, wrong or applied
    pressed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per place text ever looked up with Geoapify, so each place costs one call in total (job_fit/geocode.py).
-- A place Geoapify could not find is kept too (lat and lon NULL), so it is not asked about again every run.
CREATE TABLE place_coordinates (
    id SERIAL PRIMARY KEY,
    place_text TEXT NOT NULL UNIQUE,     -- the place as the source wrote it, lowercased and trimmed ("düsseldorf")
    lat DOUBLE PRECISION,                -- NULL when Geoapify found nothing
    lon DOUBLE PRECISION,
    country_code TEXT,                   -- "de", "fr", ... as Geoapify says
    result_type TEXT,                    -- city, street, building, county, state or country: how exact the point is
    confidence REAL,                     -- Geoapify's 0 to 1 certainty
    found_name TEXT,                     -- Geoapify's full name for the place, to check by eye
    looked_up_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    state_code TEXT                      -- the federal state as Geoapify writes it ("BY" for Bavaria); added 22 Nov 2026
);
