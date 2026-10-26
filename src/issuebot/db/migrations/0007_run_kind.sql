-- Triage (docs/superpowers/specs/2026-10-25-triage-design.md, section 4): a run is a work run
-- or a triage run, and the admission ledger's failure chain counts work runs only.
ALTER TABLE runs ADD COLUMN kind text NOT NULL DEFAULT 'work';
