# Tracker text reaches a session only as a maintainer approved it

Date: 2026-09-28
Status: approved, not yet implemented
Advisory: GHSA-jm8h-q3j6-p8xp (private, filed 2026-09-27 from a sibling deployment's security
sweep; severity high)

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
association. And the identity a session acts as cannot merge its own work.

## Design

Three changes, one per finding, each its own pull request.

### 1. The approved text is pinned, and GitHub is the record

Nothing is stored. GitHub already keeps everything the check needs, and reading it at dispatch
time means the check cannot drift from the store, cannot be reset by a restart, and costs one
GraphQL query per *dispatch* rather than per poll:

- **Approval** is the latest `LabeledEvent` on the issue for the admitting label -- `todo`,
  or `rework` -- whose actor is *not* the account issuebot runs as. The conflict bounce
  (`actions.conflict_rework`) applies `rework` itself, so issuebot's own label events are not
  approvals; the human's earlier one is.
- **Edits** are the issue's `userContentEdits` (each with `editedAt` and `editor.login`; the
  body) and its `RenamedTitleEvent` timeline items (each with `createdAt` and `actor.login`;
  the title). Both are what the prompt renders.

**Rule.** An edit made after the approval by any account other than the approver un-approves
the issue. The approver editing what they approved is fine -- a solo operator labels an issue
and then tightens its wording without being asked to approve their own change -- and anyone
else's edit is not: the author's, because that is the finding; another maintainer's, because
the alternative is a permission lookup per editor, and refusing a maintainer's edit costs one
relabel where admitting an outsider's costs the advisory. The false positive is named here so
it is not later mistaken for a bug.

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
tick asks again. An issue whose admitting label has *no* human `LabeledEvent` at all -- a label
applied by an app, or one older than the query's page ceiling -- is un-approved too, because
"nobody approved it" is the same fact as "someone edited it after approval".

**Pieces.**

- `github/models.py`: `LabelApplied(label, actor, at)`, `TextEdit(editor, at, what)` (`what`
  is `"body"` or `"title"`) and `ApprovalEvidence(approvals, edits)`, all frozen.
- `github/adapter.py`: `approval_evidence(number) -> ApprovalEvidence`, on `GhCli` (one query
  over `timelineItems(itemTypes: [LABELED_EVENT, RENAMED_TITLE_EVENT])` and
  `userContentEdits`, each paginated under a ceiling of its own; past the ceiling is a
  `response` error, as for `count_own_label_additions`) and on the fake, whose records gain an
  edit and a rename operation so the tests are hermetic.
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

Not configurable: the three associations are GitHub's own meaning of "can act on this
repository", and a setting here would be a way to widen it. Known and accepted: the account
issuebot runs as is a collaborator, so its own comments pass the filter. A session persuading
its successor holds the same authority, not more, and the workpad is that account's text by
design (#77).

### 3. The identity a session acts as cannot merge its own work

**For a solo operator**, which is who this repository expects. Run issuebot as a dedicated
GitHub account -- a second personal account, added as a collaborator with **write** and never
admin. Put a ruleset on the default branch requiring one approving review with no bypass for
that account; the operator approves the bot's pull requests from their own account. The
operator's own pull requests need an approver too, and GitHub does not let an author approve
their own, so the operator is a `Repository admin` bypass actor in `pull_request` mode: their
pull requests merge without a second account, and a direct push to the branch is still
refused. What the arrangement guarantees is the invariant's last sentence: the session's
identity cannot merge what it wrote, and cannot rewrite the rule that says so, because it is
not an admin. A team has the same shape with its members as the approvers.

`docs/security-model.md` gets a section, "The account a session acts as", saying this, and the
README's Prerequisites a pointer to it beside the token choice point -- inside the bounds
`tests/test_readme_bounds.py` pins, which holds the consequence within 700 characters of its
incentive.

**`validate`** says when a deployment is not in that shape. Two warnings in
`cli._probe_github`, from two reads the adapter gains:

- `RepoInfo.admin` (from `repos/{repo}`'s `permissions.admin`): when true, `github.token
  account` warns that the token's account is an admin of the repository and a session holding
  it can bypass or rewrite the branch ruleset; run as a dedicated account with write access.
- `branch_rules(branch) -> BranchRules(required_approving_reviews: int | None)` (from
  `repos/{repo}/rules/branches/{branch}`, the rules in force for the caller): when no
  `pull_request` rule requires at least one review, `github.branch rules` warns that the
  account a session runs as can merge its own pull requests. Rulesets only: classic branch
  protection is readable by admins alone, and the check must work for the account it
  recommends. The warning says so.

Both are warnings, not failures: `run-once` against a personal scratch repository is a
legitimate use and should not be refused.

## Testing

- `tests/test_orchestrator_approval.py`: the pure decision -- approved; edited by the approver;
  edited by the author after; title renamed after; issuebot's own `rework` event is not an
  approval; no human label event at all; edit before the approval.
- `tests/test_orchestrator_actions.py`: `unapproved_escape` label-first behaviour on a
  refused note, the idempotent block, the `Blocked` event.
- `tests/test_orchestrator.py`: a `todo` issue edited after labelling is not claimed and loses
  its label; relabelled, it is claimed; a failing evidence read skips the tick and logs once.
- `tests/test_github_ghcli.py`: the query's pagination and its `response` error past the
  ceiling; `RepoInfo.admin`; `branch_rules` over the four rule shapes above.
- `tests/test_agent_prompt.py`: the `association` attribute.
- `tests/test_workflow_default.py`: every
  comment-fetching command in the shipped workflow carries the association filter.
- `tests/test_cli.py`: the two new checks, warn and ok.
- `tests/test_doc_pointers.py` and `tests/test_readme_bounds.py` cover the prose.

## Out of scope

What the advisory listed as not verified -- the untracked prompt overlay, token scopes, the
egress list, and whether `claude`'s `Bash` tool passes `CLAUDE_CODE_OAUTH_TOKEN` to child
processes -- is a security sweep of this tree, not this design. Private vulnerability
reporting, which the advisory also noticed was off, was on again by 2026-09-27.
