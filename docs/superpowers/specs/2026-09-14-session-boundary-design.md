# What crosses the session boundary, declared once and guarded once

Date: 2026-09-14
Status: implemented
Issue: #104 (security sweep findings `hostile-issue-1`, `hostile-issue-2`, `copycat-3`);
follow-up #121

## Problem

#75 drew the line -- the worker at one uid, the session at another -- and then guarded the
crossings one artefact at a time, as each was noticed: `session.json` got an ownership check,
the completion sentinel an exclusive create, the workpad an author check for *which* comment
it is. Nothing said what may cross the line, so the next artefact was trusted by default.
Three consequences, none enumerated:

1. `<workspace>/.issuebot/env`, which the session's side writes, was a bare `read_text`: a
   FIFO there blocked the event loop for every session, a link to `/dev/zero` allocated until
   the kernel intervened, and a link to a file the worker's uid can read and the session's
   cannot -- the operator's own environment file, on the host route -- came back into the
   session's environment. The size bound was applied to the string after the whole file had
   been read.
2. The conflict-rework bound was counted out of the workpad body, which the session PATCHes
   whole, so stripping the headings zeroed it and `agent.max_conflict_reworks` never engaged.
3. One account for every concurrent session, so a hostile issue's session could write into an
   honest issue's clone before it pushed.

**Invariant.** Anything a session can write stays inside that session: it is never an input
to a worker decision, and never reachable by another session, unless it crosses one guarded
seam that checks who wrote it, what kind of object it is, and how large it may be.

## Design

### 1. One declaration: `issuebot.agent.boundary.ARTEFACTS`

Every file the worker reads back out of a workspace after the session has had its uid in it,
with its writer and the most the worker will ever read of it:

| artefact | path | writer | read at most |
|---|---|---|---|
| `env` | `.issuebot/env` | the session (a hook) | 64 KiB, head, cut at a line |
| `session` | `.issuebot/session.json` | the worker | 64 KiB, or refused |
| `created` | `.issuebot/created` | the worker | existence only |
| `turn stream` | `.issuebot/runs/<run_id>/turn-N.jsonl` | the worker | 64 MiB, head |
| `turn prompt` | `.issuebot/runs/<run_id>/turn-N.prompt.md` | the worker | 4 MiB, head |
| `turn stderr` | `.issuebot/runs/<run_id>/turn-N.stderr.log` | the worker | 16 MiB, tail |

What crosses *into* the session (the prompt on stdin, the environment on a memory file, the
clone, the hook scripts) needs no guard. What comes back over a pipe (claude's stdout and
stderr, a hook's output) is bounded where the pipe is read. A worker-side read of any other
path under a workspace is a bug.

### 2. One seam: `Boundary.read`

`Boundary(worker_uid, session_uid)` is built once per runner and workspace manager from
`agent.run_as` (`Boundary.current`); on the host route the session is the worker. A read
names a base directory the worker owns (the workspace, or a run's log directory) and the
components under it, and:

- opens the path one component at a time with `O_NOFOLLOW` at every step, so a link at the
  name or above it is refused rather than followed (Linux answers `ENOTDIR` rather than
  `ELOOP` when `O_DIRECTORY` is also set, so the name is looked at to say which it was);
- requires every directory on the walk to be the worker's, which under `agent.run_as` the
  sticky workspace and `.issuebot` are and the session cannot rename or replace;
- opens the final component with `O_NONBLOCK`, so a FIFO cannot block the open, and checks
  the descriptor with `fstat` before a byte is read: a regular file, owned by one of the
  artefact's writers, or refused;
- reads at most the artefact's limit, from the head or the tail, whatever the file's size,
  and reports the size the file had (`ReadBack.size`, `truncated`).

A refusal is `BoundaryError`, an `OSError` carrying the path and a reason, so every call
site's existing `except OSError` reports it as a warning naming the path and the reason and
never the contents, and no task ends on it. `read_workspace_env`, `read_session`,
`_is_complete`, `capture_turns` and the runner's stderr tail all go through it.

The worker's own state is created through the same object. `own_dir` makes a run's log
directory, every missing component of it, and verifies the last is the worker's and closed
to everyone else's writes before a turn file is written in it, so the files inside are the
worker's without a check per file; a directory the session placed at that path is refused
(`invalid_workspace_cwd`). `create_marker` is #75's exclusive create of the sentinel,
generalised.

### 3. The bound is read from a record only issuebot writes

`GitHubAdapter.count_own_label_additions(number, label)` counts the issue's `LABELED_EVENT`
timeline items made by the account the adapter runs as (`gh api graphql`, paginated, `-F`
for the `Int!`; the fake keeps the same history). `conflict_rework` reads its bounce number
from it: every `rework` the account added is one bounce. GitHub credits every event to its
actor and nobody can remove one, so the session -- which shares the account -- can only add
to the count, which tightens the cap. The workpad blocks remain as the note a person reads.
The limit note's presence is the session's to erase, so the orchestrator remembers per
issue and limit that it wrote one (`_conflict_limit_noted`): a stripped note is rewritten
once per process, not per tick, and a changed limit is looked at afresh.

## What this does not do

The third consequence -- one account for every concurrent session -- is not closed here.
The design that fits (a pool of session accounts, `issuebot ALL=(%agents)`, an account bound
to a workspace for as long as it exists) is a code change, but it multiplies the Anthropic
credential by the pool: N logins, one credential copied into N homes that then refresh
independently (with the `OAuth session expired and could not be refreshed` failure this
deployment has already logged), a shared config directory whose `0600` credential the other
accounts cannot read, or an environment-borne credential. That is a deployment decision a
session cannot make or test, so #121 carries the design and the decision; #101 (the same
domain in its sequential form) waits on the same answer.

## Tests

`tests/test_agent_boundary.py` drives the declaration and the seam: a FIFO is refused
without blocking, a link at or above the name is refused, a device and a directory are
refused, an owner outside the declared writers is refused (with a `Boundary` whose uids are
not this process's, since a test cannot change uid), the limit bounds the bytes read from
the head or the tail, `own_dir` refuses a link, a file or a shared directory at the end and
accepts a shared one above it, and `create_marker` fails on a pre-placed name. The runner,
workspace, turnlog, fake, gh adapter, actions and orchestrator suites cover each call site:
a FIFO at `.issuebot/env` leaves the turn running, a link there hands the session nothing,
a linked `session.json` or marker is not trusted, a linked turn file is skipped, and the
bounce cap holds when the session rewrites the workpad without issuebot's blocks.
