# Triage: issuebot sizes an issue before anyone starts it

Date: 2026-10-25
Status: approved 2026-10-25; phase 3 (the parent review) built; phase 1 planned in
[`../plans/2026-10-25-triage-phase-1.md`](../plans/2026-10-25-triage-phase-1.md), with the
refinements found while planning folded in below (sections 1, 4 and 7, and "Phases")

## Why

Whether an issue goes to issuebot is decided today by the operator's gut feeling. An issue
that feels too big for the configured limits, or that needs some back-and-forth thinking
before anyone could write a spec, is kept away from it, so issuebot ends up doing bug fixes and
improvements to things that already exist. Two concrete cases drove this:

1. **Big work fails because it lands late, not only because it is expensive.** The operator
   labels a whole backlog `issuebot/todo` at once, which is exactly what issuebot should
   support. The small issues fly through and merge. The big one finishes much later, by which
   time the default branch has moved, so it goes round the conflict-rework loop
   (`agent.max_conflict_reworks`); by the time it has merged that in, more small pull
   requests have landed, and eventually it runs out of turns (`agent.max_turns`). The cure
   today is the operator not labelling big issues -- which is the judgement this design
   moves into issuebot.
2. **Some features need a person to think first.** A large new feature request for one of the
   repositories this host serves would have benefited from brainstorming and possibly a
   throwaway spike or probe script before a spec existed. That brainstorming happens in the
   operator's own interactive Claude Code session, not in an unattended issuebot session, so
   issuebot has to be able to say "a human does this step" and step aside.

Triage makes the judgement explicit and automatic: an issue that fits goes on as it does
today, and one that does not is broken into issuebot-sized steps, some of which are the
operator's, so that a big feature lands as a run of small pull requests instead of one long
branch fighting the default branch.

## Goals

- An operator can opt a deployment in, after which every new issue from a trusted author is
  triaged without anyone labelling it.
- Triage decides, for one issue, whether a single issuebot run will finish it within the
  limits that run has and land before its branch goes stale, and whether a human has to think
  it through first.
- When it does not fit, triage files sub-issues -- issuebot steps the operator starts with
  `todo` as today, and operator steps the operator does outside issuebot and closes -- and
  says so on the issue.
- When every sub-issue is closed, issuebot checks the original ask on its own and closes the
  parent if it is met.
- Triage is visible where the operator already looks: a comment on the issue, Slack, and two
  columns on the dashboard's board.

## Non-goals

- **Changing how the queue runs.** Running big issues first, or one at a time, is another cure
  for case 1 above; it is not this one. The single exception is the order of the steps inside
  one split, which issuebot does enforce (section 5, "Order within a split"); issues that are
  not sub-issues of a triaged parent are dispatched exactly as today.
- **Starting work.** Triage never adds `issuebot/todo`. The operator stays the one who starts
  every work run. (The closing check is the single automatic run, and it changes no code.)
- **Brainstorming inside issuebot.** Triage recognises that a person needs to think and files
  a step saying so; the thinking happens in the operator's own session.
- **Precise cost estimates.** Triage judges against the limits; it does not forecast dollars.
- **Dashboard work beyond the two columns.** No triage statistics, no sub-issue progress on
  cards.

## Anti-goals

These would make it a failure even if it technically worked:

- **A stranger's issue starting a session.** Today nothing runs until a maintainer adds
  `todo`. On a public repository anyone can open an issue; auto-triage must not hand that text
  to a session.
- **Weakening the approval invariant** of
  [`2026-09-28-tracker-text-admission-design.md`](2026-09-28-tracker-text-admission-design.md):
  tracker text reaches a session that holds a shell or a GitHub credential only if a
  maintainer wrote it, in the version a maintainer approved.
- **Unbounded loops.** A big feature splitting itself for ever, or a closing check that keeps
  finding gaps and filing issues for ever.
- **Triage acting beyond its verdict.** A triage or closing-check session that could push a
  branch, open a pull request, apply `todo` or touch another issue would turn every new issue
  into a session with the work run's reach.

## Overview

```text
new issue (trusted author, auto-triage on)        a human adds triage-todo by hand
            \                                          /
             +--------> issuebot/triage-todo <--------+
                               |
                         triage session
                     (read-only; returns a verdict)
                               |
            +------------------+-------------------+
            | fits                                 | split
            v                                      v
  comment + triage-complete             comment + sub-issues + triage-complete
  operator adds todo as today           operator starts each issuebot step with todo,
  (normal work run)                     does each operator step and closes it
                                                   |
                                     every sub-issue closed
                                                   |
                                         parent review session
                                     (read-only; returns a verdict)
                                                   |
                       +---------------------------+---------------------------+
                       | next                      | met                       | gap
                       v                           v                           v
             file the next batch          comment, close the parent,   comment, file one
             of sub-issues (usually       label it complete            sub-issue per gap
             the implementation stages                                 (at most two rounds,
             once the plan has merged)                                 then hand to the human)
```

## Design

### 1. Labels: a second track beside the state labels

Two new labels, plus one marker:

| Label | Meaning | Set by |
|---|---|---|
| `issuebot/triage-todo` | waiting for triage, or being triaged | issuebot (auto-triage), or a human |
| `issuebot/triage-complete` | triage is done; its verdict is in a comment | issuebot |
| `issuebot/operator-step` | a sub-issue the operator does outside issuebot | issuebot, on the sub-issues it files |

They are a separate track from the five state labels, the way `issuebot/no-fault` already is:
the rule that an issue wears exactly one state label is unchanged, and `triage-complete` stays
on the issue when the operator then adds `todo`. Concretely they join `GitHubLabels`
(configurable names, `triage_todo`, `triage_complete`, `operator_step`), are returned by
`markers()` rather than `as_tuple()` so that `clear_state` leaves them alone, are covered by
the distinctness check, and are created by `issuebot labels ensure`.

Refined while planning: a claim (`set_state(in_progress, clear_markers=True)`) used to strip
every label in `markers()`, which would take `triage-complete` off the moment the operator's
`todo` was claimed. So `markers()` now means every non-state label issuebot owns, and a new
`claim_clears()` names what a claim strips: `no-fault`, as before, and `triage-todo`. That is
also how "a state label wins" happens on GitHub, in the same `gh issue edit` as the claim.

Moves on the triage track:

- (none) to `triage-todo`: issuebot, under auto-triage (section 2), or a human.
- `triage-todo` to `triage-complete`: issuebot, after a triage run's verdict is applied.
- `triage-complete` to `triage-todo`: a human, to re-triage. On an issue that already has
  sub-issues this runs a parent review (section 6) rather than a first triage, so nothing is
  filed twice; it is also the one action that resumes a parent issuebot has handed back.

A human removing a triage label means "leave this issue alone": auto-triage does not put it
back (section 2).

A state label wins over the triage track. An issue that carries a state label is never
dispatched for triage; if the operator adds `todo` to an issue still in `triage-todo`, the
human has decided, so issuebot removes `triage-todo` and treats the issue as it does today.
A triage run already in flight when that happens is cancelled: the reconcile step stops a
triage run whose issue gains a state label or loses `triage-todo`, and its verdict, if any, is
discarded.

An `operator-step` sub-issue is never dispatched as a work run. If the operator adds `todo` to
one by mistake, issuebot refuses it the way it refuses an unapproved issue: it removes the
state label, writes a short note saying the step is the operator's, and publishes `Blocked`.

### 2. Opt-in, and which issues are auto-triaged

Auto-triage is off by default. It is switched on with `triage.auto: true` in the workflow's
front matter, falling back to the environment variable `ISSUEBOT_AUTO_TRIAGE` when the key is
absent, in the same way `agent.run_as` falls back to `ISSUEBOT_AGENT_USER` (`config/resolve.py`).
The operator asked for an environment variable; the front-matter key beside it is this
design's call, so the setting lives where every other one does.

When it is on, the worker adds `triage-todo` to an open issue when all of these hold:

- it carries no issuebot state label and no triage label;
- it has no parent issue (section 5: triage's own sub-issues are never triaged again, and the
  rule is "has a parent" rather than "triage filed it", which the worker can read from GitHub
  without keeping a record);
- its author's `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR` -- an account with
  write access. This is the same rule on a public and a private repository: on a private one
  it changes almost nothing, since the operator already chose everyone who can open an issue
  there;
- it was opened during the current period of auto-triage being on. Turning the setting on
  must not triage the backlog -- neither the whole history nor the issues opened during a spell
  with it switched off -- and the operator can label those by hand. An issue opened while the
  worker was *stopped* with the setting on must still be caught when it restarts. So the
  boundary is a watermark kept in the store per repository: the worker records the setting it
  last ran with, and moves the watermark to the present only when it starts with the setting on
  after last running with it off (or never having run). A restart with the setting unchanged
  leaves it where it was. This is the design's call;
- issuebot has not auto-labelled it before. Auto-triage applies `triage-todo` to an issue at
  most once, recorded in the store, so an escape that removes the label (section 3, section 4)
  or a human removing it is final for auto-triage: only a human adding it again triggers
  another triage. Without this, an escape would be undone on the next poll and repeat for
  ever.

Issues issuebot's own account opened -- the follow-up issues work sessions already file -- are
auto-triaged like any other; the operator decided this. It is safe because a triage session
holds no shell and no GitHub credential (section 4), so a follow-up shaped by something hostile
a work session read costs one read-only triage run and some sub-issues that still wait for a
human's `todo`. It is also the only rule that works on a deployment running on the operator's
own token, where issuebot's issues and the operator's have the same author.

A human can add `triage-todo` to any issue, whoever opened it and whether auto-triage is on or
off.

### 3. Approval: the triage run acts only on approved text

Triage has to meet the same approval bar as a work run (`orchestrator/approval.py`), adapted
to a label that may have no person behind it:

- **`triage-todo` applied by a person** approves the way `todo` does: the latest
  `triage-todo` a person applied (issuebot's own counting only under `own_labels_approve`),
  and any edit to the title or body at or after it by anyone but that person makes the issue
  unapproved.
- **`triage-todo` applied by auto-triage** is approved by authorship: the author's association
  is a maintainer's, re-read at dispatch, and nobody but the author has edited the title or
  body since the issue was opened. The author tightening their own wording is fine, as it is
  for an approver today.
- **An issue issuebot's own account opened** passes the authorship rule on a deployment whose
  account has write access. That is a deliberate, narrow exception to "issuebot's own label
  never approves on a dedicated account", and it holds only because the session it reaches is
  read-only and credential-free. It must not leak into the work run's approval: `todo` on that
  issue is still approved only as it is today.
- An unapproved triage candidate is handed back exactly as an unapproved work candidate is
  (`actions.unapproved_escape`: a note, the triage label removed, `Blocked`).

Sub-issues need no change: issuebot writes them, and the operator's `todo` on each approves it
as it does now. The parent review (section 6) re-checks the parent's approval before it runs,
under whichever of the first three rules approved its triage.

### 4. The triage session: read-only, and the worker does every write

A triage session holds:

- **a fresh clone of the default branch**, in a throwaway workspace of its own, not the issue's
  work workspace -- so a later work run on the same issue starts clean and runs its
  `after_create` hook as it does today. Triage runs no workspace hooks.
- **read tools only**: Read, Grep and Glob. No Bash, no Edit or Write, no web tools, no MCP
  server. The session is started with only those tools available, not with the full set minus
  a deny list, so an operator's `claude.allowed_tools` cannot widen it.
- **no `GH_TOKEN`, and no other credential the worker can withhold**, in its environment. It
  still runs as a session account (`agent.run_as`) behind the egress proxy, like any session.
  The one credential the worker cannot withhold is `claude`'s own (`CLAUDE_CODE_OAUTH_TOKEN` or
  `ANTHROPIC_API_KEY`), which sits in the `claude` process's environment. So the read tools are
  confined to the clone -- no path outside it, `/proc` included, can be read -- and the verdict
  passes through the deployment's scrubber (`agent/scrub.py`) before any of it is posted, since
  on a public repository the comment it becomes is public. Section 2's exception for
  issuebot-authored issues rests on this confinement as much as on the missing `GH_TOKEN`.

It ends by returning a **verdict** as structured output in its final message. The worker
validates the verdict and performs every GitHub write itself: the comment on the issue, each
sub-issue, the sub-issue links, the labels, and the Slack message. This is what makes "a triage
session can only comment and file sub-issues" true by construction rather than by prompt, and
what keeps the approval invariant intact for issuebot-authored text.

The worker refuses a verdict that is malformed, or that exceeds `triage.max_sub_issues` per
batch, or carries over-long titles or bodies; a refused verdict counts as a failed run. The
agent never chooses labels: the worker applies `operator-step` to the sub-issues the verdict
marks as the operator's and nothing else, and never a state label.

The session has its own limits, separate from a work run's: `triage.max_turns`,
`triage.max_budget_usd`, and optionally `triage.model` (falling back to `claude.model`). It
takes a slot under `agent.max_concurrent_agents` like any run, is recorded as a run in the
store with a kind that tells it apart from a work run, and its cost counts toward the issue's
cumulative spend (`agent.max_issue_cost_usd`). A triage run that keeps failing escapes after
`agent.max_attempts` the way a work run does: a note, `triage-todo` removed, `Blocked`.

`issuebot run-once <number> --triage` runs one triage session in the foreground, as
`run-once` does for a work run; `--show-prompt` renders the triage prompt.

Refined while planning, and measured on 2026-10-25 with claude 2.1.310:

- The session is started with `--restricted --tools Read,Grep,Glob --permission-mode dontAsk
  --json-schema <the verdict schema>`, whatever `claude.permission_mode`, `allowed_tools`,
  `disallowed_tools`, `mcp_config` and `append_system_prompt` say. `--restricted` drops the
  code-running tools, ignores settings files, and confines the file tools to the working
  directory. In the probe a read inside the clone succeeded, a read of an absolute path outside
  it was refused ("is outside ... --restricted confines the file tools to the working
  directory"), and a symlink inside the clone pointing out of it was refused ("resolves through
  a symlink to ..., which is outside"). `--settings` still applies under `--restricted`, so it
  carries `permissions.deny: ["Read(//proc/**)"]` beside the `claudeMdExcludes` allow-list as a
  second line. `--json-schema` adds a `StructuredOutput` tool, and the verdict arrives already
  parsed as the result message's `structured_output`.
- `MIN_CLAUDE_VERSION` moves to 2.1.303, the shipped pin, which carries all three flags
  (checked in the built image).
- The worker fetches the issue's comments itself (GraphQL `bodyHTML`, rendered through
  `visible_text` as the body is), since the session has no `gh`. Maintainers' comments arrive
  enveloped and everyone else's as author and URL only.
- The triage workspace is `<workspace key>.triage`. It stays until the next triage of the same
  issue replaces it or the issue closes, so the turn capture, which reads the run's log
  directory after `RunEnded`, never races its removal.
- In phase 1 the triage failure chain is kept in memory, so a worker restart resets it. Every
  attempt is still bounded by `agent.max_attempts` within one process, and the store's ledger
  seed counts only work runs' failures toward the work chain, while all runs' cost counts toward
  the issue's spend.

### 5. What triage judges, and how it splits

The worker hands the session, enveloped as tracker text with author and association the way
`configs/WORKFLOW.md` envelops it today:

- the issue's title and body as approved;
- its comments, through the association filter of Ground rule 7 -- maintainers' comments as
  requests, everyone else's reduced to author and URL;
- the repository's `CLAUDE.md` and `AGENTS.md`, as the work prompt carries them;
- the limits a work run has: `agent.max_turns`, `claude.max_budget_usd`,
  `agent.run_timeout_ms`, `agent.max_conflict_reworks` and, when it is set,
  `agent.max_issue_cost_usd`.

It answers three questions:

1. **Does it fit the caps?** Is one work run likely to finish inside those turns, that spend
   and that wall clock?
2. **Will it land fast?** Is the change small and contained enough -- a modest diff in one area
   of the code -- to merge before the default branch moves under it?
3. **Does a human need to think first?** Are there open design questions, a choice between
   approaches only the operator can make, or something nobody can know until a spike has been
   tried?

The verdict is `fits` or `split`, with a short reason for each of the three questions, which
the worker puts in the comment.

**`fits`:** the worker comments, swaps `triage-todo` for `triage-complete`, and notifies Slack.
The operator adds `todo` as today.

**`split`:** the verdict lists sub-issues in order, each with a title, a body (what to do,
acceptance criteria, `Part of #<parent>`), and a kind -- `issuebot` or `operator`. The worker
files them, each created already linked as a GitHub sub-issue of the parent (GraphQL
`createIssue` takes `parentIssueId`), so no poll can ever see one without its parent and
auto-triage it. It keeps the verdict's order, comments on the parent listing them, swaps the
label for `triage-complete`, and notifies Slack.

Triage files **only the steps up to and including the plan**. When triage runs there is no
spec yet, so it cannot honestly know the implementation stages. Typical first batches:

- *needs a human first:* an operator step to brainstorm (and spike, if useful) in a Claude Code
  session and commit a spec; then an issuebot step to write the implementation plan from it;
- *big but clear:* an issuebot step to write the spec; then one to write the plan;
- either may begin with an issuebot step for a throwaway spike or probe script, when what is
  unknown is technical rather than a matter of taste.

The plan step is an ordinary work run with an ordinary pull request; the body triage writes for
it asks for a plan that breaks the work into stages, each landing as its own pull request and
leaving the default branch working, with any pause for the operator to test by hand marked as
such. The work prompt does not change. The implementation stages are filed by the parent
review once that plan has merged (section 6), so they appear only after the operator has
approved the plan by merging it.

Where the specs and plans are written is the target repository's business: its own
instructions decide, as they do for any work run.

**Order within a split.** The operator decided issuebot enforces the order, so that labelling
a whole batch `todo` at once -- the way case 1 works -- runs the steps one after another
instead of all from the same default branch at the same moment, which would be case 1 again
in smaller pieces: a plan written before its spec exists, a stage built without the stage
before it.

- A sub-issue of a parent carrying `triage-complete` that wears `todo` is **held** while any
  sub-issue before it, in the parent's order, is still open. Held means not dispatched: no
  refusal, no note, no label change and no `Blocked`. The issue stays in `todo` and in the Todo
  column, and is dispatched on the first poll after the last step before it closes.
- Operator steps count: a stage after "test this by hand" waits until the operator closes that
  step.
- "Closed" means closed by any route, as in section 6. A stage whose pull request is open is
  still open, so the next stage waits for it to merge -- which is the point, since the next
  stage needs that code on the default branch.
- Decided while planning phase 2: the order comes from the parent's sub-issue order alone, and
  no GitHub "blocked by" dependencies are filed, so there is one source of truth that the
  operator changes by dragging sub-issues; and a held issue's card does not change in phase 2,
  because the board is phase 5's -- the worker logs `dispatch_held` naming the step instead.
- Batches filed later (a `next` or `gap` verdict) go after the existing sub-issues, in the
  order the verdict gives.
- The hold is a precondition of the admission gate (`orchestrator/admission.py`): a refusal
  kind of its own whose `wait` says "until an earlier step closes", so a held issue costs no
  slot, no account binding and no approval read.
- It applies to a sub-issue's first dispatch from `todo`. A step that has already started and
  comes back as `rework` is not held, since everything before it had closed when it started.

### 6. The parent review: the next batch, or the closing check

When every sub-issue of a parent carrying `triage-complete` is closed, the worker runs a
**parent review** on the parent, by itself, with no label from anyone (the operator decided
this). It does so only for a parent whose triage filed sub-issues: an issue triaged as `fits`
that someone later gave sub-issues by hand is not reviewed unless a human adds `triage-todo`.
Any close counts as done, "not planned" included: if the operator drops a step, the review is
what notices whether that matters.

It is the same kind of session as triage -- section 4's clone, tools, absent credential and
worker-side writes -- with its own prompt. On top of section 5's inputs it is handed each
sub-issue's title, body, kind and close reason, and for each one closed by a merged pull
request, that pull request's title, body and maintainers' reviews, all as tracker text. It reads
the merged code in its clone. Its verdict is one of:

- **`next`** -- the work is not done because the steps so far were a phase (typically the plan
  has just merged). It lists the next batch of sub-issues -- usually the implementation stages
  the plan describes, with any operator test pause as an operator step -- which the worker
  files exactly as for a split.
- **`met`** -- the original ask is met. The worker comments with the review's evidence, closes
  the parent and labels it `issuebot/complete` (a new move on the state track: none to
  `complete` by issuebot, for a split parent only), and notifies Slack.
- **`gap`** -- something the original ask needs is missing. The worker comments listing the
  gaps and files one sub-issue per gap. Those wait for the operator's `todo` like any other;
  when they close, the review runs again.

Both loops are bounded. At most `triage.max_gap_rounds` (default 2) `gap` verdicts are acted on
per parent; a review after that which is still not `met` posts what it found, files nothing,
and hands the parent to the operator with `Blocked`. The total number of parent reviews is also
capped (`triage.max_parent_reviews`), so a chain of `next` verdicts cannot run on for ever.

A parent edited by anyone but its approver since triage is unapproved: the review does not run,
and the parent is handed back as in section 3.

**Every hand-over of a parent leaves it in one shape.** Whatever the cause -- gap rounds used
up, the review cap reached, the review run failing `agent.max_attempts` times, an unapproved
edit -- the parent keeps `triage-complete` and stays in the Triaged column, the worker writes a
note on it saying why and publishes `Blocked`, and it records the hand-over in the store, so
the review does not run again on the next poll merely because every sub-issue is still closed.
The operator resumes it with one action: adding `triage-todo`, which runs a parent review (the
applied label re-approves the text, under section 3's first rule) and resets the parent's gap
rounds and review count, since a human has re-authorised it. This is the design's call.

Decided while planning phase 3:

- The per-parent record is a `parent_review_state` event, not a table: the worker seeds itself
  from the latest one per parent at startup, so there is no migration. Without a store a
  restart forgets the records.
- A parent is watched only once it has a record: triage filed its split, or a human added
  `triage-todo` to an issue that already has sub-issues. Earlier splits are opted in by one
  `triage-todo` each; nothing is backfilled.
- `triage-todo` on an issue with sub-issues resets the record and runs a parent review, or, with
  a step open, puts `triage-complete` back and posts a note. That note is not a hand-over.
- Every hand-back of an issue with sub-issues is a parent hand-over, whatever the trigger, so
  `triage-complete` always stays and the held steps stay held.
- A review counts when the worker acts on its verdict, filing it or handing over at the gap
  limit; a failed run counts only on the failure chain. `max_parent_reviews` is checked before an
  automatic review, and a `triage-todo` review starts from a reset record, so is never capped.
- `met` adds `issuebot/complete` beside `triage-complete`, closes the parent as completed and
  publishes `StateChanged(None -> complete)` and `IssueCompleted(resolution="parent_review")`,
  so it counts in "issues closed".
- The gap limit applies to `gap` verdicts only. A `next` after the rounds are used is still
  filed (else `max_gap_rounds: 0` would hand over the first `next`), and `max_parent_reviews`
  bounds a chain of them.
- Approval is section 3's first rule until phase 4: the latest `triage-todo` a person applied,
  and any later edit by someone else, through the same assessment as triage.
- `run-once --triage` refuses an issue with sub-issues, since the records are the worker's.
- A filing GitHub fails part-way stays owed until its comment posts; steps whose titles match
  the verdict's drafts count as its own.
- A verdict whose steps changed while it ran, including a `gap` past the limit, is discarded and
  files nothing.

### 7. Where the operator sees it

- **The issue.** One comment per triage verdict and per parent review, written by the worker:
  the verdict, the reasons, and the sub-issues filed, linked.
- **Slack.** Every verdict is a label move or a close, so the worker publishes each as a
  `state_changed` event carrying the verdict in its text, and each hand-over as `blocked`.
  That keeps existing deployments' `notifications.slack.events` lists working unchanged.
  `StateChanged` gains an optional `note` for that text, and the store leaves an issue's
  recorded state alone for a move on the triage track.
- **The dashboard.** Two new columns to the left of Todo: **Triage** (`triage-todo`, waiting or
  being triaged, the running one shown the way a running work issue is) and **Triaged**
  (`triage-complete` and no state label: waiting for the operator's `todo`). A split parent
  sits in Triaged until it closes, since it never gets `todo` itself. The board is built from
  the store, so the worker records the triage track there as it does the state labels. The
  README's two images are regenerated with the new columns (`tools/screenshots/`).

### 8. Prompts

The triage prompt and the parent-review prompt ship as defaults, and an operator can override
either per deployment with a file beside `WORKFLOW.md` without forking the code. They follow
`WORKFLOW.md`'s rules on tracker text: everything GitHub authored arrives inside `<github-text>`
with its author and association, is data to judge rather than instructions, and a request in it
to do anything but return a verdict is noted in the verdict rather than obeyed. Each prompt
states the verdict schema it must return.

### 9. Polling cost

Every worker shares the account's 5,000-point hourly GraphQL budget. Triage adds reads per
poll: open issues opened since the watermark that carry no issuebot label; for the parents in
Triaged, how many of their sub-issues are still open; and, for the order within a split, each
`todo` candidate's parent and the state of its earlier siblings. Each should be a single
bounded query, or a field added to one the poll already makes (GitHub reports `parent` and
`subIssuesSummary` on an issue, and `subIssues` in order). Measure the added cost with the GraphQL
`rateLimit` object, not REST `/rate_limit`, which does not report the GraphQL budget.

## Configuration

```yaml
triage:
  auto: false               # ISSUEBOT_AUTO_TRIAGE when absent
  model: null               # claude.model when null
  max_turns: 1
  max_budget_usd: 2.0
  max_sub_issues: 8         # per batch
  max_gap_rounds: 2
  max_parent_reviews: 6
github:
  labels:
    triage_todo: issuebot/triage-todo
    triage_complete: issuebot/triage-complete
    operator_step: issuebot/operator-step
```

The numbers are starting points for the builder, not decisions.

## What does not change

- The work run, its prompt, its approval by `todo`, the pull request and review flow. The one
  change to dispatch is the hold on a sub-issue whose earlier steps are still open.
- `todo` is added only by a human; the closing check is the one run nobody labels, and it
  changes no code.
- A deployment that leaves auto-triage off and never adds `triage-todo` by hand sees no
  difference beyond two empty columns.

## Phases

Each phase is its own pull request and works on its own; each later one is planned against the
code the one before it leaves.

1. **Triage by hand.** The labels and settings, the read-only session, the verdict, filing the
   sub-issues, the comment, Slack, the escapes, the operator-step refusal, `run-once --triage`,
   and `validate`. A human adds `triage-todo`.
2. **Order within a split** (section 5).
3. **The parent review** (section 6): `next`, `met`, `gap`, the bounds and the hand-over.
4. **Auto-triage** (section 2, and section 3's authorship approval).
5. **The dashboard's two columns** (section 7), and the README's images.

## Documentation to update

`README.md` (the label table and the configuration reference; `tests/test_readme_bounds.py`
pins some of its passages), `docs/security-model.md` (triage sessions are read-only and
credential-free; authorship approval), `docs/operations.md` (the new hand-overs and how to
recover from each), `docs/dashboard.md` (the two columns), `docs/package-layout.md` (each
module touched), and the label table in `CLAUDE.md` -- two rows at most, since that file has a
byte budget (`tests/test_instruction_bounds.py`).

## Left to the builder

- Label colours and descriptions; the sub-issue title format; the exact verdict schema.
- ~~The store's schema for the triage track, the watermark and the run kind.~~ The parent review's
  record is an event, not a table (phase 3 ruling 1).
- ~~How triage and parent reviews are ordered against work runs for free slots.~~ Triage first,
  then parent reviews, then work (phase 3 ruling 6).
- What happens to a triage run in flight when the operator adds `todo` (section 1).
- Whether to hand triage extra signal for the "lands fast" question -- recent churn on the
  default branch, or the files open pull requests touch -- since it has no shell to run
  `git log` itself.
- ~~Whether the two prompt files are one file with two sections or two files.~~ Two files:
  `PARENT_REVIEW.md` beside `TRIAGE.md` (phase 3 ruling 10).
- ~~Whether the order within a split is read from the parent's sub-issue order alone, or the
  worker also files "blocked by" dependencies; and whether a held card says what it waits
  for.~~ Resolved in section 5.
- The default numbers in the configuration above.
