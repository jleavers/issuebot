-- Phase 6: the observability store (roadmap §2.7). Timestamps are timestamptz; every
-- issuebot connection runs with a UTC session time zone.

CREATE TABLE issues (
    number        integer PRIMARY KEY,
    identifier    text NOT NULL,
    title         text NOT NULL,
    state         text,                 -- StateLabel value (todo, in_progress, ...) or NULL
    state_label   text,                 -- the raw label name, or NULL
    github_state  text NOT NULL,        -- open | closed
    url           text NOT NULL,
    labels        text[] NOT NULL DEFAULT '{}',
    pr_number     integer,
    pr_url        text,
    pr_state      text,                 -- open | closed | merged
    pr_merged_at  timestamptz,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,
    closed_at     timestamptz,
    seen_at       timestamptz NOT NULL  -- when this snapshot was observed
);
CREATE INDEX issues_state_idx ON issues (state, updated_at DESC);
CREATE INDEX issues_closed_at_idx ON issues (closed_at) WHERE closed_at IS NOT NULL;

CREATE TABLE runs (
    run_id           text PRIMARY KEY,
    issue_number     integer NOT NULL,
    issue_identifier text NOT NULL,
    attempt          integer NOT NULL DEFAULT 0,
    session_id       text,
    started_at       timestamptz NOT NULL,
    ended_at         timestamptz,
    outcome          text,              -- NULL while running
    error            text,
    turns            integer NOT NULL DEFAULT 0,
    input_tokens     bigint NOT NULL DEFAULT 0,
    output_tokens    bigint NOT NULL DEFAULT 0,
    cost_usd         double precision NOT NULL DEFAULT 0,
    duration_s       double precision,
    workspace_path   text,
    log_dir          text
);
CREATE INDEX runs_started_at_idx ON runs (started_at DESC);
CREATE INDEX runs_issue_idx ON runs (issue_number, started_at DESC);

CREATE TABLE events (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at           timestamptz NOT NULL,
    kind         text NOT NULL,
    issue_number integer,
    run_id       text,
    payload      jsonb NOT NULL         -- Event.to_dict(), kind and at included
);
CREATE INDEX events_at_idx ON events (at DESC);
CREATE INDEX events_issue_idx ON events (issue_number, at DESC);

CREATE TABLE runtime_snapshot (
    id         boolean PRIMARY KEY DEFAULT true CHECK (id),  -- exactly one row
    at         timestamptz NOT NULL,    -- RuntimeSnapshot.at
    written_at timestamptz NOT NULL,    -- now() at the write; the dashboard's snapshot age
    data       jsonb NOT NULL           -- RuntimeSnapshot.to_dict()
);
