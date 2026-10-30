-- Triage (docs/superpowers/specs/2026-10-25-triage-design.md, section 7): which triage label an
-- issue carries, for the dashboard's Triage and Triaged columns. 'triage_todo' or
-- 'triage_complete' (the GitHubLabels field names, so a renamed label keeps its key); NULL means
-- neither label. It sits beside `state`, never in it: every reader of `state` takes it to be a
-- state label.
ALTER TABLE issues ADD COLUMN triage text;
