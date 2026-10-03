-- The Claude usage alert's memory (spec 2026-10-02, claude-limits-alert): one row per window
-- instance -- claude's window name and the moment it reopens -- written by the hub's web when it
-- posts to Slack. One subscription reports one reset to every worker, so the workers that share
-- it share a row, and a restart, which every upgrade is, never posts the same alert twice.
-- (`window` is a reserved word, hence `limit_window`.)

CREATE TABLE claude_limit_alerts (
    limit_window    text NOT NULL,              -- five_hour, seven_day, or another claude names
    resets_at       timestamptz NOT NULL,       -- when that window reopens
    alerted_percent integer NOT NULL DEFAULT 0, -- highest posted: 75 or 90, or 100 for a hit
    PRIMARY KEY (limit_window, resets_at)
);
