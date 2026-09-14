# The cloned repository is data, not a second instruction channel

Date: 2026-09-14
Status: implemented
Issue: #107 (completeness critic of the security sweep of `8d64a5e`)

## Problem

`WorkspaceManager` clones the watched repository and the session runs `claude -p` inside it.
Claude Code reads a working tree's `CLAUDE.md`, `.claude/` (settings, hooks, skills,
commands, agents) and `.mcp.json` as its own configuration when its settings sources include
the project, and they did: `claude.setting_sources` was unset by default, which is claude's
own default of every source, and the shipped `configs/WORKFLOW.md` set `[project]` on purpose
so the agent would pick up the target repository's `.claude/settings.json`.

Measured on claude 2.1.263 with a throwaway repository (`--model haiku`; the transcript is in
the issue's workpad):

| `--setting-sources` | `CLAUDE.md` in force | `.claude/skills` in force | `.claude/settings.json` `SessionStart` hook ran | `.mcp.json` server started |
|---|---|---|---|---|
| unset (claude's default) | yes | yes | yes | yes |
| `project` | yes | yes | yes | not tested |
| `user` | no | no | no | no |

Two of those columns need no model in the loop at all. A `SessionStart` hook and an MCP
server's `command` are shell run at launch, as the session's account, with `GH_TOKEN` in the
environment. `AGENTS.md` is not auto-read by this claude; it is on the list below because the
default workflow tells the session to follow it.

Everything #76 built for the issue's text -- the envelope, the attribution, the rule stated
once before the first envelope -- was missing for this channel, and this channel is the
stronger one: a file at the clone's root arrives as configuration, above the prompt.

Who can place those files is anyone who can land a commit on the default branch. A
collaborator with write access is already trusted with that. The sharp edge is what arrives
through a merged pull request, an outside contributor's or issuebot's own: a diff touching
`CLAUDE.md` or `.claude/` is reviewed as documentation and configuration, not as instructions
every future unattended session inherits. Chained with #104 that is a ratchet: a hostile issue
steers a session into committing something under `.claude/`, a human approves it inside an
ordinary change, and it is standing instruction from then on, no longer dependent on the
issue that planted it and no longer visible as attacker-authored text anywhere.

## Invariant

Instructions in force for a session are issuebot's own, or they arrive inside an envelope
naming who wrote them. A file in the cloned working tree is data the session may read, never
instruction it inherits.

## Decision

Two halves, declare rather than discover, and envelope what is declared.

### Discovery is off

`claude.setting_sources` is a list that is always passed as `--setting-sources` and defaults
to `["user"]`: the deployment's own home (`~/.claude/settings.json`, `~/.claude/CLAUDE.md`,
the account the session runs as) and nothing from the clone. Claude's own default is never
in force, since the setting is never empty (`must name at least one source`). `project` and
`local` stay available as an operator's explicit choice, and `ClaudeSettings.loads_clone_settings`
is what `validate` reads to warn about it, by name and with the population that can change
those files: "anyone who can merge to `<repo>`". The shipped `configs/WORKFLOW.md` no longer
sets the field.

Turning discovery off covers every column of the table above with one flag, including the
two that run shell at launch, and it costs the session the clone's skills and hooks. That is
the point: a skill or a hook in the clone is code the session runs by the file's location.
An operator who wants a repository's `.claude/` in force says so in the workflow and reads
the warning.

### What the session still needs is declared and enveloped

`issuebot.agent.instructions` reads a declared list, `REPOSITORY_INSTRUCTION_FILES`
(`CLAUDE.md`, `AGENTS.md`), from the clone's root after `before_run` and once per run, and
`run_session` hands the result to the prompt as `repo_instructions`. Each file renders as
`GitHubText`, the same envelope as the issue's body: `source="CLAUDE.md in the clone of
<repo>"` (with `, first N bytes of M` when cut) and `author="whoever can merge to <repo>"`,
since no one login wrote a file and the population that could is the honest attribution.
The rule paragraph at the top of the default workflow now reads "written on GitHub by the
account the tag's `author` attribute names, or committed to the repository by whoever it
describes", and says in as many words that the working tree, `CLAUDE.md`, `AGENTS.md` and
`.claude/` included, is the same kind of text. A `## Repository instructions` section after
the ground rules carries the files, or says the clone has none. Ground rule 5 now defers to
those files for how to run tools, commit and open pull requests *under* the ground rules; it
no longer says they win.

The read is total and bounded. `O_NOFOLLOW`: under `agent.run_as` (#75) the clone is the
session's and this read is the worker's, so a symlink the clone ships must not put a file
only the worker can read into a prompt the session sees; a link is skipped and logged.
`O_NONBLOCK`: the open happens before the kind of file is known, and a FIFO by that name --
which a session can make in its own clone, for the next run over the reused workspace to
find -- would otherwise block the open, and with it the worker's session task, until a writer
came. A directory, a FIFO or an unreadable file is skipped with a warning, a missing one
silently; the section's fallback says issuebot carried none rather than that none exists,
and tells the session to read a present one itself as data. The text
is cut at `INSTRUCTION_FILE_LIMIT` (128 KiB, twice this repository's own `CLAUDE.md`) and
undecodable bytes are replaced. Nothing here
fails a run: the files are a convenience for the session, and a session without them reads
the tree itself, as data, like any contributing guide.

The continuation prompt does not repeat the files; a resumed session already has them. A
retry never resumes, and reads them again.

`validate` renders the prompt with a sample `CLAUDE.md`, so a template that mishandles the
section fails there. `run-once --show-prompt` reads the real workspace's files when the
clone exists.

### The reviewer-facing half

A change to the watched repository's instruction files is a change to the agent's privileges,
and is now called out where it is reviewed:

- The self-review brief in the default workflow: a change to `CLAUDE.md`, `AGENTS.md` or
  anything under `.claude/` is reported as Critical unless the issue asks for it in as many
  words, with what it grants.
- The pull request body: a diff touching those paths carries a paragraph headed
  `Instruction files` naming each one and what the change grants.
- This repository's `.github/CODEOWNERS` routes `CLAUDE.md`, `AGENTS.md`, `.claude/`,
  `configs/` and `.github/` itself (so the file cannot be edited unrouted as the first of two
  steps) to a human, with the reason in the file. An entry requests a review; it blocks a
  merge only under branch protection's "Require review from Code Owners", which is the
  repository's setting to make and the README says so.

## Why not the alternatives

**Neutralise on clone** (rename the files out of claude's way in the post-clone setup) was
the cheapest and strongest shape in the issue. It leaves the working tree dirty from the
session's first `git status`, so a session either restores the files or commits their
removal; a session with a shell reads `git show HEAD:CLAUDE.md` anyway; and it drops the
guidance the session genuinely needs, which is why `configs/WORKFLOW.md` defers to the
repository for how to open pull requests. Discovery off plus a declared, enveloped copy
keeps the guidance and loses nothing the neutralising would have.

**`--bare`** skips CLAUDE.md discovery but also reads no OAuth login, which is the
`claude-home` volume's credential (#17); **`--safe-mode`** disables the operator's own
settings too. Both are broader than the boundary.

**Envelope alone**, with discovery left on, is the weakest: the file is still loaded as
configuration above the prompt, and the envelope is a second, lower-ranked copy of it.

**Forbidding `project` and `local` outright** was considered. An operator who runs issuebot
against a private repository whose only committers they trust may want its `.claude/` in
force, and that is their call to make by name; the default and the warning are the boundary.

**`--strict-mcp-config`** is not added: `--setting-sources user` already leaves the clone's
`.mcp.json` unread (measured), and the flag would also drop the operator's own servers.

## Tests

- `test_agent_instructions`: the list, the order, a missing file, a symlink not followed, a
  directory, an unreadable file, the cut and its flag, undecodable bytes.
- `test_settings`: the default is `["user"]`, `loads_clone_settings` reads the opt-in.
- `test_agent_runner`: `--setting-sources user` is in the fixed argv.
- `test_agent_prompt`: the variables, the envelope's source and author, a cut file's source,
  a file naming the tag cannot close its envelope.
- `test_agent_session`: the clone's `CLAUDE.md` reaches the first prompt enveloped, a symlink
  does not, the continuation prompt repeats nothing.
- `test_workflow_default`: the shipped file leaves the default in force, the section and the
  fallback render, a hostile `CLAUDE.md` survives only inside its envelope, the rule paragraph
  covers the tree, ground rule 5 no longer lets the files win, the self-review brief and the
  pull request body name the privilege change.
- `test_cli`: `validate` reports the sources and warns on the opt-in.
