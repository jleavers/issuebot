# Triage Phase 3 (The Parent Review) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When every sub-issue of a split parent has closed, the worker reviews the parent on
its own. The review files the next batch (`next`), closes the parent as complete (`met`), or
files one sub-issue per gap (`gap`). Both loops are bounded, and every hand-over leaves the
parent in one shape that a human resumes with `issuebot/triage-todo`.

**Architecture:**
- **The session.** A parent review is a third run kind, `parent_review`. It is the triage
  session with a different prompt, a different verdict schema and one extra GitHub read: the
  parent's steps, with each step's close reason and the merged pull request that closed it.
- **The record.** The worker keeps a small record per split parent: reviews acted on, gap
  rounds, the hand-over, and whether it is finished. Each change is published as a new event,
  `parent_review_state`. The store keeps that event like any other, and the worker reads the
  latest one per parent back at startup, the way it reads the admission ledger. So there is
  no migration and no new table.
- **The watch.** Each tick, when a slot is free, the worker reads the parents it is watching
  in one `parent_steps` query and starts a review for each one that is due.
- **The human trigger.** `triage-todo` on an issue that has sub-issues becomes the parent
  review's trigger, in place of phase 1's "already split" hand-back.

**Tech Stack:** Python 3.14, the `gh` CLI (GraphQL and REST), PostgreSQL through psycopg, and
pytest with pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-10-25-triage-design.md`. Phase 3 is section 6, plus the
parent review's share of sections 1 (the re-triage move), 3 (re-checking approval), 7
(comment and Slack) and 9 (polling cost). Read them before starting: this plan argues from the
spec.

## Rulings on what the spec leaves to the builder

These come from the spec's "Left to the builder" list and from gaps found while planning. Task
8 records them in the spec.

1. **The per-parent record is an event, not a table.** `ParentReviewState` (kind
   `parent_review_state`) carries `reviews`, `gap_rounds`, `handed_over` and `finished`. Every
   change publishes the whole record. The store keeps every event already (`events.payload`),
   and the worker seeds itself from the latest record per parent at startup, as it seeds the
   admission ledger (`queries.ISSUE_LEDGERS`, `cli._initial_ledger`). A worker without a store
   keeps the records in memory, and a restart loses them; such a parent then waits for a
   human's `triage-todo`. No migration.
2. **A parent is watched only once it has a record.** A record starts in two ways: triage files
   a split (in the worker, or through `run-once --triage`), or a human adds `triage-todo` to an
   issue that already has sub-issues. That is section 6's "only for a parent whose triage filed
   sub-issues", with the human's label as the stated exception. Splits filed before phase 3
   have no record, and nothing is backfilled from older events. The operator opts each one in
   by adding `triage-todo` once (Task 8's deploy notes).
3. **`triage-todo` on an issue with sub-issues runs a parent review** (section 1). The record
   is reset first, because a human has re-authorised it (section 6). If every step is closed,
   a review starts. If a step is still open, the worker puts `triage-complete` back and posts a
   short note saying the review runs once the steps close. That is not a hand-over: it publishes
   no `Blocked` and records nothing handed over. This replaces the "already split" hand-back,
   and with it phase 2's `restore_complete` flag (Task 6).
4. **Every hand-back of an issue that has sub-issues is a parent hand-over** (section 6, "one
   shape"). `triage-complete` is put back, a note is written, `Blocked` is published and the
   record says `handed_over`. This covers the cases where the trigger was `triage-todo`, such
   as an unapproved edit, the spend ceiling or a spent failure chain. Removing only
   `triage-todo` would leave a parent that a human swapped to `triage-todo` with neither label,
   and that releases every held step (the bug c002df2 fixed for one path). `Issue` gains
   `sub_issue_count` so the worker knows without an extra read.
5. **The session and its failure chain are triage's.** A parent review uses the same clone
   (`<workspace key>.triage`), tools, absent credential and worker-side writes (section 6). Its
   failures count on the issue's existing triage chain (`_triage_failures`,
   `_triage_not_before`), and they are bounded by `agent.max_attempts` in the same way.
6. **Free slots go to triage first, then parent reviews, then work.** A review is short, and it
   unblocks the next batch.
7. **What counts as a review.** A review counts when the worker acts on its verdict, whether by
   filing it or by handing over at the gap limit. A failed run counts only on the failure
   chain. `triage.max_parent_reviews` is checked before an automatic review starts. A review
   started by `triage-todo` starts from a reset record, so it is never capped.
8. **`met` closes the parent as complete.** The worker adds `issuebot/complete` beside
   `triage-complete`, which stays. It closes the issue as completed and publishes
   `StateChanged(None → complete)` and `IssueCompleted(resolution="parent_review")`. So the
   parent counts in "issues closed", as any closed issue issuebot finished does.
9. **A later batch goes after the existing sub-issues** (section 5). The footer numbering carries
   on from the last step. A retry matches drafts by title only against children the review did
   *not* see, so a new step may reuse an earlier step's title.
10. **Two prompt files.** The shipped `src/issuebot/config/parent_review_prompt.md` is
    overridden by `PARENT_REVIEW.md` beside `WORKFLOW.md`, which is read and reloaded exactly as
    `TRIAGE.md` is. An operator who overrides one prompt keeps the shipped version of the other.
11. **What the review is handed, beyond triage's inputs** (section 6). For each step: its title,
    kind, close reason and body. For a step closed by a merged pull request: that pull
    request's title, body and reviews. These are filtered by association the way comments
    are: a maintainer's text is enveloped, and anyone else's is reduced to author and URL. The
    parent's own comments go through triage's filter unchanged, so issuebot's own earlier
    comments stay out. The review is told its counts directly instead.
12. **`run-once --triage` refuses an issue with sub-issues.** The records are the worker's
    memory, so a foreground review would leave the worker with stale counts. The refusal names
    the two ways a review starts. `--show-prompt` renders the parent-review prompt.
13. **`validate` stays at 22 checks.** The `triage` check renders both prompts and reports the
    two new limits.
14. **When the reconcile stops a review.** It stops a parent review whose parent gains a state
    label, carries neither triage label, or closes. A human adding `triage-todo` mid-run does
    not stop it: the review that is running is the one they asked for.
15. **Filing re-reads the parent first.** Before the first write, the worker discards the verdict
    if the parent has closed, gained a state label or lost both triage labels, or if its steps
    differ from the ones the review saw (a step reopened, added or unlinked). Once the first
    write has landed, the verdict is owed until its comment is posted.
16. **The watch costs one point a tick.** It is one `parent_steps` read of the watched parents,
    made only when a slot is free or a hand-over note is owed. A watched parent is one with a
    record that is neither handed over nor finished.
17. **The gap limit applies to `gap` verdicts.** Section 6 says that "a review after that which
    is still not `met`" hands over. Read literally, a `next` after the gap rounds were used
    would hand over too, and with `max_gap_rounds: 0` so would the first `next` after a plan
    merges. So only a `gap` past the limit hands over. A `next` is still filed, and
    `max_parent_reviews` bounds a chain of them.
18. **Approval is rule 1 until phase 4.** The review re-checks the latest `triage-todo` a person
    applied, and any edit after it by anyone else (section 3, first rule), through
    `_assess_approval(issue, label=triage_todo)`. Authorship approval, rules 2 and 3, is
    phase 4's.

## Global Constraints

- New literals, exactly:
  - `RunKind` gains `"parent_review"`.
  - The event kind is `parent_review_state`.
  - `CompletionResolution` gains `"parent_review"`.
- New settings, exactly:
  - `triage.max_gap_rounds` (default `2`, `ge=0`)
  - `triage.max_parent_reviews` (default `6`, `ge=1`)
- The verdict's JSON fields, exactly: `verdict` (`next`, `met` or `gap`), `summary`,
  `evidence`, `ignored_requests` and `sub_issues` (each `title`, `body`, `kind`, as triage's).
- Comment headings, exactly:
  - The review: `### Issuebot parent review (<stamp>)`.
  - Hand-overs, through `_hand_back`'s `### Issuebot {title} (<stamp>)`, with these titles:
    - `parent review limit`
    - `parent review gap limit`
    - `parent review failed`
    - `parent review not filed`
    - `parent review budget limit`
    - `unapproved edit` (as today)
  - The note for open steps: title `parent review waiting`.
- The sub-issue footer, exactly:
  `Part of #{parent}: step {step} of {steps}, filed by issuebot's parent review.`
- Log events:
  - `parent_review_dispatched` (info)
  - `parent_review_filed` (info)
  - `parent_review_discarded` (info)
  - `parent_review_file_failed` (warning)
  - `parent_watch_failed` (warning)
  - `parent_records_seeded` (info)
  - `parent_records_seed_failed` (warning)
- No migration, no new label, no new table.
- `jleavers/issuebot` is public. Never name a private repository anywhere in the tree, a commit
  message or a fixture.
- A global hook rejects any Bash command whose text contains the dot-env filename. Edit such
  files with the Edit tool.
- `docs/package-layout.md` is about 10 KB under its 160 KiB budget
  (`tests/test_instruction_bounds.py`). Keep this plan's additions there under 2 KB. In
  `CLAUDE.md`, change only the label table's `issuebot/complete` row.
- Never push to `main`, and never run `git reset --hard`, `git clean -fd` or `rm -rf`. Work in
  the worktree `.claude/worktrees/triage-phase-3-review` on branch `triage/phase-3-review`.
- The live hub runs from the main checkout's compose project. Never run `docker compose down`,
  and never run `up`, `restart` or `rm` on any service but `test-db`. Never pass
  `ISSUEBOT_DB_PORT` inline. Task 7's store test needs `test-db` (`CLAUDE.md`, "Commands").
- Every commit message ends with the committing model's own `Co-Authored-By:` line, as its
  harness gives it, then
  `Claude-Session: https://claude.ai/code/session_019agwxcWUZKkqVuL73ZfE76`.
- Run the suite with `uv run pytest`, then `uv run ruff check . && uv run ruff format --check .`.
- #311's nine test stages (#332–#340) land on `main` while this is built. They touch
  `tests/test_orchestrator.py`, `tests/test_cli.py`, `tests/test_agent_verdict.py`,
  `tests/test_agent_triage.py` and `tests/test_orchestrator_triage.py`. Stage 6 (#337) changes
  `operator_step_escape` in `actions.py`. Rebase onto `origin/main` before Task 1, before Task
  5 and before the PR.
- On Python 3.14, ruff-format drops the parentheses from a multi-name `except` clause that has
  no `as`. Write `except (A, B) as exc:` when you need the name.
- Line numbers below are hints from `main` at `28784c2`. Find the code by name.

## Review Focus

1. **Steps closing while the worker is down, and a restart in the middle of the loop.** The
   review runs on the first tick after start. With a store, the counts and a hand-over survive
   the restart. Pinned in Task 5 (`test_a_seeded_parent_is_reviewed_on_the_first_tick`) and
   Task 7 (`test_parent_records_are_each_parents_latest_state`).
2. **A step reopened, added or unlinked while the review runs.** Nothing stale is filed, and the
   next tick waits for the steps to close again. Pinned in Task 5
   (`test_a_review_whose_steps_changed_is_discarded`).
3. **GitHub failing part-way through filing.** The retry files nothing twice, comments once and
   tells Slack once. It never runs the session again. Pinned in Task 4
   (`test_a_retry_after_a_failed_creation_files_only_what_is_missing`) and Task 5
   (`test_a_failed_filing_is_retried_from_memory`).
4. **The documented re-triage gesture on a split parent:** `triage-complete` swapped for
   `triage-todo`, whether the steps are open or all closed, and whether it ends in a review or
   a hand-back. `triage-complete` always comes back, and held steps stay held. Pinned in Task 6
   (`test_re_triaging_a_split_parent_keeps_its_steps_in_order`,
   `test_an_unapproved_hand_back_of_a_split_parent_puts_triage_complete_back`).
5. **A watched parent that a human closes, or takes `triage-complete` off.** There is no review
   and no comment. A closed parent's record is finished and its watch ends. Pinned in Task 5
   (`test_a_closed_parent_is_no_longer_watched`,
   `test_a_parent_without_triage_complete_is_left_alone`).

---

### Task 1: The GitHub reads and writes

**Files:**
- Modify: `src/issuebot/github/models.py`:
  - `Issue`, line 45
  - `ParentSteps`, line 116
  - new models beside `SubIssue`, line 106
- Modify: `src/issuebot/github/normalise.py`:
  - `issue_from_node`
  - `parent_steps_from_node`, line 136
  - new `review_step_from_node`
- Modify: `src/issuebot/github/ghcli.py`:
  - `ISSUE_FIELDS`, line 99
  - `parent_steps_query`, line 233
  - new `parent_review_steps_query`
  - `GhCliAdapter.parent_review_steps`
  - `GhCliAdapter.close_as_completed`
- Modify: `src/issuebot/github/adapter.py` (the protocol)
- Modify: `src/issuebot/github/fake.py`:
  - `_FakeIssue`, `_FakePr`
  - `_node`
  - `parent_steps`
  - new methods and helpers
- Modify: `src/issuebot/github/__init__.py` (exports)
- Test: `tests/test_github_normalise.py`, `tests/test_github_ghcli.py`,
  `tests/test_github_fake.py`

**Interfaces:**
- Produces:
  - `Issue.sub_issue_count: int = 0`, read from `subIssuesSummary { total }`, which the poll
    fragment `ISSUE_FIELDS` gains. It is an object rather than a connection, so the poll costs
    no more.
  - `ParentSteps` gains `id: str` (the parent's node id) and `state: GitHubState`, both scalars
    added to `parent_steps_query`. Phase 2's callers ignore them.
  - Three frozen, kw-only, slotted dataclasses in `models.py`. Association strings are
    upper-case, as GitHub spells them.
    - `PrReview(author: str | None, association: str | None, state: str, body_html: str, url: str)`
    - `ClosingPr(number: int, title: str, url: str, body_html: str, author: str | None, association: str | None, reviews: tuple[PrReview, ...])`
    - `ReviewStep(number: int, title: str, url: str, body_html: str, author: str | None, association: str | None, labels: tuple[str, ...], state: GitHubState, state_reason: str | None, closed_by: ClosingPr | None)`.
      `labels` are lower-cased. `state_reason` is GitHub's `stateReason` (`COMPLETED`,
      `NOT_PLANNED`, `DUPLICATE`, or `None` while open).
  - `GitHubAdapter.parent_review_steps(number: int) -> list[ReviewStep]`: the parent's steps in
    GitHub's order, at most `PAGE_SIZE` (100), in one GraphQL query. Each step carries its last
    `ClosedEvent`'s `closer`. `closed_by` is set only when the closer is a pull request with
    `merged: true` (section 6). A closing commit, an unmerged pull request or no closer gives
    `None`. Reviews come from `reviews(first: 30)`. A parent GitHub does not return raises
    `GitHubError("response")`.
  - `GitHubAdapter.close_as_completed(number: int) -> None`: REST
    `PATCH repos/{owner}/{name}/issues/{number}` with `state=closed` and
    `state_reason=completed`, made the way the adapter's other REST writes are made. The name
    is not `close_issue`, because the fake already has a synchronous `close_issue` that tests
    use as a human's action.
  - `normalise.review_step_from_node(node: Mapping[str, Any]) -> ReviewStep`. It raises
    `GitHubError("response")` on a malformed record, as `sub_issue_from_node` does.
  - Fake helpers:
    - `FakeGitHub.close_issue(number, *, reason: str = "COMPLETED")` records the state reason.
    - `open_pr(..., title: str = "", body: str = "")` sets the pull request's text.
    - `add_pr_review(pr_number, *, author: str, association: str, state: str = "APPROVED", body: str = "")`
      adds a review.
    - The fake's `parent_review_steps` and `close_as_completed` go through `_enter`, with the
      names `"parent_review_steps"` and `"close_as_completed"`.
    - `issue(n).sub_issue_count` is `len(children)`.
    - A merged PR's issue reports that PR as its `closed_by`.

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_github_normalise.py`, using the module's `node()` helper:
  - `test_sub_issue_count_is_the_summary_total`:
    `node(subIssuesSummary={"total": 3})` gives `3`, and a node without the key gives `0`.
  - `test_parent_steps_carry_the_parents_id_and_state`:
    `parent_steps_from_node({..., "id": "I_9", "state": "CLOSED"})` gives `.id == "I_9"` and
    `.state == "closed"`.
  - `test_a_step_closed_by_a_merged_pr_carries_it_and_its_reviews`: one step whose closer is
    `{"__typename": "PullRequest", "merged": True, ...}`, with two reviews. Assert every field,
    including `closed_by.reviews[1].association`.
  - `test_a_step_closed_without_a_merged_pr_has_no_closer`, parametrised over:
    - a closer that is a `Commit`
    - a pull request with `"merged": False`
    - an empty timeline
    - `"stateReason": "NOT_PLANNED"`

    Each gives `closed_by is None`, and the last also gives `state_reason == "NOT_PLANNED"`.
  - `test_a_malformed_step_is_a_response_error`: a missing `number` raises
    `GitHubError` with category `"response"`.

  In `tests/test_github_ghcli.py`, with the module's recording runner:
  - `test_parent_review_steps_reads_the_parents_steps_in_one_query`: one GraphQL call, whose
    query names `subIssues(first: 100)`, `stateReason`, `CLOSED_EVENT` and
    `reviews(first: 30)`. The steps come back in the response's order.
  - `test_close_as_completed_patches_the_issue`: the recorded argv is
    `api -X PATCH repos/<owner>/<name>/issues/5 -f state=closed -f state_reason=completed`,
    or whatever prefix the module's other REST calls use.
  - `test_the_poll_fragment_asks_for_the_sub_issue_total`:
    `"subIssuesSummary { total }"` is in `ISSUE_FIELDS`.

  In `tests/test_github_fake.py`:
  - `test_fake_parent_review_steps`:
    - Parent 10 has three children:
      - 11, merged through `open_pr(11, title="T", body="B")` with a review added by
        `add_pr_review`;
      - 12, closed with `reason="NOT_PLANNED"`;
      - 13, open.
    - Assert the three steps' `state`, `state_reason` and `closed_by`, in order.
  - `test_fake_close_as_completed`: the issue becomes `closed`, and the call is recorded as
    `("close_as_completed", (5,))`.
  - `test_fake_sub_issue_count_and_parent_steps_id_and_state`.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_github_normalise.py tests/test_github_ghcli.py tests/test_github_fake.py -q`

  Expected: FAIL. The names are not defined yet.

- [ ] **Step 3: Implement the models, the normaliser, the two queries' fields, both adapter
  methods and the fake.**

  `parent_review_steps_query(number: int) -> str` is one
  `issue(number: N) { subIssues(first: PAGE_SIZE) { nodes { ... } } }`, where each node holds:
  - `number title url bodyHTML state stateReason authorAssociation author { login }`
  - `labels(first: 20) { nodes { name } }`
  - `timelineItems(itemTypes: [CLOSED_EVENT], last: 1) { nodes { ... on ClosedEvent { closer { __typename ... on PullRequest { number title url bodyHTML merged authorAssociation author { login } reviews(first: 30) { nodes { url state bodyHTML authorAssociation author { login } } } } } } } }`

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS. Phase 2's `parent_steps` tests may need `id` and `state` in their fixtures.

- [ ] **Step 5: Measure the cost.** This is read-only, against this repository.

  Run `gh api graphql` once with `parent_review_steps_query(311)`, and once with
  `by_ids_query` over two open issues. Add `rateLimit { cost }` at the top level of each. #311
  has merged steps, so the closer branch is exercised.

  Expected: the review read costs 5 points or fewer, and the poll fragment still costs 1.
  Record both numbers for the PR body.

- [ ] **Step 6: Commit.**

  ```bash
  git add src/issuebot/github tests/test_github_normalise.py tests/test_github_ghcli.py tests/test_github_fake.py
  git commit -m "github: a parent's steps for its review, the sub-issue total, and closing as completed"
  ```

### Task 2: The verdict, the settings, the events

**Files:**
- Modify: `src/issuebot/config/settings.py` (`TriageSettings`, line 275)
- Modify: `src/issuebot/agent/verdict.py`
- Modify: `src/issuebot/events/types.py` and `src/issuebot/events/__init__.py`
- Modify: `src/issuebot/notifications/messages.py` (`_completed`)
- Modify: `src/issuebot/web/views.py` (`describe_event`)
- Test: `tests/test_settings.py`, `tests/test_agent_verdict.py`, `tests/test_events.py`,
  `tests/test_notifications_messages.py`, `tests/test_web_app.py`

**Interfaces:**
- Produces:
  - `TriageSettings.max_gap_rounds: int = Field(default=2, ge=0)` and
    `max_parent_reviews: int = Field(default=6, ge=1)`.
  - In `verdict.py`:
    - `ParentReviewVerdict`, frozen, kw-only and slotted:
      `verdict: Literal["next", "met", "gap"]`, `summary: str`, `evidence: str`,
      `ignored_requests: str` and `sub_issues: tuple[SubIssueDraft, ...]`.
    - `parent_review_schema(max_sub_issues: int) -> dict[str, Any]`.
    - `parse_parent_review(raw: object, *, max_sub_issues: int, scrubber: Scrubber) -> ParentReviewVerdict`.
  - The sub-issue loop moves out of `parse_verdict` into a private
    `_drafts(steps: object, *, max_sub_issues: int, scrubber: Scrubber) -> tuple[SubIssueDraft, ...]`
    that both parsers call. It covers the array check, the count limit, each item's fields, the
    one-line title, distinctness and the scrub. Triage's messages do not change.
  - `ParentReviewState(IssueEvent)`, kind `"parent_review_state"`, with fields
    `reviews: int`, `gap_rounds: int`, `handed_over: str | None = None` and
    `finished: bool = False`. It is added to `EVENT_TYPES`. `format_event` returns `None` for
    it: the Slack line for a review is its `StateChanged` (spec section 7).
  - `RunKind = Literal["work", "triage", "parent_review"]`.
  - `CompletionResolution = Literal["merged_pr", "no_change", "parent_review"]`.

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_agent_verdict.py`:

  ```python
  def review(**overrides: Any) -> dict[str, Any]:
      raw = {
          "verdict": "next",
          "summary": "The plan merged.",
          "evidence": "docs/plan.md",
          "ignored_requests": "",
          "sub_issues": [{"title": "Stage 1", "body": "Do it.", "kind": "issuebot"}],
      }
      return {**raw, **overrides}


  def test_a_next_verdict_parses() -> None:
      v = parse_parent_review(review(), max_sub_issues=8, scrubber=DEFAULT_SCRUBBER)
      assert v.verdict == "next" and [d.title for d in v.sub_issues] == ["Stage 1"]


  def test_a_met_verdict_files_nothing() -> None:
      v = parse_parent_review(
          review(verdict="met", sub_issues=[]), max_sub_issues=8, scrubber=DEFAULT_SCRUBBER
      )
      assert v.verdict == "met" and v.sub_issues == ()
  ```

  Then add `test_parse_parent_review_refuses` (parametrised, each raising `VerdictError` with
  the message shown):

  | Input | Message |
  |---|---|
  | `verdict="fits"` | `"verdict must be next, met or gap"` |
  | `verdict="met"` with one sub-issue | `"a met verdict files no sub-issues"` |
  | `verdict="next"`, `sub_issues=[]` | `"a next verdict needs at least one sub-issue"` |
  | `verdict="gap"`, `sub_issues=[]` | `"a gap verdict needs at least one sub-issue"` |
  | `evidence="  "` | `"evidence must not be empty"` |
  | a missing `evidence` | `"missing field evidence in the verdict"` |

  The remaining tests:
  - `test_both_parsers_refuse_the_same_drafts`: parametrise over the existing draft refusals
    (a duplicate title, a two-line title, a bad `kind`, too many), and assert that both
    parsers raise the same message.
  - `test_the_parent_review_schema_matches_the_parser`: it checks:
    - the `enum` is `["next", "met", "gap"]`;
    - `required` is all five fields;
    - `additionalProperties` is `False`;
    - `maxItems` is `max_sub_issues`;
    - the reason fields' `maxLength` is `REASON_LIMIT`;
    - the item schema is the same object triage's schema uses.

  In `tests/test_settings.py`:
  - `test_parent_review_defaults`: `2` and `6`.
  - `test_parent_review_limits_are_bounded`: `max_gap_rounds=-1` and `max_parent_reviews=0`
    are refused.

  In `tests/test_events.py`:
  - `test_parent_review_state_round_trips`: `to_dict()` carries `kind`, the four fields and
    `issue_number`.

  In `tests/test_notifications_messages.py`:
  - `test_a_parent_review_completion`:
    `format_event(IssueCompleted(..., pr_url=None, resolution="parent_review"))` equals
    `":tada: <https://github.com/o/r/issues/10|r-10> complete · the parent review found the ask met"`.
  - `test_a_parent_review_state_has_no_slack_line`: `format_event` returns `None`.

  In `tests/test_web_app.py`, add cases to `test_describe_event`:

  | Payload | Expected |
  |---|---|
  | `parent_review_state`, `{"reviews": 2, "gap_rounds": 1}` | `"parent review record: 2 reviews, 1 gap round"` |
  | the same with `"handed_over": "the review cap"` | `"parent review record: 2 reviews, 1 gap round, handed over: the review cap"` |
  | `{"reviews": 3, "gap_rounds": 0, "finished": True}` | `"parent review record: finished after 3 reviews"` |
  | `issue_completed` with `"resolution": "parent_review"` | `"completed: the parent review found the ask met"` |

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_agent_verdict.py tests/test_settings.py tests/test_events.py tests/test_notifications_messages.py tests/test_web_app.py -q`

  Expected: FAIL.

- [ ] **Step 3: Implement.** `parse_parent_review` checks fields in triage's order:
  1. the object and its fields;
  2. the verdict's value;
  3. the three reasons, through `_text(..., REASON_LIMIT, line_endings=True)`, with
     `evidence` required;
  4. the drafts, through `_drafts`;
  5. the verdict-specific counts.

  It scrubs last, as `parse_verdict` does.

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS. Every existing `test_agent_verdict.py` test passes unchanged.

- [ ] **Step 5: Commit.**

  ```bash
  git add src/issuebot/config/settings.py src/issuebot/agent/verdict.py src/issuebot/events src/issuebot/notifications/messages.py src/issuebot/web/views.py tests
  git commit -m "triage: the parent review's verdict, its two limits and its record event"
  ```

### Task 3: The prompt, its override, the session and `validate`

**Files:**
- Create: `src/issuebot/config/parent_review_prompt.md`
- Modify: `src/issuebot/config/workflow.py`:
  - the `Workflow` fields beside `triage_*`
  - `default_triage_prompt` and `triage_prompt_path_for`, lines 106-117
  - `load_workflow` and `_read_triage`
- Modify: `src/issuebot/orchestrator/orchestrator.py` (`_reload_workflow`, line 1032)
- Modify: `src/issuebot/agent/prompt.py`:
  - beside `TRIAGE_CONTINUATION_TEMPLATE`, line 262
  - beside `TriagePromptContext`, line 877
- Modify: `src/issuebot/agent/triage.py` (`run_triage`, `_execute`)
- Modify: `src/issuebot/agent/runner.py` (`TriageRunner`, line 1636)
- Modify: `src/issuebot/cli.py` (`_triage_check`, line 1601)
- Test: `tests/test_workflow.py`, `tests/test_orchestrator.py` (reload), `tests/test_agent_prompt.py`,
  `tests/test_agent_triage.py`, `tests/test_agent_runner.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes:
  - Task 1's `ReviewStep` and `adapter.parent_review_steps`.
  - Task 2's `parent_review_schema` and the two settings.
- Produces:
  - In `workflow.py`:
    - `PARENT_REVIEW_FILE_NAME = "PARENT_REVIEW.md"`
    - `parent_review_prompt_path_for(path: Path) -> Path`
    - `default_parent_review_prompt() -> str`, read from the package as the triage prompt is.
    - `Workflow` fields `parent_review_template: str`, `parent_review_path: Path | None`,
      `parent_review_empty: bool`, `parent_review_mtime_ns: int`, `parent_review_dev: int`,
      `parent_review_ino: int`, and the property
      `parent_review_identity -> tuple[int, int, int] | None`.
    - `_read_triage` becomes `_read_prompt(path: Path, *, base: Path, name: str)`, called once
      per file. The front-matter refusal reads
      `"{name} takes no front matter; settings belong in WORKFLOW.md"`, so triage's message is
      unchanged.
  - In `_reload_workflow`, the file is stat'ed, compared and checked for a pinned mount exactly
    as `TRIAGE.md` is: a third element in `loaded` and `found`, and the same `OSError` arm.
  - In `prompt.py`:
    - `PARENT_REVIEW_CONTINUATION_TEMPLATE` says this is turn N of M of the review of
      #{number}, and asks for the verdict now.
    - `ParentReviewPromptContext`, frozen, kw-only and slotted. It has
      `TriagePromptContext`'s fields plus `steps: tuple[ReviewStep, ...]`, `reviews: int` and
      `gap_rounds: int`, and it is built only through
      `for_issue(issue, settings, *, login, admin, comments, steps, reviews, gap_rounds, repo_instructions=(), turn_number=1)`.
      `to_variables()` adds the following to triage's variables:
      - `steps`, a list with one dict per step:
        - `number`, `url`;
        - `title`: always `GitHubText` with the step author's association;
        - `kind`: `"operator"` when the step carries `labels.operator_step`, else
          `"issuebot"`;
        - `state_reason`: the lower-cased reason with `_` as a space (`"completed"`,
          `"not planned"`), or `"closed"` when GitHub gave none;
        - `body`: `GitHubText` of `visible_text(body_html)` for a maintainer association,
          else `None`;
        - `author`: the login, or `UNKNOWN_AUTHOR`;
        - `quarantined`: `True` when the body was withheld;
        - `pr`: `None`, or a dict with `number`, `url`, `title` (`GitHubText`), `body` and
          `quarantined` (as for the step), and
          `reviews: {"maintainers": [GitHubText], "quarantined": [{"author", "url"}]}`. A
          review's `GitHubText` source names its state.
      - `reviews: {"used", "max", "left"}` and `gap_rounds: {"used", "max", "left"}`, read
        from the record and the settings.
  - In `agent/triage.py`:
    - `ParentReviewRun(reviews: int, gap_rounds: int)`, frozen, kw-only and slotted.
    - `run_triage(..., parent_review: ParentReviewRun | None = None)`. When it is given:
      - `run_kind` on `RunStarted`, `RunEnded` and both log lines is `"parent_review"`;
      - the template is `workflow.parent_review_template`, with
        `PARENT_REVIEW_CONTINUATION_TEMPLATE`;
      - `_execute` reads `adapter.parent_review_steps(issue.number)` once, after the
        comments, and fails the run on a `GitHubError` with
        `state.fail("github_error", f"could not read the sub-issues: {exc}")`;
      - the context is `ParentReviewPromptContext.for_issue`.

      Everything else stays the same: the clone, the sweep, the deadline, the stop on a
      verdict.
  - In `runner.py`, `TriageRunner` gains `def verdict_schema(self) -> dict[str, Any]`, which
    returns `verdict_schema(self._triage.max_sub_issues)` and is used by `build_argv`.
    `ParentReviewRunner(TriageRunner)` overrides it to return
    `parent_review_schema(self._triage.max_sub_issues)`.
  - `_triage_check` also renders the parent-review prompt (first turn and continuation),
    against the sample issue, the sample comments and two sample steps: one closed by a merged
    PR with a maintainer's review, and one operator step closed as not planned. Its detail
    gains `, parent review prompt {source}, {max_gap_rounds} gap round(s), at most {max_parent_reviews} reviews`,
    where `{source}` is worded as triage's is.

- [ ] **Step 1: Write the shipped prompt.** Start from `triage_prompt.md`'s structure and rules.
  It must state:
  - **What it is doing:** reviewing a split parent whose steps have all closed, reading the
    clone of the default branch (which now holds the merged work), changing nothing, and
    returning a verdict with the StructuredOutput tool.
  - **The `<github-text>` rule,** word for word as triage states it, with `ignored_requests`.
  - **The inputs:** the parent issue and the maintainers' comments, then each step with its
    kind and close reason, then its pull request and maintainers' reviews where there is one.
    Withheld text is shown as author and URL only.
  - **The three verdicts:**
    - `next`: the steps were a phase, typically a merged plan. File the stages the plan
      describes, in order, each landing as its own pull request, with any pause for the
      maintainer to test by hand as an `operator` step.
    - `met`: every part of the original ask is in the merged code, with `evidence` naming
      where.
    - `gap`: name what is missing, one sub-issue per gap.
  - **Two rules for it:**
    - A step closed as not planned is done, but the review judges whether the ask still
      needs what it would have done.
    - When `gap_rounds.left` is 0, a `gap` files nothing and hands the issue to the maintainer.
  - **Triage's rules on sub-issue bodies:** what to do, acceptance criteria, distinct one-line
    titles, and no `Part of #...`, labels or step numbers. Also `{{ max_sub_issues }}` as the
    limit, and `sub_issues` empty for `met`.
  - **The repository instructions and limits sections,** as triage renders them.

- [ ] **Step 2: Write the failing tests.**

  In `tests/test_workflow.py`, mirroring the `TRIAGE.md` tests beside it:
  - `test_without_parent_review_md_the_shipped_prompt_is_used`
  - `test_parent_review_md_replaces_the_shipped_prompt`, where the path and identity are set
  - `test_an_empty_parent_review_md_falls_back_and_says_so`
  - `test_parent_review_md_takes_no_front_matter`, asserting the message above

  In `tests/test_orchestrator.py`:
  - `test_editing_parent_review_md_reloads_the_workflow`, the shape of
    `test_editing_triage_md_reloads_the_workflow`.

  In `tests/test_agent_prompt.py`:
  - `test_parent_review_variables`:
    - Step 11 is by a `COLLABORATOR`, merged by PR 21 with an `OWNER`'s `APPROVED` review
      and a `NONE` account's review.
    - Step 12 is an operator step by a `NONE` account, closed `NOT_PLANNED`.
    - Assert:
      - step 11's `body` is a `GitHubText` whose `.text` is its visible text;
      - step 12's `body is None`, `quarantined` is true, `kind == "operator"` and
        `state_reason == "not planned"`;
      - `pr.reviews.maintainers` has one entry and `pr.reviews.quarantined == [{"author": "stranger", "url": ...}]`;
      - `reviews == {"used": 1, "max": 6, "left": 5}`.
  - Extend the test that classifies every template variable by name, so the new ones are
    classified.
  - `test_the_shipped_parent_review_prompt_renders`: first turn and continuation, with
    `check_envelopes` clean.

  In `tests/test_agent_triage.py`, using the module's `TriageHarness`:
  - `test_a_parent_review_run_is_its_own_kind`: `RunStarted.run_kind` and
    `RunEnded.run_kind` are `"parent_review"`, and the rendered prompt the fake runner
    received starts with the parent-review template's first line.
  - `test_a_parent_review_reads_the_steps_once`: one `("parent_review_steps", (N,))` call.
  - `test_unreadable_steps_fail_the_review_with_github_error`: the error starts
    `"could not read the sub-issues:"`.

  In `tests/test_agent_runner.py`:
  - `test_the_parent_review_runner_asks_for_its_own_schema`: its argv equals
    `TriageRunner`'s, except the value after `--json-schema`, which is
    `json.dumps(parent_review_schema(8), separators=(",", ":"))`.

  In `tests/test_cli.py`:
  - `test_validate_triage_reports_the_parent_review_prompt`: the detail contains
    `"parent review prompt shipped default, 2 gap round(s), at most 6 reviews"`.
  - `test_validate_fails_a_parent_review_md_that_does_not_render`: the triage check fails
    with the renderer's message.

- [ ] **Step 3: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_workflow.py tests/test_agent_prompt.py tests/test_agent_triage.py tests/test_agent_runner.py tests/test_cli.py -q -k "parent_review or triage"`

  Expected: FAIL.

- [ ] **Step 4: Implement it as the interfaces say.**

- [ ] **Step 5: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS.

- [ ] **Step 6: Commit.**

  ```bash
  git add src/issuebot/config src/issuebot/agent src/issuebot/cli.py src/issuebot/orchestrator/orchestrator.py tests
  git commit -m "triage: the parent review's prompt, its PARENT_REVIEW.md override and its session"
  ```

### Task 4: Filing a review, and the parent hand-over

**Files:**
- Create: `src/issuebot/orchestrator/parent_review.py`
- Modify: `src/issuebot/orchestrator/triage.py` (`sub_issue_body`, line 46)
- Modify: `src/issuebot/orchestrator/actions.py` (`_hand_back`, line 422, and a new
  `parent_hand_over`)
- Test: `tests/test_orchestrator_parent_review.py` (new), `tests/test_orchestrator_actions.py`

**Interfaces:**
- Consumes:
  - Task 1's `ParentSteps.state`, `adapter.close_as_completed` and `adapter.sub_issues`.
  - Task 2's `ParentReviewVerdict` and `ParentReviewState`.
- Produces, in `parent_review.py`:
  - `ParentRecord`, frozen, kw-only and slotted: `identifier: str`, `reviews: int = 0`,
    `gap_rounds: int = 0`, `handed_over: str | None = None` and `finished: bool = False`. Its
    property `watched -> bool` is `handed_over is None and not finished`. Its method
    `state_event(self, number: int) -> ParentReviewState` returns the record as an event.
  - `parent_review_due(steps: ParentSteps, labels: GitHubLabels) -> bool`. It is true when the
    parent is open, carries `triage_complete`, carries no `triage_todo` and no state label
    (`labels.as_tuple()`), and has at least one step, every one of them closed. The labels are
    compared lower-cased.
  - `ReviewCounts`, frozen, kw-only and slotted: `review: int`, `max_reviews: int`,
    `gap_round: int` and `max_gap_rounds: int`. These are the values after this review: a
    `gap` adds one to the gap rounds.
  - `parent_review_note(verdict: ParentReviewVerdict) -> str`. It returns
    `"next: 2 sub-issues"`, `"gap: 1 sub-issue"` or `"met"`, pluralised with `triage.plural`.
  - `parent_review_comment(verdict, filed, *, counts: ReviewCounts, run_id: str, attempt: int, turns: int, cost_usd: float, labels: GitHubLabels, now: datetime) -> str`.
    It is laid out as `verdict_comment` is: heading, verdict line, the numbered filed steps
    (with ` -- operator step` on operator steps), the summary, then
    `- **Evidence:** {evidence}`, the ignored-requests bullet when there is one, and the run
    line. The verdict lines, exactly:
    - next: ``**Verdict: next.** The steps so far were a phase, so issuebot has filed the next {n} sub-issues, in order, after the others. Label every issuebot step `{todo}` at once if you like: issuebot starts each only when every step before it has closed.``
    - gap: ``**Verdict: gap.** Something the original ask needs is missing, so issuebot has filed one sub-issue per gap. This is gap round {gap_round} of {max_gap_rounds}.``
    - met: ``**Verdict: met.** The original ask is met, so issuebot has closed this issue and labelled it `{complete}`.``

    The run line ends ` Review {review} of at most {max_reviews}.`
  - `async apply_parent_review(adapter: GitHubAdapter, bus: EventBus, issue: Issue, verdict: ParentReviewVerdict, *, seen: Sequence[int], counts: ReviewCounts, run_id: str, attempt: int, turns: int, cost_usd: float, now: datetime, on_moved: Callable[[], None] | None = None, moved: bool = False) -> list[SubIssue]`.
    It raises `GitHubError` from any step, and the caller retries. The writes, in order:
    - **next and gap:**
      1. Read `adapter.sub_issues(issue.number)`. Match each draft by `_key(title)` only
         against children whose number is not in `seen`, and create those that are missing,
         with footer numbers `len(seen) + i` of `len(seen) + len(drafts)`.
      2. Add `operator_step` to each operator draft's child.
      3. `edit_labels(add=[triage_complete], remove=[triage_todo])`, then `on_moved()`.
      4. Unless `moved`, publish
         `StateChanged(from_label=<triage_todo if it is in issue.labels, else triage_complete>, to_label=triage_complete, actor="issuebot", note=parent_review_note(verdict))`.
      5. Comment.
    - **met:**
      1. `edit_labels(add=[complete, triage_complete], remove=[triage_todo])`.
      2. `close_as_completed`, then `on_moved()`.
      3. Unless `moved`, publish `StateChanged(from_label=None, to_label=complete, actor="issuebot", note="met")`
         and `IssueCompleted(pr_url=None, resolution="parent_review")`.
      4. Comment.
- Produces, in `triage.py`: `sub_issue_body(..., filed_by: str = "issuebot triage")`, whose
  footer reads `filed by {filed_by}.`. The review passes `"issuebot's parent review"`.
- Produces, in `actions.py`:
  - `_hand_back(..., blocked: bool = True)`. When it is false, no `Blocked` is published.
  - `async parent_hand_over(adapter, bus, issue, *, title: str, reason: str, way_back: str, now: datetime, blocked: bool = True) -> EscapeOutcome`.
    This is `_hand_back` with `prefix="parent_hand_over"`, the label write
    `edit_labels(add=[triage_complete], remove=[triage_todo])`, `from_label` set to
    `triage_todo` if the issue carries it and otherwise to `triage_complete`, and `to_label`
    set to `triage_complete`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_orchestrator_parent_review.py`, a
  `FakeGitHub`, a `Recorder` and a `split(github, parent, *children, closed=...)` helper:

  ```python
  def test_due_when_every_step_has_closed() -> None:
      assert parent_review_due(
          steps(state="open", labels=("issuebot/triage-complete",), sub=[("closed",), ("closed",)]),
          LABELS,
      )


  @pytest.mark.parametrize(
      "change",
      [
          "a step open",
          "no steps",
          "parent closed",
          "no triage-complete",
          "triage-todo",
          "a state label",
      ],
  )
  def test_not_due(change: str) -> None: ...
  ```

  Then:
  - `test_next_files_the_batch_after_the_others`:
    - Parent 10 has closed steps 11 and 12 (`seen=(11, 12)`). The verdict holds an issuebot
      draft and an operator draft.
    - Assert:
      - two children are created, linked after 12;
      - their footers read `step 3 of 4` and `step 4 of 4` with `filed by issuebot's parent review.`;
      - the operator child carries `issuebot/operator-step`;
      - the parent carries `triage-complete`;
      - one `StateChanged` (`triage_complete` → `triage_complete`, note `"next: 2 sub-issues"`);
      - the comment is the last call and starts `### Issuebot parent review (`.
  - `test_a_label_triggered_review_swaps_triage_todo_off`: the parent carries only
    `triage-todo`. Afterwards it carries only `triage-complete`, and the `StateChanged` is
    `from_label="issuebot/triage-todo"`.
  - `test_a_new_step_may_reuse_an_earlier_steps_title`: seen step 11 is titled `"Write the plan"`,
    and a draft has the same title. A new child is created.
  - `test_a_retry_after_a_failed_creation_files_only_what_is_missing`:
    - `fail_next("network", on="create_sub_issue", after=1)`, then retry with the same `seen`.
    - Exactly two children in all, one `StateChanged` and one comment.
  - `test_a_retry_after_the_move_publishes_nothing_again`: with `moved=True`, no `StateChanged`
    is published, and the comment is posted.
  - `test_gap_says_which_round_it_is`: the comment contains `This is gap round 1 of 2.`
  - `test_met_closes_the_parent_as_complete`: the parent is closed and carries
    `issuebot/complete` and `issuebot/triage-complete`. The calls run in the order
    `edit_labels`, `close_as_completed`, `comment`. The recorder's kinds are
    `["state_changed", "issue_completed"]`, and the completion's `resolution` is
    `"parent_review"`.
  - `test_met_retried_after_the_close_only_comments`: `fail_next(on="comment")`, then retry
    with `moved=True`. One comment, and still one close.

  In `tests/test_orchestrator_actions.py`:
  - `test_parent_hand_over_puts_triage_complete_back`: the parent carries only `triage-todo`.
    Afterwards it carries only `triage-complete`, the comment starts
    `### Issuebot parent review limit (`, and the kinds are `["state_changed", "blocked"]`.
  - `test_parent_hand_over_on_an_automatic_review`: the parent carries only `triage-complete`,
    and the `StateChanged` goes from `triage_complete` to `triage_complete`.
  - `test_a_waiting_note_is_not_blocked`: with `blocked=False`, the kinds are
    `["state_changed"]`.
  - `test_parent_hand_over_whose_label_write_fails_writes_nothing`: it returns `"failed"`,
    with no comment and no event.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_orchestrator_parent_review.py tests/test_orchestrator_actions.py -q`

  Expected: FAIL.

- [ ] **Step 3: Implement it as the interfaces say.**

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS. Triage's footer tests pass unchanged.

- [ ] **Step 5: Commit.**

  ```bash
  git add src/issuebot/orchestrator tests/test_orchestrator_parent_review.py tests/test_orchestrator_actions.py
  git commit -m "orchestrator: file a parent review's verdict, and hand a parent over in one shape"
  ```

### Task 5: The worker reviews a parent by itself

**Files:**
- Modify: `src/issuebot/orchestrator/state.py`:
  - `RunningEntry`, line 160
  - a new `PendingParentReview` beside `PendingVerdict`, line 190
- Modify: `src/issuebot/orchestrator/orchestrator.py`:
  - `__init__`
  - `tick`, line 941
  - `_start_run`, line 1642
  - `_file_verdict`, line 1820
  - `_retry_pending_verdicts`, line 1783
  - `_refresh_running`, line 2207
  - `handle_worker_exit`, line 2495
  - new methods beside the triage ones
- Test: `tests/test_orchestrator.py` (a new section after phase 2's, and the `Harness`)

**Interfaces:**
- Consumes: Tasks 1 to 4.
- Produces:
  - `Orchestrator.__init__` gains:
    - `parent_review_runner_factory: Callable[[Settings], TurnRunner] = ParentReviewRunner`
    - `initial_parents: Mapping[int, ParentRecord] | None = None`

    `Harness` passes `parent_review_runner_factory=self.make_triage_runner` and
    `initial_parents`.
  - State:
    - `self._parents: dict[int, ParentRecord]`, seeded from `initial_parents`
    - `self._parent_pending: dict[str, PendingParentReview]`
    - `self._parent_handovers: dict[str, ParentHandOver]`, a parent hand-over GitHub refused
      to write. `ParentHandOver(title: str, reason: str, way_back: str)` is a frozen dataclass
      in `parent_review.py`.
  - `RunningEntry.steps_seen: tuple[int, ...] = ()`.
  - `PendingParentReview`, frozen, kw-only and slotted: `issue`, `verdict`,
    `seen: tuple[int, ...]`, `counts: ReviewCounts`, `run_id`, `attempt`, `turns`, `cost_usd`,
    `tries: int = 0` and `moved: bool = False`.
  - `_record_parent(number: int, record: ParentRecord) -> None` stores the record and publishes
    `record.state_event(number)`. It is the one place a record changes.
  - When `_file_verdict` files a split, it calls
    `_record_parent(issue.number, ParentRecord(identifier=issue.identifier))`.
  - `async _dispatch_parent_reviews() -> int` runs from `tick` between `_dispatch_triage` and
    `_dispatch_candidates`:
    1. **The watch list.** It is the numbers of watched records. With none, it returns 0. It
       returns 0 without a read when `_slots() == 0` and no hand-over is owed.
    2. **The read.** One `parent_steps(watched)`. A `GitHubError` logs `parent_watch_failed`
       and returns 0. It does not feed the GitHub hold.
    3. **Before the due test,** for each watched number:
       - If GitHub returned nothing for it, or it is closed, it gets
         `_record_parent(n, replace(record, finished=True))`.
       - If it is not `parent_review_due`, it is skipped.
    4. **The issues.** One `fetch_issues_by_ids` of the due parents' `id`s.
    5. **For each due issue, in order:**
       - skip it while it is running, pending, or before its `_triage_not_before`;
       - retry an owed hand-over and `continue`;
       - if `record.reviews >= max_parent_reviews`, hand it over as `parent review limit`;
       - ask `_admit(kind="parent_review")`. On `slots`, `hold` or `stopping`, `break`. On
         `spend`, hand it over as `parent review budget limit`.
       - `_assess_approval(issue, label=triage_todo)`: `None` skips it, and `Unapproved` hands
         it over as `unapproved edit`;
       - `_bind_account`;
       - `_start_run(kind="parent_review", attempt=_triage_failures.get(id, 0) + 1)`, with
         `steps_seen` set to the parent's step numbers.
  - `async _hand_over_parent(issue, *, title: str, reason: str, way_back: str) -> EscapeOutcome`
    calls `actions.parent_hand_over` with a scrubbed reason.
    - If it was applied: bump `blocked`, record
      `replace(record_or_new, handed_over=reason)`, forget the chain, and drop any owed entry.
    - If it failed: store `ParentHandOver(title, reason, way_back)` in `_parent_handovers`.

    The way-back texts, exactly:
    - **limit:** ``It has had {max_parent_reviews} reviews. Add `{triage_todo}` to review it again with its count reset.``
    - **budget:** as triage's spend way back, ending ``then add `{triage_todo}` again.``
    - **unapproved:** ``Apply `{triage_todo}` again to approve the text as it stands now; issuebot then reviews it afresh.``
    - **not filed:** ``Unlink any sub-issues this review filed from this issue, then add `{triage_todo}` again.``
  - `_start_run` names the task `issuebot-parent-review-{n}` and logs
    `parent_review_dispatched` for this kind.
  - `async _parent_review_worker(entry, workflow, record) -> RunResult` calls
    `self._run_triage(...)` with `runner=self._parent_review_runner_factory(settings)` and
    `parent_review=ParentReviewRun(reviews=record.reviews, gap_rounds=record.gap_rounds)`.
  - In `_refresh_running`, a `parent_review` entry is stopped (`"moved"`) when the current
    issue has a state label, or carries neither triage label.
  - `handle_worker_exit` routes `parent_review` to `async _parent_review_exit(entry, result, error)`,
    which is `_triage_exit`'s shape:
    - The stop causes and the account-wide holds are handled the same way.
    - On a verdict, `parse_parent_review`. A refused verdict goes to
      `_triage_failed(entry, f"verdict refused: {exc}")`.
    - A `gap` with `record.gap_rounds >= max_gap_rounds` is Task 6's gap-limit hand-over.
    - Otherwise it builds a `PendingParentReview`, with `counts` from the record and settings,
      and `seen=entry.steps_seen`, and calls `_file_parent_review`.
  - `async _file_parent_review(pending) -> GitHubError | None` and
    `async _parent_review_owed(pending) -> Issue | None` follow `_file_verdict` and
    `_verdict_owed`:
    - **The owed check** reads `fetch_issues_by_ids` and `parent_steps` for the one parent.
      Before the move, it is owed only while the parent is open, carries a triage label and
      no state label, and its step numbers equal `seen` with every step closed. After the
      move, it is owed while the parent exists.
    - **On success:**
      - `_record_parent(n, ParentRecord(identifier, reviews=counts.review, gap_rounds=counts.gap_round, finished=verdict == "met"))`;
      - forget the chain;
      - schedule the clone's discard;
      - log `parent_review_filed`.
    - **On failure,** the pending entry is kept with `moved`, and it logs
      `parent_review_file_failed`.
    - **When not owed,** it logs `parent_review_discarded`. Its `reason` is one of
      `"missing"`, `"closed"`, `"state label"`, `"triage labels removed"` or
      `"steps changed"`.
  - `_retry_pending_verdicts` also retries `_parent_pending`. After `agent.max_attempts` failed
    tries, the parent is handed over as `parent review not filed`.

- [ ] **Step 1: Write the failing tests.** Add a helper beside phase 2's `add_split`:

  ```python
  def watched_split(
      h: Harness,
      parent: int,
      *children: int,
      open_: tuple[int, ...] = (),
      record: ParentRecord | None = None,
  ) -> None:
      """A triaged parent whose steps are closed but `open_`, with a parent record."""
  ```

  Seed the record through a `Harness(..., initial_parents=...)` argument. The parent's
  `triage-todo` is in its history, recorded with
  `github.human_record_label(parent, labels.triage_todo, actor="maintainer")`, so approval
  passes while the parent carries only `triage-complete`.

  The tests:
  - `test_a_seeded_parent_is_reviewed_on_the_first_tick`:
    - One triage-scripted run has started, with `kwargs["parent_review"] == ParentReviewRun(reviews=0, gap_rounds=0)`.
    - `h.entry(10).kind == "parent_review"`.
    - The parent's labels are unchanged.
    - `h.calls("parent_steps") == [([10],)]`.
  - `test_a_parent_with_an_open_step_is_not_reviewed`.
  - `test_a_step_closed_as_not_planned_counts_as_closed`.
  - `test_a_parent_without_a_record_is_not_reviewed`: a `fits` issue to which a human linked
    sub-issues, all closed. No run starts, and no `parent_steps` call is made.
  - `test_a_split_filed_by_triage_is_watched`:
    - A triage run splits issue 10 into two steps.
    - Close both, then tick.
    - A parent review starts, and the recorder holds a `parent_review_state` for 10 with
      `reviews == 0`.
  - `test_a_next_verdict_files_the_next_batch_and_keeps_watching`:
    - Exit the run with a `next` verdict holding two drafts.
    - Two new children follow the old ones.
    - The record is `reviews == 1`, and a `parent_review_state` is published.
    - Label both new steps `todo` and tick: phase 2 holds the second.
    - Close both and tick: a second review starts with `ParentReviewRun(reviews=1, gap_rounds=0)`.
  - `test_a_gap_verdict_counts_a_round`: the record is `reviews == 1, gap_rounds == 1`.
  - `test_a_met_verdict_closes_the_parent_and_ends_the_watch`:
    - The parent is closed and labelled `complete`.
    - The record is `finished`.
    - On the next tick, no `parent_steps` call is made.
  - `test_a_review_whose_steps_changed_is_discarded`:
    - Reopen step 11 while the run is going, and exit with `next`.
    - Nothing is created, there is no comment, and `parent_review_discarded` is logged with
      `reason="steps changed"`.
    - The record is unchanged.
  - `test_a_review_stops_when_the_parent_gains_a_state_label`, and
    `test_a_review_stops_when_the_parent_loses_both_triage_labels`: the reconcile stops it,
    and its verdict is discarded at exit.
  - `test_a_failed_filing_is_retried_from_memory`:
    - `fail_next("network", on="create_sub_issue")`, then exit with `next`, then tick.
    - Both children exist, with one `StateChanged`, one comment and one session in all.
  - `test_a_closed_parent_is_no_longer_watched`:
    - A human closes 10, then tick.
    - A `parent_review_state` with `finished=True` is published, and no run starts.
    - On the next tick, no `parent_steps` call is made.
  - `test_a_parent_without_triage_complete_is_left_alone`: a human removes `triage-complete`.
    No run starts and no comment is posted, and the record is unchanged and still watched.
  - `test_the_watch_is_skipped_without_a_free_slot`: with `max_concurrent=1` and a work run
    going, no `parent_steps` call is made.
  - `test_triage_takes_a_slot_before_a_review_and_a_review_before_work`: with
    `max_concurrent=1`, a triage candidate, a due parent and a `todo` issue, only the triage
    run starts.
  - `test_the_review_cap_hands_the_parent_over`:
    - The record is seeded with `reviews=6`.
    - No run starts.
    - The comment starts `### Issuebot parent review limit (`.
    - `Blocked` is published, and `triage-complete` is kept.
    - The record is `handed_over`.
    - On the next tick, no `parent_steps` call is made.
  - `test_an_edited_parent_is_handed_over_unapproved`:
    - `human_edit_body(10, ..., actor="someone")` after the maintainer's `triage-todo`.
    - The comment starts `### Issuebot unapproved edit (`, and `triage-complete` is kept.
    - The record is `handed_over`, and no run starts.
  - `test_a_parent_over_its_budget_is_handed_over`: `max_issue_cost_usd` is below the
    ledger's spend. The comment starts `### Issuebot parent review budget limit (`.
  - `test_a_refused_hand_over_is_written_on_the_next_tick`:
    - `fail_next("network", on="edit_labels")` during the limit hand-over.
    - The next tick writes it, with no session.
  - `test_a_filing_that_keeps_failing_is_handed_over`:
    - With `max_attempts=2`, `create_sub_issue` fails every time.
    - After two failed retries, the comment starts `### Issuebot parent review not filed (`.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_orchestrator.py -q -k "parent or review or watch"`

  Expected: FAIL.

- [ ] **Step 3: Implement it as the interfaces say.**

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS.

- [ ] **Step 5: Commit.**

  ```bash
  git add src/issuebot/orchestrator tests/test_orchestrator.py
  git commit -m "orchestrator: review a split parent once every step has closed"
  ```

### Task 6: `triage-todo` on a split parent, and every hand-over

**Files:**
- Modify: `src/issuebot/orchestrator/orchestrator.py`:
  - `_dispatch_triage`, line 1727
  - `_dispatch_triage_one`, line 1920
  - `_triage_failed`, line 2070
  - `_triage_hand_back`, line 2111
  - `_parent_review_exit`
- Modify: `src/issuebot/orchestrator/actions.py`: remove `triage_escape`'s `restore_complete`
- Test: `tests/test_orchestrator.py`, `tests/test_orchestrator_actions.py`

**Interfaces:**
- Consumes:
  - Task 4's `parent_hand_over`.
  - Task 5's `_record_parent`, `_hand_over_parent` and `_parent_handovers`.
- Produces:
  - `_dispatch_triage` retries an owed parent hand-over for a candidate before anything else,
    as `_dispatch_parent_reviews` already does.
  - `_triage_hand_back` delegates to `_hand_over_parent` when `issue.sub_issue_count > 0`
    (ruling 4). Its `restore_complete` parameter, and `triage_escape`'s, are removed. Their
    only caller was the "already split" path this task replaces.
  - In `_dispatch_triage_one`, after the approval check (and an unapproved split parent goes
    through `_triage_hand_back`, so it is now a parent hand-over), an issue with
    `sub_issue_count > 0` takes this path instead of the "already split" hand-back:
    1. Read `adapter.sub_issues(n)`. A failure skips it this tick, as today.
    2. If any step is open:
       - `_record_parent(n, ParentRecord(identifier=issue.identifier))`;
       - `actions.parent_hand_over(..., title="parent review waiting", blocked=False)`, whose
         reason is ``#{n} still has open steps: #a, #b. issuebot reviews it by itself once every step has closed.``
         and whose way back is
         ``Its review count and gap rounds are reset. issuebot has put `{triage_complete}` back, so its steps still run in order.``;
       - return `False`.
    3. If every step is closed:
       - `_record_parent(n, ParentRecord(identifier=issue.identifier))`;
       - bind the account;
       - `_start_run(kind="parent_review", steps_seen=<the numbers>)`;
       - return `True`.
  - In `_triage_failed`, when the limit is reached on a `parent_review` entry, it hands over as
    `parent review failed`, with ``Parent review failed {n} times; the last: {error}`` and the
    way back ``Add `{triage_todo}` again to review it once the cause is fixed.``. A `triage`
    entry is unchanged.
  - The gap limit, in `_parent_review_exit`, for a `gap` with
    `record.gap_rounds >= max_gap_rounds`:
    - `_hand_over_parent(title="parent review gap limit")`, with the reason
      ``The review found gaps again after {max_gap_rounds} gap round(s), so it filed nothing: {summary}``
      and the way back
      ``What it would have filed:\n{"- " + title per draft}\n\nFile what is still needed yourself, or add `{triage_todo}` to review it afresh with its rounds reset.``.
    - Then record `reviews + 1`, keeping the `handed_over` that the hand-over set.
    - A `next` past the gap rounds is filed as usual (ruling 17).

- [ ] **Step 1: Write the failing tests.**

  Update phase 2's tests:
  - `test_an_issue_with_sub_issues_is_handed_back_as_already_split` becomes
    `test_triage_todo_on_a_split_parent_with_open_steps_waits`:
    - The comment starts `### Issuebot parent review waiting (`.
    - The parent carries `triage-complete` and not `triage-todo`.
    - `"blocked" not in h.recorder.kinds`.
    - A `parent_review_state` with `reviews == 0` is published.
  - `test_re_triaging_a_split_parent_keeps_its_steps_in_order`: the same swap, now expecting
    the waiting note. Step 12 stays held over two ticks.

  New tests:
  - `test_triage_todo_on_a_split_parent_with_every_step_closed_runs_a_review`:
    - The record is seeded with `reviews=6, gap_rounds=2`.
    - A human adds `triage-todo`.
    - A review starts with `ParentReviewRun(reviews=0, gap_rounds=0)`, and a reset
      `parent_review_state` is published first.
    - Exiting with `next` leaves the parent with only `triage-complete`.
  - `test_the_gap_limit_files_nothing_and_hands_over`:
    - The record is seeded with `gap_rounds=2`, and the run exits with a `gap` holding two
      drafts.
    - No child is created.
    - The comment starts `### Issuebot parent review gap limit (` and lists both titles.
    - The record is `reviews == 1` (from 0) and `handed_over`.
  - `test_a_next_past_the_gap_rounds_is_still_filed`:
    - The record is seeded with `gap_rounds=2`, and the run exits with a `next`.
    - Its children are created, and the record is not handed over.
  - `test_a_review_that_keeps_failing_is_handed_over`: with `max_attempts=1` and an exit with
    no verdict, the comment starts `### Issuebot parent review failed (`.
  - `test_triage_todo_resumes_a_handed_over_parent`:
    - The record is `handed_over`, and every step is closed.
    - A human adds `triage-todo`, and a review runs.
    - The record's `handed_over` is `None`.
  - `test_an_unapproved_hand_back_of_a_split_parent_puts_triage_complete_back`:
    - The parent was swapped to `triage-todo` only, and edited afterwards by someone else.
    - It ends with `triage-complete` back and a `parent_review_state` with `handed_over` set.
    - A `todo` step under it is still held.
  - In `tests/test_orchestrator_actions.py`, delete
    `test_triage_escape_can_put_triage_complete_back`. Task 4's `parent_hand_over` tests
    replace it.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_orchestrator.py tests/test_orchestrator_actions.py -q -k "parent or split or triage_escape"`

  Expected: FAIL.

- [ ] **Step 3: Implement it as the interfaces say.**

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS.

- [ ] **Step 5: Commit.**

  ```bash
  git add src/issuebot/orchestrator tests/test_orchestrator.py tests/test_orchestrator_actions.py
  git commit -m "orchestrator: triage-todo on a split parent runs its review; every hand-over keeps triage-complete"
  ```

### Task 7: The store's seed, the worker's wiring and `run-once`

**Files:**
- Modify: `src/issuebot/db/queries.py` (a new query and a `RepoQueries.parent_records()`
  beside `issue_ledgers`, line 408)
- Modify: `src/issuebot/db/__init__.py` (export `ParentRecordRow`)
- Modify: `src/issuebot/cli.py`:
  - a new `_initial_parents` beside `_initial_ledger`, line 2545
  - `_run_worker`, line 2582
  - `_run_once_triage`, line 2164
- Test: `tests/test_db_queries.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 2's event, Task 3's prompt context, and Task 5's `initial_parents` and
  `ParentRecord`.
- Produces:
  - `ParentRecordRow(number: int, identifier: str, reviews: int, gap_rounds: int, handed_over: str | None)`.
  - `RepoQueries.parent_records() -> list[ParentRecordRow]`: the latest `parent_review_state`
    event per issue in this repository (`DISTINCT ON (issue_number) ... ORDER BY issue_number, at DESC, id DESC`),
    leaving out the finished ones. It has no time window: a parent can wait weeks for its
    steps.
  - `async _initial_parents(database: Database | None, repo: str) -> dict[int, ParentRecord]`
    follows `_initial_ledger`'s shape and failure handling. A failed read logs
    `parent_records_seed_failed` and returns `{}`. Success logs `parent_records_seeded` with
    `parents=` and `handed_over=` counts. `_run_worker` passes `initial_parents=` to the
    orchestrator factory.
  - In `_run_once_triage`:
    - **An issue with sub-issues** is refused before anything is written, and before the
      approval check, with:
      ``[FAIL] issue: #{n} already has sub-issues; its parent review runs on the worker, by itself once every step has closed, or when you add `{triage_todo}` with the worker running``.
    - **`--show-prompt` on such an issue** renders `ParentReviewPromptContext` with the steps
      from `adapter.parent_review_steps` and a reset record, and skips the refusal.
    - **A split that `run-once --triage` files** also publishes
      `ParentReviewState(issue_number=n, issue_identifier=..., reviews=0, gap_rounds=0)` on
      the sinks' bus, so the store records it. A running worker picks it up at its next
      restart. `docs/operations.md` says so (Task 8).

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_db_queries.py`, against `test-db`, writing through `PostgresStore.apply_event`
  as the module's other seeds do:
  - `test_parent_records_are_each_parents_latest_state`:
    - Issue 10 gets two states, the later one with `reviews=1`.
    - Issue 11's latest state is `finished=True`.
    - Issue 12 has a state under another repository.
    - The result is `[ParentRecordRow(number=10, identifier=..., reviews=1, gap_rounds=0, handed_over=None)]`.

  In `tests/test_cli.py`:
  - `test_initial_parents_maps_the_rows` and `test_initial_parents_survives_a_failed_read`,
    beside the `_initial_ledger` tests.
  - `test_the_worker_is_seeded_with_its_parent_records`: the factory captures
    `initial_parents`, as the existing ledger wiring test does.
  - Rename and update `test_run_once_triage_refuses_an_issue_that_already_has_sub_issues`:
    assert the new message, and that no `approval_evidence` call was made.
  - `test_run_once_triage_show_prompt_on_a_split_parent_renders_the_review`: the output
    contains a step's title envelope and `parent_review_steps` was read.
  - `test_run_once_triage_split_records_the_parent`: the sinks' bus saw one
    `parent_review_state` for the issue.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_cli.py -q -k "parent or run_once_triage"`

  Then start `test-db` and run the store test:

  ```bash
  docker compose --profile test up -d --wait test-db
  DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest tests/test_db_queries.py -q -k parent_records
  ```

  Expected: FAIL.

- [ ] **Step 3: Implement it as the interfaces say.**

- [ ] **Step 4: Run the tests and see them pass.** Run the whole suite once with
  `DATABASE_URL` set, then throw the database away:

  ```bash
  DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest -q
  docker compose rm -sf test-db
  ```

  Expected: PASS, with the store tests running rather than skipping.

- [ ] **Step 5: Commit.**

  ```bash
  git add src/issuebot/db src/issuebot/cli.py tests/test_db_queries.py tests/test_cli.py
  git commit -m "cli: seed the worker's parent records from the store; run-once leaves the review to the worker"
  ```

### Task 8: Documentation and the spec's record

**Files:**
- Modify: `README.md`: the triage paragraph, and the configuration reference's `triage` keys
  and prompt files
- Modify: `docs/operations.md`, "Triage"
- Modify: `docs/security-model.md`, "A triage session"
- Modify: `docs/dashboard.md`, what "issues closed" counts
- Modify: `docs/package-layout.md`:
  - `issuebot.orchestrator`, `issuebot.agent`, `issuebot.github`, `issuebot.db`
  - under 2 KB added
- Modify: `CLAUDE.md` (the `issuebot/complete` row only)
- Modify: `docs/superpowers/specs/2026-10-25-triage-design.md`
- Test: `tests/test_readme_bounds.py`, `tests/test_doc_pointers.py`,
  `tests/test_instruction_bounds.py`, `tests/test_package_layout.py`

**Interfaces:** none. This task is prose only.

- [ ] **Step 1: Write the operator documentation.**
  - **`docs/operations.md`.** Say:
    - what starts a parent review: every step closed on a watched parent, or `triage-todo`;
    - what each verdict does;
    - the two limits;
    - each hand-over's title and its one way back, which is `triage-todo` and resets both
      counts;
    - that a waiting note is not a hand-over;
    - that splits filed before this release are watched only once `triage-todo` has been
      added to each;
    - that a split filed by `run-once --triage` is watched from the worker's next restart;
    - that the worker costs one more GraphQL point a tick while any parent is watched.
  - **`README.md`.** Add the review to the triage paragraph, and add `triage.max_gap_rounds`,
    `triage.max_parent_reviews` and `PARENT_REVIEW.md` to the configuration reference.
  - **`docs/security-model.md`.** A parent review is the same read-only, credential-free
    session, and the sub-issue and pull-request text it reads is filtered by association.
  - **`docs/dashboard.md`.** A parent closed by a `met` review counts as an issue closed.
  - **`CLAUDE.md`.** The row becomes
    `| \`issuebot/complete\` | automatically, when the issue closes via linked-PR merge or with \`issuebot/no-fault\`, or when a parent review finds a split issue's ask met |`.

- [ ] **Step 2: Record the rulings in the spec.**
  - In section 6, add a bullet list headed "Decided while planning phase 3", with rulings 1, 2,
    3, 4, 7, 8, 12, 17 and 18, one or two lines each.
  - Strike through, in "Left to the builder":
    - the store's schema (for the review record);
    - how triage and reviews are ordered for free slots;
    - the prompt files question.

    Note the resolution beside each, as phase 2 did.
  - Add phase 3 to the status line.

- [ ] **Step 3: Run the documentation tests.**

  Run: `uv run pytest tests/test_readme_bounds.py tests/test_doc_pointers.py tests/test_instruction_bounds.py tests/test_package_layout.py -q`, then `uv run pytest -q`

  Expected: PASS.

- [ ] **Step 4: Commit.**

  ```bash
  git add README.md CLAUDE.md docs
  git commit -m "docs: the parent review: what starts it, its verdicts, its limits and the way back"
  ```

- [ ] **Step 5: Put the deploy notes in the PR body.**
  - There is no migration and no `labels ensure`. Run `python3 tools/upgrade/upgrade.py` from
    the hub checkout.
  - List every open split parent filed before this release. It is watched only after one
    `triage-todo` from a maintainer. A parent whose steps have all closed is reviewed on that
    tick.
  - Give the two measured GraphQL costs (Task 1, Step 5), and the one point a tick the watch
    adds.
