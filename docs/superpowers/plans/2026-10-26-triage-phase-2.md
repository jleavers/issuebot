# Triage Phase 2 (Order Within a Split) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The operator can label a whole split `issuebot/todo` at once. The worker then starts
each step only when every sub-issue before it, in the parent's order, has closed.

**Architecture:** The poll's issue fragment gains `parent { number }`, so a candidate knows
whether it is a sub-issue. Once per tick, the worker reads every parent of a `todo` candidate
in a single GraphQL query (`parent_steps`): each parent's labels and its sub-issues in GitHub's
order. A pure rule (`earlier_open_step`) names the first open step before the candidate.
The admission gate (`admit`) refuses that candidate with a new refusal kind, `order`, before it
can cost a slot, an account or an approval read. A held issue gets no comment, no label change
and no `Blocked`. The worker logs `dispatch_held` once per change, and `run-once` refuses a held
issue with the same reason.

**Tech Stack:** Python 3.14, the `gh` CLI (GraphQL), and pytest with pytest-asyncio
(`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-10-25-triage-design.md`. Phase 2 is section 5's
"Order within a split" plus its share of section 9 (polling cost). Read both before starting:
this plan argues from the spec.

## Rulings on what the spec leaves to the builder

These come from the spec's "Left to the builder" list and from gaps found while planning. Task 5
records the first two in the spec.

1. **The order comes from the parent's sub-issue order alone.** No GitHub "blocked by"
   dependencies are filed. That leaves one source of truth, which the operator can change by
   dragging sub-issues on the parent. Dependencies would be a second order that can disagree
   with the first, and they need a write per step and a REST API the adapter does not use yet.
2. **A held issue's card does not change in this phase.** The board is phase 5's. The worker
   logs `dispatch_held` naming the step it waits for, once per change.
3. **Only the tick's candidate sweep applies the hold, and only to `todo`.** The retry timer does
   not read the order. A retry exists only for an issue that has already run, and the spec says
   a step that has started is not held.
4. **A failed parent read holds every affected candidate for that poll only.** It fails closed,
   because running a step early is the failure this phase exists to stop and waiting one poll
   costs 30 s. The failure is logged as `parent_steps_failed` and does not feed the GitHub hold.
5. **An operator step is never held.** If one is labelled `todo`, the existing operator-step
   refusal takes the label off at once instead of waiting for its turn.
6. **`run-once` refuses a held issue,** as it already refuses an operator step: an operator
   running `run-once` over a whole batch would otherwise recreate the race this phase removes.
   `--show-prompt` skips the check, as it skips the phase-1 sub-issue check.
7. **Two cases are not held:** a candidate that is missing from its parent's list (unlinked
   between the two reads), and a parent GitHub no longer returns.

## Global Constraints

- Refusal reasons, exactly:
  - held: `waits for #<earlier>, an earlier step of #<parent>`
  - unreadable: `the order of #<parent>'s sub-issues could not be read`
- New literals, exactly: `RefusalKind` gains `"order"`, `RetryKind` gains `"order"`, and the
  log events are `dispatch_held` (info) and `parent_steps_failed` (warning).
- `parent_steps` reads at most `PAGE_SIZE` (100) sub-issues per parent in one page, as
  `SUB_ISSUES_QUERY` does. A verdict files at most 20 per batch.
- No new setting, label, migration or store column.
- `jleavers/issuebot` is public. Never name a private repository anywhere in the tree, a commit
  message or a fixture.
- A global hook rejects any Bash command whose text contains the dot-env filename. Edit such
  files with the Edit tool.
- `docs/package-layout.md` is about 11 KB under its 160 KiB budget
  (`tests/test_instruction_bounds.py`). Keep this plan's additions there under 1.5 KB. Leave
  `CLAUDE.md` unchanged.
- Never push to `main`, and never run `git reset --hard`, `git clean -fd` or `rm -rf`. Work in
  the worktree `.claude/worktrees/triage-phase-2-order` on branch `triage/phase-2-order`.
- The live hub runs from the main checkout's compose project. Never run `docker compose down`.
  Never `up`, `restart` or `rm` any service but `test-db`. Never pass `ISSUEBOT_DB_PORT`
  inline. This phase needs no database tests.
- Every commit message ends with the committing model's own `Co-Authored-By:` line, as its
  harness gives it, then
  `Claude-Session: https://claude.ai/code/session_019agwxcWUZKkqVuL73ZfE76`.
- Run the suite with `uv run pytest`, then `uv run ruff check . && uv run ruff format --check .`.
- Several follow-up PRs are open while this is planned: #319 (`tick`, `apply_verdict`), #314
  (`workflow.py`) and the test-gap issue #311. Rebase onto `origin/main` before Task 1 and
  before the PR. #311 proposes the same `FakeGitHub.link_sub_issue(parent, child)` helper
  that Task 1 adds: if it is already on `main`, use it and skip that step.
- On Python 3.14, ruff-format drops the parentheses from a multi-name `except` clause that has
  no `as`. Write `except (A, B) as exc:` when you need the name.

## Review Focus

1. **The whole batch labelled `todo` at once, operator step included.** The issuebot steps
   run one at a time. The operator step's `todo` comes off at once with the operator-step
   note, without waiting its turn. Pinned in Task 3
   (`test_a_batch_labelled_at_once_runs_one_step_at_a_time`,
   `test_an_operator_step_labelled_todo_is_refused_at_once_not_held`).
2. **An earlier step whose pull request is open.** That step's issue is still open, so the
   next step waits, and is dispatched on the first poll after the issue closes. Pinned in
   Task 3 (`test_a_held_step_starts_on_the_poll_after_the_step_before_it_closes`).
3. **GitHub refusing the parent read.** Sub-issues are held for that poll. Candidates that are
   not sub-issues still dispatch. The next poll recovers. Pinned in Task 3
   (`test_a_failed_parent_read_holds_sub_issues_for_that_poll_only`).
4. **A stage sent back to `rework`, or resumed `in-progress`.** It is never held, even if an
   earlier sibling has been reopened. Pinned in Task 2 (`test_a_started_step_is_never_held`) and
   Task 3 (`test_a_reworked_step_is_not_held`).
5. **Sub-issues nobody triaged, and a reordered parent.** Children of a parent without
   `triage-complete` are never held. A parent whose sub-issues were dragged into a new order on
   GitHub is held by the new order. Pinned in Task 2
   (`test_a_parent_without_triage_complete_holds_nothing`, `test_the_order_is_githubs`).

---

### Task 1: The GitHub reads: an issue's parent, and a parent's steps

**Files:**
- Modify: `src/issuebot/github/models.py` (`Issue`, beside `SubIssue` at line 104)
- Modify: `src/issuebot/github/normalise.py` (`issue_from_node`, line 38)
- Modify: `src/issuebot/github/ghcli.py` (`ISSUE_FIELDS` line 91, `_sub_issue_from` line 1008,
  `sub_issues` line 425)
- Modify: `src/issuebot/github/adapter.py` (protocol, beside `sub_issues` at line 69)
- Modify: `src/issuebot/github/fake.py` (`_node` line 625, beside `sub_issues` line 263)
- Modify: `src/issuebot/github/__init__.py` (export `ParentSteps`)
- Test: `tests/test_github_normalise.py`, `tests/test_github_ghcli.py`,
  `tests/test_github_fake.py`

**Interfaces:**
- Produces:
  - `Issue.parent: int | None = None`, the parent issue's number.
  - `ParentSteps`, a frozen, kw-only, slotted dataclass in `models.py`:
    `number: int`, `labels: tuple[str, ...]` (lower-cased, as `Issue.labels`) and
    `sub_issues: tuple[SubIssue, ...]` (in GitHub's order).
  - `GitHubAdapter.parent_steps(numbers: Iterable[int]) -> dict[int, ParentSteps]`. It makes one
    read per `ID_BATCH_SIZE` (50) parents, which in practice is one. Numbers are deduplicated.
    An empty input returns `{}` without a call. A number GitHub returns no issue for is absent
    from the result.
  - In `normalise.py`: `sub_issue_from_node(node: Any) -> SubIssue` (moved from ghcli's
    `_sub_issue_from`) and `parent_steps_from_node(node: Mapping[str, Any]) -> ParentSteps`.
  - `FakeGitHub.link_sub_issue(parent: int, child: int) -> None`, a test helper.

- [ ] **Step 1: Write the failing tests**

In `tests/test_github_normalise.py`, using the module's `node()` helper and its usual
`issue_from_node(..., repo=, labels=, login=)` arguments:

```python
def test_parent_is_the_parent_issue_number() -> None:
    assert normalised(node(parent={"number": 7})).parent == 7
    assert normalised(node(parent=None)).parent is None
    assert normalised(node()).parent is None  # an older fixture without the field


@pytest.mark.parametrize("parent", [{"number": "7"}, {"number": True}, {}, 7])
def test_a_malformed_parent_is_a_response_error(parent: Any) -> None:
    with pytest.raises(GitHubError) as info:
        normalised(node(parent=parent))
    assert info.value.category == "response"


def test_parent_steps_keep_githubs_order_and_lowercase_labels() -> None:
    steps = parent_steps_from_node(
        {
            "number": 5,
            "labels": {"nodes": [{"name": "Issuebot/Triage-Complete"}]},
            "subIssues": {
                "nodes": [
                    {"number": 7, "title": "b", "url": "u7", "state": "OPEN"},
                    {"number": 6, "title": "a", "url": "u6", "state": "CLOSED"},
                ]
            },
        }
    )
    assert steps.number == 5
    assert steps.labels == ("issuebot/triage-complete",)
    assert [(s.number, s.state) for s in steps.sub_issues] == [(7, "open"), (6, "closed")]
```

(`normalised` is shorthand for whatever call the module already uses. Add a local helper if
it has none.)

In `tests/test_github_ghcli.py`:

```python
def test_the_issue_fragment_asks_for_the_parent_number() -> None:
    assert "parent { number }" in ISSUE_FIELDS


async def test_parent_steps_reads_every_parent_in_one_query() -> None:
    runner = StubRunner()
    parent = {
        "number": 5,
        "labels": {"nodes": [{"name": "issuebot/triage-complete"}]},
        "subIssues": {"nodes": [{"number": 7, "title": "b", "url": "u7", "state": "OPEN"}]},
    }
    # What GitHub sends for a number with no issue: a null alias and a NOT_FOUND error on it.
    missing = {"type": "NOT_FOUND", "path": ["repository", "p9"], "message": "no issue 9"}
    runner.on(
        has("p5: issue(number: 5)"),
        stdout=json.dumps(
            {"data": {"repository": {"p5": parent, "p9": None}}, "errors": [missing]}
        ),
    )
    steps = await make_adapter(runner).parent_steps([9, 5, 5])
    assert list(steps) == [5]
    assert [s.number for s in steps[5].sub_issues] == [7]
    assert len(runner.calls) == 1
    query = " ".join(runner.calls[0][0])
    assert "p9: issue(number: 9)" in query and query.count("p5: issue") == 1


async def test_parent_steps_of_nothing_makes_no_call() -> None:
    runner = StubRunner()
    assert await make_adapter(runner).parent_steps([]) == {}
    assert runner.calls == []


async def test_a_malformed_parent_steps_record_is_a_response_error() -> None:
    runner = StubRunner()
    runner.on(
        has("p5: issue(number: 5)"),
        stdout=json.dumps({"data": {"repository": {"p5": {"number": 5, "subIssues": 3}}}}),
    )
    with pytest.raises(GitHubError) as info:
        await make_adapter(runner).parent_steps([5])
    assert info.value.category == "response"
```

In `tests/test_github_fake.py`:

```python
async def test_link_sub_issue_links_both_ways(fake: FakeGitHub) -> None:
    parent = fake.add_issue("Big thing")
    child = fake.add_issue("Step")
    fake.link_sub_issue(parent.number, child.number)
    assert fake.issue(child.number).parent == parent.number
    assert [s.number for s in await fake.sub_issues(parent.number)] == [child.number]
    with pytest.raises(ValueError):
        fake.link_sub_issue(parent.number, child.number)  # already has a parent


async def test_fake_parent_steps(fake: FakeGitHub) -> None:
    parent = fake.add_issue("Big thing", labels=("issuebot/triage-complete",))
    first = await fake.create_sub_issue(parent.number, title="A", body="a")
    second = await fake.create_sub_issue(parent.number, title="B", body="b")
    fake.close_issue(first.number)
    steps = await fake.parent_steps([parent.number, 999])
    assert list(steps) == [parent.number]
    assert steps[parent.number].labels == ("issuebot/triage-complete",)
    assert [(s.number, s.state) for s in steps[parent.number].sub_issues] == [
        (first.number, "closed"),
        (second.number, "open"),
    ]
    assert fake.issue(first.number).parent == parent.number
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_github_normalise.py tests/test_github_ghcli.py tests/test_github_fake.py -k "parent" -v`
Expected: FAIL. `Issue` has no attribute `parent`, and `parent_steps_from_node`, `parent_steps`
and `link_sub_issue` do not exist.

- [ ] **Step 3: Implement**

- `ISSUE_FIELDS` gains `parent { number }` on its first line of scalars. It is not a
  connection, so the poll's cost does not change.
- `issue_from_node` reads `parent`. Absent or `null` gives `None`. A mapping whose `number` is
  an `int` that is not a `bool` gives that number. Anything else raises
  `GitHubError("response", f"malformed issue record #{number}: parent")`.
- Move `_sub_issue_from` to `normalise.py` as `sub_issue_from_node`, unchanged, and import it
  in `ghcli.py`.
- `parent_steps_from_node` raises `GitHubError("response", ...)` when `number` is not an int,
  or when `subIssues` is not a mapping with a `nodes` list. Labels go through `_label_names`.
- `ghcli.py`: add `parent_steps_query(numbers: Sequence[int]) -> str`, shaped like
  `by_ids_query`. It has one alias per number:
  `p{n}: issue(number: {n}) { number labels(first: 50) { nodes { name } } subIssues(first: {PAGE_SIZE}) { nodes { number title url state } } }`.
  `GhCliAdapter.parent_steps` follows `fetch_issues_by_ids` (line 292). It sorts the
  deduplicated numbers and makes one `_graphql(..., allow_missing_aliases=True)` call per
  `ID_BATCH_SIZE` batch. It skips `null` aliases and normalises the rest with
  `parent_steps_from_node`.
- `fake.py`:
  - `_node` adds `"parent": {"number": record.parent} if record.parent is not None else None`.
  - `parent_steps` calls `self._enter("parent_steps", numbers)`, so `fail_next(on=...)` and
    `calls` see it. It then builds each GraphQL-shaped node and returns
    `parent_steps_from_node` of it, so the fake and the real adapter normalise identically.
  - `link_sub_issue` raises `ValueError` if the child already has a parent.

- [ ] **Step 4: Run the tests and the suite**

Run: `uv run pytest tests/test_github_normalise.py tests/test_github_ghcli.py tests/test_github_fake.py -v`, then `uv run pytest`
Expected: PASS. The suite stays green, because `parent` defaults to `None` everywhere else.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/github tests/test_github_normalise.py tests/test_github_ghcli.py tests/test_github_fake.py
git commit -m "github: an issue's parent, and a parent's steps in one read"
```

---

### Task 2: The rule and the gate

**Files:**
- Modify: `src/issuebot/orchestrator/triage.py` (beside `is_triage_candidate`, line 23)
- Modify: `src/issuebot/orchestrator/admission.py` (`RefusalKind` line 29, `AdmissionRequest`
  line 305, `admit` line 356)
- Modify: `src/issuebot/orchestrator/state.py` (`RetryKind`, line 17)
- Test: `tests/test_orchestrator_triage.py`, `tests/test_admission.py`

**Interfaces:**
- Consumes: `ParentSteps`, `SubIssue` and `Issue.parent` (Task 1).
- Produces:
  - `earlier_open_step(issue: Issue, steps: ParentSteps, labels: GitHubLabels) -> int | None`
    in `orchestrator/triage.py`.
  - `OrderHold`, a frozen, slotted dataclass in `admission.py`: `parent: int` and
    `waits_for: int | None` (`None` means the order could not be read).
  - `AdmissionRequest.order: OrderHold | None = None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_orchestrator_triage.py`:

```python
def parent_order(
    *states: tuple[int, str], labels: tuple[str, ...] = ("issuebot/triage-complete",)
) -> ParentSteps:
    subs = tuple(SubIssue(number=n, title=f"s{n}", url=f"u{n}", state=s) for n, s in states)
    return ParentSteps(number=10, labels=labels, sub_issues=subs)


def test_a_step_waits_for_the_first_open_step_before_it(make_issue) -> None:
    order = parent_order((11, "closed"), (12, "open"), (13, "open"), (14, "open"))
    assert earlier_open_step(make_issue(number=14, parent=10), order, LABELS) == 12


def test_the_first_step_and_a_step_after_closed_ones_wait_for_nothing(make_issue) -> None:
    order = parent_order((11, "closed"), (12, "open"), (13, "open"))
    assert earlier_open_step(make_issue(number=12, parent=10), order, LABELS) is None
    assert earlier_open_step(make_issue(number=11, parent=10), order, LABELS) is None


def test_a_parent_without_triage_complete_holds_nothing(make_issue) -> None:
    order = parent_order((11, "open"), (12, "open"), labels=())
    assert earlier_open_step(make_issue(number=12, parent=10), order, LABELS) is None


def test_a_step_missing_from_its_parent_holds_nothing(make_issue) -> None:
    order = parent_order((11, "open"))
    assert earlier_open_step(make_issue(number=12, parent=10), order, LABELS) is None


def test_the_order_is_githubs(make_issue) -> None:
    order = parent_order((13, "open"), (12, "open"))  # dragged on GitHub: 13 now comes first
    assert earlier_open_step(make_issue(number=12, parent=10), order, LABELS) == 13
```

In `tests/test_admission.py`:

```python
HELD = OrderHold(parent=10, waits_for=11)


def test_a_held_todo_sub_issue_waits_for_an_earlier_step(make_issue) -> None:
    assert admit(request(issue=make_issue(), order=HELD)) == Refused(
        kind="order", reason="waits for #11, an earlier step of #10", wait="order"
    )


def test_an_unreadable_order_holds_too(make_issue) -> None:
    verdict = admit(request(issue=make_issue(), order=OrderHold(parent=10, waits_for=None)))
    assert verdict == Refused(
        kind="order", reason="the order of #10's sub-issues could not be read", wait="order"
    )


@pytest.mark.parametrize(
    "state, label",
    [(StateLabel.REWORK, "issuebot/rework"), (StateLabel.IN_PROGRESS, "issuebot/in-progress")],
)
def test_a_started_step_is_never_held(make_issue, state: StateLabel, label: str) -> None:
    issue = make_issue(state=state, state_labels=(label,), labels=(label,))
    assert isinstance(admit(request(issue=issue, order=HELD)), Admitted)


def test_the_hold_ranks_after_the_worker_and_before_the_budget(make_issue) -> None:
    assert admit(request(issue=make_issue(), order=HELD, slots=0)).kind == "slots"
    over = IssueLedger(failures=9, cost_usd=99.0)
    verdict = admit(request(issue=make_issue(), order=HELD, ledger=over, max_issue_cost_usd=1.0))
    assert verdict.kind == "order"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_orchestrator_triage.py tests/test_admission.py -k "step or held or order or hold" -v`
Expected: FAIL. `earlier_open_step` and `OrderHold` do not exist.

- [ ] **Step 3: Implement**

`earlier_open_step` returns `None` in four cases: the issue's parent is not `steps.number`,
`labels.triage_complete.lower()` is not in `steps.labels`, the issue is not among
`steps.sub_issues`, or every step before it is closed. Otherwise it returns the number of the
first open step before it.

In `admit`, the new check goes right after the `inactive` check and before `attempts`. Add a
line to `admit`'s docstring: the hold is a "later", as a full worker is, so it outranks the
budget.

```python
if work and issue.state is StateLabel.TODO and request.order is not None:
    return Refused(kind="order", reason=_order_reason(request.order), wait="order")
```

`_order_reason` produces the two Global Constraints strings. Add `"order"` to `RefusalKind` and
`RetryKind`. In `AdmissionRequest`'s docstring, say that the orchestrator gathers `order` only
for a `todo` sub-issue in the candidate sweep (spec section 5).

- [ ] **Step 4: Run the tests and the suite**

Run: `uv run pytest tests/test_orchestrator_triage.py tests/test_admission.py -v`, then `uv run pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/orchestrator/triage.py src/issuebot/orchestrator/admission.py src/issuebot/orchestrator/state.py tests/test_orchestrator_triage.py tests/test_admission.py
git commit -m "admission: hold a todo sub-issue while an earlier step is open"
```

---

### Task 3: The worker holds a step until the one before it closes

**Files:**
- Modify: `src/issuebot/orchestrator/orchestrator.py`: `__init__` (line 317), `_admit`
  (line 1179), `_handle_refusal` (line 1204), `_dispatch_candidates` (line 1252)
- Test: `tests/test_orchestrator.py` (beside the triage tests, around line 5160)

**Interfaces:**
- Consumes: `GitHubAdapter.parent_steps` and `FakeGitHub.link_sub_issue` (Task 1);
  `earlier_open_step`, `OrderHold` and `AdmissionRequest.order` (Task 2).
- Produces:
  - `Orchestrator._read_parent_steps(issues: Sequence[Issue]) -> dict[int, ParentSteps] | None`
  - `Orchestrator._order_hold(issue: Issue, steps: Mapping[int, ParentSteps] | None) -> OrderHold | None`
  - `_admit(..., order: OrderHold | None = None)`

- [ ] **Step 1: Write the failing tests**

Add a helper next to the tests:

```python
def add_split(h: Harness, parent: int, *children: int, todo: tuple[int, ...] = ()) -> None:
    """A triaged parent and its sub-issues in order; those in `todo` labelled and approved."""
    h.github.add_issue(f"Parent {parent}", labels=(h.labels.triage_complete,), number=parent)
    for child in children:
        if child in todo:
            h.add_issue(child, "todo")
        else:
            h.github.add_issue(f"Step {child}", number=child)
        h.github.link_sub_issue(parent, child)
```

Write these tests. Each takes `tmp_path`, and uses `Harness(tmp_path, max_concurrent=3)` unless
noted.

- `test_a_batch_labelled_at_once_runs_one_step_at_a_time`:
  - Setup: `add_split(h, 10, 11, 12, 13, todo=(11, 12, 13))`, then one tick.
  - Assert:
    - `h.calls("set_state") == [(11, StateLabel.IN_PROGRESS)]`.
    - 12 and 13 still carry `issuebot/todo`, and `h.github.comments_for(12) == []`.
    - No `blocked` among `h.recorder.kinds`.
    - No `approval_evidence` call names 12 or 13.
    - `len(h.calls("parent_steps")) == 1`, and its argument names 10 once.
- `test_a_held_step_starts_on_the_poll_after_the_step_before_it_closes`:
  - Setup: `add_split(h, 10, 11, 12, todo=(12,))`, where 11 is open with no label (its pull
    request still in review). Tick. Then `h.github.close_issue(11)` and tick again.
  - Assert: the first tick dispatched nothing. After the second, `"12"` is in
    `h.orchestrator.running`.
- `test_an_earlier_operator_step_holds_the_next_step`:
  - Setup: `add_split(h, 10, 11, 12, todo=(12,))`, then
    `h.github.human_add_label(11, h.labels.operator_step)`, then tick.
  - Assert: nothing is dispatched.
- `test_an_operator_step_labelled_todo_is_refused_at_once_not_held`:
  - Setup: `add_split(h, 10, 11, 12, todo=(12,))`, where 11 is open. Then
    `h.github.human_add_label(12, h.labels.operator_step)`, then tick.
  - Assert: 12's first comment starts `"### Issuebot operator step ("`, and 12 has no state
    label.
- `test_a_reworked_step_is_not_held`:
  - Setup: `add_split(h, 10, 11)`, where 11 is open. Then `h.add_issue(12, "rework")`, then
    `h.github.link_sub_issue(10, 12)`, then tick.
  - Assert: `"12"` is running, and `h.calls("parent_steps") == []`. Only `todo` candidates
    cause a read.
- `test_a_failed_parent_read_holds_sub_issues_for_that_poll_only`:
  - Setup: `add_split(h, 10, 11, 12, todo=(12,))`, then `h.github.close_issue(11)`. Add an
    unrelated `h.add_issue(20, "todo")`. Then
    `h.github.fail_next("transport", on="parent_steps")`, and tick under `capture_logs()`.
  - Assert:
    - `"20"` is running and `"12"` is not.
    - A `parent_steps_failed` warning is logged.
    - After a second tick, `"12"` is running.
- `test_a_sub_issue_of_an_untriaged_parent_is_not_held`:
  - Setup: `h.github.add_issue("Parent", number=10)` with no `triage-complete`. Add an open
    11, then `h.add_issue(12, "todo")`, and link both under 10. Tick.
  - Assert: `"12"` is running.
- `test_no_todo_sub_issue_means_no_parent_read`:
  - Setup: `h.add_issue(20, "todo")`, then tick.
  - Assert: `h.calls("parent_steps") == []`.
- `test_a_held_step_is_logged_once`:
  - Setup: `add_split(h, 10, 11, 12, todo=(12,))`, then two ticks under `capture_logs()`.
  - Assert: exactly one `dispatch_held` entry, with `issue_number == 12` and
    `reason == "waits for #11, an earlier step of #10"`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py -k "step or parent or batch" -v`
Expected: FAIL. Without the hold, 12 and 13 are dispatched beside 11.

- [ ] **Step 3: Implement**

- `_dispatch_candidates`:
  1. After the fetch, drop `self._held` entries whose issue is not a `todo` issue in this poll.
  2. Call `steps = await self._read_parent_steps(issues)` once.
  3. In the loop, pass `order=self._order_hold(issue, steps)` to `_admit`.
  4. When a candidate is admitted, `self._held.pop(issue.id, None)`.
- `_read_parent_steps` collects the parents of the poll's `todo` issues that have a parent and
  are not operator steps. With none, it returns `{}` without a read. On a `GitHubError` it logs
  the `parent_steps_failed` warning, with `error=str(exc)` and `parents=sorted(...)`, and
  returns `None`.
- `_order_hold` returns `None` unless the issue is `todo`, has a parent and is not an operator
  step. Given `steps is None`, it returns `OrderHold(parent, None)`. For a parent missing from
  `steps` it returns `None`. Otherwise it returns an `OrderHold` around the result of
  `earlier_open_step`, or `None` when that is `None`.
- `_handle_refusal` handles `kind == "order"` before its budget branch. If
  `self._held.get(issue.id) != verdict.reason`, it records the reason and logs `dispatch_held`
  at info, with `issue_number`, `issue_identifier` and `reason`. Then it returns.
- `self._held: dict[str, str]` is initialised in `__init__`, with a comment: issue id → the
  hold reason last logged, so a held step says so once per change rather than every poll.
- The retry path (`_fire`) is unchanged (ruling 3).

- [ ] **Step 4: Run the tests and the suite**

Run: `uv run pytest tests/test_orchestrator.py -v`, then `uv run pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/orchestrator/orchestrator.py tests/test_orchestrator.py
git commit -m "orchestrator: hold a step of a split until the steps before it close"
```

---

### Task 4: `run-once` refuses a held step

**Files:**
- Modify: `src/issuebot/cli.py` (`_run_once`, after `not_runnable` at line 2070)
- Test: `tests/test_cli.py` (beside the run-once tests, around line 2356)

**Interfaces:**
- Consumes: `GitHubAdapter.parent_steps` (Task 1) and `earlier_open_step` (Task 2).
- Produces: `held_reason(adapter: GitHubAdapter, issue: Issue, labels: GitHubLabels) -> str | None`,
  an `async` function in `cli.py`.

- [ ] **Step 1: Write the failing tests**

Each test takes the fixtures `test_run_once_claims_a_todo_issue_and_prints_the_summary` takes
(`capsys`, `monkeypatch`, `tmp_path`, `fake_github`, `stub_session`, `recording_sink`) and the
same `_workflow_with_root(tmp_path)` workflow. It sets `GH_TOKEN` and links a split in
`fake_github`:
- 10 carries `issuebot/triage-complete`.
- 11 is open with no label.
- 12 carries `issuebot/todo` and is approved with `_approve(fake_github, 12)`.
- `fake_github.link_sub_issue(10, 11)` and `(10, 12)`.

- `test_run_once_refuses_a_held_step`:
  - Run: `main(["run-once", "12", "--workflow", str(path)])`.
  - Assert: it returns `1`. The output contains
    `"[FAIL] issue: #12 waits for #11, an earlier step of #10; close that first, or reorder #10's sub-issues on GitHub"`.
    `stub_session.calls == []`, and no `set_state` is in `fake_github.calls`.
- `test_run_once_runs_a_step_whose_earlier_steps_closed`:
  - Setup: `fake_github.close_issue(11)`, then the same command.
  - Assert: it returns `0`, and `stub_session.calls` has one call.
- `test_run_once_fails_when_the_order_cannot_be_read`:
  - Setup: `fake_github.fail_next("transport", on="parent_steps")`.
  - Assert: it returns `1`, and the output contains
    `"[FAIL] issue: #12 could not read the order of #10's sub-issues:"`.
- `test_run_once_show_prompt_skips_the_order_read`:
  - Run: the same command with `--show-prompt` added.
  - Assert: it returns `0`, and no `parent_steps` call is in `fake_github.calls`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "run_once and (held or order or earlier)" -v`
Expected: FAIL. `run-once` dispatches 12.

- [ ] **Step 3: Implement**

`held_reason` returns `None` unless the issue is `todo` and has a parent. It reads
`await adapter.parent_steps([issue.parent])` and asks `earlier_open_step`. When a step is held
it returns `f"waits for #{w}, an earlier step of #{p}; close that first, or reorder #{p}'s sub-issues on GitHub"`.
On `GitHubError` it returns `f"could not read the order of #{p}'s sub-issues: {exc}"`. In
`_run_once`, call it only when `not show_prompt`, right after the `not_runnable` check, and
print `[FAIL] issue: #{number} {reason}` with exit code 1, as that check does.

- [ ] **Step 4: Run the tests and the suite**

Run: `uv run pytest tests/test_cli.py -v`, then `uv run pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/cli.py tests/test_cli.py
git commit -m "cli: run-once refuses a step whose earlier steps are still open"
```

---

### Task 5: The comment, the docs, the spec's record and a read-only probe

**Files:**
- Modify: `src/issuebot/orchestrator/triage.py` (`verdict_comment`'s split sentence, line 75)
- Modify: `README.md` (the "Triage is by hand" paragraph, line 63)
- Modify: `docs/operations.md` (`### Triage`, after the paragraph ending "open them yourself
  from the merged plan.")
- Modify: `docs/package-layout.md` (the `issuebot.github` and `issuebot.orchestrator` sections)
- Modify: `docs/superpowers/specs/2026-10-25-triage-design.md` (section 5's "Order within a
  split", and the matching "Left to the builder" bullet)
- Test: `tests/test_orchestrator_triage.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_a_split_comment_says_the_steps_can_be_labelled_at_once() -> None:
    github, _recorder, bus = setup()
    await apply(github, bus, SPLIT)
    [comment] = github.comments_for(1)
    assert (
        "Label every issuebot step `issuebot/todo` at once if you like: issuebot starts each "
        "only when every step before it has closed."
    ) in comment.body
    assert "once the step before it has closed" not in comment.body
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_orchestrator_triage.py -k labelled_at_once -v`
Expected: FAIL.

- [ ] **Step 3: Change the comment**

Replace the sentence "Start each issuebot step with `{todo}` once the step before it has
closed. Operator steps (`{operator_step}`) are yours to do and close." with this exact copy:
"Label every issuebot step `{todo}` at once if you like: issuebot starts each only when every
step before it has closed. Operator steps (`{operator_step}`) are yours to do and close, and the
steps after one wait for it."

- [ ] **Step 4: Update the docs**

- `README.md`: after "you read the verdict and label the issues you want worked.", add:
  "A split's steps can be labelled together: the worker starts each only once every sub-issue
  before it, in the parent's order on GitHub, has closed."
- `docs/operations.md`: add a paragraph after the one ending "open them yourself from the
  merged plan.". It should say:
  - A `todo` sub-issue of a parent carrying `issuebot/triage-complete` waits, silently, while
    any sub-issue before it in the parent's order is open. Operator steps count, and "closed"
    means closed by any route, so a step whose pull request is still open holds the next one
    until it merges.
  - `docker compose logs worker` shows `dispatch_held` with the step it waits for, once per
    change.
  - To change the order, drag the sub-issues on the parent. To run a step early, remove it
    from the parent.
  - `rework` and resumed steps are never held.
  - A poll that cannot read the order holds every sub-issue for that poll only
    (`parent_steps_failed`).
- `docs/package-layout.md`, at most 1.5 KB in all:
  - In `issuebot.github`: `Issue.parent`, `ParentSteps`, and `parent_steps` as one aliased
    read per tick.
  - In `issuebot.orchestrator`: the `order` refusal in `admission.py` and where it ranks,
    `earlier_open_step` in `triage.py`, and the per-tick parent read with its fail-closed
    poll.
- The spec: under "Order within a split", add one bullet beginning "Decided while planning
  phase 2:" that records rulings 1 and 2 above. Strike the matching "Left to the builder"
  bullet down to "Resolved in section 5."

- [ ] **Step 5: Run the docs tests and the suite**

Run: `uv run pytest tests/test_readme_bounds.py tests/test_doc_pointers.py tests/test_instruction_bounds.py tests/test_orchestrator_triage.py -v`, then `uv run pytest`, then `uv run pre-commit run --all-files`
Expected: PASS.

- [ ] **Step 6: Probe the reads against GitHub, read-only**

From the worktree, with the host's own `gh` login, read the live parents and the cost of each
query. Nothing here writes.

```bash
uv run python - <<'EOF'
import asyncio, json
from issuebot.config.settings import GitHubSettings
from issuebot.github.ghcli import GhCliAdapter, by_ids_query, parent_steps_query

adapter = GhCliAdapter(GitHubSettings(repo="jleavers/issuebot"), login="jleavers")

async def main() -> None:
    print(await adapter.parent_steps([273, 274, 279, 280]))
    for query in (by_ids_query([292, 293]), parent_steps_query([273, 274, 279, 280])):
        probed = query.replace("query($owner: String!, $name: String!) {",
                               "query($owner: String!, $name: String!) { rateLimit { cost }", 1)
        data = await adapter._graphql(probed, {"owner": "jleavers", "name": "issuebot"})
        print(json.dumps(data["rateLimit"]), json.dumps(data["repository"])[:300])

asyncio.run(main())
EOF
```

Expected:
- #273's steps are `[292, 293]` in that order.
- `by_ids_query` reports `"parent": {"number": 273}` for both.
- Both queries report `{"cost": 1}`.

Record the two costs in the PR body. If `GitHubSettings` or `_graphql` take different names at
HEAD, adapt the probe and keep it read-only.

- [ ] **Step 7: Commit**

```bash
git add src/issuebot/orchestrator/triage.py tests/test_orchestrator_triage.py README.md docs/operations.md docs/package-layout.md docs/superpowers/specs/2026-10-25-triage-design.md
git commit -m "triage: say a split can be labelled at once; document the hold"
```

---

## After the merge (the operator, not this branch)

Run `python3 tools/upgrade/upgrade.py` from the hub checkout. No `labels ensure` is needed.
Then label a waiting split's plan step `issuebot/todo` while its operator step is still open,
for example #293 while #292 is open. The worker log should show one `dispatch_held` line for
it, and nothing should be dispatched until the earlier step closes. Until then, label one step
at a time, as the phase-1 comment says: the deployed worker does not hold anything yet.
