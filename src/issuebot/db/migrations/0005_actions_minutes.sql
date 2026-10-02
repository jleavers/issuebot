-- The billing account's GitHub Actions minutes (spec 2026-10-02): one row per account, written
-- hourly by the hub's web service and read by the dashboard's limits tile. alerted_percent is
-- the Slack alert's memory -- the highest threshold posted for `period` -- kept here so that a
-- restart, which every upgrade is, never posts the same alert twice.

CREATE TABLE actions_minutes (
    account          text PRIMARY KEY,           -- the token's login, as GET /user returned it
    period           date,                       -- first day of the month the reading is for
    used_minutes     double precision,           -- null until the first successful read
    included_minutes integer,                    -- ISSUEBOT_ACTIONS_INCLUDED_MINUTES at that read
    observed_at      timestamptz,                -- when issuebot read it
    alerted_percent  integer NOT NULL DEFAULT 0,
    error            text,                       -- the last failure, in issuebot's own words
    error_at         timestamptz
);
