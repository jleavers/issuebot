-- One dashboard for every repository (spec 2026-09-10 §3): a repos registry and a repo
-- column on every table. The guard first: these columns are NOT NULL with no default, and a
-- migration cannot know which repository existing rows belong to, so a database that holds
-- any refuses the upgrade and points at the import command. runtime_snapshot is not part of
-- the guard: its one row is replaced by the new table, not stamped.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM issues)
       OR EXISTS (SELECT 1 FROM runs)
       OR EXISTS (SELECT 1 FROM events) THEN
        RAISE EXCEPTION 'issues, runs or events already hold rows and 0003 cannot tell which repository they belong to; give the hub a fresh database and copy these in with the import command';
    END IF;
END $$;

CREATE TABLE repos (
    repo          text PRIMARY KEY,      -- owner/name, as github.repo
    labels        jsonb NOT NULL,        -- GitHubLabels.model_dump(): five roles + no_fault
    workflow_path text,                  -- the worker's, for the operator's orientation
    registered_at timestamptz NOT NULL,  -- first registration
    seen_at       timestamptz NOT NULL   -- latest registration
);

ALTER TABLE issues ADD COLUMN repo text NOT NULL;
ALTER TABLE issues DROP CONSTRAINT issues_pkey;
ALTER TABLE issues ADD PRIMARY KEY (repo, number);
DROP INDEX issues_state_idx;
DROP INDEX issues_closed_at_idx;
CREATE INDEX issues_state_idx ON issues (repo, state, updated_at DESC);
CREATE INDEX issues_closed_at_idx ON issues (repo, closed_at) WHERE closed_at IS NOT NULL;

-- run_id stays the primary key: a second-resolution stamp plus six hex digits, unique enough
-- across repositories, and run_turns references it.
ALTER TABLE runs ADD COLUMN repo text NOT NULL;
DROP INDEX runs_started_at_idx;
DROP INDEX runs_issue_idx;
CREATE INDEX runs_started_at_idx ON runs (repo, started_at DESC);
CREATE INDEX runs_issue_idx ON runs (repo, issue_number, started_at DESC);

ALTER TABLE events ADD COLUMN repo text NOT NULL;
DROP INDEX events_at_idx;
DROP INDEX events_issue_idx;
CREATE INDEX events_at_idx ON events (repo, at DESC);
CREATE INDEX events_issue_idx ON events (repo, issue_number, at DESC);

-- One row per worker, keyed by its repository, in place of the one-row table.
DROP TABLE runtime_snapshot;
CREATE TABLE runtime_snapshot (
    repo       text PRIMARY KEY,
    at         timestamptz NOT NULL,    -- RuntimeSnapshot.at
    written_at timestamptz NOT NULL,    -- now() at the write; the dashboard's snapshot age
    data       jsonb NOT NULL           -- RuntimeSnapshot.to_dict()
);
