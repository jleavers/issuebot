-- run_turns carries its own repository (#111). Until now it was the one table without a repo
-- column: its tenancy rested on run_id being unique across repositories, asserted in 0003's
-- comment and checked only by the read's join to runs. The check moves to the write: runs is
-- keyed (repo, run_id), run_turns (repo, run_id, turn_number), and the foreign key spans both,
-- so a turn can only ever be written against its own repository's run.
--
-- The backfill is the one legitimate use of the join: at this point every run_turns row still
-- references exactly one runs row through run_id, so its repository is that row's.

ALTER TABLE run_turns ADD COLUMN repo text;
UPDATE run_turns t SET repo = r.repo FROM runs r WHERE r.run_id = t.run_id;
ALTER TABLE run_turns ALTER COLUMN repo SET NOT NULL;

ALTER TABLE run_turns DROP CONSTRAINT run_turns_run_id_fkey;
ALTER TABLE runs DROP CONSTRAINT runs_pkey;
ALTER TABLE runs ADD PRIMARY KEY (repo, run_id);

ALTER TABLE run_turns DROP CONSTRAINT run_turns_pkey;
ALTER TABLE run_turns ADD PRIMARY KEY (repo, run_id, turn_number);
ALTER TABLE run_turns ADD FOREIGN KEY (repo, run_id) REFERENCES runs (repo, run_id)
    ON DELETE CASCADE;
