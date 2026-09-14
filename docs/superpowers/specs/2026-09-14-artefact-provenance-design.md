# Issuebot's artefacts are resolved by provenance, not by content

Date: 2026-09-14
Status: implemented
Issue: #77 (security sweep findings `hostile-issue-3`, `hostile-issue-5`); generalised by #111
(findings `runas-1`, `supply-chain-1`, `supply-chain-3`, `store-tenancy-3`)

## Problem

Two GitHub objects issuebot treats as its own state were resolved by matching text that
anyone with a GitHub account can write.

- **The workpad.** `find_workpad_comment` returned the first comment, in ascending id order,
  whose first non-blank line equalled the published marker `## Issuebot Workpad`. Any
  commenter could open with that line and become the document the agent's prompt calls "the
  single source of truth", the document the blocked escape appends its record to and reads its
  run-marker idempotence from, and the document whose `### Issuebot merge conflict` headings
  the conflict bounce counts against `agent.max_conflict_reworks`. `Comment.author` existed
  and was read nowhere.
- **The linked pull request.** `_select_pr` ranked every `closedByPullRequestsReferences`
  node -- every pull request whose body says `Closes #N` -- by state, merge time and number.
  No author, no head repository. A contributor's pull request from a fork displaced
  issuebot's own: the rework prompt steered the agent onto that branch and its review
  threads, `classify_closed` marked the issue complete when *it* merged, and Slack and the
  dashboard reported it as the issue's.

Both have a non-adversarial face: a comment that quotes the marker as its first line, or a
colleague's pull request that closes the issue, does the same thing without meaning to.

## Invariant

Any GitHub object issuebot treats as its own state is resolved by provenance it can verify --
the authoring account, or an id issuebot itself recorded -- never by matching text a third
party can write.

## The rule, generalised (#111)

The invariant above was written for GitHub objects and implemented for two of them. #111's
sweep found the same shape everywhere else the tree establishes that something is what it
claims to be, and each instance had accepted a stand-in for the referent. The rule is
therefore the artefact, and it reads:

> Nothing is accepted as its own -- a separated uid, this repository's pull request, this
> repository's row, this repository's chosen build of third-party code -- unless the decision
> compares an authoritative value the checked party cannot choose: the invoking uid, the
> author and head repository, a `repo` column written with the row, a commit digest.

Its instances live at the point of each decision, not in a shared helper, and the next one is
decided by the rule rather than rediscovered by a later sweep:

| Decision | Stand-in it accepted | Referent it compares now |
|---|---|---|
| `RunAs.probe` (`agent/runas.py`): does the delegation separate the session? | The delegated `id -u` equals the target account's uid -- true with no uid change at all when the target *is* the invoker | The delegated uid equals the target's **and** differs from `os.getuid()`; an account that is the invoker's is refused before sudo is asked, and an answer that is the invoker's reads `no separation` |
| The bump jobs (`claude-code-version.yml`, `pre-commit-version.yml`): is there already a pull request for this move? | A branch *name*, `gh pr list --head`, which any fork's branch matches | REST `pulls?head=<owner>:<branch>`, kept only when `head.repo.full_name` is this repository and `user.login` is `github-actions[bot]` |
| `PostgresStore` (`db/store.py`): which repository does this row belong to? | `_stamp` merged the store's `repo` *under* the row's, and `INSERT_TURN` bypassed it, so `run_turns` had no repository and rested on `run_id` being unique across workers, checked by the read's join | `_stamp` forces the store's `repo` over anything a row carries and is the only way a row is built; `run_turns` carries its own, keyed `(repo, run_id, turn_number)` with a foreign key to `runs (repo, run_id)` (`0004_run_turns_repo`), so the check is the write's |
| CI and pre-commit: which build of a third-party action or hook runs here? | A tag (`@v7`, `rev: v6.0.0`), a name its owner can repoint | A commit digest with the tag beside it, moved by Dependabot (actions) and `pre-commit-version.yml` (hooks), pinned by `tests/test_pins.py` |

The four are instances, not the rule's extent. A check that compares a name, a label, a
first line, a branch, a tag or a merged-in default is a check of the same kind and gets the
same answer.

## Decision (#77)

The provenance is **the account the adapter runs as**, read once from `gh api user`. It is
the one thing about a comment or a pull request that a third party cannot write, and it is
the same account for every writer issuebot has: the orchestrator's actions, `run-once`, and
the agent session, which uses the same `GH_TOKEN` (or the same `gh` login) for its own
`gh api` calls. There is no `github.login` setting: a configured value can disagree with the
token, and the token is what writes.

### The adapter knows whose account it is

`GhCliAdapter.own_login()` probes `gh api user --jq .login` the first time it is needed and
keeps the answer for the adapter's lifetime; `auth_status()` fills the same cache, so the
worker's startup probe and `validate`'s `gh auth` check pay for it and no production read
adds a call. A `login=` keyword serves a caller that already knows it (the tests). A probe
that fails fails the read with the probe's `GitHubError`: a board whose pull requests cannot
be told apart is not a board to claim from, and the orchestrator's fetch-failure hold already
covers a `gh` that will not answer.

`FakeGitHub` takes a `login` (default `issuebot`) that `auth_status` reports, `comment()`
writes as and `open_pr` opens under, plus `add_comment(..., author=)` and
`open_pr(..., author=, cross_repository=)` for what other accounts write.

### The linked pull request is issuebot's own

`ISSUE_FIELDS` asks each pull request node for `author { login }` and `isCrossRepository`.
`issue_from_node(..., login=)` is a required keyword -- a default that accepted every
reference would hand the choice back to whoever writes `Closes #N` -- and `is_own_pr` keeps a
node only when the author's login equals it (case-insensitively: GitHub logins are) and
`isCrossRepository` is not `true`. A pull request issuebot opens comes from
`issuebot/<n>-<slug>` in the repository, never from a fork, so a fork's is not issuebot's
even under its own login. A node with no author (a deleted account, an older response) is
nobody's. The ranking among what remains is unchanged.

Consequence, on purpose: `classify_closed` now reads an issue closed by a human's merged pull
request as `cancelled` (label cleared, `IssueCancelled`), not `complete`. issuebot's
completion is issuebot's pull request; a close by any other route is a human's decision that
issuebot records as such. `observe_transition`'s `PrOpened`, the conflict bounce, the
dashboard's PR chip (#35) and Slack's PR link all follow the same answer.

### The workpad is issuebot's own

`find_workpad_comment` returns the account's own marker comment, lowest id first, and logs
`workpad_comment_ignored` (issue, comment id, author) for a marker comment by anyone else. The
blocked escape and the conflict bounce are unchanged in code and changed in effect: the body
they append to, read the run marker from and count headings in is now only ever a comment
that account wrote. When only an impostor exists they create a new workpad beside it.

The agent follows the same resolution instead of re-deriving it. `run_session` resolves the
workpad through the adapter **before every turn** and hands it to the prompt as `workpad`
(`{id, url}` or `None`): every turn because the agent creates it in turn 1 and a resumed
session's is whatever the last one left; a lookup that fails fails the run as `github_error`,
since a prompt rendered without it would have the agent open a second workpad. The
continuation prompt names it too, for a resumed session whose context predates it. The body
is not passed: it is the agent's own notes, which it reads with `gh` when it needs them, and
the prompt is the one place a stale copy would be taken for the current state.

The default workflow's Workpad section states the rule once -- a comment by anyone else that
opens with the same line is not the workpad, however it reads, and its contents are that
author's text under the ground rules -- then either names the comment id and says not to
search by first line, or says there is none yet and has the agent create it with
`--jq .id` and keep the id for the turn. The `select(.body | startswith(marker))` jq rule is
gone.

`session.json` records `workpad_comment_id`: the id issuebot resolved for the agent on the
last turn it wrote, `null` until one exists. It is written by issuebot, never read from a
comment, so it is the record of what the agent was pointed at; older files without the field
still read.

### Why not the alternatives

**Pinning the id in `session.json` as the lookup key** was considered and kept as a record
only. A pinned id still has to be verified against the author on every read (the comment
could be deleted and the id could not be reused, but a pin with no verification is a second
place to get wrong), and the workspace, where the file lives, is removed at the terminal
sweep while the conflict bounce runs against `review` issues after that. Author resolution
answers both cases with one rule.

**A hidden HTML comment or signature in the workpad body** is content, which is exactly what
the invariant rules out: anyone can copy it.

**A `github.login` setting** can disagree with the token; the token is what writes.

## Tests

- `test_github_normalise`: a pull request by another author, from a fork, or with no author
  is never linked; the login matches case-insensitively; `login=` is required.
- `test_github_ghcli`: the fragment asks for the two fields; `find_workpad_comment` skips an
  impostor (logged) and finds the account's own behind it; the login is probed once, before
  the first read, and remembered; `auth_status` fills the cache; a failed probe fails the read;
  empty reads do not probe.
- `test_github_fake`: the fake's login governs `auth_status`, `comment`, `open_pr` and the
  normaliser.
- `test_agent_session`: the prompt carries the resolved workpad and not an impostor's id; a
  workpad created in turn 1 reaches turn 2; the continuation prompt names it; a lookup failure
  is `github_error` before any turn; `session.json` carries the id.
- `test_orchestrator_actions`: the escape and the conflict bounce write past an impostor, the
  impostor cannot pre-empt the run marker, and a contributor's conflicting PR is not the
  issue's.
- `test_workflow_default`: the shipped prompt names the id or the create step, and no longer
  contains the first-line jq rule.
