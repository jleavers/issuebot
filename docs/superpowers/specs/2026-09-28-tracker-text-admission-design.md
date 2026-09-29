# Tracker text reaches a session only as a maintainer approved it

Date: 2026-09-28
Status: sections 1--3 implemented (#246, #247, #248); section 4 approved 2026-09-29
Advisory: GHSA-jm8h-q3j6-p8xp (private, filed 2026-09-27 from a sibling deployment's security
sweep; severity high; closed 2026-09-29 with the three fixes on `main`) and, for section 4,
GHSA-f3fm-r55f-2vgm (private, filed 2026-09-29 from the same deployment's pre-publication
sweep against `3b7b6ad`; severity high)

## Problem

issuebot's workflow was built on one premise: a human applying `issuebot/todo` approves what a
session will act on. The advisory found three things that let text, or authority, past that
approval on any repository that accounts other than its maintainers can write to -- which
includes this one.

1. **The approved body is not pinned.** `configs/WORKFLOW.md` tells a session to *run* the
   steps of any `Validation`, `Test Plan` or `Testing` section in the issue description
   (`## Workpad`, and step 4 of `## No fault found`). The description is rendered from the
   issue as it is fetched for the turn (`agent/prompt.py`, `issue_variables`), and nothing
   records the text a maintainer read when they applied the label, or notices an edit after
   it. An issue's author can edit their own issue after a maintainer has labelled it, and the
   next turn runs the edited steps.
2. **Comments are not filtered by author association.** The rework context and Step 6 have
   the session read every review comment on the pull request and every comment on the issue,
   and answer each one. The text is framed as data, which is the right frame, but on a public
   repository any account can comment on an issue in `review` or `rework`, and nothing drops,
   quarantines or flags a comment whose `author_association` is not a maintainer's. What
   stands between that text and the session's shell is the model obeying the frame.
3. **The GitHub identity a deployment runs as.** The documentation covers the *local* account
   a session runs as (`agent.run_as`, `ISSUEBOT_AGENT_USER`) and says nothing about the
   GitHub account. A deployment that runs with its maintainer's token gives a persuaded
   session the maintainer's push and merge power, and `api.github.com` is on the egress
   allow-list because the workflow needs it. Containment bounds which hosts data can reach;
   it does not bound what a session does with `GH_TOKEN` at GitHub.

## Invariant

Tracker text reaches a session that holds a shell or a GitHub credential only if a maintainer
wrote it, in the version a maintainer approved, labelled with its author and that author's
association. And the identity a session acts as cannot merge work no human approved.

## Design

Three changes, one per finding, each its own pull request.

### 1. The approved text is pinned, and GitHub is the record

Amended 2026-09-28 after the whole-branch review: issuebot's own `todo` approves when its
account administers the repository; only `todo` approves, never `rework`; an edit in the
approval's own second counts as after it; and a history past the page ceiling is a verdict,
not a failed read, with verdicts remembered while the issue is unchanged. (The same review
also made a label applied by an app or a bot no approval.)

Nothing is stored. GitHub already keeps everything the check needs, and reading it at dispatch
time means the check cannot drift from the store, cannot be reset by a restart, and costs one
GraphQL query per *dispatch* rather than per poll:

- **Approval** is the `LabeledEvent` for `todo` with the latest timestamp (the greatest `at`,
  not the last event read) whose actor is a person -- a `User`; a label an app or a bot
  applies has no approver -- and is not the account issuebot runs as, unless that account
  administers the repository. Only `todo` approves. `rework` never does, whoever applies it:
  the conflict bounce (`actions.conflict_rework`) applies it itself, and a reviewer's `rework`
  asks for changes to the pull request, not for the issue's current text, so an edit made
  during review is checked against the `todo` before it and adopted only by applying `todo`
  again. issuebot's own `todo` is excluded on a dedicated account, so its relabel cannot
  launder an edit that came before it; on the maintainer's own token it counts
  (`RepoInfo.admin`, read once per process), because excluding it there leaves no approver at
  all and every issue refused for ever, and an account that administers the repository can
  rewrite the branch ruleset anyway, so its label is a maintainer's.
- **Edits** are the issue's `userContentEdits` (each with `editedAt` and `editor.login`; the
  body) and its `RenamedTitleEvent` timeline items (each with `createdAt` and `actor.login`;
  the title). Both are what the prompt renders.

**Rule.** An edit made at or after the approval by any account other than the approver
un-approves the issue. The approver editing what they approved is fine -- a solo operator
labels an issue and then tightens its wording without being asked to approve their own change
-- and anyone else's edit is not: the author's, because that is the finding; another
maintainer's, because the alternative is a permission lookup per editor, and refusing a
maintainer's edit costs one relabel where admitting an outsider's costs the advisory. The false
positive is named here so it is not later mistaken for a bug. An edit stamped in the
approval's own second counts as after it: GitHub records both to the second, the approver's
own edits are exempt whatever their time, so a tie that matters is someone else's edit landing
in that second, and assuming the label went on last would admit it.

**What happens.** A new escape in `orchestrator/actions.py`, `unapproved_escape`, shaped like
`blocked_escape` and label-first under the same rule (#128, #157): it appends a workpad block

```text
### Issuebot unapproved edit (<stamp>)

The description or title was edited at <edited at> by <editor>, after <approver> applied
`issuebot/todo` at <approved at>. issuebot has removed the label. Read the current text; if it
is what you want done, apply `issuebot/todo` again.
```

then removes the state label (`adapter.clear_state`). The issue leaves the board, which is
what a human notices; a `Blocked` event is published (`reason`: "description edited after
approval by <editor>"), which is a Slack line and a count on the dashboard's blocked tile; and
re-approval is applying the label again, which is a new `LabeledEvent` after the edit and so
passes. That is the recovery [`docs/operations.md`, "Blocked"](../../operations.md#blocked)
already documents -- fix the cause, then relabel -- so the operator learns nothing new. The
ledger marks the issue `escaped` as for the other escapes, and `dispatched` clears it.

The check runs for every dispatch: a `todo` claim, a `rework` claim, and an orphaned
`in_progress` resume, whose approval is the human's label event before issuebot's own claim.
It runs in `Orchestrator._dispatch` before `_bind_account` and before `actions.claim`, so an
un-approved issue is never moved to `in_progress` and never holds an account. A `GitHubError`
from the read fails closed: the issue is not dispatched this tick, `approval_check_failed` is
logged once per issue and reason (the `IssueLedger.reported_refusal` pattern), and the next
tick asks again. A history past the page ceiling (`PageCeilingError`) is the exception: it is
a property of the issue, not of the moment, so it is an `Unapproved` verdict -- nobody can say
what was approved -- and the escape takes the label off rather than the next tick reading the
same pages again. The last verdict is remembered per issue against the poll snapshot's
`updated_at`, title and body, so a candidate held back by a busy account is not re-read every
tick. An issue whose `todo` has *no* human `LabeledEvent` at all -- a label applied by an app
-- is un-approved too, because "nobody approved it" is the same fact as "someone edited it
after approval".

**Pieces.**

- `github/models.py`: `LabelApplied(label, actor, at)`, `TextEdit(editor, at, what)` (`what`
  is `"body"` or `"title"`) and `ApprovalEvidence(approvals, edits)`, all frozen.
- `github/adapter.py`: `approval_evidence(number) -> ApprovalEvidence`, on `GhCli` (one query
  over `timelineItems(itemTypes: [LABELED_EVENT, RENAMED_TITLE_EVENT])` and
  `userContentEdits`, each paginated under a ceiling of its own; past the ceiling is a
  `response` error, as for `count_own_label_additions` (a `PageCeilingError`, which the
  orchestrator maps to `Unapproved`)) and on the fake, whose records gain an edit and a
  rename operation so the tests are hermetic.
- `orchestrator/approval.py`, pure: `assess(evidence, *, admitting: StateLabel, own_login,
  labels) -> Approved(approver, at) | Unapproved(reason, edit, approval)`.
- `orchestrator/actions.py`: `unapproved_escape`.
- `orchestrator/orchestrator.py`: the call in `_dispatch`, and the refusal's logging.
- `configs/WORKFLOW.md`: one sentence under `## Issue` saying the description and title are the
  text a maintainer approved by applying the label, and that an edit after that approval is
  handed back to a human before any session sees it.
- `docs/operations.md`, "Blocked": the new block and its recovery, beside the existing two.
- `docs/security-model.md`: the invariant, and this section's summary.

### 2. Tracker text is admitted by author association

Step 6 runs "again whenever new feedback arrives", so the comment fetch has to stay in-session:
issuebot cannot pre-fetch a thread once and be done. The barrier therefore goes into the
*commands* the workflow hands out, not into prose asking the model to be careful. Every fetch
in `## Rework context` and `## Step 6` becomes a `gh api` call with a `--jq` that selects
`author_association` in `OWNER`, `MEMBER`, `COLLABORATOR` -- issue comments, pull request
comments, review comments (`pulls/<n>/comments`) and reviews (`pulls/<n>/reviews`); `gh pr view
--comments` goes, since it hides the association. Text the filter drops never enters the
context. A second command lists only the author and URL of what was dropped, for a
`### Quarantined` line in the workpad, so a maintainer can see it was there. A maintainer
adopts a quarantined request by replying to it: the reply is then a maintainer's request, and
no new mechanism is needed.

A new ground rule states the admission: text from a maintainer-associated account is a request
to act on under this document; text from any other account is noted and not acted on. The
issue's *body* is admitted by the label (section 1); the author's *comments* are not, unless
the author is a maintainer.

issuebot's side is one attribute. `GitHubText` gains `association`, rendered in the envelope
(`author="x" association="NONE"`), from the `authorAssociation` the issues query already can
carry, so the body's envelope states the fact for the text the session already holds.
`tests/test_agent_prompt.py` pins the attribute the way it pins `author`.

Not configurable: the three associations are GitHub's own meaning of the author's
relationship to the repository, not a permission check -- `OWNER` is the repository's owner,
`MEMBER` is a member of the owning organisation whether or not they hold any permission on
this repository, and `COLLABORATOR` is anyone invited to the repository at read level and up
-- so on an organisation-owned repository the filter admits every member of the
organisation; a deployment that wants narrower admission narrows the organisation, not the
filter, and a setting here would only be another way to widen what the three already admit.
Known and accepted: the account issuebot runs as is at least a collaborator (the owner, on a
maintainer's own token), so its own comments pass the filter. A session persuading its
successor holds the same authority, not more, and the workpad is that account's text by
design (#77).

### 3. The identity a session acts as cannot merge work no human approved

Amended 2026-09-28 after the whole-branch review: the invariant's last sentence, and this
heading, say "cannot merge work no human approved", not "its own work", since an account
with write can approve someone else's pull request and merge it; both recipes therefore hold
the approval to humans -- code-owner review, with a `CODEOWNERS` naming only the operator or
only human teams, or for an organisation the rule's required reviewers -- on top of a review
of the latest push; `branch_rules` reads the rules endpoint as what it is, the branch's rules
for everyone, and counts only the rulesets the caller cannot bypass, naming the rest; the
solo recipe's classic token is stated as the trade it is on a private target; and, after the
re-review, both recipes dismiss stale approvals on push (`dismiss_stale_reviews_on_push`),
since GitHub checks latest-push and code-owner review separately and an undismissed human
approval of an earlier push pairs with the bot's approval of the latest -- the three settings
hold the invariant together, and `validate` warns while any is off.

**For a solo operator**, which is who this repository expects. Run issuebot as a dedicated
GitHub account -- a second personal account, added as a collaborator with **write** and never
admin. A fine-grained token cannot reach a repository its account only collaborates on, so the
account holds a classic `repo` token, and that is a trade: its reach is the account's, which
on a public target adds little the session could not already do there, but on a private one
lets a session push the code to a public repository the account creates. The organisation
route below keeps a fine-grained token, and is the better one for a private target. Put a
ruleset on the default branch whose `pull_request` rule sets three things together: one
approving review of the latest push (`require_last_push_approval`), from a code owner
(`require_code_owner_review`), with stale approvals dismissed on push
(`dismiss_stale_reviews_on_push`) -- and a `CODEOWNERS` naming only the operator
(`* @<operator>`), so that every path has a human owner. The operator approves the bot's pull
requests from their own account. Of the latest push, because otherwise an approval of an
earlier push still satisfies the rule after a later one, and issuebot's own conflict bounce
pushes to an already-approved pull request. From a code owner, because otherwise any account
with write approves: on a pull request someone opened from a fork, they are the last pusher,
so the bot's approval satisfies the rule and the bot can merge it. Stale approvals dismissed,
because GitHub checks the other two separately: without it the operator's approval of an
earlier push survives the next one, and the bot's approval of that push completes the pair.
A ruleset's bypass list cannot name a personal account, so the hazard is a role -- the
`Write` role, which is the bot's, must never be on it. The operator's own pull requests need
an approver too, and GitHub does not let an author approve their own, so the `Repository
admin` role is a bypass actor in `pull_request` mode: their pull requests merge without a
second account, and a direct push to the branch is still refused. What the three settings
guarantee together, and none of them alone, is the invariant's last sentence: the session's
identity cannot merge work no human approved, and cannot rewrite the rule that says so,
because it is not an admin.

**For an organisation**, the same shape with the organisation's own tools in place of the
personal ones. The dedicated account is a machine user -- GitHub's name for a personal account
an organisation creates for automation -- made a member of the organisation, which keeps a
fine-grained token (an outside collaborator is back to the classic one), with **write** on the
repository and no seat on any team that carries admin or maintain. The ruleset is an
*organisation* ruleset targeting the repository's default branch, rather than a repository
one: a repository admin cannot remove it, so the guarantee holds against the repository's own
admins too, and it applies to every repository the organisation points a deployment at. It
requires a review of the latest push and dismisses stale approvals for the same reasons, and
holds the approval to humans the same way, as part of the recipe rather than the
organisation's choice: code-owner review with a `CODEOWNERS` naming human teams for every
path, or the rule's required reviewers naming them. Its
bypass actors are teams of humans the machine user is not on; its approvers are whoever
reviews there already, so no admin bypass is needed and none is granted. A GitHub App
installation token is *not* the recommended credential, though `gh` accepts one: it expires
after an hour, and a session can run longer than that.

Both are documented in `docs/security-model.md` in a section, "The account a session acts as",
solo operator first and organisation second, since the first is who this repository expects;
and the README's Prerequisites gets a pointer to it beside the token choice point -- inside
the bounds `tests/test_readme_bounds.py` pins, which holds the consequence within 700
characters of its incentive.

**`validate`** says when a deployment is not in that shape. Two warnings in
`cli._probe_github`, from two reads the adapter gains:

- `RepoInfo.admin` (from `repos/{repo}`'s `permissions.admin`): when true, `github.token
  account` warns that the token's account is an admin of the repository and a session holding
  it can bypass or rewrite the branch ruleset; run as a dedicated account with write access.
- `branch_rules(branch) -> BranchRules(required_approving_reviews: int | None,
  require_last_push_approval, require_code_owner_review, dismiss_stale_reviews_on_push,
  bypassable)` (from
  `repos/{repo}/rules/branches/{branch}`, which lists the branch's rules for everyone -- it
  does not leave out a rule the caller can bypass -- and, for each ruleset carrying a
  `pull_request` rule, `repos/{repo}/rulesets/{id}`'s `current_user_can_bypass`, where only
  `never` binds): when the token's account can bypass a ruleset carrying the review rule,
  `github.branch rules` warns and names it; otherwise it warns unless the rulesets that bind
  require at least one approving review of the latest push from a code owner, and dismiss
  stale approvals on push. Rulesets only: classic branch protection is readable by admins
  alone, and the check must work for the account it recommends. The warning says so. It cannot
  read `CODEOWNERS`, so an OK line proves neither that the session's account is not itself a
  code owner nor that every path has one: code-owner review binds only the paths that do.

Both are warnings, not failures: `run-once` against a personal scratch repository is a
legitimate use and should not be refused.

### 4. What the approver saw, and nothing the session itself wrote

Added 2026-09-29 for GHSA-f3fm-r55f-2vgm, which found what sections 1--3 leave open on a
public repository. Two stand-ins for a maintainer's intent were sound while the target was
private and are not once anyone can open an issue:

- **The label approves what the maintainer saw rendered; the session gets the raw
  Markdown.** Section 1 pins the bytes as they stood when a person applied `issuebot/todo`,
  but the person read GitHub's rendered page. An HTML comment is hidden from the page and
  reaches the prompt intact, inside the `Validation` / `Test Plan` section the workflow tells
  the session to run. So are Markdown link-reference definitions nothing references, and
  Unicode format characters. And a body a maintainer read can point at something outside
  it -- a fork branch, a file, a release asset -- that its author rewrites after approval
  while the body's edit history stays clean; the egress allow-list keeps that to
  GitHub-hosted references, which is still a fork branch.
- **The account issuebot runs as passes section 2's filter.** A dedicated bot account is a
  `COLLABORATOR`, so a session steered through the first gap can leave comments and reviews
  on every open issue and pull request, and each later session must address them before
  review. Section 2 accepted "a session persuading its successor" as the same authority;
  what it under-weighted is persistence and spread from one injection. The workpad is
  excluded by id; nothing else the account writes is. (An issue the account files is not the
  same vector: it still needs a human's `issuebot/todo` before any session acts on it.)

**Invariant, tightened.** A session treats tracker text as a request only when it is what a
human maintainer wrote or approved *as they saw it*: not bytes the rendered page hid, not
something the text points at that can change after approval, and not the session account's
own output.

**Fix shape, one pull request.**

1. **The body a session gets is the body the approver saw.** A pure `visible_text(markdown)`
   in `agent/visible.py`, applied by `issue_variables` to the title and body before they go
   into the envelope, removes what GitHub renders as nothing: HTML comments; link-reference
   definitions (`[label]: target`) that no reference in the text uses; and Unicode format
   characters (category `Cf`: zero-width joiners and spaces, direction marks, the BOM). It is
   fence-aware -- an HTML comment inside a fenced block or an inline code span *is* rendered,
   so those stay. What it deliberately leaves, and the docs say so: a collapsed `<details>`
   block (its summary line is visible and a reader can expand it) and length (a long body was
   on the page). GraphQL's `bodyText` is the rendered text and was rejected because it
   flattens code fences, where the steps live. The prompt's paragraph after the description
   says the text is as it renders, and that what the page hides is not here.
2. **A reference the body makes is followed only if pinned.** A ground rule in
   `WORKFLOW.md`: a step that fetches something the description points at is followed only
   when the reference is pinned by content -- a commit SHA, a digest -- and a branch name or a
   URL whose content can change after approval is a request to note in the workpad, not a
   step to run. A prompt rule, not code: egress already refuses everything but GitHub.
3. **The session's own account is not a maintainer.** `PromptContext` gains `login`, the
   account the session acts as (`adapter.own_login()`, which `run_session` and `run-once`
   both have; `validate`'s sample context uses a placeholder). Step 6's four filters and the
   quarantine list become `select(.author_association | IN("OWNER","MEMBER","COLLABORATOR")
   and .user.login != "{{ login }}")`, and Ground rule 7 says text the account itself wrote
   -- comments, reviews -- is agent output, not a request. The workpad's by-id calls are
   unchanged.
4. **`validate` checks the prompt in force**, which is also #250. The `prompt` check renders
   the merged template already; it gains the scan `tests/test_workflow_default.py` makes over
   the shipped prompt: every `gh` command touching `/comments` or `/reviews` carries the
   association filter *and* the own-login exclusion, or is one of the workpad's by-id calls;
   a warning names the first command that does not, and a warning when `--comments` or
   `--json reviews`/`--json comments` survives. A warning, since a replaced prompt is the
   operator's, but they are told it has no comment barrier.

Not configurable, as before. Accepted residual: a collapsed `<details>` block, which the
docs name; and the model's obedience to the reference rule, which is the same class as
every other prompt rule.

## Testing

- `tests/test_orchestrator_approval.py`: the pure decision -- approved; edited by the approver;
  edited by the author after; title renamed after; issuebot's own `rework` event is not an
  approval; no human label event at all; edit before the approval.
- `tests/test_orchestrator_actions.py`: `unapproved_escape` label-first behaviour on a
  refused note, the idempotent block, the `Blocked` event.
- `tests/test_orchestrator.py`: a `todo` issue edited after labelling is not claimed and loses
  its label; relabelled, it is claimed; a failing evidence read skips the tick and logs once.
- `tests/test_github_ghcli.py`: the query's pagination and its `response` error past the
  ceiling; `RepoInfo.admin`; `branch_rules` over the rule shapes above, the union over the
  rulesets that bind, and a bypassable ruleset named rather than counted.
- `tests/test_agent_prompt.py`: the `association` attribute.
- `tests/test_workflow_default.py`: every
  comment-fetching command in the shipped workflow carries the association filter.
- `tests/test_cli.py`: the two new checks, warn and ok.
- `tests/test_doc_pointers.py` and `tests/test_readme_bounds.py` cover the prose.
- Section 4: `tests/test_agent_visible.py` (each hidden class removed; a comment inside a
  fence and inside an inline code span kept; a comment straddling lines; a referenced link
  definition kept and an unreferenced one removed; an all-hidden body renders empty);
  `tests/test_agent_prompt.py` (the title and body variables carry the visible text; the
  `login` variable); `tests/test_workflow_default.py` (the login in every filter; the
  ground rules' phrases; the every-command scan requiring both halves);
  `tests/test_cli.py` (the `prompt` check warns on an overlay prompt lacking either half and
  passes on the shipped one).

## Out of scope

What the advisory listed as not verified -- the untracked prompt overlay, token scopes, the
egress list, and whether `claude`'s `Bash` tool passes `CLAUDE_CODE_OAUTH_TOKEN` to child
processes -- is a security sweep of this tree, not this design. Private vulnerability
reporting, which the advisory also noticed was off, was on again by 2026-09-27.
