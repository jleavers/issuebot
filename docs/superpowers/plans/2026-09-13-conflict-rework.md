# Conflict Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The worker notices a `review` issue whose linked pull request has become `CONFLICTING` and moves it to `rework` itself, at most `agent.max_conflict_reworks` times per issue, so the existing rework path resolves the conflict.

**Architecture:** GitHub's `mergeable` field rides along on the linked PR the issue query already returns. After every fetch the orchestrator walks the `review` issues and hands each conflicting one to a new `conflict_rework` action beside the blocked escape, which counts its own earlier blocks in the workpad, sets `rework`, appends a block and publishes the transition as issuebot's own. One setting caps it; `0` is off.

**Tech Stack:** Python 3.14, `uv`, pydantic `Settings`, `FakeGitHub` for every test, pytest (hermetic, no network), ruff + pre-commit.

**Spec:** `docs/superpowers/specs/2026-09-13-conflict-rework-design.md`

## Global Constraints

- Run everything with `uv run ...`; tests are `uv run pytest`, lint is `uv run ruff check . && uv run ruff format --check .`, and `uv run pre-commit run --all-files` must pass before each commit (the git hook runs it anyway).
- Work on the branch `issuebot/conflict-rework` (already exists, holds the spec). Never push to `main`; open the PR through `gh api repos/jleavers/issuebot/pulls -X POST ... -F body=@file.md` (see `CLAUDE.md`, "Creating PRs"), never `gh pr create`.
- Every commit message ends with the two attribution lines given in the session's system reminder (`Co-Authored-By:` and `Claude-Session:`).
- The `mergeable` values are exactly `"mergeable"`, `"conflicting"`, `"unknown"`; the setting is exactly `agent.max_conflict_reworks`, default `3`, minimum `0`.
- Workpad headings are exactly `### Issuebot merge conflict (<stamp>)` and `### Issuebot merge conflict limit (<stamp>)`, stamp format `%Y-%m-%dT%H:%M:%SZ` in UTC, as `blocked_block` does.
- `tests/fixtures/runs/` is byte-for-byte and never touched.
- Do not add a `debounce`, a database column, a dashboard change or a prompt variable: the spec rules them out.

---

## File map

| File | Change |
|---|---|
| `src/issuebot/github/models.py` | `Mergeable` literal; `LinkedPr.mergeable` with default `"unknown"` |
| `src/issuebot/github/normalise.py` | `_MERGEABLE` table; `_select_pr` reads the field |
| `src/issuebot/github/ghcli.py` | `mergeable` added to the `closedByPullRequestsReferences` fragment |
| `src/issuebot/github/fake.py` | `_FakePr.mergeable`; `_node` emits it; `set_pr_mergeable` helper |
| `src/issuebot/github/__init__.py` | export `Mergeable` |
| `src/issuebot/github/state.py` | transition `(REVIEW, REWORK, ISSUEBOT)`; `rework` label description |
| `src/issuebot/config/settings.py` | `AgentSettings.max_conflict_reworks` |
| `src/issuebot/orchestrator/actions.py` | `conflict_block`, `conflict_limit_block`, `count_conflict_bounces`, `conflict_rework`; shared `_append_workpad` |
| `src/issuebot/orchestrator/state.py` | `conflict_candidate(issue)` |
| `src/issuebot/orchestrator/orchestrator.py` | `fetch_states`; `_bounce_conflicts`; `_fetch_issues` and `_poll_issues` use them |
| `configs/WORKFLOW.md` | rework context names both authors |
| `README.md`, `CLAUDE.md`, `docs/BLUEPRINT.md` | who sets `rework`; the setting |
| Tests | `test_github_normalise.py`, `test_github_fake.py`, `test_github_state.py`, `test_settings.py`, `test_orchestrator_actions.py`, `test_orchestrator_state.py`, `test_orchestrator.py`, `test_workflow_default.py` |

---

### Task 1: The PR's mergeability reaches `LinkedPr`

**Files:**
- Modify: `src/issuebot/github/models.py:33-38` (`LinkedPr`)
- Modify: `src/issuebot/github/normalise.py:11-12` and `:125-145` (`_select_pr`)
- Modify: `src/issuebot/github/ghcli.py:30-36` (`ISSUE_FIELDS`)
- Modify: `src/issuebot/github/fake.py:43-48` (`_FakePr`), `:268-276` (`open_pr`), `:343-349` (`_linked_pr`), `:364-373` (`_node`)
- Modify: `src/issuebot/github/__init__.py` (export)
- Test: `tests/test_github_normalise.py`, `tests/test_github_fake.py`

**Interfaces:**
- Produces: `Mergeable = Literal["mergeable", "conflicting", "unknown"]` in `issuebot.github.models`, exported from `issuebot.github`; `LinkedPr.mergeable: Mergeable = "unknown"`; `FakeGitHub.set_pr_mergeable(pr_number: int, mergeable: Mergeable) -> None`.

- [ ] **Step 1: Write the failing normaliser tests**

In `tests/test_github_normalise.py`, change the `pr()` helper to take the field and add two tests after `test_unusable_pr_reference_is_skipped`:

```python
def pr(
    number: int, state: str, merged_at: str | None = None, mergeable: str | None = "MERGEABLE"
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "number": number,
        "url": f"https://github.com/example/repo/pull/{number}",
        "state": state,
        "mergedAt": merged_at,
    }
    if mergeable is not None:
        fields["mergeable"] = mergeable
    return fields
```

```python
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("MERGEABLE", "mergeable"), ("CONFLICTING", "conflicting"), ("UNKNOWN", "unknown")],
)
def test_linked_pr_carries_mergeability(raw: str, expected: str) -> None:
    refs = {"nodes": [pr(52, "OPEN", mergeable=raw)]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None
    assert issue.linked_pr.mergeable == expected


@pytest.mark.parametrize("raw", [None, "", "WEIRD", 7])
def test_absent_or_unrecognised_mergeability_reads_unknown(raw: object) -> None:
    """An older response, or a value GitHub adds later, must never look like a conflict."""
    reference = pr(52, "OPEN", mergeable=None)
    if raw is not None:
        reference["mergeable"] = raw
    refs = {"nodes": [reference]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None
    assert issue.linked_pr.mergeable == "unknown"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_github_normalise.py -k mergeab -v`
Expected: FAIL with `AttributeError: 'LinkedPr' object has no attribute 'mergeable'`.

- [ ] **Step 3: Add the type and the field**

In `src/issuebot/github/models.py`, after `PrState = Literal["open", "closed", "merged"]`:

```python
Mergeable = Literal["mergeable", "conflicting", "unknown"]
```

and in `LinkedPr`, after `merged_at`:

```
    # GitHub's MergeableState, lowercased; "unknown" when it has not been computed or asked for.
    mergeable: Mergeable = "unknown"
```

In `src/issuebot/github/normalise.py`, import `Mergeable` from `issuebot.github.models`, add beside `_PR_RANK`:

```python
_MERGEABLE: dict[str, Mergeable] = {
    "MERGEABLE": "mergeable",
    "CONFLICTING": "conflicting",
    "UNKNOWN": "unknown",
}
```

and in `_select_pr`, replace the `candidates.append(...)` call with:

```
        raw_mergeable = item.get("mergeable")
        mergeable = (
            _MERGEABLE.get(raw_mergeable, "unknown") if isinstance(raw_mergeable, str) else "unknown"
        )
        candidates.append(
            LinkedPr(
                number=number,
                url=url,
                state=state,
                merged_at=_optional_timestamp(item.get("mergedAt")),
                mergeable=mergeable,
            )
        )
```

In `src/issuebot/github/__init__.py`, add `Mergeable` to the `models` import and to `__all__` (alphabetical, after `LinkedPr`).

- [ ] **Step 4: Run the normaliser tests to verify they pass**

Run: `uv run pytest tests/test_github_normalise.py -v`
Expected: all PASS, including the two new ones.

- [ ] **Step 5: Write the failing fake test**

In `tests/test_github_fake.py`, after `test_close_pr_does_not_close_issue`:

```python
def test_pr_mergeability_round_trips_through_the_node(fake: FakeGitHub) -> None:
    """The fake emits GitHub's upper-case value so the shared normaliser lowercases it."""
    issue = fake.add_issue("A", labels=("issuebot/review",))
    pr = fake.open_pr(issue.number)
    assert pr.mergeable == "mergeable"
    assert fake.issue(issue.number).linked_pr.mergeable == "mergeable"  # type: ignore[union-attr]
    fake.set_pr_mergeable(pr.number, "conflicting")
    assert fake.issue(issue.number).linked_pr.mergeable == "conflicting"  # type: ignore[union-attr]
    fake.set_pr_mergeable(pr.number, "unknown")
    assert fake.issue(issue.number).linked_pr.mergeable == "unknown"  # type: ignore[union-attr]
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_github_fake.py -k mergeab -v`
Expected: FAIL with `AttributeError: 'FakeGitHub' object has no attribute 'set_pr_mergeable'`.

- [ ] **Step 7: Teach the fake**

In `src/issuebot/github/fake.py`: import `Mergeable` alongside `PrState`; add to `_FakePr`:

```
    mergeable: Mergeable = "mergeable"
```

Add after `close_pr`:

```
    def set_pr_mergeable(self, pr_number: int, mergeable: Mergeable) -> None:
        """What GitHub's test merge would answer for the pull request from now on."""
        self._require_pr(pr_number).mergeable = mergeable
```

In `_linked_pr`, add `mergeable=pr.mergeable,` after `merged_at=pr.merged_at,`. In `_node`'s PR dictionary, add after the `"mergedAt"` line:

```
                        "mergeable": pr.mergeable.upper(),
```

- [ ] **Step 8: Add the field to the GraphQL fragment**

In `src/issuebot/github/ghcli.py`, change the fragment line to:

```
  closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
    nodes { number url state mergedAt mergeable }
  }
```

- [ ] **Step 9: Run the github tests and lint**

Run: `uv run pytest tests/test_github_fake.py tests/test_github_normalise.py tests/test_github_ghcli.py -v && uv run ruff check . && uv run ruff format --check .`
Expected: all PASS, lint clean. (If a ghcli test pins the fragment text verbatim, update the expected string to include `mergeable`.)

- [ ] **Step 10: Commit**

```bash
git add src/issuebot/github tests/test_github_normalise.py tests/test_github_fake.py tests/test_github_ghcli.py
git commit -F - <<'EOF'
feat: the linked pull request carries GitHub's mergeable state

`LinkedPr.mergeable` reads MERGEABLE, CONFLICTING or UNKNOWN out of the
issue query, lowercased; anything else, or an older response without the
field, is "unknown", so nothing can mistake it for a conflict. The fake
emits the same node shape and gets `set_pr_mergeable` for tests.

<attribution lines>
EOF
```

---

### Task 2: issuebot may move `review` to `rework`

**Files:**
- Modify: `src/issuebot/github/state.py:24-38` (`TRANSITIONS`), `:50` (`LABEL_STYLES[REWORK]`)
- Test: `tests/test_github_state.py:44-84`

**Interfaces:**
- Produces: `is_allowed(StateLabel.REVIEW, StateLabel.REWORK, Actor.ISSUEBOT)` is `True`; `len(TRANSITIONS) == 11`.

- [ ] **Step 1: Write the failing test**

In `tests/test_github_state.py`, add `(StateLabel.REVIEW, StateLabel.REWORK, Actor.ISSUEBOT),` to the `test_allowed_transitions` parametrisation directly after the `(StateLabel.REVIEW, StateLabel.REWORK, Actor.HUMAN),` row, and change `test_transition_table_size` to:

```python
def test_transition_table_size() -> None:
    assert len(TRANSITIONS) == 11
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_github_state.py -k "allowed_transitions or table_size" -v`
Expected: the new `REVIEW-REWORK-issuebot` case and `test_transition_table_size` FAIL; the rest PASS.

- [ ] **Step 3: Add the row and reword the label**

In `src/issuebot/github/state.py`, add to `TRANSITIONS` after the `(StateLabel.REVIEW, StateLabel.REWORK, Actor.HUMAN),` row:

```
        (StateLabel.REVIEW, StateLabel.REWORK, Actor.ISSUEBOT),
```

and change the `REWORK` style to:

```
    StateLabel.REWORK: LabelStyle(
        "D93F0B", "Reviewer wants changes, or the PR conflicts with main; issuebot will pick it up"
    ),
```

- [ ] **Step 4: Run the state tests and the whole suite**

Run: `uv run pytest tests/test_github_state.py -v && uv run pytest -q`
Expected: PASS. (A test elsewhere may pin the old description text: `grep -rn "Reviewer wants changes" tests/` finds none today, so none should fail.)

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/github/state.py tests/test_github_state.py
git commit -F - <<'EOF'
feat: issuebot may move review to rework

The transition the conflict bounce makes, and the rework label's
description says who else sets it.

<attribution lines>
EOF
```

---

### Task 3: `agent.max_conflict_reworks`

**Files:**
- Modify: `src/issuebot/config/settings.py:86-91` (`AgentSettings`)
- Test: `tests/test_settings.py:30-42`, `:112-130`

**Interfaces:**
- Produces: `Settings.agent.max_conflict_reworks: int`, default `3`, `ge=0`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_settings.py`, add to the defaults test after `assert s.agent.max_retry_backoff_ms == 300_000`:

```
    assert s.agent.max_conflict_reworks == 3
```

Add `("agent", "max_conflict_reworks", -1),` to `test_constraints_reject_out_of_range_values` after the `max_retry_backoff_ms` row. Then add a new test after it:

```python
def test_zero_conflict_reworks_is_the_off_switch() -> None:
    s = Settings.model_validate({**MINIMAL, "agent": {"max_conflict_reworks": 0}})
    assert s.agent.max_conflict_reworks == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_settings.py -k "defaults or conflict or out_of_range" -v`
Expected: the defaults test FAILS with `AttributeError`, the `-1` case FAILS (no error raised, or `extra` forbidden), the zero test FAILS with a validation error about an extra field.

- [ ] **Step 3: Add the field**

In `src/issuebot/config/settings.py`, `AgentSettings`, after `max_retry_backoff_ms`:

```
    # How many times the worker may move one issue from review to rework because its pull
    # request conflicts with the default branch; 0 turns the automatic bounce off.
    max_conflict_reworks: int = Field(default=3, ge=0)
```

- [ ] **Step 4: Run the settings tests**

Run: `uv run pytest tests/test_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/config/settings.py tests/test_settings.py
git commit -F - <<'EOF'
feat: agent.max_conflict_reworks, default 3, 0 for off

<attribution lines>
EOF
```

---

### Task 4: The bounce: `conflict_rework` in `actions.py`

**Files:**
- Modify: `src/issuebot/orchestrator/actions.py` (new blocks, count, action; `blocked_escape` shares the append helper)
- Test: `tests/test_orchestrator_actions.py`

**Interfaces:**
- Consumes: `LinkedPr.mergeable` (Task 1), the transition (Task 2).
- Produces, all in `issuebot.orchestrator.actions`:
  - `CONFLICT_HEADING = "### Issuebot merge conflict ("`, `CONFLICT_LIMIT_HEADING = "### Issuebot merge conflict limit ("`
  - `ConflictOutcome = Literal["reworked", "limit_reached", "limit_noted", "failed"]`
  - `conflict_block(pr_number: int, bounce: int, limit: int, now: datetime, labels: GitHubLabels) -> str`
  - `conflict_limit_block(pr_number: int, limit: int, now: datetime, labels: GitHubLabels) -> str`
  - `count_conflict_bounces(body: str) -> int`
  - `async conflict_rework(adapter, bus, issue, *, limit: int, now: datetime) -> ConflictOutcome`

- [ ] **Step 1: Write the failing tests for the pure pieces**

In `tests/test_orchestrator_actions.py`, extend the import from `issuebot.orchestrator.actions` with `CONFLICT_HEADING, CONFLICT_LIMIT_HEADING, conflict_block, conflict_limit_block, conflict_rework, count_conflict_bounces` (keep it alphabetical), then add a new section before `# --- claim ---`:

```python
# --- conflict rework --------------------------------------------------------------------

LABELS = Settings.model_validate({"github": {"repo": "a/b"}}).github.labels


def test_conflict_block_names_the_pr_the_bounce_and_the_next_session() -> None:
    block = conflict_block(51, 1, 3, NOW, LABELS)
    assert block.startswith("### Issuebot merge conflict (2026-09-03T14:02:11Z)\n\n")
    assert "Pull request #51 conflicts with the default branch (bounce 1 of 3)." in block
    assert "Moved to `issuebot/rework`:" in block
    assert block.endswith("returns the issue to `issuebot/review`.")


def test_conflict_limit_block_says_it_stopped() -> None:
    block = conflict_limit_block(51, 3, NOW, LABELS)
    assert block.startswith("### Issuebot merge conflict limit (2026-09-03T14:02:11Z)\n\n")
    assert "moved this issue to `issuebot/rework` 3 times" in block
    assert block.endswith("A human resolves the conflict on the branch, or moves the issue.")


def test_count_conflict_bounces_counts_bounce_headings_only() -> None:
    body = "\n\n".join(
        [
            WORKPAD_MARKER,
            "### Plan\n\n- [ ] 1. Do it",
            conflict_block(51, 1, 3, NOW, LABELS),
            "### Issuebot blocked (2026-09-03T15:00:00Z)\n\nr.",
            conflict_block(51, 2, 3, NOW, LABELS),
            conflict_limit_block(51, 3, NOW, LABELS),
        ]
    )
    assert count_conflict_bounces(body) == 2
    assert count_conflict_bounces("") == 0
    assert count_conflict_bounces(f"{WORKPAD_MARKER}\n") == 0
    # The headings are what the count keys on, so they must stay distinguishable.
    assert not CONFLICT_LIMIT_HEADING.startswith(CONFLICT_HEADING)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator_actions.py -k conflict -v`
Expected: FAIL at import with `ImportError: cannot import name 'CONFLICT_HEADING'`.

- [ ] **Step 3: Write the pure pieces**

In `src/issuebot/orchestrator/actions.py`, after `CANCEL_REASON`:

```python
ConflictOutcome = Literal["reworked", "limit_reached", "limit_noted", "failed"]

# The workpad headings the conflict bounce writes. The count of the first is the bounce
# number, so the second must not start with it (it ends in "limit (" rather than "(").
CONFLICT_HEADING = "### Issuebot merge conflict ("
CONFLICT_LIMIT_HEADING = "### Issuebot merge conflict limit ("


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def conflict_block(
    pr_number: int, bounce: int, limit: int, now: datetime, labels: GitHubLabels
) -> str:
    """The block one bounce appends to the workpad."""
    return (
        f"{CONFLICT_HEADING}{_stamp(now)})\n\n"
        f"Pull request #{pr_number} conflicts with the default branch (bounce {bounce} of {limit}).\n"
        f"Moved to `{labels.rework}`: the next session merges the default branch into the "
        f"branch, resolves the conflict, re-runs validation and returns the issue to "
        f"`{labels.review}`."
    )


def conflict_limit_block(pr_number: int, limit: int, now: datetime, labels: GitHubLabels) -> str:
    """The block written once when the bounces are used up; the issue stays in review."""
    times = "time" if limit == 1 else "times"
    return (
        f"{CONFLICT_LIMIT_HEADING}{_stamp(now)})\n\n"
        f"Pull request #{pr_number} conflicts with the default branch again. issuebot has "
        f"moved this issue to `{labels.rework}` {limit} {times} for it and will not again. "
        f"A human resolves the conflict on the branch, or moves the issue."
    )


def count_conflict_bounces(body: str) -> int:
    """How many times the issue has been bounced, read back from its workpad."""
    return sum(1 for line in body.split("\n") if line.startswith(CONFLICT_HEADING))
```

`blocked_block` has its own inline stamp; change it to call `_stamp(now)` so there is one format.

- [ ] **Step 4: Run to verify the pure tests pass**

Run: `uv run pytest tests/test_orchestrator_actions.py -k "conflict or blocked_block" -v`
Expected: PASS.

- [ ] **Step 5: Write the failing tests for the action**

Append to the same section. Extend the `from issuebot.github import ...` line with `GitHubError`.

```python
async def seed(h: Harness, *blocks: str) -> None:
    """A review issue whose PR #51 conflicts, with a workpad holding ``blocks`` if given."""
    h.github.add_issue("Task", labels=("issuebot/review",), number=42)
    h.github.open_pr(42, pr_number=51)
    h.github.set_pr_mergeable(51, "conflicting")
    if blocks:
        body = "\n\n".join([WORKPAD_MARKER, "### Plan\n\n- [ ] 1. Do it", *blocks]) + "\n"
        await h.github.comment(42, body)
    h.github.calls.clear()


async def test_conflict_rework_moves_the_issue_and_records_the_bounce(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await seed(h)
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "reworked"
    assert h.github.issue(42).state is StateLabel.REWORK
    comments = h.github.comments_for(42)
    assert len(comments) == 1
    assert comments[0].body.startswith(f"{WORKPAD_MARKER}\n\n{CONFLICT_HEADING}")
    assert "(bounce 1 of 3)" in comments[0].body
    # The workpad is read for the count, then the label moves, then the note lands.
    assert [name for name, _ in h.github.calls] == ["find_workpad_comment", "set_state", "comment"]
    assert h.calls("set_state") == [(42, StateLabel.REWORK)]
    assert h.recorder.kinds == ["state_changed"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        "issuebot/review",
        "issuebot/rework",
        "issuebot",
    )
    assert changed.pr_url == "https://github.com/example/repo/pull/51"


async def test_conflict_rework_appends_to_an_existing_workpad_and_counts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await seed(h, conflict_block(51, 1, 3, NOW, LABELS), conflict_block(51, 2, 3, NOW, LABELS))
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "reworked"
    body = h.github.comments_for(42)[0].body
    assert body.startswith(f"{WORKPAD_MARKER}\n\n### Plan\n\n- [ ] 1. Do it\n\n")
    assert body.count(CONFLICT_HEADING) == 3
    assert "(bounce 3 of 3)" in body
    assert body.endswith("`issuebot/review`.\n")
    assert h.calls("comment") == []
    assert len(h.calls("update_comment")) == 1
    assert h.github.issue(42).state is StateLabel.REWORK


async def test_conflict_rework_at_the_limit_notes_it_once_and_stays_in_review(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    await seed(h, *(conflict_block(51, n, 3, NOW, LABELS) for n in (1, 2, 3)))
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "limit_reached"
    body = h.github.comments_for(42)[0].body
    assert body.count(CONFLICT_LIMIT_HEADING) == 1
    assert body.endswith("or moves the issue.\n")
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.calls("set_state") == []
    assert h.recorder.events == []
    h.github.calls.clear()
    # The block's presence is the idempotence: the next tick changes nothing.
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "limit_noted"
    assert h.github.comments_for(42)[0].body == body
    assert [name for name, _ in h.github.calls] == ["find_workpad_comment"]


async def test_conflict_rework_with_the_limit_lowered_below_the_count(tmp_path: Path) -> None:
    """An operator dropping the setting to 1 after two bounces gets the limit note, not a third."""
    h = Harness(tmp_path)
    await seed(h, conflict_block(51, 1, 3, NOW, LABELS), conflict_block(51, 2, 3, NOW, LABELS))
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=1, now=NOW) == "limit_reached"
    assert h.github.issue(42).state is StateLabel.REVIEW


async def test_conflict_rework_failure_on_set_state_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    await seed(h)

    async def refuse(*args: object, **kwargs: object) -> None:
        raise GitHubError("rate_limited", "slow down")

    monkeypatch.setattr(h.github, "set_state", refuse)
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "failed"
    assert h.github.comments_for(42) == []
    assert h.github.issue(42).state is StateLabel.REVIEW
    assert h.recorder.events == []


async def test_conflict_rework_failure_on_the_note_still_counts_as_reworked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label moved, so the rework session resolves the conflict either way."""
    h = Harness(tmp_path)
    await seed(h)
    original = h.github.set_state

    async def then_fail_the_note(*args: object, **kwargs: object) -> None:
        await original(*args, **kwargs)  # type: ignore[arg-type]
        h.github.fail_next("server_error")  # armed for the very next call: the note

    monkeypatch.setattr(h.github, "set_state", then_fail_the_note)
    issue = h.github.issue(42)
    assert await conflict_rework(h.github, h.bus, issue, limit=3, now=NOW) == "reworked"
    assert h.github.issue(42).state is StateLabel.REWORK
    assert h.github.comments_for(42) == []
    assert h.recorder.kinds == ["state_changed"]
```

- [ ] **Step 6: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator_actions.py -k conflict_rework -v`
Expected: FAIL with `TypeError`/`NameError` because `conflict_rework` is not defined (the import in Step 1 will already have failed; define a stub `async def conflict_rework(...)` raising `NotImplementedError` only if needed to see the individual failures, then replace it in Step 7).

- [ ] **Step 7: Write the action and share the workpad append**

In `src/issuebot/orchestrator/actions.py`, add a helper and use it from `blocked_escape` too:

```python
async def _append_workpad(
    adapter: GitHubAdapter, number: int, workpad: Comment | None, block: str
) -> None:
    """Append ``block`` to the workpad, creating the workpad when there is none."""
    if workpad is None:
        await adapter.comment(number, f"{WORKPAD_MARKER}\n\n{block}\n")
    else:
        await adapter.update_comment(workpad.id, workpad.body.rstrip("\n") + "\n\n" + block + "\n")
```

(import `Comment` from `issuebot.github`). In `blocked_escape`, replace the three lines from `if workpad is None:` to `await adapter.update_comment(workpad.id, body)` with:

```
        if workpad is None or _run_marker(context) not in workpad.body:
            await _append_workpad(adapter, issue.number, workpad, block)
```

Then the action, after `blocked_escape`:

```python
async def conflict_rework(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    *,
    limit: int,
    now: datetime,
) -> ConflictOutcome:
    """Move a review issue whose pull request conflicts to ``rework``, at most ``limit`` times.

    The workpad is the counter: each bounce leaves a ``CONFLICT_HEADING`` block, so the count
    survives a restart and a person can read it. Label first, note second: a note without
    the label would be counted again on the next tick, while a label without the note still
    gets the conflict resolved by the rework session (Step 6 of the prompt).
    """
    log = get_logger(__name__)
    pr = issue.linked_pr
    if pr is None:
        return "failed"
    try:
        workpad = await adapter.find_workpad_comment(issue.number)
        body = workpad.body if workpad is not None else ""
        bounces = count_conflict_bounces(body)
        if bounces >= limit:
            if CONFLICT_LIMIT_HEADING in body:
                return "limit_noted"
            block = conflict_limit_block(pr.number, limit, now, adapter.labels)
            await _append_workpad(adapter, issue.number, workpad, block)
            log.warning(
                "conflict_rework_limit",
                issue_number=issue.number,
                issue_identifier=issue.identifier,
                pr_number=pr.number,
                limit=limit,
            )
            return "limit_reached"
        await adapter.set_state(issue.number, StateLabel.REWORK)
    except GitHubError as exc:
        log.warning(
            "conflict_rework_failed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            pr_number=pr.number,
            error=str(exc),
        )
        return "failed"
    bus.publish(
        StateChanged(
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            from_label=state_label_name(issue),
            to_label=adapter.labels.rework,
            actor="issuebot",
            pr_url=pr.url,
        )
    )
    log.info(
        "conflict_rework",
        issue_number=issue.number,
        issue_identifier=issue.identifier,
        pr_number=pr.number,
        bounce=bounces + 1,
        limit=limit,
    )
    try:
        block = conflict_block(pr.number, bounces + 1, limit, now, adapter.labels)
        await _append_workpad(adapter, issue.number, workpad, block)
    except GitHubError as exc:
        # The label moved, so the session will resolve it; only the count is short by one.
        log.warning(
            "conflict_rework_note_failed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            pr_number=pr.number,
            error=str(exc),
        )
    return "reworked"
```

- [ ] **Step 8: Run the actions tests, then the suite**

Run: `uv run pytest tests/test_orchestrator_actions.py -v && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS, including every existing `blocked_escape` test (the refactor must not change its behaviour). If ruff reformats the long f-string in `conflict_block`, accept its formatting.

- [ ] **Step 9: Commit**

```bash
git add src/issuebot/orchestrator/actions.py tests/test_orchestrator_actions.py
git commit -F - <<'EOF'
feat: conflict_rework, the bounce from review to rework with a workpad count

<attribution lines>
EOF
```

---

### Task 5: The orchestrator bounces conflicting review issues

**Files:**
- Modify: `src/issuebot/orchestrator/state.py` (`conflict_candidate`)
- Modify: `src/issuebot/orchestrator/orchestrator.py:70-77` (`fetch_states`), `:531-552` (`_fetch_issues`, `_poll_issues`)
- Test: `tests/test_orchestrator_state.py`, `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `conflict_rework` (Task 4), `Settings.agent.max_conflict_reworks` (Task 3).
- Produces: `conflict_candidate(issue: Issue) -> bool` in `issuebot.orchestrator.state`; `fetch_states(*, observed: bool, conflicts: bool) -> tuple[StateLabel, ...]` in `issuebot.orchestrator.orchestrator`; `Orchestrator._bounce_conflicts(issues)`.

- [ ] **Step 1: Write the failing pure tests**

In `tests/test_orchestrator_state.py`, add (import `conflict_candidate` from `issuebot.orchestrator.state`, `LinkedPr` and `StateLabel` from `issuebot.github`; use the `make_issue` fixture):

```python
def _pr(state: str = "open", mergeable: str = "conflicting") -> LinkedPr:
    return LinkedPr(
        number=51,
        url="https://github.com/example/repo/pull/51",
        state=state,  # type: ignore[arg-type]
        merged_at=None,
        mergeable=mergeable,  # type: ignore[arg-type]
    )


def test_conflict_candidate_is_an_open_conflicting_pr_on_a_review_issue(
    make_issue: Callable[..., Issue],
) -> None:
    review = {"state": StateLabel.REVIEW, "state_labels": ("issuebot/review",)}
    assert conflict_candidate(make_issue(**review, linked_pr=_pr()))
    assert not conflict_candidate(make_issue(**review, linked_pr=None))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(mergeable="mergeable")))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(mergeable="unknown")))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(state="merged")))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(state="closed")))
    assert not conflict_candidate(make_issue(**review, linked_pr=_pr(), dispatchable=False))
    assert not conflict_candidate(
        make_issue(state=StateLabel.REWORK, state_labels=("issuebot/rework",), linked_pr=_pr())
    )
    assert not conflict_candidate(
        make_issue(
            state=StateLabel.IN_PROGRESS, state_labels=("issuebot/in-progress",), linked_pr=_pr()
        )
    )
```

In `tests/test_orchestrator.py`, import `fetch_states` and `CANDIDATE_STATES` beside `OBSERVED_STATES` and add near `test_without_an_observer_review_is_not_fetched`:

```python
@pytest.mark.parametrize(
    ("observed", "conflicts", "expected"),
    [
        (False, False, CANDIDATE_STATES),
        (True, False, OBSERVED_STATES),
        (False, True, OBSERVED_STATES),
        (True, True, OBSERVED_STATES),
    ],
)
def test_fetch_states_adds_review_for_either_reason(
    observed: bool, conflicts: bool, expected: tuple[StateLabel, ...]
) -> None:
    assert fetch_states(observed=observed, conflicts=conflicts) == expected
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator_state.py -k conflict_candidate tests/test_orchestrator.py -k fetch_states -v`
Expected: FAIL at import (`ImportError`) for both files.

- [ ] **Step 3: Write the pure pieces**

In `src/issuebot/orchestrator/state.py`, after `pr_url`:

```python
def conflict_candidate(issue: Issue) -> bool:
    """A review issue whose open pull request GitHub reports as conflicting (spec §3)."""
    pr = issue.linked_pr
    return (
        issue.state is StateLabel.REVIEW
        and issue.dispatchable
        and pr is not None
        and pr.state == "open"
        and pr.mergeable == "conflicting"
    )
```

In `src/issuebot/orchestrator/orchestrator.py`, after `OBSERVED_STATES`:

```python
def fetch_states(*, observed: bool, conflicts: bool) -> tuple[StateLabel, ...]:
    """Which states a poll fetches: review rides along for the history store, or for the
    conflict bounce, and the dispatch loop never runs it either way."""
    return OBSERVED_STATES if observed or conflicts else CANDIDATE_STATES
```

- [ ] **Step 4: Run the pure tests**

Run: `uv run pytest tests/test_orchestrator_state.py tests/test_orchestrator.py -k "conflict_candidate or fetch_states" -v`
Expected: PASS.

- [ ] **Step 5: Write the failing orchestrator tests**

In `tests/test_orchestrator.py`, add a `max_conflict_reworks: int = 3` parameter to `Harness.__init__` and to `write_workflow` (pass it through like `max_attempts`), and add `  max_conflict_reworks: {max_conflict_reworks}\n` to `WORKFLOW_TEMPLATE` under `agent:` after `max_retry_backoff_ms`. Add a helper on `Harness` after `add_issue`:

```
    def add_conflicting_review(self, number: int, *, pr_number: int) -> Issue:
        issue = self.add_issue(number, "review")
        self.github.open_pr(number, pr_number=pr_number)
        self.github.set_pr_mergeable(pr_number, "conflicting")
        return issue
```

Change `test_without_an_observer_review_is_not_fetched` to build `Harness(tmp_path, max_conflict_reworks=0)` and rename it `test_without_an_observer_or_the_bounce_review_is_not_fetched`. Then add a new section at the end of the file:

```python
# --- conflict bounce (spec 2026-09-13-conflict-rework-design.md) ----------------------------


async def test_a_conflicting_review_issue_is_bounced_then_dispatched_as_rework(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REWORK
    assert h.calls("fetch_issues_by_states")[-1] == (OBSERVED_STATES,)
    assert h.calls("set_state") == [(1, StateLabel.REWORK)]
    body = h.github.comments_for(1)[0].body
    assert body.startswith(f"{WORKPAD_MARKER}\n\n### Issuebot merge conflict (")
    assert "(bounce 1 of 3)" in body
    changed = h.recorder.of(StateChanged)
    assert [(e.from_label, e.to_label, e.actor) for e in changed] == [
        ("issuebot/review", "issuebot/rework", "issuebot")
    ]
    assert h.orchestrator.running == {}
    await h.tick()
    assert list(h.orchestrator.running) == ["1"]
    assert h.run_for(1).kwargs["rework"] is True
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS


async def test_the_bounce_waits_for_the_review_grace_to_end(tmp_path: Path) -> None:
    """A running entry would report the move as a human's and stop for the wrong reason."""
    h = Harness(tmp_path, interval_ms=30_000)
    h.add_issue(1, "todo")
    await h.tick()
    h.workspace_dir("repo-1")
    h.github.human_set_state(1, StateLabel.REVIEW)
    h.github.open_pr(1, pr_number=7)
    h.github.set_pr_mergeable(7, "conflicting")
    h.clock.advance(30)
    await h.tick()  # grace starts; the entry is still running
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.calls("set_state") == [(1, StateLabel.IN_PROGRESS)]
    h.clock.advance(30)
    await h.tick()  # grace over: the entry is stopped, but it has not exited yet
    assert h.entry(1).cancel.is_set()
    assert h.github.issue(1).state is StateLabel.REVIEW
    await h.drain()
    assert h.orchestrator.running == {}
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REWORK
    assert h.recorder.of(StateChanged)[-1].actor == "issuebot"


@pytest.mark.parametrize(
    ("pr_state", "mergeable"),
    [
        ("open", "mergeable"),
        ("open", "unknown"),
        ("merged", "conflicting"),
        ("closed", "conflicting"),
    ],
)
async def test_only_an_open_conflicting_pr_is_bounced(
    tmp_path: Path, pr_state: str, mergeable: str
) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "review")
    h.github.open_pr(1, pr_number=7)
    h.github.set_pr_mergeable(7, mergeable)  # type: ignore[arg-type]
    if pr_state == "merged":
        h.github.merge_pr(7)
        h.github.reopen_issue(1)  # merge_pr closes the issue; a closed one is swept, not bounced
    elif pr_state == "closed":
        h.github.close_pr(7)
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.calls("set_state") == []
    assert h.github.comments_for(1) == []


async def test_the_setting_at_zero_turns_the_bounce_off(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_conflict_reworks=0)
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.calls("fetch_issues_by_states")[-1] == (CANDIDATE_STATES,)
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.calls("set_state") == []


async def test_the_setting_at_zero_with_an_observer_still_does_not_bounce(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_conflict_reworks=0, observe_issues=True)
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.calls("fetch_issues_by_states")[-1] == (OBSERVED_STATES,)
    assert h.github.issue(1).state is StateLabel.REVIEW


async def test_a_held_worker_still_bounces(tmp_path: Path) -> None:
    """The hold stops claude, not gh; a conflict is about the board, not dispatch."""
    h = Harness(tmp_path)
    h.which_missing = {"claude"}
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.snapshots[-1].dispatch_hold is not None
    assert h.github.issue(1).state is StateLabel.REWORK
    await h.tick()
    assert h.orchestrator.running == {}  # held: bounced, not dispatched


async def test_the_bounce_stops_at_the_limit(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_conflict_reworks=1)
    h.add_conflicting_review(1, pr_number=7)
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REWORK
    await h.tick()  # dispatched as rework
    assert list(h.orchestrator.running) == ["1"]
    # The session returns the issue to review; the fake PR still reads conflicting, as it
    # would after the next sibling merge.
    h.github.human_set_state(1, StateLabel.REVIEW)
    await h.exit(h.run_for(1), final_state=StateLabel.REVIEW, final_issue=h.github.issue(1))
    await h.fire(1.0)  # the continuation retry a review exit queues; it finds review and clears
    assert h.orchestrator.running == {}
    assert h.orchestrator.retries == {}
    h.github.calls.clear()
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.calls("set_state") == []
    body = h.github.comments_for(1)[0].body
    assert body.count("### Issuebot merge conflict (") == 1
    assert "### Issuebot merge conflict limit (" in body
    await h.tick()
    assert h.github.comments_for(1)[0].body == body


async def test_a_bounce_failure_is_logged_and_retried_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.add_conflicting_review(1, pr_number=7)
    original = h.github.set_state
    refusals = {"left": 1}

    async def refuse_once(*args: Any, **kwargs: Any) -> None:
        if refusals["left"]:
            refusals["left"] -= 1
            raise GitHubError("server_error", "injected")
        await original(*args, **kwargs)

    monkeypatch.setattr(h.github, "set_state", refuse_once)
    with capture_logs() as logs:
        await h.tick()
    assert h.github.issue(1).state is StateLabel.REVIEW
    assert h.github.comments_for(1) == []
    assert any(entry["event"] == "conflict_rework_failed" for entry in logs)
    await h.tick()
    assert h.github.issue(1).state is StateLabel.REWORK
```

(`capture_logs` is already imported from `structlog.testing`; add `GitHubError` to the `from issuebot.github import ...` line.) If `h.fire(1.0)` in the limit test finds no retry queued, drop that line and the `retries == {}` assertion: the point is only that the issue is neither running nor retrying before the tick that hits the limit.

- [ ] **Step 6: Run to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py -k "bounce or conflicting or setting_at_zero or held_worker_still" -v`
Expected: FAIL: the issue stays in `review` (no bounce exists yet), and `add_conflicting_review`/`max_conflict_reworks` are unknown until the harness edit lands; `test_without_an_observer_or_the_bounce_review_is_not_fetched` PASSES only after the harness gains the parameter.

- [ ] **Step 7: Wire the orchestrator**

In `src/issuebot/orchestrator/orchestrator.py`: import `conflict_candidate` from `issuebot.orchestrator.state` and use the existing `actions` import. Add a property and change `_fetch_issues` and `_poll_issues`:

```
    def _conflict_limit(self) -> int:
        return self._workflow.config.agent.max_conflict_reworks

    async def _fetch_issues(self) -> Sequence[Issue] | None:
        """The polled issues, reported to the observer; None when the fetch failed."""
        states = fetch_states(
            observed=self._on_issues is not None, conflicts=self._conflict_limit() > 0
        )
        try:
            issues = await self._adapter.fetch_issues_by_states(states)
        except GitHubError as exc:
            self._log.warning("candidates_fetch_failed", error=str(exc))
            return None
        self._report_issues(issues)
        await self._bounce_conflicts(issues)
        return issues

    async def _bounce_conflicts(self, issues: Sequence[Issue]) -> None:
        """Move each review issue whose pull request conflicts to rework (spec §3, §4).

        Skipped for an issue the orchestrator still holds: a running entry in its review
        grace would read the move as a human's and stop for the wrong reason, and a retry is
        an in-flight decision about the same issue. The next poll gets it.
        """
        limit = self._conflict_limit()
        if limit <= 0:
            return
        for issue in issues:
            if not conflict_candidate(issue):
                continue
            if issue.id in self._running or issue.id in self._retries:
                continue
            outcome = await actions.conflict_rework(
                self._adapter, self._bus, issue, limit=limit, now=self._now()
            )
            if outcome == "limit_noted":
                self._log.debug(
                    "conflict_rework_limit_noted",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                )

    async def _poll_issues(self) -> None:
        """Keep the history store current while dispatch is held, so the board does not go stale.

        An authentication hold stops ``claude``, not ``gh``, and a preflight hold may name only
        ``claude.command``; either way the fetch still works and the board can stay current. A
        hold that ``fetch_preflight`` reports on skips this, since the request would only fail.
        The request is worth making at all only when an observer is watching or the conflict
        bounce is on: a conflict is about the board, not about dispatch.
        """
        if self._on_issues is None and self._conflict_limit() <= 0:
            return
        await self._fetch_issues()
```

Also update the comment above `OBSERVED_STATES` to say review is fetched "when an on_issues observer is attached or the conflict bounce is on (`fetch_states`)".

- [ ] **Step 8: Run the orchestrator tests, then everything**

Run: `uv run pytest tests/test_orchestrator.py -v -x && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS. Existing tests that assert `h.calls("fetch_issues_by_states")` against `CANDIDATE_STATES` without an observer (search for `(StateLabel.IN_PROGRESS, StateLabel.REWORK, StateLabel.TODO),\n    )` in the file) now see `OBSERVED_STATES` because the default limit is 3; change those assertions to `OBSERVED_STATES` or build their harness with `max_conflict_reworks=0`, whichever keeps the test's intent (a test *about* the observer keeps its harness and asserts `OBSERVED_STATES`; a test about not fetching review passes `max_conflict_reworks=0`). Existing tests counting `h.polled` batches are unaffected: `_report_issues` runs before the bounce.

- [ ] **Step 9: Commit**

```bash
git add src/issuebot/orchestrator tests/test_orchestrator.py tests/test_orchestrator_state.py
git commit -F - <<'EOF'
feat: the worker bounces a conflicting review PR to rework

After every fetch the orchestrator walks the review issues and hands each
one whose open pull request GitHub reports CONFLICTING to conflict_rework,
skipping issues it still holds (a review grace, a pending retry). Review is
now fetched whenever the bounce is on, observer or not, and a held worker
still bounces: the hold stops claude, not gh.

<attribution lines>
EOF
```

---

### Task 6: Prompt and documentation

**Files:**
- Modify: `configs/WORKFLOW.md:47` (rework context)
- Modify: `README.md:12-13` (intro), `:149-150` (settings table), `:325-329` (Step 5 "Send it back")
- Modify: `CLAUDE.md:480` (label table), the `issuebot.orchestrator` paragraph (`actions.py` sentence), the `issuebot.github` paragraph (`LinkedPr`)
- Modify: `docs/BLUEPRINT.md:17`
- Test: `tests/test_workflow_default.py`

- [ ] **Step 1: Write the failing prompt test**

In `tests/test_workflow_default.py`, add after `test_follow_up_and_rework_context`:

```python
def test_rework_context_names_both_authors(make_issue: Callable[..., Issue]) -> None:
    """issuebot moves an issue to rework too, on a merge conflict, and the agent must not
    go looking for review comments that do not exist."""
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, dispatched(make_issue, linked_pr=PR), rework=True)
    )
    assert "or issuebot did because the pull request conflicts with the default branch" in text
    assert "`### Issuebot merge conflict` block says which" in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_workflow_default.py -k both_authors -v`
Expected: FAIL on the first assertion.

- [ ] **Step 3: Edit the prompt**

In `configs/WORKFLOW.md`, replace the line

```
- A reviewer moved this issue from `{{ labels.review }}` to `{{ labels.rework }}`: the pull request needs more work.
```

with

```
- A reviewer moved this issue from `{{ labels.review }}` to `{{ labels.rework }}` because the pull request needs more work, or issuebot did because the pull request conflicts with the default branch; the workpad's last `### Issuebot merge conflict` block says which. There may be no review comments in the second case.
```

- [ ] **Step 4: Run the prompt tests**

Run: `uv run pytest tests/test_workflow_default.py -v`
Expected: PASS.

- [ ] **Step 5: Update the docs**

`README.md`:
- Intro (line 12-13): after "labelling it `issuebot/rework` sends it back to the agent with your review comments." add "The worker does that itself when a sibling merge leaves the pull request conflicting, up to `agent.max_conflict_reworks` times."
- Settings table: after the `agent.self_review` row add
  `| \`agent.max_conflict_reworks\` | times the worker may move one issue from \`issuebot/review\` to \`issuebot/rework\` because its PR conflicts with the default branch; \`0\` turns it off | \`3\` |`
- Step 5 "Send it back" bullet: append "You need not do this for a merge conflict: when a sibling PR merges and yours turns `CONFLICTING`, the worker moves the issue to `issuebot/rework` itself and records each bounce in the workpad, up to `agent.max_conflict_reworks` times, after which it leaves a note and waits for you."

`CLAUDE.md`:
- Label table row: `| \`issuebot/rework\` | human, if the PR needs more work; or issuebot, when the PR conflicts with the default branch (bounded by \`agent.max_conflict_reworks\`) |`
- In the `issuebot.github` paragraph, after "frozen `Issue`/`LinkedPr`/`Comment` records (`models.py`)": add "; `LinkedPr.mergeable` is GitHub's `MergeableState` lowercased, `unknown` when absent".
- In the `issuebot.orchestrator` paragraph, after the `finish_terminal` sentence, add: "`conflict_rework` (spec `2026-09-13-conflict-rework-design.md`): a `review` issue whose open PR reads `conflicting` is moved to `rework` by issuebot, label first and then a `### Issuebot merge conflict` workpad block, whose count is the bounce number; at `agent.max_conflict_reworks` (default 3, `0` off) it writes one `... conflict limit` block and stays in `review`. `_bounce_conflicts` runs after every fetch, observer or not (`fetch_states`), skipping issues in `_running` or `_retries`."

`docs/BLUEPRINT.md` line 17: `4. issuebot/rework: set by human if PR needs more work (or by issuebot when the PR conflicts with the default branch)`.

- [ ] **Step 6: Run the whole suite, lint and pre-commit**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add configs/WORKFLOW.md README.md CLAUDE.md docs/BLUEPRINT.md tests/test_workflow_default.py
git commit -F - <<'EOF'
docs: rework is set by a human or by issuebot on a merge conflict

<attribution lines>
EOF
```

---

### Task 7: Push and open the pull request

- [ ] **Step 1: Push the branch**

```bash
git push -u origin issuebot/conflict-rework
```

- [ ] **Step 2: Write the PR body (separate call from the API call, per the hook)**

Write `/tmp/claude-1001/-home-jleavers--dev-issuebot/<session>/scratchpad/pr-conflict-rework.md` with the Write tool: a summary (the problem, the bounce, the cap, the off switch), what it depends on (#83's Step 6), validation (the suite count, ruff, pre-commit; note that CI is blocked by the exhausted Actions minutes so the local run is the evidence), and the two trailer lines from the session reminder.

- [ ] **Step 3: Open it**

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='feat: the worker bounces a conflicting review PR to rework' \
  -f head='issuebot/conflict-rework' -f base='main' \
  -F body=@<the body file> --jq '.number, .html_url'
```

If the call returns "unexpected end of JSON input", check `gh pr list --head issuebot/conflict-rework` before retrying: a 502 can still have created the PR.

- [ ] **Step 4: Confirm the body took**

```bash
gh pr view <number> -R jleavers/issuebot --json body --jq '.body' | head -5
```
