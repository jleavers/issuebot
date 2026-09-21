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

The three file columns are always the *project's* `CLAUDE.md`, its in-clone `@INSIDE.md` and
the out-of-clone `@../outside/EXTERNAL.md`, whichever row it is. So the `user` rows read
"project CLAUDE.md not read" -- it is not a setting source there -- beside "EXTERNAL read",
which is the *user's* CLAUDE.md reaching the same include; that file's own atime is not a
column. The point of those rows is that the key moves nothing in them.

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

Abridged: the same `D` also reaches `Q0(dQ("Managed"), "Managed", v, D)`, the project's
`.claude/CLAUDE.md`, and the `lfe({rulesDir, includeExternal: D})` calls for the project's and
local's `.claude/rules`. The key gates all of them, which is why the allow-list below is not
written in terms of `CLAUDE.md` alone.

`Or`/`hF` are the `--setting-sources` gates, and `OZ(e){return vf(e, he())}` -- the test an
include has to fail to count as external -- is "inside the cwd". The only *writer* of the key
is the interactive dialog's `Sit`, so a `claude -p` session cannot approve external includes
through claude at all; it can write `~/.claude.json` itself, which is the vector and the same
one `mcpServers` had.

## Decision: an argv allow-list, as #119 chose for `mcpServers`

At the shipped `claude.setting_sources: [user]` (#107) the key reaches nothing of the clone's.
No Project or Local CLAUDE.md is a source, so there is none for an `@` include to hang off. The
last three rows of the table are the *user* CLAUDE.md, whose external includes load whatever
the key says -- and `~/.claude/CLAUDE.md` is the first name in `CLAUDE_HOME_SWEEP` (#101,
#137), removed before every turn and every hook. Two caveats on that cover, both of which are
why the flag below is unconditional rather than conditional on the opt-in: `sweep_agent_home`
returns early when `agent.run_as` is unset, so nothing sweeps it on the host route, where the
home is the operator's own; and the key never gated that row anyway.

The route that the key *does* gate opens under the `project` or `local` opt-in
`ClaudeSettings.loads_clone_settings` reports. Under it the grant is carried by more than the
clone's `CLAUDE.md`: the same `includeExternal` is passed to the project's `.claude/CLAUDE.md`
and to the `lfe({rulesDir: …})` call for its `.claude/rules`, which was measured separately --
a `.claude/rules/r.md` holding an `@` include outside the clone was read with the key true and
not read with it false. So the reach is every instruction file the opt-in hands over.

**This is closed the way #119 closed `mcpServers`: with an argument, not by clearing a key.**
`claudeMdExcludes` is a documented setting -- "Glob patterns or absolute paths of CLAUDE.md
files to exclude from loading. Patterns are matched against absolute file paths" -- applied in
the loader by `Sgr(path, type)` inside `Q0`, on every memory file *and on every include it
recurses into*. It is reachable from the command line through `--settings`, and
`ClaudeRunner.build_argv` now passes it on every turn, unconditionally, beside
`--strict-mcp-config` and for the same reason: it is what makes the run safe rather than what
makes it convenient, so no setting turns it off.

The value is an allow-list of the turn's own workspace and the two paths `claude` reads as
user memory, as a single *negated* brace pattern:

```json
{"claudeMdExcludes": ["!{/workspaces/issuebot-135/**,/home/agent-5/.claude/rules/**,/home/agent-5/.claude/CLAUDE.md}"]}
```

Measured with exactly the string `claude_md_allowlist` emits, under `--setting-sources
user,project` with the key `true`: the clone's `CLAUDE.md`, its `@INSIDE.md`, its
`.claude/rules/r.md`, the user's `CLAUDE.md` and the user's `rules/ur.md` are all still read,
while an `@` include pointing outside the clone and an `@` include of
`<config>/projects/other/notes.md` are both not. And it is an argument rather than a
preference: with `.claude/settings.json` in the clone setting `claudeMdExcludes` back to `[]`,
the argv value still won and the external includes were still not read -- which is the property
AC2 asked for, "cannot be switched off by a setting".

Five things about the shape, each of which a simpler one gets wrong.

- **One negated pattern, not one per arm.** picomatch matches a list when *any* pattern
  matches, so `!a/**` and `!b/**` would each match everything outside their own arm and OR
  together to "exclude everything" -- not a weaker allow-list but a session with no instruction
  files at all. Braced, the negation is evaluated once against the union.
- **This workspace, not `settings.workspace.root`.** That is every workspace's parent; one
  issue's CLAUDE.md has no more business reading another issue's clone than reading the home.
  So `build_argv` takes the turn's `workspace`, which `run_turn` has already contained.
- **The config directory by its two memory paths, never as a tree.** `claude` loads exactly
  `CLAUDE.md` and `rules/` from it as user memory -- `dQ("User")` and `age()` in the loader --
  and those two have to be allowed or the argument would stop the user memory issuebot
  deliberately leaves in place. Allowing the directory itself would have drawn the fence around
  the most valuable target in the home: `.credentials.json`, and the other sessions' transcripts
  under `projects/` that `CLAUDE_HOME_SWEEP` keeps on purpose, would all have become things a
  clone's `CLAUDE.md` could `@` include. Measured both ways.
- **`$CLAUDE_CONFIG_DIR` when the deployment sets one.** `claude` resolves that pair against
  `process.env.CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")`, and `CLAUDE_` is a
  `PASSTHROUGH_PREFIXES` entry, so a deployment that sets one gets it in the child. Naming
  `~/.claude` regardless would leave the operator's own user memory excluded by the very
  argument meant to preserve it, and silently. The session cannot re-point it either way:
  `CLAUDE_` is also a `PROTECTED_ENV_PREFIXES` entry, so `.issuebot/env` is refused it.
- **An arm it cannot spell means no argument at all.** A brace, a comma or a glob
  metacharacter would change what the pattern matches; so would a *relative* path, and worse,
  since `claudeMdExcludes` is matched against absolute paths, so a relative arm matches nothing
  and the negation then matches everything. An unresolved config directory is the same case.
  The failure that matters is not "too little is excluded" but "everything is", which costs the
  session every instruction file it should have loaded and looks, from outside, like a session
  that ignored them. `claude_md_allowlist` returns `None` for all of those.

One behaviour change worth naming, because it is not an `@` include. `claudeMdExcludes`
excludes the memory *files* themselves, and the loader walks every ancestor directory of the
cwd up to `/`, taking `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules` and `CLAUDE.local.md`
from each. Those ancestors are outside the workspace, so the allow-list drops them. That is the
intended reading of "outside the clone" rather than a side effect -- a `CLAUDE.md` above the
workspace is as much somebody else's instructions as one in the home -- and under compose the
ancestors are `/workspaces` and `/`, both the worker's and neither carrying one. On the host
route, with `workspace.root` under an operator's home and `setting_sources` naming `project`,
it does mean a `~/CLAUDE.md` of theirs stops reaching the session; `validate`'s
`claude.setting_sources` line is where that is said.

`Managed` memory is outside all of this by claude's own rule: `Sgr` returns false for any type
but `User`, `Project` and `Local`, so an operator's root-owned policy CLAUDE.md is untouched --
which is right, since it is also outside the session's privilege domain.

The two options this document weighed for `mcpServers` were weighed again and are still worse.
`CLAUDE_CODE_DISABLE_CLAUDE_MDS` is the blunt one, and the last row of the table is what it
does: it voids *every* CLAUDE.md, the project file and its own internal include included, so
putting it in `FIXED_ENVIRONMENT` beside `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` would make #107's
opt-in silently do nothing. Clearing the key out of `~/.claude.json` is the denylist over
claude's own format that this document already refused, and #135 makes it worse rather than
better: it would be a read-modify-write of that file, as the session account, in a home the
single-account route shares with a session that may be running right now (#121 binds one
account per workspace; it does not serialise two workspaces bound to one account), so the
failure mode of the fix is a corrupted `.claude.json` breaking every session in the container.

So the file's non-MCP keys are now settled rather than open. `allowedTools` is not honoured
(above, #119). `hasClaudeMdExternalIncludesApproved` is honoured, and what it can still reach
is bounded by the argument above rather than by a setting. `hasClaudeMdExternalIncludesWarningShown`
only suppresses the dialog a `-p` session never sees, and the dialog's `Sit` is the only writer
of either key, so a session that wants the approval has to write the file itself -- which is
the same act, on the same file, that `--strict-mcp-config` already answers for `mcpServers`.

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

For the CLAUDE.md allow-list (#135), in the same file:

- `test_build_argv_always_confines_claude_md_to_the_workspace_and_the_account` pins the
  argument for a fresh session and a resumed one, over each shape of `claude.setting_sources`
  and over `permission_mode`, since those are the settings that might look as though they
  already cover it.
- `test_the_claude_md_allow_list_is_one_negated_pattern_over_every_root` pins the brace, which
  is the part a reasonable edit would get wrong in the direction that loads nothing.
- `test_the_claude_md_allow_list_refuses_a_root_it_cannot_spell` and
  `test_no_claude_md_allow_list_without_a_home` pin the two ways the argument is omitted
  rather than guessed.

The measurement itself is not a test and deliberately is not one: it needs a real `claude`, a
writable `~/.claude.json` and a filesystem whose atimes move, and the thing it establishes --
that this release reads the key at all -- is claude's behaviour rather than issuebot's. What
the tests pin is that issuebot keeps sending the argument. Re-run the measurement against a new
`claude` if the question comes up again; the trap to avoid is in the section above.
