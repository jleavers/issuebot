# The session's authority is fixed outside the prompt

Date: 2026-09-14
Status: implemented (tools, credentials, the envelope); egress landed in #126
(`2026-09-15-session-egress-design.md`), `--restricted` declined in #127
Issue: #109 (security sweep findings `hostile-issue-1`, `hostile-issue-3`)

## Problem

The boundary between GitHub-authored data and issuebot's own instructions was implemented
entirely inside the rendered prompt, at both of its layers, and nothing below the prompt
constrained what a session that read past it could do.

- **The mechanical half was a regex over one literal spelling.** `_TAG_IN_TEXT` defanged a
  `<` followed by whitespace and `github-text`, and nothing else. A zero-width space, a byte
  order mark or a bidi control between the `<` and the name is invisible to a reader and was
  invisible to the regex, so `<​github-text>` reached the model as something it may well
  read as the tag; `check_envelopes` walked the render for the same literal spelling.
- **The policy half handed the text back as commands.** `configs/WORKFLOW.md` stated the rule
  about GitHub text once and then, deliberately and usefully, had the session run a
  reporter's test plan "as you would your own" and follow the issue's reproduction steps.
  Running a reporter's steps is most of what a triage session is for; that is why the prose
  cannot be where the limit lives.
- **And nothing below the prompt limited the result.** `build_argv` emitted `--permission-mode
  auto --permission-prompts none` and appended a tool restriction only when
  `claude.allowed_tools` or `claude.disallowed_tools` was non-empty, and both shipped empty.
  MCP servers came from wherever `claude` found them, the clone's `.mcp.json` included. So the
  session's authority was never a function of how much its input was trusted, and every
  character and word of the prompt layer was load-bearing: each sweep found the next way round
  it, and the next sweep would have found the one after.

#75 separated the session from the *worker*. It did not separate the session from the two
credentials it is handed on purpose, or bound the tools it holds them with.

## Invariant

> A session's authority -- its tool set, its network egress, and the credentials in its
> environment and home -- is the least the workflow needs, and is fixed entirely outside the
> prompt, so that neither a re-authorisation in the workflow's prose nor a bypass of the text
> envelope can widen what GitHub-authored text is able to do.

## Design

Three properties of the *process*, set where it is spawned, and then a prompt that describes
what the session may do within them.

- **Tools are a setting, and the setting ships restrictive.** `claude.disallowed_tools`
  defaults to `DEFAULT_DISALLOWED_TOOLS`, `WebFetch` and `WebSearch`: the workflow never needs
  the model's own network tools -- it reads GitHub through `gh` and the repository through its
  clone -- and a session whose input is text somebody else wrote should not hold a
  purpose-built way to fetch the next page of it. `build_argv` emits the list, so a default
  argv now carries `--disallowedTools WebFetch WebSearch`, and always emits
  `--strict-mcp-config`, so no MCP server from the clone's `.mcp.json`, the project's settings
  or the account's home joins the set (#119, `2026-09-14-mcp-config-confinement-design.md`,
  measured that `claude -p` does load `mcpServers` from the session account's `~/.claude.json`
  at the shipped defaults, and landed the same unconditional flag); `claude.mcp_config`, a list of what `--mcp-config`
  takes (a path resolved against the workflow's directory, never relative to the clone, which
  is the session's cwd and the session's to write) and empty by default, is the one route in, so a deployment that used a server from
  its home names it in the front matter and gets it back. That is a change on upgrade: a
  server a session found for itself is gone until the front matter names it. A deny list rather than an allow list, on purpose: an allow list would
  have to name every tool the workflow needs, and tool names move between `claude` releases
  (`Task` became `Agent`), so a hard-coded one would break sessions silently on the weekly
  version bump, while the deny list names two tools that are not going anywhere. A rename
  fails *open* -- the session would hold the renamed tool -- and that is the trade taken
  knowingly: an allow list fails closed on the same rename, by breaking every session, and this
  list is two entries long, so the cost of the open failure is bounded and the weekly version
  bump is where a rename would be seen. An operator widens the list in
  the front matter (`disallowed_tools: []`; a list replaces as a whole under the overlay), and
  that is outside the prompt, which is the point: neither the prose nor an issue can. The
  Dockerfile asserts both flags at build beside `--permission-prompts`, since a release that
  dropped either would widen every session's tool set without a word.

  The web tools are the model's egress, not the process's: a `curl` under `Bash` still leaves
  the container. That is the egress half below.

- **The credential's reach is named.** The session necessarily holds `GH_TOKEN` -- it clones,
  pushes and comments with it -- and the Claude login is its own by construction (#75). What
  can be scoped is the token's *reach*: a fine-grained token restricted to `github.repo` with
  Contents, Issues and Pull requests, which the README has always asked for, reaches one
  repository, while a classic (`ghp_`), OAuth (`gho_`) or App user (`ghu_`) token reaches every
  repository its account can. `validate`'s `github.token` check now reads GitHub's own prefix
  and warns for the second kind, naming the alternative and the repository; a fine-grained
  (`github_pat_`) or installation (`ghs_`) token, or an unknown prefix, stays `ok`. A warning
  rather than a failure: the prefix is a heuristic, the token is the operator's, and a classic
  token is the only kind that reads check runs (README, "Prerequisites").

- **The envelope is demoted from boundary to hint, and the hint is fixed.** `_defang` replaces
  the literal regex, and reads the text through its *skeleton* (`tag_skeleton`: Unicode
  format characters, general category `Cf`, removed; compatibility forms folded by NFKC). The
  format characters are stripped once, each kept character's raw index remembered; for every
  `<` -- and the fullwidth and small forms NFKC folds to it -- the gap that may follow it
  (`\s*/?\s*`, unbounded as the literal regex's was, and linear since a run of whitespace
  follows one `<` and no other) is matched on the stripped text, and the dozen characters
  where the name would be are NFKC-folded and matched against `github-text\b`. So a `<`
  split from the name by U+200B, a BOM before a `/`, a name with a U+200B inside it, a
  fullwidth less-than or a fullwidth `g`, and a `<` with any length of spaces or invisible
  characters before the name are all the tag, and the `<` is replaced in the raw text with the
  padding left as data. It is total over the same skeleton `check_envelopes` walks, so text
  inside an envelope can never fail the render however its tag is spelled -- a deterministic
  failure would be retried `max_attempts` times and escalated blaming the template -- while a
  template that cut a tag, or a value no envelope wraps (what #105 closed for labels and
  logins), still does. The `Cf` class is
  written as ranges, since a table walk at import is 1.1 M code points, and a test pins it
  against `unicodedata`, so a Unicode update that adds a format character fails a test rather
  than a sweep.

  What this normalisation misses -- a `<` lookalike NFKC does not fold, a combining mark
  between the `<` and the name (`<` U+0338 composes to a single negated less-than), a
  mathematical-script letter in the name -- reaches the model as text the prompt's rule may or
  may not cover, and widens nothing. That is the demotion: the regex is no longer the thing
  the next sweep has to get past to reach a token.

- **The prose describes the authority; it does not grant one.** `configs/WORKFLOW.md` states,
  once, after the rule about GitHub text and before the first envelope, that what the session
  may do is fixed by issuebot before the document is read and by nothing in it -- the tools
  `claude` was started with, a token that should reach the one repository, the account it runs
  as -- and that nothing written there, in the issue or in anything fetched can widen it. The
  two passages that hand reporter-supplied steps back as work to do now say "within your
  authority and under the ground rules", and a step that needs more than the session has is a
  request to note in the workpad, not a reason to look for a way round. Both passages keep
  their purpose: running a reporter's reproduction is still most of what a triage session is
  for, and the change is which layer says how far it may go.

## What this does not do

- **Egress.** The session's network is the container's, and a compose network cannot filter
  by name. The design that does is an allow-listing forward proxy beside the worker -- the
  worker on an `internal` network, the proxy on that and the default one, `HTTP_PROXY` and
  `HTTPS_PROXY` in `agent_environment`'s allow-list, `api.anthropic.com`, GitHub and the
  registries the hooks need on the list -- which is a deployment change too large to make
  unattended here. Filed as #126 with that sketch; until it lands, the tool policy removes
  the model's own egress and the container's remains.
- **`--restricted`.** `claude` has a mode that removes the code-running tools unless `--tools`
  names them, confines the file tools to the working directories, refuses `bypassPermissions`,
  ignores user, project and local settings files, and lets only a person or the configured
  permission handler approve writes to settings, git and tool-configuration files. **Declined
  in #127** -- not adopted as a default, and not added as an opt-in `claude.restricted` either,
  since a setting this project cannot recommend turning on is a supported surface bought for
  nothing. The reason is the allow list, and it is the one this document already gives two
  sections above.

  The mode does not leave the choice open. Measured against the image's own `claude` 2.1.263,
  through the credential-free init-line probe the CI `docker` job already uses for
  `--strict-mcp-config`: the same argv lists `Bash` without the flag and does not list it with
  it, so `--restricted` removes the tool an issuebot session *is* -- `git`, `gh`, the target
  repository's test runner -- unless `--tools` names it back. Adoption therefore forces the
  allow list that the tool policy above refused on purpose, on the ground that "tool names move
  between `claude` releases (`Task` became `Agent`), so a hard-coded one would break sessions
  silently on the weekly version bump". The same probe confirms the prediction and sharpens it:
  `--tools Bash,Edit,NoSuchTool` comes back `Bash,Edit`, exit 0, nothing on stderr, and
  `TodoWrite` -- a real-looking name, not a nonsense one -- disappears from a longer list just
  as quietly. So the failure is not merely closed, it is *silent*: a rename would start a
  session that looks healthy, has no way to do its work, and fails `max_attempts` times per
  issue with claude's own words rather than a build that stops. `claude-code-version.yml`
  bumps this deployment weekly, which is the cadence the hazard runs at. The Dockerfile's
  `claude --help` assertions cannot cover it, because the flag would still be there; what
  the argument list resolves *to* is what moves.

  Two of the three doubts the issue was filed with are settled, so that nobody spends the
  investigation again. The version floor is **not** an obstacle: the changelog carried in the
  `claude` binary records `--restricted` (and `CLAUDE_CODE_RESTRICTED=1`) added at **2.1.248**,
  below `MIN_CLAUDE_VERSION`'s 2.1.259, so every release issuebot permits already has it and
  neither a raised floor nor a version probe would be needed. And the existing confinement
  would survive adoption: `--disallowedTools` still layers on top of `--tools` under the flag
  (`--restricted --tools Bash,Edit,WebFetch` lists `WebFetch`; adding
  `--disallowedTools WebFetch WebSearch` removes it again), as does `--strict-mcp-config`, so a
  future attempt would keep the deny list rather than trade it away. The env spelling is
  already inert from the workspace: `CLAUDE_` is in `PROTECTED_ENV_PREFIXES`, so a session
  cannot set or clear `CLAUDE_CODE_RESTRICTED` through its own `.issuebot/env`.

  The third doubt stands and is joined by a larger one. `--restricted` ignores the settings
  files `--setting-sources` selects -- measured the same way, with a skill planted at user
  scope and another at project scope: `--setting-sources user,project` lists both in the init
  line, and adding `--restricted` lists neither. So the flag would make that argument a no-op,
  and silently void the README's promise that `claude.setting_sources: [project]` loads a
  target repository's own permission rules and hooks -- a documented opt-in that would keep
  reading as honoured while doing nothing. And the mode's most valuable clause is the one that
  might break the workflow outright: with `--permission-prompts none` there is no approval
  surface,
  so anything routed to "only a person or the configured permission handler" is denied
  automatically, and every issuebot session commits, merges and pushes. Whether that clause
  reaches `git commit` was **not measured**, and neither was whether the working-directory
  confinement binds `Bash` or only the file tools -- which matters because adoption has to hand
  `Bash` back, and a session holding it writes wherever its uid reaches whatever the file tools
  may touch. Both need a permission decision on a real tool call, so both need a model turn,
  and the session that decided this held no Claude credential. They are the first thing a
  reconsideration owes.

  What the mode would add, set against that, is mostly already held: the session runs at its
  own uid in its own workspace (#75, #121), the worker reads back out of it only through
  `boundary.py` (#104), the clone's `CLAUDE.md`, `.claude/` and `.mcp.json` are already data
  rather than configuration (#107, #119), the account home's instruction, shell start-up and
  tool-configuration surfaces are swept before every turn and every login shell (#101, #137,
  #151), the environment spelling of that last one is refused (#171), and egress goes through
  an allow-listing proxy (#126). `bypassPermissions` is refused by a mode an operator sets in
  the front matter, outside the prompt, so refusing it guards a misconfiguration rather than
  the threat this document is about. What is left is the confinement over the account's own
  `$HOME` -- worth having, but bounded above by the `Bash` question just named, and standing
  beside sweeps that already clear that home before every turn and every login shell. So the
  trade is a mandatory silently-lossy allow list and a voided promise, for confinement that is
  largely bought elsewhere and whose remainder is not yet known to hold.

  Reopen it if `--tools` grows a way to fail loudly on a name it does not know -- an error, a
  warning, or an init line issuebot could assert the resolved set against at build, the way
  the CI `docker` job already asserts `mcp_servers` -- or if the person-only approval turns out
  not to reach an ordinary `git commit`, and the `setting_sources` promise is re-stated to say
  what the flag leaves standing.
- **The Claude credential.** It is the session's own and stays so (#75, "What this does not
  do").
- **Labels and the clone's instruction files.** #105 (a label reaching the prompt bare,
  landed in #120 while this was in review: every GitHub-authored value now goes through the
  same defang) and #107 (the clone's `CLAUDE.md` and `.claude/`, landed in #131 while this was in
  rework: `claude.setting_sources` now defaults to `[user]`, so those files reach the session
  as enveloped data rather than as claude's own configuration) are their own
  bypasses; this change bounds what any of them can reach, which is why it is independent
  of both.

## Tests

`tests/test_agent_runner.py` pins the argv (the deny list and `--strict-mcp-config` on a
default session, the list emptied by a setting with the MCP flag staying, and both reaching
the recorded process); `tests/test_settings.py` the default and its widening;
`tests/test_agent_prompt.py` the defang across format characters, compatibility spellings and
padding of any length, that no spelling inside an envelope fails a render, the `Cf` class
against `unicodedata`, and the structure check on the skeleton; `tests/test_workflow_default.py` the authority paragraph's place and wording and the
shipped front matter's tool policy, MCP set included; `tests/test_resolve.py` an `mcp_config`
path resolved against the workflow's directory and a JSON document passed through;
`tests/test_cli.py` the token-reach verdicts (a literal token reporting both halves on one
line) and the `claude.mcp_config` check -- a missing path, one that is not a regular file, one
the session's account cannot read, a delegation too broken to ask, and the warning an inline
document earns for being an argv element; `tests/test_agent_scrub.py` the credential names
spelled as JSON keys, which is how an MCP `env` block spells them; `tests/test_agent_runner.py`
again for the turn's start line carrying that argv scrubbed;
`tests/test_image_layout.py` the Dockerfile's flag assertions.
