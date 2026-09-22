# Watching a running session

`issuebot status` and the dashboard both read the worker's snapshot, which says whether a
session is alive and roughly where it is — `turns`, `last_event`, `last_activity_at`. Neither
can say **what it is doing**, because `run_turns` is written in the `run_ended` transaction:
until the run finishes there is no transcript in the database, and one turn can last an hour.

This reads the turn log the session is still writing.

```bash
uv run python tools/watch/watch.py 218
```

```text
  scrubbing: deployment (/home/jleavers/_dev/issuebot/configs/WORKFLOW.md)
  /workspaces/issuebot-218  run 20260922T123309Z-02085f  turn-1.jsonl  1,914,530 bytes
  text           Every mutant is killed. Now the CLAUDE.md wording and full validation.
  Bash           Correct the CLAUDE.md breadth claim and validate
  Bash           Prove both shapes end to end
  think          (thinking)
  Bash           Commit and push the fourth-pass fixes
  Bash           Merge, check mergeability and feedback
```

Add `--follow` to keep printing, `--lines` for more or fewer, and `--bytes` to read further
back up the file.

## What it reads, and why that needs care

`.issuebot/runs/<run_id>/turn-N.jsonl` is `claude`'s stdout tee'd byte for byte. It is **not
scrubbed on disk** — `capture_turns` is the scrubbing step for those files, and it runs on the
way into the database — and issuebot put `GH_TOKEN` into that process's environment. So a
reader that went straight to the bytes would be a second exit for the credential, past the step
that exists to stop exactly that.

Everything this prints therefore goes through a `Scrubber`, and the first line says which one:

- **`deployment (<path>)`** — the workflow loaded, so this deployment's own `github.token`,
  `database.url` password and Slack webhook are masked *by value* as well as by shape.
- **`credential shapes only`** — no workflow, or one that would not load. Token and key shapes
  are still masked; a DSN password, which is not shaped like anything, is not.

`tests/test_tools_watch.py` holds that claim rather than leaving it to the docstring.

Read-only throughout: it runs `ls`, `tail` and `wc` and writes nothing, so it is safe against a
live session.

## Where it looks

By default, inside the `worker` container, since that is where the workspaces volume is mounted:

| flag | for |
|---|---|
| `--service NAME` | a compose service other than `worker` |
| `--local` | the host route, where the workspaces are an ordinary directory |
| `--workspaces PATH` | a workspace root other than `/workspaces` |
| `--project-directory PATH` | the checkout compose reads its environment file from |
| `--workspace PATH` | when two workspace keys end in the same issue number |
| `--run-id ID` | a run other than the newest |

The workspace is found by the suffix every key carries — a key is `<repository name>-<number>`,
so only the number is known here. The newest run directory is the running one if any is; run ids
start with a UTC stamp precisely so they sort.

**From a git worktree, pass `--project-directory`** at the deployment's checkout. A worktree
carries no environment file of its own, so compose refuses to interpolate and never reaches the
container.

## When it says there is nothing to read

- *no workspace for issue #N* — the session has not claimed it yet, or the workspace has been
  removed after the issue completed.
- *no runs under …* — claimed, but no turn has started.
- *nothing rendered from the tail* — the tail landed inside one enormous line; raise `--bytes`.
