-- Phase 7: one row per captured turn of a run (Phase 7 spec §3.1). The PostgreSQL sink fills it
-- from the run's turn files when it drains run_ended; the dashboard's turn page reads it.

CREATE TABLE run_turns (
    run_id                      text NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    turn_number                 integer NOT NULL,
    captured_at                 timestamptz NOT NULL,
    model                       text,               -- system/init .model
    subtype                     text,               -- result .subtype
    is_error                    boolean,            -- result .is_error
    num_turns                   integer,            -- result .num_turns (agent iterations)
    input_tokens                bigint,             -- result .usage.*
    cache_creation_input_tokens bigint,
    cache_read_input_tokens     bigint,
    output_tokens               bigint,
    cost_usd                    double precision,   -- result .total_cost_usd
    duration_ms                 bigint,             -- result .duration_ms
    result_text                 text,               -- result .result, first RESULT_TEXT_LIMIT chars
    prompt                      text NOT NULL,      -- turn-N.prompt.md, first PROMPT_LIMIT bytes
    prompt_bytes                integer NOT NULL,   -- the file's size
    stream                      text NOT NULL,      -- turn-N.jsonl, capped (spec §4.1)
    stream_bytes                integer NOT NULL,
    stream_lines                integer NOT NULL,   -- lines in the file
    omitted_lines               integer NOT NULL,   -- lines replaced by a stub
    stderr                      text NOT NULL,      -- turn-N.stderr.log, last STDERR_LIMIT bytes
    stderr_bytes                integer NOT NULL,
    truncated                   boolean NOT NULL,   -- the head cap dropped at least one line
    PRIMARY KEY (run_id, turn_number)
);
