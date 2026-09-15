# MCP config is confined to the command line, not swept out of the session's home

Date: 2026-09-14
Status: implemented
Issue: #119 (follow-up to #101)

## Problem

The session account's `~/.claude.json` is not in the `claude-home` volume. It sits in `$HOME`,
a sibling of `.claude/`, so it is recreated with each container and reaches no other
deployment -- but within one container's lifetime it is shared by every session, of every
issue, at that uid. On a live worker it accumulates one `projects` entry per workspace:

```text
/workspaces/issuebot-101 … /workspaces/issuebot-119, and per entry:
allowedTools  disabledMcpjsonServers  enabledMcpjsonServers
hasClaudeMdExternalIncludesApproved  hasClaudeMdExternalIncludesWarningShown
hasTrustDialogAccepted  hasUnseenTeamArtifacts  mcpContextUris  mcpServers
```

`mcpServers` is the live one. A session can write it -- `claude mcp add --scope user` is one
command -- and a server planted there is a process `claude` starts, with tools it offers the
model, for whichever issue runs next in that container.

#101 is the volume's half of this -- sweeping the loadable surfaces under `~/.claude` between
sessions -- and it deliberately scoped this file out, for two reasons: it is outside the volume
that issue is about, and removing it risks the onboarding and trust behaviour that makes a
headless `claude -p` run without a prompt. #101 is still open (PR #123) as this is written, so
nothing below depends on it having landed.

## What was measured

Run as the session account itself (`agent`, uid 1001), against the `claude` the image pins
(2.1.263), with a real stdio MCP server registered under a throwaway `CLAUDE_CONFIG_DIR` so
the live home was never touched, and with the worker's own flags:

| `--setting-sources` | `mcpServers` scope | init line |
|---|---|---|
| omitted (`claude.setting_sources` defaults to `None`) | user | `[{"name":"probe119","status":"connected"}]`, tool `mcp__probe119__probe_exfiltrate` offered |
| omitted | project | connected, same tool |
| `user,project` | user | connected |
| `project` (what `configs/WORKFLOW.md` passes) | user or project | `[]` |

So: **yes, `claude -p` loads `mcpServers` from `~/.claude.json`**, at the shipped defaults.
The repository's own workflow suppresses it only as a side effect of a setting that exists for
other reasons and defaults to `None`.

Two things measured alongside it, because they bound the scope:

- A planted `projects.<path>.allowedTools` is **not** honoured. With `--permission-mode
  default --permission-prompts none`, a `Bash(chmod:*)` entry in that file changed nothing:
  the call was denied identically with and without it. The persisted approvals in this file
  are not a privilege the next session inherits.
- `CLAUDE_CONFIG_DIR` relocates `.claude/` as well as `.claude.json` -- a fresh directory
  answered `Not logged in · Please run /login`.

## Decision

Pass `--strict-mcp-config` on every turn, unconditionally, from `ClaudeRunner.build_argv`,
beside `--permission-prompts none` and for the same reason: it is what makes the run safe
rather than what makes it convenient, so no setting reaches it. It keeps only servers named by
`--mcp-config`, and issuebot names none.

Amended by #109 (`2026-09-14-session-authority-design.md`), which landed beside this: the
flag is still unconditional, and `claude.mcp_config` in the front matter is now the one place
a server may be named, a path there resolved against the workflow's directory rather than the
clone. Empty by default, so a deployment that sets nothing is exactly what this document
describes. The image build asserts `--disallowedTools` beside the two flags named below, so
"both flags" there is three.

The three options the issue put up, against the measurements:

- **Remove the file per session.** It holds `oauthAccount`, `userID`, `hasTrustDialogAccepted`
  and the caches; removing it is the onboarding and trust risk #101 declined to take, and it
  would be retaken before every session forever.
- **Relocate it per session** (`CLAUDE_CONFIG_DIR`). Measured above: it moves the credential
  too. A per-session config directory would mean copying `.credentials.json` into it each
  time, multiplying the live token across the disk to fix a config problem. Strictly worse.
- **Clear only the risky keys.** Workable, and it is the shape #101 proposes one directory
  over, but it is a denylist over a file whose format is claude's own and undocumented: a key
  added by a release is a hole until someone notices. It also cannot reach a `.mcp.json` in
  the repository being worked on, which a hostile issue's branch can carry.

`--strict-mcp-config` names what survives instead of what is removed, so it covers the file,
the repository's `.mcp.json`, and any MCP location a later `claude` adds, with no list to
maintain. It is claude's own guarantee rather than issuebot's reconstruction of one.

Nothing here settles #101: the volume and this file are different surfaces, and a sweep of the
first is still wanted whatever happens to the second. Nor does this need `agent.run_as`, unlike
a sweep -- the file belongs to the session's uid and is closed to the worker's, so clearing it
would have to be delegated, while an argv flag is the worker's to set on the host route and the
container route alike.

`MIN_CLAUDE_VERSION` is not raised for the flag. The floor is 2.1.259 because that is the
oldest `claude` carrying `--permission-prompts none`; `--strict-mcp-config` is far older than
that, so the existing floor already implies it. What pins it is the image build, which asserts
both flags against `claude --help` beside the version pin, and the CI step below, which runs
the flag rather than reading about it.

`docs/superpowers/specs/2026-09-03-phase-3-agent-runner-design.md` still shows the argv without
this flag. That is left alone on purpose: the phase specs are a record of what was decided
then, and this document is the amendment.

## Residual

`hasClaudeMdExternalIncludesApproved` still persists per project path, so a rework session on
the same workspace path inherits an approval its predecessor gave for CLAUDE.md includes
outside the project. It is bounded to one issue's own workspace rather than shared across
issues, and no measurement here shows it granting anything; it is recorded rather than fixed.

## Proof

- `tests/test_agent_runner.py` pins the flag in the argv, for a fresh session and a resumed
  one, over the settings that might look as though they already cover it -- including
  `setting_sources` in each of its shapes -- and asserts no `--mcp-config` beside it.
- The CI `docker` job runs the real `claude` in the built image as `agent`, with a server
  planted in `/home/agent/.claude.json`, and asserts both directions: listed in the init line
  without the flag, and `"mcp_servers":[]` with it. It needs no credential, because that line
  is emitted before the login check -- and reaching it is itself the evidence that the flag
  leaves a headless `claude -p` working rather than failing on an unknown argument.

The two-sidedness is the point: a one-sided assertion would pass just as well against a
`claude` that had stopped reading the file, and would then stop testing anything.
