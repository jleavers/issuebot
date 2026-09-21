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
| `project` (what `configs/WORKFLOW.md` passed then) | user or project | `[]` |

So: **yes, `claude -p` loads `mcpServers` from `~/.claude.json`**, at the shipped defaults.
The repository's own workflow suppressed it only as a side effect of a setting that exists for
other reasons. The `--setting-sources` column is as measured: #107 has since made the setting
always passed and defaulted it to `[user]`, and the workflow names it no longer, so the last
row is no longer the shipped shape and nothing suppresses the entry but the flag. That
strengthens the conclusion below rather than changing it.

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

## The non-MCP keys: `hasClaudeMdExternalIncludesApproved` (#135)

This section replaces the "Residual" that stood here. That residual recorded
`hasClaudeMdExternalIncludesApproved` as persisting per project path with "no measurement here
[showing] it granting anything" -- which was an absence of measurement, not a finding. #135
measured it. The answer is **yes, `claude -p` honours it**, and the rest of this section is
what that does and does not reach.

Measured against the same `claude` the image pins (2.1.263), run as a session account
(`agent-5`, uid 1015), with the worker's own flags and a throwaway `CLAUDE_CONFIG_DIR`. The
signal is not what the model said: `ANTHROPIC_API_KEY` is present but *empty* in a container
session's environment, so no credential was reachable, exactly as #119 found when
`CLAUDE_CONFIG_DIR` cost it the OAuth login. It did not need one. CLAUDE.md is loaded before
the login check -- the same property that let #119 read the init line -- so the question
"was the include honoured" is answerable as "did `claude` read the include target at all",
which is an atime under `relatime` with the mtime bumped before each run. An `@INSIDE.md`
include *inside* the project is the control that separates "CLAUDE.md was not loaded" from
"the external include was refused".

| `--setting-sources` | CLAUDE.md | key | `CLAUDE.md` | `@INSIDE.md` | `@../outside/EXTERNAL.md` |
|---|---|---|---|---|---|
| `project` | project's | absent | read | read | **not read** |
| `project` | project's | `false` | read | read | **not read** |
| `project` | project's | `true` | read | read | **read** |
| `user` | user's | absent | not read | not read | read |
| `user` | user's | `false` | not read | not read | read |
| `user` | user's | `true` | not read | not read | read |
| `project`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` | project's | `true` | not read | not read | not read |

Two things the table alone does not say.

**claude keys `projects[]` by the git root of the cwd, not by the cwd.** The first run of the
probe wrote the key onto the working directory and measured "not honoured"; the probe was
sitting inside a clone, so `claude` was reading a different entry entirely. `git init` in the
probe inverted the result. Anyone re-running this hits that trap first.

**The key gates the project's and local's CLAUDE.md, never the user's.** From the 2.1.263
bundle, in the memory loader:

```js
let x = es(),                                   // projects[<git root>] of ~/.claude.json
    D = o || x.hasClaudeMdExternalIncludesApproved || !1;
if (Or("userSettings"))    { ... Q0(Pe, "User",    v, !0) }   // external includes always on
if (hF("projectSettings")) { ... Q0(Ie, "Project", v, D)  }   // gated by the key
if (hF("localSettings"))   { ... Q0(Ie, "Local",   v, D)  }   // gated by the key
```

`Or`/`hF` are the `--setting-sources` gates, and `OZ(e){return vf(e, he())}` -- the test an
include has to fail to count as external -- is "inside the cwd". The only *writer* of the key
is the interactive dialog's `Sit`, so a `claude -p` session cannot approve external includes
through claude at all; it can write `~/.claude.json` itself, which is the vector and the same
one `mcpServers` had.

## What that leaves, and why no flag is set for it

At the shipped `claude.setting_sources: [user]` (#107) the key reaches nothing. No Project or
Local CLAUDE.md is a source, so the clone has no CLAUDE.md loaded for an `@` include to hang
off, and the last three rows are the *user* CLAUDE.md, which loads external includes whatever
the key says -- and `~/.claude/CLAUDE.md` is the first name in `CLAUDE_HOME_SWEEP` (#101,
#137), removed before every turn and before every hook. The route opens only under the
`project` or `local` opt-in that `ClaudeSettings.loads_clone_settings` reports and `validate`
already warns about, and under it the incremental grant is that a CLAUDE.md include may point
*outside* the clone -- at the session account's home, which the sweep's denylist does not
cover -- and be read by the next session at that workspace path.

Unlike `mcpServers`, this one gets no flag, and the reason is that there is no flag to set.
`D` above reads no environment variable and no argument; nothing in the argv can turn external
includes off while leaving CLAUDE.md on. The one env-shaped switch is
`CLAUDE_CODE_DISABLE_CLAUDE_MDS`, and the last row of the table is what it does: it voids
*every* CLAUDE.md, the project file and its own internal include included. Putting that in
`FIXED_ENVIRONMENT` beside `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` would make #107's opt-in
silently do nothing -- a regression dressed as a fix, and a worse one than the hole, since an
operator who names `project` is asking for that file on purpose.

Clearing the key out of `~/.claude.json` is the option this document already refused for
`mcpServers`, and #135 makes it worse rather than better. It is still a denylist over a format
that is claude's own, but it is now also a read-modify-write of that file, performed as the
session account, in a home the single-account route shares with a session that may be running
*right now* (#121 is what narrows that to one account per workspace; it does not serialise two
workspaces bound to one account). The failure mode of the fix is a corrupted `.claude.json`
that breaks every session in the container, and it would be paid to close a route the shipped
setting already shuts.

So the decision is: measured, bounded, and carried by the setting that governs it rather than
by a flag. What #135 changes in code is that `validate`'s `claude.setting_sources` warning now
names this consequence, so an operator turning the opt-in on is told that the clone's CLAUDE.md
becomes configuration *and* that its `@` includes may then reach outside the clone on an
approval a previous session at that workspace path wrote. The keys that are settled and grant
nothing are recorded here rather than re-measured: `allowedTools` (above, #119),
`hasClaudeMdExternalIncludesApproved` (this section), and `hasClaudeMdExternalIncludesWarningShown`,
which only suppresses the dialog a `-p` session never sees.

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
