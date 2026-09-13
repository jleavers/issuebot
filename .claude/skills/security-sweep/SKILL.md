---
name: security-sweep
description: Use when sweeping this repository for security bugs - before publishing it, or on a schedule afterwards. Audits origin/main in a throwaway worktree with a multi-agent workflow, clusters findings by root cause, checks the tracker for duplicates, and files only the clusters a human approves.
---

# Security sweep

Audits the tree a reader would clone, not the tree you happen to have checked out.

Findings are clustered by root cause and each cluster names the invariant it touches, because
a sweep that emits one issue per finding produces a round of local patches and those patches
are the next sweep's findings.

**Nothing is filed without the operator saying so.** The workflow computes the dedupe verdict;
it does not make the decision.

## Phase 0: preflight

On Windows chain with `;` and use PowerShell equivalents; the commands below are the Bash form.

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WT=.claude/worktrees/security-sweep-$STAMP
RD=.claude/security-sweeps/$STAMP
git fetch origin
git rev-list --count main..origin/main          # informational only
git worktree add --detach "$WT" origin/main
git -C "$WT" rev-parse HEAD                     # the swept SHA
mkdir -p "$RD"
```

Then write `$RD/run.json`:

```json
{"stamp": "...", "sha": "...", "repo": "jleavers/issuebot",
 "worktree": "<absolute>", "run_dir": "<absolute>", "phases": {}}
```

Three things about this, each load-bearing:

- **The worktree is cut from `origin/main`, not from local `main`.** That makes the local
  checkout's state irrelevant to what is audited, which is a stronger guarantee than
  fast-forwarding first. The behind-count is reported because an operator should know, not
  because anything depends on it.
- **`--detach`** so there is no branch to clean up and no name to collide across runs.
- **The run directory lives in the main checkout, not in the worktree**, so that cleanup — or
  a crash during cleanup — cannot take the findings with it.

## Phase 1-5: the workflow

```
Workflow({
  name: "security-sweep",
  args: {stamp, sha, repo, worktree: <absolute>, runDir: <absolute>, escalationCap: 3}
})
```

`worktree` and `runDir` must be absolute: the agents resolve them directly.

It runs recon, then four threat-model lanes (`copycat`, `secrets`, `hostile-issue`,
`services`) with an independent refuter behind each, then a second refuter for up to
`escalationCap` confirmed critical/high findings, then triage and the completeness critic in
parallel, then dedupe and the report. Twelve to fifteen agents.

## Phase 6: present, and get approval

Read `<runDir>/report-<stamp>.md`. Present the clusters ranked by severity; for each give the
title, the **invariant**, the blast radius, and the dedupe verdict with the issue numbers it
matched. Then ask which to file.

Say explicitly:

- which clusters the dedupe pass marked `duplicate` or `related`, and to what;
- that singletons are not proposed unless `critical`, and which ones exist;
- what the completeness critic said was never looked at.

Do not file anything the operator did not name. Do not file a `duplicate` without saying so
first.

## Phase 7: file the approved clusters

`gh issue create` with title or body flags is blocked by a PreToolUse hook. Use `gh api`, and
write the body file in a **separate** Bash call — the hook aborts the whole call, so a chained
heredoc never runs and the API call then fails with a misleading "no such file or directory".

Write the body to a temp `.md` file (Write tool), then:

```bash
gh api repos/jleavers/issuebot/issues -X POST \
  -f title='TITLE' \
  -F body=@bodyfile.md
```

Capital `-F` for the body file; lowercase `-f` posts the literal string `@bodyfile.md`.

The body carries the cluster's root cause, invariant, blast radius and fix shape, and the
finding ids behind it. It does **not** link to the run directory, which is local and stays
local: the issue has to stand alone. It does not carry a patch.

Record what was filed in `<runDir>/06-filed.json` as
`[{"cluster_title": "...", "issue_number": N}]`. A later sweep reads this to say "that is the
cluster filed as #N and not yet fixed" instead of re-finding it as new.

## Phase 8: cleanup

```bash
git worktree remove .claude/worktrees/security-sweep-$STAMP
git worktree list
```

Never `rm -rf` (AGENTS.md), and never `--force`. If the remove fails because the worktree is
dirty — it should not be, nothing writes there — report it and leave it for the operator.

**Keep the run directory.** It is the comparison the next sweep needs.

## Resuming after a crash

The Workflow tool's own resume is same-session only, so there are three tiers:

1. **Same session, run killed or script edited** — `Workflow({scriptPath, resumeFromRunId})`.
   The longest unchanged prefix of `agent()` calls returns from cache.
2. **Session gone, run directory intact** — read `<runDir>/run.json` and see which artefacts
   exist: `01-surface-map.md`, `02-findings-<lane>.json`, `03-verdicts-<lane>.json`,
   `03-escalated-<id>.json`, `04-clusters.json`, `04-gaps.md`, `05-dedupe.json`. Restart from
   the first missing phase, by hand if need be — the files are the contract.
3. **Worktree gone too** — `run.json` records the SHA, so re-cut it:
   `git worktree add --detach <path> <sha>`. A partial run stays comparable with itself
   rather than with a moved target.
