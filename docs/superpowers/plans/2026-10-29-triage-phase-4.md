# Triage Phase 4 (Auto-Triage) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** With `triage.auto` on, the worker labels each new issue from a maintainer
`issuebot/triage-todo` by itself. It never labels the backlog, never labels an issue twice, and
never labels a stranger's issue. A label with no person behind it is approved by authorship:
the author is a maintainer and nobody else has edited the text.

**Architecture:**
- **The watermark.** The worker keeps one record per repository: whether auto-triage was on
  when it last ran, and a watermark. Each change is published as a repository-level event,
  `auto_triage_state`, and the worker reads the latest one back at startup, as it reads phase
  3's parent records. So there is no migration.
  - Switching auto-triage on puts the watermark at the present.
  - Each scan decides, oldest first, every open issue opened after the watermark, labelling it
    or passing it over, and the watermark moves past it.
  - An issue behind the watermark is never looked at again. That is the "at most once" record.
- **The scan.** It is one bounded GraphQL read of the open issues opened since the watermark,
  newest first. It runs at most every two minutes, and only on a tick that could dispatch.
- **The approval.** Triage and the parent review try section 3's first rule (a person's
  `triage-todo`) first. When that refuses and the latest `triage-todo` is issuebot's own,
  authorship decides. The work run's approval does not change.

**Tech Stack:** Python 3.14, the `gh` CLI (GraphQL and REST), PostgreSQL through psycopg, and
pytest with pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-10-25-triage-design.md`. Phase 4 is section 2 and
section 3's authorship rules, plus their share of sections 1 (the move from no label to
`triage-todo`), 7 (Slack) and 9 (polling cost). Read them before starting: this plan argues
from the spec.

## Rulings on what the spec leaves to the builder

These come from the spec's "Left to the builder" list and from gaps found while planning. Task
6 records them in the spec.

1. **The record is an event, not a table.** `AutoTriageState` (kind `auto_triage_state`) is a
   repository-level `Event` with no issue number, carrying `auto` and `watermark`. Every change
   publishes the whole record. The store keeps every event already: `events.issue_number` is
   nullable, and `db/sink.py` writes `None` for a non-`IssueEvent`. The worker seeds itself
   from the latest record at startup, as it seeds parent records (`cli._initial_parents`).
   Without a store, or when the seed read fails, a restart counts as "never having run": the
   watermark moves to the present. Issues opened while the worker was stopped are then not
   auto-triaged, and the backlog never is.
2. **The watermark moves as issues are decided, and that is the "at most once" record.** After
   a scan, the watermark sits at the `created_at` of the newest issue it decided. An issue
   behind the watermark is never auto-labelled, whatever later happens to its labels. So a human
   or an escape removing `triage-todo` is final (section 2). Cost if wrong: if a watermark event
   is lost because the worker stops before the store writes it, the few issues behind it are
   decided again. One whose label a human removed in that window would be labelled again.
3. **The setting is settled every tick, not only at start.** `WORKFLOW.md` reloads live.
   Turning `triage.auto` on by a reload moves the watermark the way a start with it on does,
   and turning it off is recorded. `ISSUEBOT_AUTO_TRIAGE` reaches the worker through the env
   file, which compose reads only when it creates the container.
4. **One bounded read.** `fetch_issues_opened_since(since)` reads
   `repository.issues(states: [OPEN], orderBy: {field: CREATED_AT, direction: DESC},
   filterBy: {since: $since})` at 33 a page.
   - `filterBy.since` is GitHub's *updated*-since, a superset of opened-since that keeps old
     issues out of the pages.
   - Reading stops at the first page that holds an issue opened at or before `since`.
   - Past `MAX_ISSUE_PAGES` it fails with `PageCeilingError`, as the label reads do.
5. **The scan runs at most every `SCAN_INTERVAL_S` (120 s), and only on a tick that could
   dispatch**: a free slot, not stopping, no hold. That is at most 30 GraphQL points an hour per
   deployment, and a new issue is labelled within about three minutes. Cost if wrong: on a
   worker whose slots stay busy, a new issue is labelled only when a slot frees, which is also
   the earliest it could be triaged.
6. **A settle window of `SETTLE` (60 s).** An issue opened less than a minute before the scan
   waits for the next one. Otherwise an issue that GitHub has not yet listed could be overtaken
   by a newer one, with the watermark moving past both. Triage files its own steps already
   linked (`create_sub_issue` is one write), so they are never seen without a parent.
7. **An issuebot label is any of `labels.as_tuple()` and `labels.markers()`**: the five states,
   `no-fault`, both triage labels and `operator-step`. Section 2 names the state and triage
   labels, and section 9 says "no issuebot label".
8. **The approval rule is chosen by GitHub's record, and rule 1 goes first.**
   `approval.assess_triage` runs `assess` (rule 1). If rule 1 approves, that stands. If it
   refuses and the latest `triage-todo` label event is issuebot's own account's, authorship
   decides.
   - On a dedicated account, issuebot's own label never approves under rule 1, so an
     auto-labelled issue is always judged by authorship.
   - On an operator's own admin token (`own_labels_approve`), rule 1 approves issuebot's own
     label as it does today. The worker cannot tell the operator's hand from auto-triage
     there, and refusing would refuse the operator.
   - The parent review uses the same check. An auto-triaged parent, whose only `triage-todo`
     was issuebot's, is re-checked by authorship (section 3's last paragraph).
9. **What authorship checks:**
   - the author is named (not a deleted account);
   - the author's association is `OWNER`, `MEMBER` or `COLLABORATOR`, read from the issue as
     the dispatching tick fetched it (section 3's "re-read at dispatch");
   - every title and body edit since the issue was opened is the author's.

   It returns `Approved(approver=<author>, at=<the label's time>)`.
10. **Authorship never reaches a work run.** Only the triage and parent-review call sites ask
    for it (`authorship=True`), and the work run's `todo` approval does not change (section 3,
    third bullet).
11. **The approval cache key gains the author's association.** Losing write access does not
    move `updated_at`, so without it a cached `Approved` would outlive the association it rests
    on.
12. **A refused auto-labelled issue is handed back as today**: titled `unapproved edit`, with
    `triage-todo` off and `Blocked` published. It is behind the watermark, so auto-triage never
    labels it again, and a human's `triage-todo` approves it under rule 1.
13. **The auto label is a move on the triage track.** It publishes
    `StateChanged(from_label=None, to_label=<triage-todo>, actor="issuebot", note="auto-triage")`.
    The store keeps it as history only (`_on_the_triage_track`), and Slack's default
    `state_changed` posts one line for it.
14. **Section 1's "`todo` added while triage runs" is phase 1's, and works.** The reconcile stops
    a triage run on any state label, the verdict is discarded, and the claim strips
    `triage-todo` (`claim_clears`). Task 6 records it in the spec as resolved.
15. **A failed label write stops the scan without skipping the issue.** The watermark moves
    only past issues decided before the failure whose `created_at` is strictly older than the
    failed issue's. The failed issue is decided again on the next scan. Any issue sharing its
    second that was already labelled now carries `triage-todo`, so it is passed over rather
    than labelled twice.
16. **`run-once --triage` uses the same approval**, authorship included, so it cannot run what
    the worker would refuse.
17. **`validate` stays at 22 checks.** The `triage` line ends `, auto-triage on` or
    `, auto-triage off`.
18. **`Event.to_dict` writes every `datetime` field as ISO 8601.** Today only `at` is converted,
    and the watermark is the first other `datetime` an event carries. The store would refuse
    to serialise it otherwise.

## Global Constraints

- New setting, exactly: `triage.auto: bool = False`. When the key is absent (or `null`), it
  falls back to the environment variable `ISSUEBOT_AUTO_TRIAGE`, as `agent.run_as` falls back to
  `ISSUEBOT_AGENT_USER` (`config/resolve.py`). An explicit `auto: false` wins over the variable.
  The value is parsed as pydantic parses a `bool` (`true`, `false`, `1`, `0`, `yes`, `no`,
  `on`, `off`).
- New literals, exactly:
  - the event kind `auto_triage_state`, with fields `auto: bool` and `watermark: datetime`;
  - the auto label's `StateChanged.note`, `"auto-triage"`.
- New constants in `src/issuebot/orchestrator/auto_triage.py`, exactly:
  - `SCAN_INTERVAL_S = 120.0`
  - `SETTLE = timedelta(seconds=60)`
- The new adapter method, exactly: `fetch_issues_opened_since(since: datetime) -> list[Issue]`.
- `MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")` moves from
  `agent/prompt.py` to `issuebot.github.models` and is exported from `issuebot.github`.
  `agent/prompt.py` imports it from there.
- Reason strings, exactly:
  - `AUTHOR_DELETED = "opened by an account GitHub has deleted"`
  - `NOT_A_MAINTAINER = "opened by {author}, whose association is {association}, not a maintainer's"`
    (`association` is `unknown` when GitHub reports none)
  - an edit: `"{what} edited at {stamp} by {editor}, not by {author}, who opened it"`
    (`stamp` is `utc_stamp(edit.at)`; `editor` is `DELETED_ACCOUNT` when `None`)
  - the scan's other skip reasons: `"closed"`, `"carries an issuebot label"` and
    `"has a parent issue"`
- Log events:
  - `auto_triage_on` (info, with `watermark`)
  - `auto_triage_off` (info)
  - `auto_triage_labelled` (info, with the issue)
  - `auto_triage_passed_over` (info, with the issue and `reason`)
  - `auto_triage_scan_failed` (warning)
  - `auto_triage_label_failed` (warning)
  - `auto_triage_seeded` (info)
  - `auto_triage_seed_failed` (warning)
- No migration, no new label, no new table. A scan failure does not feed the GitHub hold, just
  as `triage_fetch_failed` does not.
- `jleavers/issuebot` is public. Never name a private repository anywhere in the tree, a commit
  message or a fixture.
- A global hook rejects any Bash command whose text contains the dot-env filename, including
  `.env.example`, `git add` of it, and attribute names such as `self` followed by `.environ`.
  Stage that file with `git add -u`, never by name. Edit such files
  with the Edit tool, and search them with the Read tool.
- `docs/package-layout.md` is about 9.5 KB under its 160 KiB budget
  (`tests/test_instruction_bounds.py`). Keep this plan's additions there under 1.5 KB. In
  `CLAUDE.md`, change only the label table's `issuebot/triage-todo` row.
- Never push to `main`, and never run `git reset --hard`, `git clean -fd` or `rm -rf`. Work in
  the worktree `.claude/worktrees/triage-phase-4-auto` on branch `triage/phase-4-auto-triage`.
- The live hub runs from the main checkout's compose project. Never run `docker compose down`,
  and never run `up`, `restart` or `rm` on any service but `test-db`. Never pass
  `ISSUEBOT_DB_PORT` inline. Task 4's store test needs `test-db` (`CLAUDE.md`, "Commands").
- Every commit message ends with the committing model's own `Co-Authored-By:` line, as its
  harness gives it, then
  `Claude-Session: https://claude.ai/code/session_019agwxcWUZKkqVuL73ZfE76`.
- Run the suite with `uv run pytest`, then `uv run ruff check . && uv run ruff format --check .`.
- Follow-up issues #354–#360 touch the same files (the triage pass, the parent reads, the
  owed notes). Do not fix them here. If one lands on `main` while this is built, rebase onto
  `origin/main` before the next task.
- On Python 3.14, ruff-format drops the parentheses from a multi-name `except` clause that has
  no `as`. Write `except (A, B) as exc:` when you need the name.
- Line numbers below are hints from `main` at `0269907`. Find the code by name.

## Review Focus

1. **Switching auto-triage on with a backlog, a restart with it on, and a spell with it off.**
   - Nothing already open is labelled.
   - An issue opened while the worker was stopped is labelled after a restart.
   - An issue opened while the setting was off is never labelled.

   Pinned in Task 5: `test_switching_auto_triage_on_labels_nothing_already_open`,
   `test_issues_opened_while_the_worker_was_stopped_are_labelled_after_restart` and
   `test_turning_auto_triage_off_and_on_skips_issues_opened_while_off`.
2. **A stranger's issue, and a maintainer's issue someone else has edited.** The first is
   never labelled. The second is handed back before any session reads it. Pinned in Task 5
   (`test_a_strangers_issue_is_never_auto_triaged`) and Task 3
   (`test_an_auto_labelled_issue_edited_by_another_account_is_handed_back_without_a_session`).
3. **A human removing the auto label, or an escape removing it.** It is never put back. Pinned
   in Task 5: `test_a_removed_auto_label_is_not_put_back`.
4. **Authorship reaching a work run.** An issue issuebot opened, with `todo` applied by
   issuebot on a dedicated account, is still refused. Pinned in Task 3:
   `test_authorship_never_approves_a_work_run`.
5. **GitHub refusing a label write mid-scan, and an issue opened seconds before a scan.**
   Neither is skipped, and nothing is labelled twice. Pinned in Task 5:
   `test_a_failed_label_write_leaves_the_issue_for_the_next_scan` and
   `test_an_issue_opened_in_the_last_minute_waits_for_the_next_scan`.

---

### Task 1: The GitHub read

**Files:**
- Modify: `src/issuebot/github/ghcli.py`:
  - new `OPENED_SINCE_QUERY` beside `_issues_query`, line 113
  - new `GhCliAdapter.fetch_issues_opened_since` beside `fetch_issues_with_label`, line 344
- Modify: `src/issuebot/github/adapter.py` (the protocol)
- Modify: `src/issuebot/github/fake.py`:
  - `fetch_issues_opened_since`
  - `add_issue(..., created_at: datetime | None = None)`, line 451
- Modify: `src/issuebot/github/models.py` (`MAINTAINER_ASSOCIATIONS`),
  `src/issuebot/github/__init__.py` (exports), `src/issuebot/agent/prompt.py:350` (import it
  instead of defining it)
- Test: `tests/test_github_ghcli.py`, `tests/test_github_fake.py`

**Interfaces:**
- Produces:
  - `GitHubAdapter.fetch_issues_opened_since(since: datetime) -> list[Issue]`: open issues
    whose `created_at` is strictly after `since`, oldest first (by `(created_at, number)`).
    Normalised exactly as `fetch_issues_with_label` normalises them, with the same
    `own_login()`.
  - `OPENED_SINCE_QUERY`, with variables `$owner`, `$name`, `$since: DateTime!` and `$cursor`:
    `issues(states: [OPEN], first: ISSUE_PAGE_SIZE, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}, filterBy: {since: $since})`
    over `...IssueFields`, with `pageInfo { hasNextPage endCursor }`.
  - Paging:
    - Ask for the next page only while every issue on this page was opened after `since` and
      `hasNextPage` is true.
    - Past `MAX_ISSUE_PAGES` pages, raise `PageCeilingError`, worded as `_issues_with_label`
      words it ("more than N issues were opened since ...").
  - `FakeGitHub.fetch_issues_opened_since(since)` goes through `_enter` with the name
    `"fetch_issues_opened_since"`. `add_issue(..., created_at=...)` sets `created_at` and
    `updated_at` when given, and otherwise uses the clock as today.
  - `issuebot.github.MAINTAINER_ASSOCIATIONS`.

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_github_ghcli.py`, with the module's recording runner and issue-node helper:
  - `test_fetch_issues_opened_since_reads_newest_first_and_stops_at_the_watermark`:
    - Setup: one page with three nodes, opened at 12:05, 12:03 and 11:59, and
      `hasNextPage: true`. `since` is 12:00.
    - Exactly one GraphQL call. Its query names `states: [OPEN]`,
      `orderBy: {field: CREATED_AT, direction: DESC}` and `filterBy: {since: $since}`, and its
      `since` variable is the ISO 8601 text of 12:00.
    - The result is the 12:03 and 12:05 issues, in that order.
  - `test_fetch_issues_opened_since_pages_until_it_passes_the_watermark`: page one holds only
    issues opened after `since`, with `hasNextPage: true`, and page two holds one at or before
    it. That makes two calls, the second carrying page one's `endCursor`.
  - `test_fetch_issues_opened_since_past_the_ceiling_fails`: every page holds only newer
    issues, with `hasNextPage: true`. It raises `PageCeilingError` after `MAX_ISSUE_PAGES`
    calls.

  In `tests/test_github_fake.py`:
  - `test_fake_fetch_issues_opened_since`:
    - Setup: three issues added with `created_at` 11:00, 12:30 and 13:00. The 12:30 one is
      then closed.
    - With `since` at 12:00, the result is the 13:00 issue alone.
    - The call is recorded as `("fetch_issues_opened_since", (since,))`.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_github_ghcli.py tests/test_github_fake.py -q`

  Expected: FAIL. `fetch_issues_opened_since` does not exist.

- [ ] **Step 3: Implement the query, the adapter method, the fake method and `add_issue`'s
  `created_at`, and move `MAINTAINER_ASSOCIATIONS`.**

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS. The prompt tests still pass, which shows the constant moved without changing.

- [ ] **Step 5: Measure the cost.** This is read-only, against this repository.

  Run `gh api graphql` once with `OPENED_SINCE_QUERY`, `since` a week ago, and
  `rateLimit { cost }` added at the top level.

  Expected: 1 point. Put the number in the report.

- [ ] **Step 6: Commit.**

  `git commit -m "github: the open issues opened since a watermark, newest first, in one bounded read"`

---

### Task 2: The setting

**Files:**
- Modify: `src/issuebot/config/settings.py`: `TriageSettings`, line 275
- Modify: `src/issuebot/config/resolve.py`: constants beside `AGENT_RUN_AS_FIELD`, line 25,
  and the fallback in `resolve_config` beside the `run_as` block, line 164
- Modify: `src/issuebot/cli.py`: `_triage_check`, line 1607
- Modify: `.env.example`, with the Edit tool. Add a commented `# ISSUEBOT_AUTO_TRIAGE=true`
  after the `ISSUEBOT_AGENT_USER` block. Its comment says three things: the key in
  `WORKFLOW.md` wins; leave it unset to keep auto-triage off; changing it needs
  `docker compose up -d worker`.
- Test: `tests/test_resolve.py`, `tests/test_settings.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `TriageSettings.auto: bool = False`.
  - `resolve.TRIAGE_AUTO_FIELD = ("triage", "auto")` and
    `resolve.TRIAGE_AUTO_FALLBACK = "ISSUEBOT_AUTO_TRIAGE"`, resolved through
    `resolve_env_value` as `run_as` is. The env text is left for pydantic to parse.
  - `validate`'s `triage` line ends with `, auto-triage on` or `, auto-triage off`.

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_resolve.py`:
  - `test_auto_triage_falls_back_to_the_environment_variable`: no `triage.auto` key, with
    `ISSUEBOT_AUTO_TRIAGE=true` in `environ`, loads as `config.triage.auto is True`.
  - `test_an_explicit_auto_triage_wins_over_the_environment`: `triage: {auto: false}` with
    `ISSUEBOT_AUTO_TRIAGE=true` gives `False`.
  - `test_auto_triage_is_off_by_default`: neither set gives `False`.
  - `test_an_empty_auto_triage_variable_counts_as_unset`: `ISSUEBOT_AUTO_TRIAGE=""` gives
    `False`.

  In `tests/test_settings.py`:
  - `test_triage_auto_parses_the_usual_spellings`, parametrised over `"true"`, `"1"`, `"yes"`
    and `"on"` (`True`), and `"false"`, `"0"`, `"no"` and `"off"` (`False`).
  - `test_an_unreadable_auto_triage_value_names_the_field`: `"maybe"` raises the loader's
    error, naming `triage.auto`.

  In `tests/test_cli.py`:
  - `test_validate_reports_auto_triage`: the `triage` line ends `, auto-triage off` by
    default, and `, auto-triage on` with `triage: {auto: true}`.
  - Extend the existing exact-text triage tests (`test_validate_reports_triage` and its
    neighbours around line 5945) to the new ending.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_resolve.py tests/test_settings.py tests/test_cli.py -q -k "auto_triage or triage"`

  Expected: FAIL.

- [ ] **Step 3: Implement the field, the fallback, the `validate` ending and the `.env.example`
  line.**

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS. `tests/test_compose_credentials.py` still passes over `.env.example`.

- [ ] **Step 5: Commit.**

  `git commit -m "config: triage.auto, falling back to ISSUEBOT_AUTO_TRIAGE; validate says which"`

---

### Task 3: Authorship approval

**Files:**
- Modify: `src/issuebot/orchestrator/approval.py`:
  - new `AUTHOR_DELETED` and `NOT_A_MAINTAINER`
  - new `assess_authorship` and `assess_triage`
- Modify: `src/issuebot/orchestrator/actions.py`: new `assess_triage_issue` beside
  `assess_issue`, line 299
- Modify: `src/issuebot/orchestrator/orchestrator.py`:
  - `_assess_approval`, line 1597
  - its triage call site, line 2036, in `_dispatch_triage_one`
  - its parent-review call site, line 2416, in `_dispatch_parent_review_one`
- Modify: `src/issuebot/cli.py`: `_approved_for_run_once`, line 2541
- Test: `tests/test_orchestrator_approval.py`, `tests/test_orchestrator_actions.py`,
  `tests/test_orchestrator.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `issuebot.github.MAINTAINER_ASSOCIATIONS` (Task 1).
- Produces:
  - `approval.assess_authorship(evidence: ApprovalEvidence, *, author: str | None, author_association: str | None, labelled: LabelApplied) -> Approved | Unapproved`.
    It applies ruling 9's three checks in that order, with the reason strings from Global
    Constraints:
    - A refused association gives `Unapproved(approval=labelled, edit=None)`.
    - A refused edit gives `Unapproved(approval=labelled, edit=<that edit>)`.
    - Otherwise `Approved(approver=author, at=labelled.at)`.
    - Login comparisons are case-insensitive.
  - `approval.assess_triage(evidence: ApprovalEvidence, *, triage_label: str, own_login: str, own_labels_approve: bool, author: str | None, author_association: str | None) -> Approved | Unapproved`:
    ruling 8. "The latest `triage-todo`" is the label event with the greatest `at` among those
    naming `triage_label` (case-insensitive) with a non-`None` actor.
  - `actions.assess_triage_issue(adapter: GitHubAdapter, issue: Issue, *, triage_label: str, own_labels_approve: bool | None = None) -> Approved | Unapproved`:
    `assess_issue`'s reads, with the author and association taken from `issue`.
  - `Orchestrator._assess_approval(self, issue: Issue, *, label: str | None = None, authorship: bool = False)`.
    With `authorship=True` it calls `actions.assess_triage_issue`. The cache key becomes
    `(label, authorship, issue.updated_at, issue.title, issue.body, issue.author_association)`.
    Both triage call sites pass `authorship=True`, and the work call site does not change.
  - `cli._approved_for_run_once` gains `authorship: bool = False`, and `run-once --triage`
    passes `True`.

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_orchestrator_approval.py`, building `ApprovalEvidence` by hand as the module
  does. Here `own` is `"issuebot-bot"`, `t1` is later than `t0`, and every test except the last
  passes `own_labels_approve=False`.
  - `test_an_auto_label_is_approved_by_authorship`:
    - The label events are `(own, triage-todo, t1)`.
    - The edits are one body edit by `"maintainer"` at `t0`.
    - The author is `"maintainer"`, with association `"MEMBER"`.
    - `assess_triage` gives `Approved(approver="maintainer", at=t1)`.
  - `test_authorship_refuses_an_edit_by_anyone_but_the_author`, parametrised over editor
    `"someone-else"` and `None`. The reason is exactly
    `f"body edited at {utc_stamp(t0)} by {editor_text}, not by maintainer, who opened it"`, and
    `verdict.edit` is that edit.
  - `test_authorship_refuses_an_author_who_is_not_a_maintainer`, parametrised over
    `"CONTRIBUTOR"`, `"NONE"` and `None`. The reason is
    `NOT_A_MAINTAINER.format(author="maintainer", association=...)`, with `None` shown as
    `unknown`.
  - `test_authorship_refuses_a_deleted_author`: `author=None` gives `AUTHOR_DELETED`.
  - `test_a_persons_label_is_judged_by_rule_one`:
    - The label events are `(own, t0)` then `("maintainer", t1)`.
    - There is an edit by `"other"` after `t1`.
    - The verdict is rule 1's refusal, naming `maintainer`, although the author is a
      maintainer.
  - `test_an_issue_issuebot_opened_passes_authorship_on_a_write_account`:
    - The author is `own`, with association `"COLLABORATOR"`.
    - The only label is `own`'s, and the only edit is `own`'s.
    - The verdict is `Approved(approver=own)`.
  - `test_on_an_admin_token_issuebot_label_approves_under_rule_one`:
    - `own_labels_approve=True`.
    - The only label is `own`'s, and the author is a stranger with association `"NONE"`.
    - The verdict is `Approved(approver=own)`.

  In `tests/test_orchestrator_actions.py`:
  - `test_assess_triage_issue_reads_the_evidence_and_the_issues_author`: a fake issue
    authored by `"maintainer"` (`MEMBER`), labelled `triage-todo` by the worker's own
    `edit_labels`, is `Approved`.

  In `tests/test_orchestrator.py`, with the `Harness` (dedicated account, `admin=False`):
  - `test_an_auto_labelled_issue_is_triaged_under_authorship`:
    - Setup: an issue authored by `"maintainer"` (`MEMBER`), labelled `triage-todo` through
      `h.github.edit_labels`, so the label event is the worker's own.
    - After one tick, one triage run has started.
  - `test_an_auto_labelled_issue_edited_by_another_account_is_handed_back_without_a_session`:
    - Setup: the same issue, then `human_edit_body(..., editor="someone-else")`.
    - After one tick, no triage run has started.
    - The issue's comment is the `### Issuebot unapproved edit` note, whose reason contains
      `"not by maintainer, who opened it"`.
    - `triage-todo` is gone and `Blocked` is published.
  - `test_authorship_never_approves_a_work_run`:
    - Setup: an issue authored by the fake's own login (`COLLABORATOR`), with `issuebot/todo`
      added through `h.github.edit_labels`.
    - After one tick, no work run has started, and the issue is handed back by
      `unapproved_escape` with `NO_OTHER_APPROVER`'s reason.
  - `test_an_auto_triaged_parent_is_reviewed_under_authorship`:
    - Setup: a parent authored by `"maintainer"` (`MEMBER`), whose only `triage-todo` event
      is the worker's own. It now carries `triage-complete`, with two closed steps and a
      watched record in `initial_parents`.
    - After one tick, a `parent_review` run has started.
  - `test_the_approval_check_runs_again_when_the_association_changes`:
    - Setup: as `test_an_auto_labelled_issue_is_triaged_under_authorship`, before any tick.
    - Call `h.orchestrator._assess_approval(issue, label="issuebot/triage-todo", authorship=True)`:
      `Approved`.
    - Call it again with `dataclasses.replace(issue, author_association="NONE")`, which leaves
      `updated_at` alone: `Unapproved` with `NOT_A_MAINTAINER`'s reason.
    - The fake recorded two `approval_evidence` calls.

  In `tests/test_cli.py`:
  - `test_run_once_triage_approves_an_auto_labelled_issue_by_authorship`: the same
    `MEMBER`-authored issue, worker-labelled, runs under `run-once --triage`.
  - `test_run_once_triage_refuses_an_auto_labelled_issue_someone_else_edited`: it prints
    `[FAIL] approval: ...` with the edit reason.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_orchestrator_approval.py tests/test_orchestrator_actions.py tests/test_orchestrator.py tests/test_cli.py -q -k "authorship or auto_labelled or association or auto_triaged"`

  Expected: FAIL.

- [ ] **Step 3: Implement the two pure functions, the action, the orchestrator flag and cache
  key, and `run-once`'s flag.**

  Extend the module docstring of `approval.py` by one paragraph covering four points:
  - section 3's authorship rule;
  - why rule 1 goes first (the admin token);
  - why the latest label decides which rule applies;
  - that `assess_triage` is never called for `todo`.

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS.

- [ ] **Step 5: Commit.**

  `git commit -m "triage: approve a label with no person behind it by authorship, never for a work run"`

---

### Task 4: The record and its seed

**Files:**
- Modify: `src/issuebot/events/types.py`:
  - `Event.to_dict`, line 19
  - new `AutoTriageState` beside `ParentReviewState`, line 120
  - `EVENT_TYPES`
- Modify: `src/issuebot/events/__init__.py` (export)
- Create: `src/issuebot/orchestrator/auto_triage.py`, holding:
  - `AutoTriageRecord`
  - `settle`
  - `skip_reason`
  - the two constants
- Modify: `src/issuebot/db/queries.py`:
  - new `AUTO_TRIAGE_STATE`
  - new `AutoTriageRow`
  - new `auto_triage_state()` beside `parent_records()`
- Modify: `src/issuebot/db/__init__.py` (export), `tests/fakes/database.py`
  (`FakeRepoQueries.auto_triage_state`)
- Modify: `src/issuebot/cli.py`:
  - new `_initial_auto_triage` beside `_initial_parents`, line 2705
  - pass it in `_run_worker`
- Modify: `src/issuebot/orchestrator/orchestrator.py`: the constructor's
  `initial_auto_triage: AutoTriageRecord | None = None`, kept as `self._auto_triage_record`
- Test: `tests/test_events.py`, new `tests/test_orchestrator_auto_triage.py`,
  `tests/test_db_queries.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `approval.AUTHOR_DELETED` and `approval.NOT_A_MAINTAINER` (Task 3),
  `MAINTAINER_ASSOCIATIONS` (Task 1).
- Produces:
  - `AutoTriageState(Event)`:
    - fields `kind = "auto_triage_state"`, `auto: bool`, `watermark: datetime`;
    - not an `IssueEvent`;
    - in `EVENT_TYPES`.
  - `Event.to_dict()` converts any `datetime` field with `.isoformat()`.
  - In `auto_triage.py`:
    - `@dataclass(frozen=True, kw_only=True, slots=True) class AutoTriageRecord: auto: bool; watermark: datetime`
    - `def settle(record: AutoTriageRecord | None, *, auto: bool, now: datetime) -> AutoTriageRecord | None`:
      the new record when it changes, else `None`.
      - With `auto` on, no record, or a record that says off, it returns `(True, now)`.
      - With `auto` off and a record that says on, it returns `(False, <same watermark>)`.
      - Anything else is unchanged.
    - `def skip_reason(issue: Issue, labels: GitHubLabels) -> str | None`: why `issue` is not
      auto-triaged, or `None`. The checks run in this order, each with its exact string:
      - `"closed"`;
      - `"carries an issuebot label"` (ruling 7);
      - `"has a parent issue"`;
      - `AUTHOR_DELETED`;
      - `NOT_A_MAINTAINER`.
    - `SCAN_INTERVAL_S` and `SETTLE`.
  - `queries.AutoTriageRow(auto: bool, watermark: datetime)`, and
    `RepoQueries.auto_triage_state() -> AutoTriageRow | None`. It returns the latest
    `auto_triage_state` event for the repository by `(at, id)`, with no time window, as
    `PARENT_RECORDS` reads.
  - `cli._initial_auto_triage(database: Database | None, repo: str) -> AutoTriageRecord | None`:
    - `None` without a store;
    - `None` with a `auto_triage_seed_failed` warning on a `DatabaseError`;
    - otherwise the record, logged as `auto_triage_seeded`.

- [ ] **Step 1: Write the failing tests.**

  In `tests/test_events.py`:
  - `test_auto_triage_state_is_a_repository_event`:
    - It is not an `IssueEvent`, and its kind is in `EVENT_KINDS`.
    - `to_dict()` gives `{"kind": "auto_triage_state", "at": ..., "auto": True, "watermark": "<iso>"}`
      and passes `json.dumps`.

  In `tests/test_orchestrator_auto_triage.py`:
  - `test_settle_turns_on_with_the_watermark_at_now`: `settle(None, auto=True, now=T)` gives
    `(True, T)`.
  - `test_settle_keeps_the_watermark_across_a_restart_with_it_on`: `(True, W)` with
    `auto=True` gives `None`.
  - `test_settle_records_turning_off`: `(True, W)` with `auto=False` gives `(False, W)`.
  - `test_settle_after_a_spell_off_moves_the_watermark_to_now`: `(False, W)` with `auto=True`
    gives `(True, T)`.
  - `test_settle_with_it_off_and_no_record_records_nothing`: `settle(None, auto=False, ...)`
    gives `None`.
  - `test_skip_reason`, parametrised over:
    - a closed issue;
    - `issuebot/todo`;
    - `issuebot/triage-complete`;
    - `issuebot/operator-step`;
    - `parent=7`;
    - `author=None`;
    - association `"NONE"`;
    - a `MEMBER`'s plain issue labelled `enhancement`, which gives `None`.

    Each expects its exact string.

  In `tests/test_db_queries.py`, beside `test_parent_records_are_each_parents_latest_state`
  (line 906), with `test-db`:
  - `test_auto_triage_state_is_the_latest_record`:
    - Three `AutoTriageState` events go through the store sink: two for this repository and
      one for another.
    - The later of this repository's two comes back.
    - A repository with none gives `None`.

  In `tests/test_cli.py`:
  - `test_the_worker_seeds_auto_triage_from_the_store`: the fake database returns a row, and
    the orchestrator is built with that record.
  - `test_a_failed_auto_triage_seed_starts_without_one`: the fake's `_check` raises, the
    worker starts, and `auto_triage_seed_failed` is logged.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_events.py tests/test_orchestrator_auto_triage.py tests/test_cli.py -q -k "auto_triage or skip_reason or settle"`

  Expected: FAIL.

- [ ] **Step 3: Implement the event, the record, `settle`, `skip_reason`, the query, the fake,
  the seed and the constructor parameter.**

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite, with the store.

  Run, from the main checkout:
  `docker compose --profile test up -d --wait test-db`

  Then, here:
  `DATABASE_URL=postgresql://issuebot@<port from docker compose port test-db 5432>/issuebot uv run pytest -q`

  Expected: PASS. Then run `docker compose rm -sf test-db` from the main checkout.

- [ ] **Step 5: Commit.**

  `git commit -m "triage: the auto-triage record, published as an event and seeded from the store"`

---

### Task 5: The worker's scan

**Files:**
- Modify: `src/issuebot/orchestrator/orchestrator.py`:
  - `tick`, line 985: `_settle_auto_triage()` after `_reload_workflow()`, and
    `await self._scan_for_auto_triage()` before `_dispatch_triage()` in the dispatch branch, line 1026
  - new `_settle_auto_triage` and `_scan_for_auto_triage`
  - new `self._auto_triage_scanned: float = -math.inf`
- Modify: `tests/test_orchestrator.py`: `Harness` gains `auto_triage: bool = False`, which writes
  `triage: {auto: true}` through `write_workflow`, and `initial_auto_triage`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes:
  - `fetch_issues_opened_since` (Task 1);
  - `triage.auto` (Task 2);
  - `AutoTriageState`, `AutoTriageRecord`, `settle`, `skip_reason`, `SCAN_INTERVAL_S` and
    `SETTLE` (Task 4).
- Produces:
  - `Orchestrator._settle_auto_triage(self) -> None`: synchronous.
    - It calls `settle(self._auto_triage_record, auto=self._workflow.config.triage.auto, now=self._now())`.
    - On a change, it stores the record, publishes `AutoTriageState`, and logs
      `auto_triage_on` or `auto_triage_off`.
  - `Orchestrator._scan_for_auto_triage(self) -> int`: the number of issues labelled. It reads nothing
    unless all of these hold:
    - the record says on;
    - `self._slots() > 0`;
    - the worker is not stopping;
    - `self._current_hold() is None`;
    - `self._clock() - self._auto_triage_scanned >= SCAN_INTERVAL_S`.

    It then:
    1. Sets `_auto_triage_scanned`, whether or not the read succeeds.
    2. Reads `fetch_issues_opened_since(record.watermark)`. A `GitHubError` is logged as
       `auto_triage_scan_failed`, and the pass returns 0.
    3. Decides, in order, each issue whose `created_at <= self._now() - SETTLE`:
       - An issue with a `skip_reason` is logged as `auto_triage_passed_over` and passed
         over.
       - Otherwise it is labelled with `edit_labels(n, add=[triage_todo])`, the
         `StateChanged` of ruling 13 is published, and `auto_triage_labelled` is logged.
       - A failed write is logged as `auto_triage_label_failed` and ends the pass (ruling 15).
    4. Publishes one `AutoTriageState` if the watermark moved.

- [ ] **Step 1: Write the failing tests.**

  Every test uses `Harness(tmp_path, auto_triage=True)` unless it says otherwise.
  - "Scan" means: advance `h.clock` by `SCAN_INTERVAL_S`, then `await h.orchestrator.tick()`.
  - Issues are added with `h.github.add_issue(..., author="maintainer", author_association="MEMBER", created_at=...)`.
  - `T` is the harness start.

  The tests:
  - `test_switching_auto_triage_on_labels_nothing_already_open`:
    - Setup: an issue opened at `T - 1h`.
    - The first tick publishes `AutoTriageState(auto=True, watermark=T)`.
    - After a scan, the issue has no `triage-todo`.
  - `test_a_new_maintainer_issue_is_labelled_triage_todo`:
    - Setup: after the first tick, an issue opened at `T + 10s`. Scan.
    - The issue carries `triage-todo`.
    - The recorder holds `StateChanged(from_label=None, to_label="issuebot/triage-todo", actor="issuebot", note="auto-triage")`
      and `AutoTriageState(auto=True, watermark=T + 10s)`.
  - `test_issues_opened_while_the_worker_was_stopped_are_labelled_after_restart`:
    - Setup: `initial_auto_triage=AutoTriageRecord(auto=True, watermark=T - 1h)`, and an
      issue opened at `T - 30m`.
    - The first tick labels it.
    - No `AutoTriageState(auto=True, watermark=T)` is published, because a restart with the
      setting on keeps the watermark.
  - `test_turning_auto_triage_off_and_on_skips_issues_opened_while_off`:
    - Rewrite the workflow without `auto` and tick: `AutoTriageState(auto=False, ...)`.
    - Add an issue.
    - Rewrite with `auto: true` and tick: `AutoTriageState(auto=True, watermark=<now>)`.
    - Scan: the issue opened while off is never labelled.
  - `test_auto_triage_off_reads_nothing`: with `Harness(tmp_path)`, two scans make no
    `fetch_issues_opened_since` call and publish no `AutoTriageState`.
  - `test_a_strangers_issue_is_never_auto_triaged`:
    - Setup: an issue with association `"NONE"`. Scan.
    - It has no label, and `auto_triage_passed_over` is logged with `NOT_A_MAINTAINER`'s
      reason.
    - The watermark has moved past it.
  - `test_sub_issues_and_labelled_issues_are_not_auto_triaged`:
    - Setup: one issue linked under a parent, and one opened with `issuebot/todo`. Scan.
    - Neither gains `triage-todo`.
  - `test_a_removed_auto_label_is_not_put_back`:
    - Setup: a labelled issue. Then `h.github.human_remove_label(n, "issuebot/triage-todo")`.
    - Two more scans: still no `triage-todo`, and `edit_labels` was called for it once.
  - `test_a_failed_label_write_leaves_the_issue_for_the_next_scan`:
    - Setup: issues A (`T + 10s`) and B (`T + 20s`), with
      `h.github.fail_next("response", on="edit_labels", after=1)`, so B's write fails. Scan.
    - A is labelled, B is not, and the published watermark is A's `created_at`.
    - Scan again: B is labelled, and A's `edit_labels` count is still 1.
  - `test_an_issue_opened_in_the_last_minute_waits_for_the_next_scan`:
    - Setup: an issue opened 30 s before the scan's `h.now()`.
    - That scan leaves it unlabelled. The next scan labels it.
  - `test_the_scan_runs_at_most_every_two_minutes_and_only_with_a_slot_free`:
    - Three ticks 30 s apart make one `fetch_issues_opened_since` call.
    - With every slot taken by running work, a scan makes none.
  - `test_a_failed_scan_is_logged_and_retried_next_interval`:
    - `fail_next("response", on="fetch_issues_opened_since")`: `auto_triage_scan_failed` is
      logged, and the
      dispatch hold is unchanged.
    - The next scan labels the issue.
  - `test_an_auto_labelled_issue_is_triaged`: after the scan that labels a `MEMBER`'s issue, a
    later tick starts its triage run.

- [ ] **Step 2: Run the tests and see them fail.**

  Run: `uv run pytest tests/test_orchestrator.py -q -k "auto_triage or auto_labelled or opened_while or strangers or removed_auto or last_minute or failed_label_write or failed_scan or two_minutes or not_auto_triaged"`

  Expected: FAIL.

- [ ] **Step 3: Implement `_settle_auto_triage`, `_scan_for_auto_triage` and the two calls in `tick`.**

  The comment on `_scan_for_auto_triage` cites spec sections 2 and 9 and rulings 2, 5, 6 and 15. It
  says why the scan needs no slot of its own yet is gated on one: a label then waits for the
  slot its triage needs anyway, and the read is the account's shared budget.

- [ ] **Step 4: Run the tests and see them pass.** Then run the whole suite.

  Run: `uv run pytest -q`

  Expected: PASS.

- [ ] **Step 5: Commit.**

  `git commit -m "orchestrator: label each new maintainer issue triage-todo once auto-triage is on"`

---

### Task 6: The docs and the spec's record

**Files:**
- Modify: `README.md`:
  - the "Triage is by hand" paragraph, line 63: by hand, or by issuebot once `triage.auto` is
    on;
  - the `issuebot/triage-todo` row of the label table, line 49;
  - the `triage.*` settings table, line 514: add `triage.auto`, naming `ISSUEBOT_AUTO_TRIAGE`;
  - the `validate` sample line, line 358.
- Modify: `docs/operations.md`: a new `#### Auto-triage` under `### Triage`, line 186, saying:
  - how to switch it on: the key, which reloads live, or the variable, which needs
    `docker compose up -d worker`;
  - what is labelled;
  - the watermark: switching on labels nothing already open, a restart catches what opened
    while stopped, a spell off is skipped, and without a store a restart is a fresh start;
  - that a human or an escape removing the label is final;
  - the cost: at most 30 points an hour, and the label lands within about three minutes;
  - that a refused issue is handed back as `unapproved edit`, and how to resume it.
- Modify: `docs/security-model.md`, under `## A triage session`, line 144: one paragraph on
  authorship approval, the exception for issuebot-authored issues, why rule 1 goes first on an
  admin token, and that the work run's approval does not change.
- Modify: `docs/package-layout.md`, under 1.5 KB in all:
  - the orchestrator entry: `auto_triage.py`, and authorship in `approval.py`;
  - the github entry: `fetch_issues_opened_since`;
  - the events entry: `AutoTriageState`, the first event with no issue;
  - the config entry: the `triage.auto` fallback.
- Modify: `CLAUDE.md:356`: the `issuebot/triage-todo` row becomes
  `human, or issuebot under opt-in auto-triage (`triage.auto`)`.
- Modify: `docs/superpowers/specs/2026-10-25-triage-design.md`:
  - the status line says phase 4 is built;
  - a "Decided while planning phase 4" list, one line per ruling 1–18;
  - in "Left to the builder", strike the watermark's half of the store item, and the item on a
    triage run in flight when `todo` is added (ruling 14).
- Test: `tests/test_doc_pointers.py`, `tests/test_instruction_bounds.py`,
  `tests/test_readme_bounds.py` (existing; they must pass)

- [ ] **Step 1: Write the docs.**

  Every claim must match the code as built. Read `_scan_for_auto_triage` and `assess_triage` before
  writing the operations and security paragraphs.

- [ ] **Step 2: Run the doc tests and the suite.**

  Run: `uv run pytest tests/test_doc_pointers.py tests/test_instruction_bounds.py tests/test_readme_bounds.py -q && uv run pytest -q`

  Expected: PASS.

- [ ] **Step 3: Write the deploy notes at the end of the task report**, for the PR body:
  - No migration and no `labels ensure`. Run `python3 tools/upgrade/upgrade.py` from the hub
    checkout.
  - Auto-triage stays off until a deployment opts in. Switch it on with `triage.auto: true`
    in that checkout's `WORKFLOW.md`, which reloads live. Or set `ISSUEBOT_AUTO_TRIAGE=true` in
    its env file and run `docker compose up -d worker`.
  - The measured cost from Task 1.

- [ ] **Step 4: Commit.**

  `git commit -m "docs: auto-triage: switching it on, the watermark, authorship approval and the way back"`
