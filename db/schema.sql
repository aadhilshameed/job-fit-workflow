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


