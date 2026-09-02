# Phase 2: GitHub Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read and write GitHub issue state through the `gh` CLI behind an async `GitHubAdapter` protocol, with the label state machine as data, an in-memory `FakeGitHub` for every later phase's tests, and three CLI additions (`labels ensure`, `issues list`, network checks in `validate`).

**Architecture:** A new `issuebot.github` package. `models.py` holds frozen records and the `StateLabel` roles; `state.py` the transition table and pure helpers; `normalise.py` turns GraphQL nodes into `Issue`; `runner.py` is the only place that spawns `gh` (asyncio subprocess); `ghcli.py` implements the protocol with GraphQL reads and `gh issue`/`gh label`/`gh api` writes; `fake.py` implements the same protocol in memory and reuses `normalise.py` so both produce identical `Issue` records. The CLI gets a module-level adapter factory that tests replace with the fake.

**Tech Stack:** Python 3.14, asyncio subprocesses, `gh` CLI (GraphQL via `gh api graphql`), pydantic settings from Phase 1, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff 0.16.5.

**Spec:** `docs/superpowers/specs/2026-09-02-phase-2-github-adapter-design.md` (parent: `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`; foundations: `docs/superpowers/specs/2026-09-02-phase-1-foundations-design.md`).

## Global Constraints

- Python `>=3.14`; `uv run` for everything; `uv.lock` unchanged (no new dependencies).
- Package `issuebot.github` depends only on `issuebot.config` and `issuebot.log`; it never imports `issuebot.events`.
- Every adapter method is `async`; the CLI calls them through `asyncio.run`.
- `StateLabel` values are exactly `todo`, `in_progress`, `review`, `rework`, `complete` and equal the `GitHubLabels` field names.
- `Issue.dispatchable == (github_state == "open" and state is not None)`; two or more state labels give `state = None`.
- Linked PR selection: merged (latest `merged_at`) beats open (highest number) beats closed (highest number).
- Reads use `gh api graphql` with `-f` string variables and manual cursor pagination, one query per role, page size 100, id batches of 50.
- Writes: `set_state` = one `gh issue edit` adding the target and removing the four other names; `clear_state` removes all five.
- Workpad marker: a comment whose first line, stripped, equals `## Issuebot Workpad`.
- Error categories exactly: `auth`, `not_found`, `rate_limited`, `transport`, `status`, `response`, `config`; `retryable` is true only for `rate_limited` and `transport`.
- Child environment for `gh`: parent's plus `GH_PROMPT_DISABLED=1`, `GH_NO_UPDATE_NOTIFIER=1`, `NO_COLOR=1`, `GH_PAGER=cat`, and `GH_TOKEN` only when a token is configured; the token is never logged.
- New setting `github.request_timeout_ms`: int ≥ 1000, default 30000.
- `validate` has twelve checks; the three network checks come right after the `gh` executable check and are `[WARN] …: skipped (gh not found)` when `gh` is absent.
- ruff: `target-version = "py314"`, line length 100, rules `E F I UP B N SIM RUF`; `SIM300` is on (swap operands, never suppress).
- Tests are hermetic: no network, no real `gh`; the CLI tests substitute `issuebot.cli._adapter_factory` and `issuebot.cli._which`.
- Never push to `main`; work on branch `phase-2-github-adapter`; never run `rm -rf`, `git reset --hard`, `git clean -fd` (AGENTS.md). Linux host: Bash, `&&` chaining.
- Commit messages: conventional prefix plus whatever attribution trailer the executing harness requires.
- A Bash-level hook on this host blocks any shell command whose text contains the dot-env filename; write files that mention it with the Write/Edit tools.
- Before every commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`.

---

## File map

| Path | Responsibility | Task |
|---|---|---|
| `src/issuebot/config/settings.py` | `github.request_timeout_ms` | 1 |
| `src/issuebot/github/models.py` | `StateLabel`, `Issue`, `LinkedPr`, `Comment`, `RateLimit`, `RepoInfo`, `AuthStatus`, `LabelEnsured`, `WORKPAD_MARKER` | 1 |
| `src/issuebot/github/state.py` | `Actor`, `TRANSITIONS`, `ACTIVE_STATES`, `TERMINAL_STATES`, `LABEL_STYLES`, helpers | 1 |
| `tests/conftest.py` | `make_issue` fixture | 1 |
| `src/issuebot/github/errors.py` | `GitHubError`, `ErrorCategory` | 2 |
| `src/issuebot/github/normalise.py` | `issue_from_node`, `label_name`, `role_for`, `repo_short_name` | 2 |
| `src/issuebot/github/runner.py` | `GhResult`, `GhRunnerLike`, `GhRunner` | 3 |
| `tests/fakes/gh` | fake `gh` executable for runner tests | 3 |
| `src/issuebot/github/adapter.py` | `GitHubAdapter` protocol | 4 |
| `src/issuebot/github/ghcli.py` | `GhCliAdapter` (reads, error mapping in 4; writes, labels, probes in 5) | 4, 5 |
| `tests/fixtures/gh/*.json` | recorded responses | 4, 5 |
| `src/issuebot/github/fake.py` | `FakeGitHub` | 6 |
| `src/issuebot/github/__init__.py` | re-exports | 1, 2, 3, 4, 6 |
| `src/issuebot/cli.py` | `labels ensure`, `issues list`, validate network checks, `_adapter_factory` | 7 |
| `CLAUDE.md`, Phase 1 spec, roadmap | docs for the new package and setting | 8 |

---

### Task 1: Models, state machine and the timeout setting

**Files:**
- Create: `src/issuebot/github/__init__.py`, `src/issuebot/github/models.py`, `src/issuebot/github/state.py`, `tests/test_github_state.py`
- Modify: `src/issuebot/config/settings.py`, `tests/test_settings.py`, `tests/conftest.py`

**Interfaces:**
- Produces: `StateLabel` (StrEnum), `Issue`, `LinkedPr`, `Comment`, `RateLimit`, `RepoInfo`, `AuthStatus`, `LabelEnsured`, `WORKPAD_MARKER`, `GitHubState`, `PrState`, `LabelOutcome` (models); `Actor`, `ClosedOutcome`, `ACTIVE_STATES`, `TERMINAL_STATES`, `TRANSITIONS`, `LabelStyle`, `LABEL_STYLES`, `is_allowed(current, target, actor) -> bool`, `is_active(state) -> bool`, `is_terminal(state) -> bool`, `next_state_for(issue) -> StateLabel | None`, `classify_closed(issue) -> ClosedOutcome` (state); `GitHubSettings.request_timeout_ms: int`; the `make_issue(**overrides) -> Issue` pytest fixture.

- [ ] **Step 1: Add the setting and its tests**

In `src/issuebot/config/settings.py`, change `GitHubSettings` to:

```python
class GitHubSettings(_Model):
    repo: RepoName
    token: SecretStr | None = None
    labels: GitHubLabels = Field(default_factory=GitHubLabels)
    request_timeout_ms: int = Field(default=30_000, ge=1000)
```

In `tests/test_settings.py`, add to `test_minimal_config_applies_every_default` (after the `labels` assertion):

```python
    assert s.github.request_timeout_ms == 30_000
```

and add `("github", "request_timeout_ms", 999)` to the `test_constraints_reject_out_of_range_values` parametrisation list. Note the parametrised test builds `{**MINIMAL, section: {field: value}}`; for `github` that drops `repo`, so add this dedicated test instead of the parametrisation entry:

```python
def test_request_timeout_lower_bound() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings.model_validate({"github": {"repo": "o/r", "request_timeout_ms": 999}})
    assert "github.request_timeout_ms" in _locs(exc.value)
```

Run: `uv run pytest tests/test_settings.py -v`
Expected: the two touched tests pass (36 total in the file).

- [ ] **Step 2: Write the failing state-machine tests**

`tests/test_github_state.py`:

```python
"""Tests for the label state machine."""

from collections.abc import Callable

import pytest

from issuebot.github.models import Issue, LinkedPr, StateLabel
from issuebot.github.state import (
    ACTIVE_STATES,
    LABEL_STYLES,
    TERMINAL_STATES,
    TRANSITIONS,
    Actor,
    classify_closed,
    is_active,
    is_allowed,
    is_terminal,
    next_state_for,
)
from datetime import UTC, datetime

MERGED = LinkedPr(
    number=51,
    url="https://github.com/example/repo/pull/51",
    state="merged",
    merged_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
)
OPEN = LinkedPr(
    number=52, url="https://github.com/example/repo/pull/52", state="open", merged_at=None
)
CLOSED = LinkedPr(
    number=50, url="https://github.com/example/repo/pull/50", state="closed", merged_at=None
)


def test_state_label_values_match_settings_field_names() -> None:
    assert [s.value for s in StateLabel] == ["todo", "in_progress", "review", "rework", "complete"]


@pytest.mark.parametrize(
    ("current", "target", "actor"),
    [
        (None, StateLabel.TODO, Actor.HUMAN),
        (StateLabel.TODO, StateLabel.IN_PROGRESS, Actor.ISSUEBOT),
        (StateLabel.REWORK, StateLabel.IN_PROGRESS, Actor.ISSUEBOT),
        (StateLabel.IN_PROGRESS, StateLabel.REVIEW, Actor.AGENT),
        (StateLabel.IN_PROGRESS, StateLabel.REVIEW, Actor.ISSUEBOT),
        (StateLabel.REVIEW, StateLabel.REWORK, Actor.HUMAN),
        (StateLabel.REVIEW, StateLabel.TODO, Actor.HUMAN),
        (StateLabel.IN_PROGRESS, StateLabel.TODO, Actor.HUMAN),
        (StateLabel.REVIEW, StateLabel.COMPLETE, Actor.ISSUEBOT),
        (StateLabel.IN_PROGRESS, StateLabel.COMPLETE, Actor.ISSUEBOT),
    ],
)
def test_allowed_transitions(current: StateLabel | None, target: StateLabel, actor: Actor) -> None:
    assert is_allowed(current, target, actor)
    assert (current, target, actor) in TRANSITIONS


@pytest.mark.parametrize(
    ("current", "target", "actor"),
    [
        (StateLabel.TODO, StateLabel.IN_PROGRESS, Actor.HUMAN),
        (StateLabel.TODO, StateLabel.REVIEW, Actor.AGENT),
        (StateLabel.COMPLETE, StateLabel.TODO, Actor.ISSUEBOT),
        (StateLabel.REVIEW, StateLabel.COMPLETE, Actor.AGENT),
        (None, StateLabel.IN_PROGRESS, Actor.ISSUEBOT),
    ],
)
def test_disallowed_transitions(
    current: StateLabel | None, target: StateLabel, actor: Actor
) -> None:
    assert not is_allowed(current, target, actor)


def test_transition_table_size() -> None:
    assert len(TRANSITIONS) == 10


def test_active_and_terminal_partition() -> None:
    assert ACTIVE_STATES == {StateLabel.TODO, StateLabel.REWORK, StateLabel.IN_PROGRESS}
    assert TERMINAL_STATES == {StateLabel.COMPLETE}
    assert is_active(StateLabel.TODO) and is_active(StateLabel.REWORK)
    assert is_active(StateLabel.IN_PROGRESS)
    assert not is_active(StateLabel.REVIEW) and not is_active(StateLabel.COMPLETE)
    assert not is_active(None)
    assert is_terminal(StateLabel.COMPLETE)
    assert not is_terminal(StateLabel.REVIEW) and not is_terminal(None)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (StateLabel.TODO, StateLabel.IN_PROGRESS),
        (StateLabel.REWORK, StateLabel.IN_PROGRESS),
        (StateLabel.IN_PROGRESS, StateLabel.IN_PROGRESS),
        (StateLabel.REVIEW, None),
        (StateLabel.COMPLETE, None),
        (None, None),
    ],
)
def test_next_state_for(
    make_issue: Callable[..., Issue], state: StateLabel | None, expected: StateLabel | None
) -> None:
    assert next_state_for(make_issue(state=state)) == expected


@pytest.mark.parametrize(
    ("pr", "expected"),
    [(MERGED, "complete"), (OPEN, "cancelled"), (CLOSED, "cancelled"), (None, "cancelled")],
)
def test_classify_closed(
    make_issue: Callable[..., Issue], pr: LinkedPr | None, expected: str
) -> None:
    issue = make_issue(github_state="closed", linked_pr=pr, dispatchable=False)
    assert classify_closed(issue) == expected


def test_label_styles_cover_every_role() -> None:
    assert set(LABEL_STYLES) == set(StateLabel)
    for style in LABEL_STYLES.values():
        assert len(style.color) == 6
        int(style.color, 16)
        assert style.description
```

Add the `make_issue` fixture to `tests/conftest.py` (append; keep the existing `clean_env` fixture):

```python
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from issuebot.github.models import Issue, StateLabel


@pytest.fixture
def make_issue() -> Callable[..., Issue]:
    """Build a consistent Issue; pass field overrides as keyword arguments."""

    def factory(**overrides: Any) -> Issue:
        fields: dict[str, Any] = {
            "id": "42",
            "identifier": "repo-42",
            "number": 42,
            "title": "Add retry backoff",
            "body": None,
            "github_state": "open",
            "state": StateLabel.TODO,
            "state_labels": ("issuebot/todo",),
            "labels": ("issuebot/todo",),
            "url": "https://github.com/example/repo/issues/42",
            "assignees": (),
            "created_at": datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
            "updated_at": datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            "closed_at": None,
            "linked_pr": None,
            "dispatchable": True,
        }
        fields.update(overrides)
        return Issue(**fields)

    return factory
```

(Move the new imports to the top of `conftest.py` in the normal order: stdlib, then `pytest`, then `issuebot`.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.github'`.

- [ ] **Step 4: Implement the models**

`src/issuebot/github/models.py`:

```python
"""Normalised GitHub records shared by the adapter, the fake, the orchestrator and the CLI."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

WORKPAD_MARKER = "## Issuebot Workpad"


class StateLabel(StrEnum):
    """The five roles of the label state machine; values equal GitHubLabels field names."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    REWORK = "rework"
    COMPLETE = "complete"


GitHubState = Literal["open", "closed"]
PrState = Literal["open", "closed", "merged"]
LabelOutcome = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True, kw_only=True, slots=True)
class LinkedPr:
    number: int
    url: str
    state: PrState
    merged_at: datetime | None


@dataclass(frozen=True, kw_only=True, slots=True)
class Issue:
    """One tracked issue, normalised as in the Symphony spec (§4.1.1, §11.3)."""

    id: str
    identifier: str
    number: int
    title: str
    body: str | None
    github_state: GitHubState
    state: StateLabel | None
    state_labels: tuple[str, ...]
    labels: tuple[str, ...]
    url: str
    assignees: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    linked_pr: LinkedPr | None
    dispatchable: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class Comment:
    id: int
    body: str
    url: str
    author: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class RateLimit:
    limit: int
    remaining: int
    used: int
    reset_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class RepoInfo:
    full_name: str
    default_branch: str
    private: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class AuthStatus:
    login: str


@dataclass(frozen=True, kw_only=True, slots=True)
class LabelEnsured:
    name: str
    outcome: LabelOutcome
```

- [ ] **Step 5: Implement the state machine**

`src/issuebot/github/state.py`:

```python
"""The label state machine as data, plus pure helpers over it. No I/O."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from issuebot.github.models import Issue, StateLabel


class Actor(StrEnum):
    HUMAN = "human"
    ISSUEBOT = "issuebot"
    AGENT = "agent"


ClosedOutcome = Literal["complete", "cancelled"]

ACTIVE_STATES: frozenset[StateLabel] = frozenset(
    {StateLabel.TODO, StateLabel.REWORK, StateLabel.IN_PROGRESS}
)
TERMINAL_STATES: frozenset[StateLabel] = frozenset({StateLabel.COMPLETE})

TRANSITIONS: frozenset[tuple[StateLabel | None, StateLabel, Actor]] = frozenset(
    {
        (None, StateLabel.TODO, Actor.HUMAN),
        (StateLabel.TODO, StateLabel.IN_PROGRESS, Actor.ISSUEBOT),
        (StateLabel.REWORK, StateLabel.IN_PROGRESS, Actor.ISSUEBOT),
        (StateLabel.IN_PROGRESS, StateLabel.REVIEW, Actor.AGENT),
        (StateLabel.IN_PROGRESS, StateLabel.REVIEW, Actor.ISSUEBOT),
        (StateLabel.REVIEW, StateLabel.REWORK, Actor.HUMAN),
        (StateLabel.REVIEW, StateLabel.TODO, Actor.HUMAN),
        (StateLabel.IN_PROGRESS, StateLabel.TODO, Actor.HUMAN),
        (StateLabel.REVIEW, StateLabel.COMPLETE, Actor.ISSUEBOT),
        (StateLabel.IN_PROGRESS, StateLabel.COMPLETE, Actor.ISSUEBOT),
    }
)


@dataclass(frozen=True, slots=True)
class LabelStyle:
    color: str
    description: str


LABEL_STYLES: dict[StateLabel, LabelStyle] = {
    StateLabel.TODO: LabelStyle("0E8A16", "Queued for issuebot; a human sets this"),
    StateLabel.IN_PROGRESS: LabelStyle("FBCA04", "An issuebot agent is working on it"),
    StateLabel.REVIEW: LabelStyle("1D76DB", "PR opened; waiting for human review"),
    StateLabel.REWORK: LabelStyle("D93F0B", "Reviewer wants changes; issuebot will pick it up"),
    StateLabel.COMPLETE: LabelStyle("5319E7", "Closed by a merged issuebot PR"),
}


def is_allowed(current: StateLabel | None, target: StateLabel, actor: Actor) -> bool:
    return (current, target, actor) in TRANSITIONS


def is_active(state: StateLabel | None) -> bool:
    return state in ACTIVE_STATES


def is_terminal(state: StateLabel | None) -> bool:
    return state in TERMINAL_STATES


def next_state_for(issue: Issue) -> StateLabel | None:
    """The label the orchestrator sets when it dispatches this issue, or None if it must not."""
    if issue.state in ACTIVE_STATES:
        return StateLabel.IN_PROGRESS
    return None


def classify_closed(issue: Issue) -> ClosedOutcome:
    """A closed issue is complete only when a linked pull request was merged."""
    pr = issue.linked_pr
    return "complete" if pr is not None and pr.state == "merged" else "cancelled"
```

`src/issuebot/github/__init__.py` (first version; later tasks extend it):

```python
"""GitHub integration: normalised issue model, the label state machine and adapters."""

from issuebot.github.models import (
    WORKPAD_MARKER,
    AuthStatus,
    Comment,
    Issue,
    LabelEnsured,
    LinkedPr,
    RateLimit,
    RepoInfo,
    StateLabel,
)
from issuebot.github.state import (
    ACTIVE_STATES,
    LABEL_STYLES,
    TERMINAL_STATES,
    TRANSITIONS,
    Actor,
    LabelStyle,
    classify_closed,
    is_active,
    is_allowed,
    is_terminal,
    next_state_for,
)

__all__ = [
    "ACTIVE_STATES",
    "LABEL_STYLES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "WORKPAD_MARKER",
    "Actor",
    "AuthStatus",
    "Comment",
    "Issue",
    "LabelEnsured",
    "LabelStyle",
    "LinkedPr",
    "RateLimit",
    "RepoInfo",
    "StateLabel",
    "classify_closed",
    "is_active",
    "is_allowed",
    "is_terminal",
    "next_state_for",
]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_state.py tests/test_settings.py -v`
Expected: all pass (state file: 29 tests including parametrised cases).

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/github src/issuebot/config/settings.py tests/conftest.py tests/test_settings.py tests/test_github_state.py
git commit -m "feat: add github models, label state machine and request_timeout_ms setting"
```

---

### Task 2: Errors and GraphQL normalisation

**Files:**
- Create: `src/issuebot/github/errors.py`, `src/issuebot/github/normalise.py`, `tests/test_github_normalise.py`
- Modify: `src/issuebot/github/__init__.py`

**Interfaces:**
- Consumes: `Issue`, `LinkedPr`, `StateLabel` (Task 1); `GitHubLabels` from `issuebot.config`.
- Produces: `ErrorCategory`, `GitHubError(category, message, *, exit_code=None, stderr=None)` with `.category`, `.message`, `.exit_code`, `.stderr`, `.retryable`; `label_name(labels, role) -> str`, `role_for(labels, name) -> StateLabel | None`, `repo_short_name(repo) -> str`, `issue_from_node(node, *, repo, labels) -> Issue`.

- [ ] **Step 1: Write the failing tests**

`tests/test_github_normalise.py`:

```python
"""Tests for GitHubError and GraphQL issue normalisation."""

from datetime import UTC, datetime
from typing import Any

import pytest

from issuebot.config import GitHubLabels
from issuebot.github.errors import GitHubError
from issuebot.github.models import StateLabel
from issuebot.github.normalise import issue_from_node, label_name, repo_short_name, role_for

LABELS = GitHubLabels()
REPO = "example/repo"


def node(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 42,
        "title": "Add retry backoff",
        "body": "We need exponential backoff.",
        "state": "OPEN",
        "url": "https://github.com/example/repo/issues/42",
        "createdAt": "2026-09-01T09:00:00Z",
        "updatedAt": "2026-09-02T10:11:12Z",
        "closedAt": None,
        "labels": {"nodes": [{"name": "Bug"}, {"name": "issuebot/in-progress"}, {"name": "bug"}]},
        "assignees": {"nodes": [{"login": "jleavers"}]},
        "closedByPullRequestsReferences": {"nodes": []},
    }
    base.update(overrides)
    return base


def pr(number: int, state: str, merged_at: str | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "url": f"https://github.com/example/repo/pull/{number}",
        "state": state,
        "mergedAt": merged_at,
    }


# --- errors ----------------------------------------------------------------------


def test_error_str_and_retryable() -> None:
    err = GitHubError("rate_limited", "slow down", exit_code=1, stderr="x" * 600)
    assert str(err) == "rate_limited: slow down"
    assert err.category == "rate_limited"
    assert err.message == "slow down"
    assert err.exit_code == 1
    assert err.stderr is not None and len(err.stderr) == 500
    assert err.retryable
    assert GitHubError("transport", "down").retryable
    for category in ("auth", "not_found", "status", "response", "config"):
        assert not GitHubError(category, "x").retryable  # type: ignore[arg-type]


# --- label helpers -----------------------------------------------------------------


def test_label_name_and_role_for() -> None:
    assert label_name(LABELS, StateLabel.IN_PROGRESS) == "issuebot/in-progress"
    assert role_for(LABELS, "issuebot/in-progress") is StateLabel.IN_PROGRESS
    assert role_for(LABELS, "  ISSUEBOT/Review ") is StateLabel.REVIEW
    assert role_for(LABELS, "bug") is None
    custom = GitHubLabels(todo="queue", complete="done")
    assert label_name(custom, StateLabel.TODO) == "queue"
    assert role_for(custom, "Done") is StateLabel.COMPLETE


def test_repo_short_name() -> None:
    assert repo_short_name("jleavers/issuebot") == "issuebot"


# --- issue_from_node ---------------------------------------------------------------


def test_full_record() -> None:
    issue = issue_from_node(node(), repo=REPO, labels=LABELS)
    assert issue.id == "42"
    assert issue.identifier == "repo-42"
    assert issue.number == 42
    assert issue.title == "Add retry backoff"
    assert issue.body == "We need exponential backoff."
    assert issue.github_state == "open"
    assert issue.state is StateLabel.IN_PROGRESS
    assert issue.state_labels == ("issuebot/in-progress",)
    assert issue.labels == ("bug", "issuebot/in-progress")
    assert issue.url == "https://github.com/example/repo/issues/42"
    assert issue.assignees == ("jleavers",)
    assert issue.created_at == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert issue.updated_at == datetime(2026, 9, 2, 10, 11, 12, tzinfo=UTC)
    assert issue.closed_at is None
    assert issue.linked_pr is None
    assert issue.dispatchable


def test_minimal_closed_record() -> None:
    issue = issue_from_node(
        node(
            body=None,
            state="CLOSED",
            closedAt="2026-09-03T00:00:00Z",
            labels={"nodes": []},
            assignees={"nodes": []},
        ),
        repo=REPO,
        labels=LABELS,
    )
    assert issue.body is None
    assert issue.github_state == "closed"
    assert issue.closed_at == datetime(2026, 9, 3, tzinfo=UTC)
    assert issue.state is None
    assert issue.state_labels == ()
    assert issue.labels == ()
    assert issue.assignees == ()
    assert not issue.dispatchable


def test_empty_body_is_none() -> None:
    assert issue_from_node(node(body=""), repo=REPO, labels=LABELS).body is None


def test_two_state_labels_is_a_conflict() -> None:
    issue = issue_from_node(
        node(labels={"nodes": [{"name": "issuebot/review"}, {"name": "issuebot/todo"}]}),
        repo=REPO,
        labels=LABELS,
    )
    assert issue.state is None
    assert issue.state_labels == ("issuebot/todo", "issuebot/review")  # role order
    assert not issue.dispatchable


def test_closed_issue_with_state_label_is_not_dispatchable() -> None:
    issue = issue_from_node(node(state="CLOSED"), repo=REPO, labels=LABELS)
    assert issue.state is StateLabel.IN_PROGRESS
    assert not issue.dispatchable


def test_custom_label_names_are_recognised() -> None:
    custom = GitHubLabels(in_progress="wip")
    issue = issue_from_node(node(labels={"nodes": [{"name": "WIP"}]}), repo=REPO, labels=custom)
    assert issue.state is StateLabel.IN_PROGRESS
    assert issue.state_labels == ("wip",)


def test_linked_pr_prefers_merged_then_open_then_closed() -> None:
    refs = {
        "nodes": [
            pr(50, "CLOSED"),
            pr(52, "OPEN"),
            pr(51, "MERGED", "2026-09-02T12:00:00Z"),
            pr(48, "MERGED", "2026-09-01T12:00:00Z"),
        ]
    }
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None
    assert issue.linked_pr.number == 51
    assert issue.linked_pr.state == "merged"
    assert issue.linked_pr.merged_at == datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    refs = {"nodes": [pr(50, "CLOSED"), pr(53, "OPEN"), pr(52, "OPEN")]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None and issue.linked_pr.number == 53
    assert issue.linked_pr.state == "open" and issue.linked_pr.merged_at is None

    refs = {"nodes": [pr(50, "CLOSED"), pr(49, "CLOSED")]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None and issue.linked_pr.number == 50


def test_unusable_pr_reference_is_skipped() -> None:
    refs = {"nodes": [{"number": "x"}, None, pr(52, "OPEN")]}
    issue = issue_from_node(node(closedByPullRequestsReferences=refs), repo=REPO, labels=LABELS)
    assert issue.linked_pr is not None and issue.linked_pr.number == 52


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("number", None),
        ("number", "42"),
        ("number", True),
        ("title", ""),
        ("state", "WEIRD"),
        ("url", None),
        ("createdAt", None),
        ("updatedAt", "not a date"),
    ],
)
def test_malformed_record_raises_response_error(field: str, value: Any) -> None:
    with pytest.raises(GitHubError) as exc:
        issue_from_node(node(**{field: value}), repo=REPO, labels=LABELS)
    assert exc.value.category == "response"
    assert field in exc.value.message


def test_unusable_optional_metadata_normalises_quietly() -> None:
    issue = issue_from_node(
        node(labels="nope", assignees={"nodes": [None, {"login": ""}]}, closedAt="garbage"),
        repo=REPO,
        labels=LABELS,
    )
    assert issue.labels == ()
    assert issue.assignees == ()
    assert issue.closed_at is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_normalise.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.github.errors'`.

- [ ] **Step 3: Implement the errors module**

`src/issuebot/github/errors.py`:

```python
"""One error type for every GitHub failure, with a stable category for logs and retries."""

from typing import Literal

ErrorCategory = Literal[
    "auth", "not_found", "rate_limited", "transport", "status", "response", "config"
]

RETRYABLE_CATEGORIES: frozenset[str] = frozenset({"rate_limited", "transport"})
_STDERR_LIMIT = 500


class GitHubError(Exception):
    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        exit_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(f"{category}: {message}")
        self.category: ErrorCategory = category
        self.message = message
        self.exit_code = exit_code
        self.stderr = stderr[:_STDERR_LIMIT] if stderr else None

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES
```

- [ ] **Step 4: Implement normalisation**

`src/issuebot/github/normalise.py`:

```python
"""Turn GraphQL issue nodes into Issue records; shared by the gh adapter and the fake."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from issuebot.config import GitHubLabels
from issuebot.github.errors import GitHubError
from issuebot.github.models import Issue, LinkedPr, PrState, StateLabel

_PR_STATES: dict[str, PrState] = {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}
_PR_RANK: dict[PrState, int] = {"merged": 0, "open": 1, "closed": 2}


def label_name(labels: GitHubLabels, role: StateLabel) -> str:
    """The configured label name for a role."""
    return getattr(labels, role.value)


def role_for(labels: GitHubLabels, name: str) -> StateLabel | None:
    """The role whose configured name matches ``name`` (case-insensitive, trimmed), if any."""
    wanted = name.strip().lower()
    for role in StateLabel:
        if label_name(labels, role).lower() == wanted:
            return role
    return None


def repo_short_name(repo: str) -> str:
    return repo.split("/", 1)[1]


def issue_from_node(node: Mapping[str, Any], *, repo: str, labels: GitHubLabels) -> Issue:
    """Normalise one ``IssueFields`` node. Raises ``GitHubError("response")`` when malformed."""
    number = node.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise GitHubError("response", "malformed issue record: missing number")
    title = node.get("title")
    if not isinstance(title, str) or not title:
        raise GitHubError("response", f"malformed issue record #{number}: missing title")
    raw_state = node.get("state")
    if raw_state not in ("OPEN", "CLOSED"):
        raise GitHubError("response", f"malformed issue record #{number}: missing state")
    url = node.get("url")
    if not isinstance(url, str) or not url:
        raise GitHubError("response", f"malformed issue record #{number}: missing url")
    created_at = _required_timestamp(node.get("createdAt"), number, "createdAt")
    updated_at = _required_timestamp(node.get("updatedAt"), number, "updatedAt")
    closed_at = _optional_timestamp(node.get("closedAt"))

    body = node.get("body")
    all_labels = _label_names(node.get("labels"))
    state_labels = tuple(
        name
        for role in StateLabel
        for name in all_labels
        if name == label_name(labels, role).lower()
    )
    state = role_for(labels, state_labels[0]) if len(state_labels) == 1 else None
    github_state = "open" if raw_state == "OPEN" else "closed"

    return Issue(
        id=str(number),
        identifier=f"{repo_short_name(repo)}-{number}",
        number=number,
        title=title,
        body=body if isinstance(body, str) and body else None,
        github_state=github_state,
        state=state,
        state_labels=state_labels,
        labels=all_labels,
        url=url,
        assignees=_logins(node.get("assignees")),
        created_at=created_at,
        updated_at=updated_at,
        closed_at=closed_at,
        linked_pr=_select_pr(node.get("closedByPullRequestsReferences")),
        dispatchable=github_state == "open" and state is not None,
    )


def _required_timestamp(value: Any, number: int, field: str) -> datetime:
    parsed = _optional_timestamp(value)
    if parsed is None:
        raise GitHubError("response", f"malformed issue record #{number}: missing {field}")
    return parsed


def _optional_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _nodes(connection: Any) -> list[Any]:
    if not isinstance(connection, Mapping):
        return []
    nodes = connection.get("nodes")
    return list(nodes) if isinstance(nodes, list) else []


def _label_names(connection: Any) -> tuple[str, ...]:
    seen: list[str] = []
    for item in _nodes(connection):
        name = item.get("name") if isinstance(item, Mapping) else None
        if isinstance(name, str) and name.strip():
            lowered = name.strip().lower()
            if lowered not in seen:
                seen.append(lowered)
    return tuple(seen)


def _logins(connection: Any) -> tuple[str, ...]:
    logins: list[str] = []
    for item in _nodes(connection):
        login = item.get("login") if isinstance(item, Mapping) else None
        if isinstance(login, str) and login and login not in logins:
            logins.append(login)
    return tuple(logins)


def _select_pr(connection: Any) -> LinkedPr | None:
    candidates: list[LinkedPr] = []
    for item in _nodes(connection):
        if not isinstance(item, Mapping):
            continue
        number = item.get("number")
        url = item.get("url")
        raw_state = item.get("state")
        state = _PR_STATES.get(raw_state) if isinstance(raw_state, str) else None
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        if not isinstance(url, str) or state is None:
            continue
        candidates.append(
            LinkedPr(
                number=number,
                url=url,
                state=state,
                merged_at=_optional_timestamp(item.get("mergedAt")),
            )
        )
    if not candidates:
        return None

    def rank(candidate: LinkedPr) -> tuple[int, float, int]:
        merged = candidate.merged_at.timestamp() if candidate.merged_at else 0.0
        return (_PR_RANK[candidate.state], -merged, -candidate.number)

    return min(candidates, key=rank)
```

Extend `src/issuebot/github/__init__.py`: add `from issuebot.github.errors import ErrorCategory, GitHubError` and `from issuebot.github.normalise import issue_from_node, label_name, repo_short_name, role_for`, and add `"ErrorCategory"`, `"GitHubError"`, `"issue_from_node"`, `"label_name"`, `"repo_short_name"`, `"role_for"` to `__all__` (keep it sorted the way ruff's `RUF022` wants: uppercase constants first, then classes, then functions, each group alphabetical).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_normalise.py -v`
Expected: 20 passed (including parametrised cases).

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/github tests/test_github_normalise.py
git commit -m "feat: add GitHubError and GraphQL issue normalisation"
```

---

### Task 3: The `gh` runner

**Files:**
- Create: `src/issuebot/github/runner.py`, `tests/fakes/gh` (executable), `tests/test_github_runner.py`
- Modify: `src/issuebot/github/__init__.py`

**Interfaces:**
- Consumes: `GitHubError` (Task 2); `get_logger` from `issuebot.log`.
- Produces: `GhResult(returncode: int, stdout: str, stderr: str)`; `GhRunnerLike` protocol with `async run(args: Sequence[str], *, stdin: str | None = None) -> GhResult`; `GhRunner(*, command="gh", token: SecretStr | None = None, timeout_ms: int = 30_000, environ: Mapping[str, str] | None = None)` with `child_environment() -> dict[str, str]` and `run(...)`.

- [ ] **Step 1: Create the fake `gh` executable**

`tests/fakes/gh` (no extension; make it executable with `chmod +x tests/fakes/gh` and confirm `git add` records mode 100755):

```python
#!/usr/bin/env python3
"""Fake `gh` for runner tests: echoes argv, stdin and selected env as JSON, or misbehaves.

Scenario comes from FAKE_GH_SCENARIO: echo (default), fail (exit 1 with stderr), sleep.
"""

import json
import os
import sys
import time

scenario = os.environ.get("FAKE_GH_SCENARIO", "echo")
if scenario == "sleep":
    time.sleep(10)
if scenario == "fail":
    sys.stderr.write("gh: Not Found (HTTP 404)\n")
    sys.exit(1)

payload = {
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "env": {
        key: os.environ.get(key)
        for key in (
            "GH_TOKEN",
            "GH_PROMPT_DISABLED",
            "GH_NO_UPDATE_NOTIFIER",
            "NO_COLOR",
            "GH_PAGER",
        )
    },
}
print(json.dumps(payload))
```

- [ ] **Step 2: Write the failing tests**

`tests/test_github_runner.py`:

```python
"""Tests for the gh subprocess boundary, against tests/fakes/gh."""

import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from issuebot.github.errors import GitHubError
from issuebot.github.runner import GhResult, GhRunner

FAKE_GH = Path(__file__).parent / "fakes" / "gh"


def _runner(**kwargs: object) -> GhRunner:
    environ = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    environ.update(kwargs.pop("extra_env", {}))  # type: ignore[arg-type]
    return GhRunner(command=str(FAKE_GH), environ=environ, **kwargs)  # type: ignore[arg-type]


async def test_run_passes_argv_and_captures_output() -> None:
    result = await _runner().run(["api", "user", "--jq", ".login"])
    assert isinstance(result, GhResult)
    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["argv"] == ["api", "user", "--jq", ".login"]
    assert payload["stdin"] == ""


async def test_run_writes_stdin() -> None:
    result = await _runner().run(["api", "--input", "-"], stdin='{"body": "hi"}')
    assert json.loads(result.stdout)["stdin"] == '{"body": "hi"}'


async def test_child_environment_sets_fixed_variables_and_token() -> None:
    runner = _runner(token=SecretStr("sekret"))
    env = runner.child_environment()
    assert env["GH_TOKEN"] == "sekret"
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert env["NO_COLOR"] == "1"
    assert env["GH_PAGER"] == "cat"
    result = await runner.run(["x"])
    assert json.loads(result.stdout)["env"]["GH_TOKEN"] == "sekret"


async def test_child_environment_without_token_has_no_gh_token() -> None:
    result = await _runner().run(["x"])
    env = json.loads(result.stdout)["env"]
    assert env["GH_TOKEN"] is None
    assert env["GH_PROMPT_DISABLED"] == "1"


async def test_non_zero_exit_is_returned_not_raised() -> None:
    result = await _runner(extra_env={"FAKE_GH_SCENARIO": "fail"}).run(["api", "x"])
    assert result.returncode == 1
    assert result.stdout == ""
    assert "HTTP 404" in result.stderr


async def test_timeout_kills_and_raises_transport() -> None:
    runner = _runner(timeout_ms=1000, extra_env={"FAKE_GH_SCENARIO": "sleep"})
    with pytest.raises(GitHubError) as exc:
        await runner.run(["api", "slow"])
    assert exc.value.category == "transport"
    assert exc.value.retryable
    assert "timed out" in exc.value.message


async def test_missing_executable_raises_config() -> None:
    runner = GhRunner(command="/nonexistent/gh", environ={"PATH": "/nonexistent"})
    with pytest.raises(GitHubError) as exc:
        await runner.run(["--version"])
    assert exc.value.category == "config"
    assert not exc.value.retryable
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.github.runner'`.

- [ ] **Step 4: Implement the runner**

`src/issuebot/github/runner.py`:

```python
"""The only place that spawns the gh CLI."""

import asyncio
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

from issuebot.github.errors import GitHubError
from issuebot.log import get_logger

_FIXED_ENVIRONMENT = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "GH_PAGER": "cat",
}
_LOGGED_ARG_LENGTH = 120


@dataclass(frozen=True, slots=True)
class GhResult:
    returncode: int
    stdout: str
    stderr: str


class GhRunnerLike(Protocol):
    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult: ...


class GhRunner:
    """Runs ``gh`` as an asyncio subprocess with a controlled environment and a timeout."""

    def __init__(
        self,
        *,
        command: str = "gh",
        token: SecretStr | None = None,
        timeout_ms: int = 30_000,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._command = command
        self._token = token
        self._timeout_s = timeout_ms / 1000
        self._environ = dict(os.environ if environ is None else environ)
        self._log = get_logger(__name__)

    def child_environment(self) -> dict[str, str]:
        env = dict(self._environ)
        env.update(_FIXED_ENVIRONMENT)
        if self._token is not None:
            env["GH_TOKEN"] = self._token.get_secret_value()
        return env

    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult:
        argv = [self._command, *args]
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.child_environment(),
            )
        except FileNotFoundError as exc:
            raise GitHubError("config", f"{self._command!r} not found on PATH") from exc

        payload = stdin.encode("utf-8") if stdin is not None else None
        try:
            out, err = await asyncio.wait_for(process.communicate(payload), timeout=self._timeout_s)
        except TimeoutError:
            process.kill()
            await process.wait()
            summary = " ".join(args[:3])
            raise GitHubError(
                "transport", f"gh timed out after {self._timeout_s:.0f}s: {summary}"
            ) from None

        result = GhResult(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )
        self._log.debug(
            "gh_invocation",
            argv=[arg[:_LOGGED_ARG_LENGTH] for arg in argv],
            exit_code=result.returncode,
            duration_ms=round((time.monotonic() - started) * 1000),
            stdout_bytes=len(out),
            stderr_bytes=len(err),
        )
        return result
```

Extend `src/issuebot/github/__init__.py` with `from issuebot.github.runner import GhResult, GhRunner, GhRunnerLike` and the three names in `__all__`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_runner.py -v`
Expected: 7 passed. The timeout test takes about one second.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/github tests/fakes/gh tests/test_github_runner.py
git commit -m "feat: add GhRunner subprocess boundary for gh"
```

Check `git show --stat HEAD` lists `tests/fakes/gh` as mode `100755`; if not, `chmod +x tests/fakes/gh && git add tests/fakes/gh && git commit --amend --no-edit`.

---

### Task 4: Adapter protocol and `GhCliAdapter` reads

**Files:**
- Create: `src/issuebot/github/adapter.py`, `src/issuebot/github/ghcli.py`, `tests/test_github_ghcli.py`, `tests/fixtures/gh/list_todo_page1.json`, `tests/fixtures/gh/list_todo_page2.json`, `tests/fixtures/gh/list_in_progress.json`, `tests/fixtures/gh/by_ids.json`
- Modify: `src/issuebot/github/__init__.py`

**Interfaces:**
- Consumes: models, `GitHubError`, `issue_from_node`, `label_name`, `GhRunner`, `GhRunnerLike`, `GhResult`, `LABEL_STYLES`.
- Produces: `GitHubAdapter` protocol (all methods of spec §6.1 plus `missing_labels() -> list[str]`); `GhCliAdapter(settings: GitHubSettings, *, runner: GhRunnerLike | None = None)` with properties `repo`, `labels`, and this task's methods `fetch_issues_by_states`, `fetch_issues_by_ids`, `fetch_terminal_issues`; module constants `PAGE_SIZE = 100`, `ID_BATCH_SIZE = 50`, `ISSUE_FIELDS`, `OPEN_ISSUES_QUERY`, `CLOSED_ISSUES_QUERY`, `by_ids_query(numbers)`; private plumbing `_graphql`, `_gh`, `_error_for` that Task 5 reuses. Task 5 adds the write methods to the same class.

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/gh/list_todo_page1.json`:

```json
{
  "data": {
    "repository": {
      "issues": {
        "nodes": [
          {
            "number": 42,
            "title": "Add retry backoff",
            "body": "We need exponential backoff.",
            "state": "OPEN",
            "url": "https://github.com/example/repo/issues/42",
            "createdAt": "2026-09-01T09:00:00Z",
            "updatedAt": "2026-09-02T10:11:12Z",
            "closedAt": null,
            "labels": {"nodes": [{"name": "bug"}, {"name": "issuebot/todo"}]},
            "assignees": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": []}
          },
          {
            "number": 43,
            "title": "Fix label parsing",
            "body": null,
            "state": "OPEN",
            "url": "https://github.com/example/repo/issues/43",
            "createdAt": "2026-09-01T10:00:00Z",
            "updatedAt": "2026-09-02T09:00:00Z",
            "closedAt": null,
            "labels": {"nodes": [{"name": "issuebot/todo"}, {"name": "issuebot/in-progress"}]},
            "assignees": {"nodes": [{"login": "jleavers"}]},
            "closedByPullRequestsReferences": {
              "nodes": [
                {
                  "number": 51,
                  "url": "https://github.com/example/repo/pull/51",
                  "state": "OPEN",
                  "mergedAt": null
                }
              ]
            }
          }
        ],
        "pageInfo": {"hasNextPage": true, "endCursor": "Y3Vyc29yOjI="}
      }
    }
  }
}
```

`tests/fixtures/gh/list_todo_page2.json`:

```json
{
  "data": {
    "repository": {
      "issues": {
        "nodes": [
          {
            "number": 44,
            "title": "Write docs",
            "body": "",
            "state": "OPEN",
            "url": "https://github.com/example/repo/issues/44",
            "createdAt": "2026-09-01T11:00:00Z",
            "updatedAt": "2026-09-01T11:00:00Z",
            "closedAt": null,
            "labels": {"nodes": [{"name": "issuebot/todo"}]},
            "assignees": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": []}
          },
          {
            "number": 45,
            "state": "OPEN",
            "url": "https://github.com/example/repo/issues/45",
            "createdAt": "2026-09-01T12:00:00Z",
            "updatedAt": "2026-09-01T12:00:00Z",
            "labels": {"nodes": [{"name": "issuebot/todo"}]},
            "assignees": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": []}
          }
        ],
        "pageInfo": {"hasNextPage": false, "endCursor": null}
      }
    }
  }
}
```

(Issue 45 has no `title`: it is the malformed record the list read must skip.)

`tests/fixtures/gh/list_in_progress.json`:

```json
{
  "data": {
    "repository": {
      "issues": {
        "nodes": [
          {
            "number": 43,
            "title": "Fix label parsing",
            "body": null,
            "state": "OPEN",
            "url": "https://github.com/example/repo/issues/43",
            "createdAt": "2026-09-01T10:00:00Z",
            "updatedAt": "2026-09-02T09:00:00Z",
            "closedAt": null,
            "labels": {"nodes": [{"name": "issuebot/todo"}, {"name": "issuebot/in-progress"}]},
            "assignees": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": []}
          },
          {
            "number": 40,
            "title": "Older in-progress issue",
            "body": null,
            "state": "OPEN",
            "url": "https://github.com/example/repo/issues/40",
            "createdAt": "2026-08-30T08:00:00Z",
            "updatedAt": "2026-09-02T08:00:00Z",
            "closedAt": null,
            "labels": {"nodes": [{"name": "issuebot/in-progress"}]},
            "assignees": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": []}
          }
        ],
        "pageInfo": {"hasNextPage": false, "endCursor": null}
      }
    }
  }
}
```

`tests/fixtures/gh/by_ids.json` (what `gh` prints, with exit code 1, when one alias is not found):

```json
{
  "data": {
    "repository": {
      "i7": {
        "number": 7,
        "title": "Seven",
        "body": null,
        "state": "CLOSED",
        "url": "https://github.com/example/repo/issues/7",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-02T00:00:00Z",
        "closedAt": "2026-08-02T00:00:00Z",
        "labels": {"nodes": [{"name": "issuebot/complete"}]},
        "assignees": {"nodes": []},
        "closedByPullRequestsReferences": {
          "nodes": [
            {
              "number": 8,
              "url": "https://github.com/example/repo/pull/8",
              "state": "MERGED",
              "mergedAt": "2026-08-02T00:00:00Z"
            }
          ]
        }
      },
      "i42": {
        "number": 42,
        "title": "Add retry backoff",
        "body": "We need exponential backoff.",
        "state": "OPEN",
        "url": "https://github.com/example/repo/issues/42",
        "createdAt": "2026-09-01T09:00:00Z",
        "updatedAt": "2026-09-02T10:11:12Z",
        "closedAt": null,
        "labels": {"nodes": [{"name": "issuebot/in-progress"}]},
        "assignees": {"nodes": []},
        "closedByPullRequestsReferences": {"nodes": []}
      },
      "i9999": null
    }
  },
  "errors": [
    {
      "type": "NOT_FOUND",
      "path": ["repository", "i9999"],
      "locations": [{"line": 5, "column": 5}],
      "message": "Could not resolve to an Issue with the number of 9999."
    }
  ]
}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_github_ghcli.py`:

```python
"""Tests for GhCliAdapter against a stub runner and recorded gh output."""

import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import SecretStr

from issuebot.config import GitHubSettings
from issuebot.github.errors import GitHubError
from issuebot.github.ghcli import ID_BATCH_SIZE, GhCliAdapter, by_ids_query
from issuebot.github.models import StateLabel
from issuebot.github.runner import GhResult
from issuebot.log import configure_logging

FIXTURES = Path(__file__).parent / "fixtures" / "gh"
Predicate = Callable[[list[str]], bool]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def has(*needles: str) -> Predicate:
    """True when every needle appears inside some argv element."""
    return lambda argv: all(any(needle in arg for arg in argv) for needle in needles)


def lacks(needle: str) -> Predicate:
    return lambda argv: not any(needle in arg for arg in argv)


def both(*predicates: Predicate) -> Predicate:
    return lambda argv: all(p(argv) for p in predicates)


class StubRunner:
    """Answers gh invocations from canned results and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self._responses: list[tuple[Predicate, GhResult]] = []

    def on(
        self, predicate: Predicate, *, stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> None:
        self._responses.append(
            (predicate, GhResult(returncode=returncode, stdout=stdout, stderr=stderr))
        )

    async def run(self, args: list[str], *, stdin: str | None = None) -> GhResult:
        argv = list(args)
        self.calls.append((argv, stdin))
        for predicate, result in self._responses:
            if predicate(argv):
                return result
        raise AssertionError(f"unexpected gh call: {argv[:4]}")

    def argv(self, index: int) -> list[str]:
        return self.calls[index][0]


def make_adapter(runner: StubRunner, **overrides: object) -> GhCliAdapter:
    settings = GitHubSettings(repo="example/repo", **overrides)  # type: ignore[arg-type]
    return GhCliAdapter(settings, runner=runner)


def query_of(argv: list[str]) -> str:
    return next(arg for arg in argv if arg.startswith("query=")).removeprefix("query=")


# --- reads: by state ---------------------------------------------------------------


async def test_fetch_by_states_paginates_merges_and_sorts() -> None:
    runner = StubRunner()
    runner.on(
        both(has("label=issuebot/todo"), lacks("cursor=")), stdout=fixture("list_todo_page1.json")
    )
    runner.on(
        both(has("label=issuebot/todo"), has("cursor=Y3Vyc29yOjI=")),
        stdout=fixture("list_todo_page2.json"),
    )
    runner.on(has("label=issuebot/in-progress"), stdout=fixture("list_in_progress.json"))
    adapter = make_adapter(runner)

    issues = await adapter.fetch_issues_by_states([StateLabel.TODO, StateLabel.IN_PROGRESS])

    assert [issue.number for issue in issues] == [40, 42, 43, 44]
    assert len(runner.calls) == 3
    first = runner.argv(0)
    assert first[:3] == ["api", "graphql", "-f"]
    assert "states: [OPEN]" in query_of(first)
    assert "first: 100" in query_of(first)
    assert "-f" in first and "owner=example" in first and "name=repo" in first
    assert not any(arg.startswith("cursor=") for arg in first)
    assert "cursor=Y3Vyc29yOjI=" in runner.argv(1)
    by_number = {issue.number: issue for issue in issues}
    assert by_number[43].state is None  # todo + in-progress from the second query is a conflict
    assert by_number[43].linked_pr is not None and by_number[43].linked_pr.number == 51
    assert by_number[42].identifier == "repo-42"


async def test_fetch_by_states_deduplicates_roles_and_skips_empty_input() -> None:
    runner = StubRunner()
    runner.on(has("label=issuebot/todo"), stdout=fixture("list_todo_page2.json"))
    adapter = make_adapter(runner)
    assert await adapter.fetch_issues_by_states([]) == []
    assert runner.calls == []
    issues = await adapter.fetch_issues_by_states([StateLabel.TODO, StateLabel.TODO])
    assert [issue.number for issue in issues] == [44]
    assert len(runner.calls) == 1


async def test_malformed_list_record_is_skipped_and_logged() -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)
    runner = StubRunner()
    runner.on(has("label=issuebot/todo"), stdout=fixture("list_todo_page2.json"))
    issues = await make_adapter(runner).fetch_issues_by_states([StateLabel.TODO])
    assert [issue.number for issue in issues] == [44]
    record = json.loads(stream.getvalue().splitlines()[0])
    assert record["event"] == "issue_record_skipped"
    assert record["issue_number"] == 45
    assert "title" in record["reason"]


async def test_fetch_terminal_issues_queries_closed_state_for_every_role() -> None:
    runner = StubRunner()
    empty = json.dumps(
        {
            "data": {
                "repository": {
                    "issues": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
                }
            }
        }
    )
    runner.on(has("api"), stdout=empty)
    assert await make_adapter(runner).fetch_terminal_issues() == []
    assert len(runner.calls) == 5
    labels = sorted(
        next(arg for arg in argv if arg.startswith("label=")) for argv, _ in runner.calls
    )
    assert labels == [
        "label=issuebot/complete",
        "label=issuebot/in-progress",
        "label=issuebot/review",
        "label=issuebot/rework",
        "label=issuebot/todo",
    ]
    assert all("states: [CLOSED]" in query_of(argv) for argv, _ in runner.calls)


async def test_page_without_cursor_raises_response() -> None:
    runner = StubRunner()
    page = json.loads(fixture("list_todo_page1.json"))
    page["data"]["repository"]["issues"]["pageInfo"]["endCursor"] = None
    runner.on(has("api"), stdout=json.dumps(page))
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).fetch_issues_by_states([StateLabel.TODO])
    assert exc.value.category == "response"
    assert "endCursor" in exc.value.message


# --- reads: by id ------------------------------------------------------------------


async def test_fetch_by_ids_batches_sorts_and_omits_not_found() -> None:
    runner = StubRunner()
    runner.on(has("i42: issue"), stdout=fixture("by_ids.json"), returncode=1)
    adapter = make_adapter(runner)
    issues = await adapter.fetch_issues_by_ids(["42", "9999", "abc", "7"])
    assert [issue.number for issue in issues] == [7, 42]
    assert issues[0].linked_pr is not None and issues[0].linked_pr.state == "merged"
    query = query_of(runner.argv(0))
    assert query.index("i7: issue(number: 7)") < query.index("i42: issue(number: 42)")
    assert "i9999: issue(number: 9999)" in query
    assert "abc" not in query


async def test_fetch_by_ids_empty_and_non_numeric_make_no_call() -> None:
    runner = StubRunner()
    adapter = make_adapter(runner)
    assert await adapter.fetch_issues_by_ids([]) == []
    assert await adapter.fetch_issues_by_ids(["abc", ""]) == []
    assert runner.calls == []


async def test_fetch_by_ids_splits_into_batches_of_fifty() -> None:
    runner = StubRunner()
    runner.on(has("api"), stdout=json.dumps({"data": {"repository": {}}}))
    ids = [str(n) for n in range(1, ID_BATCH_SIZE + 11)]
    assert await make_adapter(runner).fetch_issues_by_ids(ids) == []
    assert len(runner.calls) == 2
    first, second = query_of(runner.argv(0)), query_of(runner.argv(1))
    assert "i1: issue" in first and "i50: issue" in first and "i51: issue" not in first
    assert "i51: issue" in second and "i60: issue" in second


def test_by_ids_query_shape() -> None:
    query = by_ids_query([3, 5])
    assert query.startswith("query($owner: String!, $name: String!)")
    assert "i3: issue(number: 3) { ...IssueFields }" in query
    assert "fragment IssueFields on Issue" in query


async def test_fetch_by_ids_malformed_record_raises() -> None:
    runner = StubRunner()
    payload = json.loads(fixture("by_ids.json"))
    del payload["errors"]
    del payload["data"]["repository"]["i42"]["title"]
    runner.on(has("api"), stdout=json.dumps(payload))
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).fetch_issues_by_ids(["7", "42"])
    assert exc.value.category == "response"


# --- GraphQL error handling ----------------------------------------------------------


async def test_repository_not_found_raises_not_found_even_for_id_reads() -> None:
    runner = StubRunner()
    body = {
        "data": {"repository": None},
        "errors": [
            {
                "type": "NOT_FOUND",
                "path": ["repository"],
                "message": "Could not resolve to a Repository with the name 'example/nope'.",
            }
        ],
    }
    runner.on(has("api"), stdout=json.dumps(body), returncode=1)
    adapter = make_adapter(runner)
    with pytest.raises(GitHubError) as exc:
        await adapter.fetch_issues_by_ids(["1"])
    assert exc.value.category == "not_found"
    with pytest.raises(GitHubError) as exc:
        await adapter.fetch_issues_by_states([StateLabel.TODO])
    assert exc.value.category == "not_found"


async def test_rate_limited_graphql_error() -> None:
    runner = StubRunner()
    body = {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
    runner.on(has("api"), stdout=json.dumps(body), returncode=1)
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).fetch_issues_by_states([StateLabel.TODO])
    assert exc.value.category == "rate_limited"
    assert exc.value.retryable


async def test_other_graphql_error_raises_response() -> None:
    runner = StubRunner()
    body = {"errors": [{"message": "Field 'nope' doesn't exist on type 'Issue'"}]}
    runner.on(has("api"), stdout=json.dumps(body), returncode=1)
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).fetch_issues_by_states([StateLabel.TODO])
    assert exc.value.category == "response"
    assert "doesn't exist" in exc.value.message


async def test_missing_data_object_raises_response() -> None:
    runner = StubRunner()
    runner.on(has("api"), stdout="{}")
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).fetch_issues_by_states([StateLabel.TODO])
    assert exc.value.category == "response"


# --- error mapping table -------------------------------------------------------------


@pytest.mark.parametrize(
    ("returncode", "stderr", "category", "retryable"),
    [
        (4, "", "auth", False),
        (1, "gh: HTTP 401: Bad credentials (https://api.github.com/graphql)", "auth", False),
        (1, "To get started with GitHub CLI, please run:  gh auth login", "auth", False),
        (1, "gh: Not Found (HTTP 404)", "not_found", False),
        (1, "gh: API rate limit exceeded for user ID 1 (HTTP 429)", "rate_limited", True),
        (1, "gh: Bad Gateway (HTTP 502)", "transport", True),
        (1, "error connecting to api.github.com\ndial tcp: connection refused", "transport", True),
        (1, "gh: Resource not accessible by integration (HTTP 403)", "auth", False),
        (1, "something unexpected happened", "status", False),
        (1, "", "status", False),
    ],
)
async def test_error_mapping(returncode: int, stderr: str, category: str, retryable: bool) -> None:
    runner = StubRunner()
    runner.on(has("api"), stderr=stderr, returncode=returncode)
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).fetch_issues_by_states([StateLabel.TODO])
    assert exc.value.category == category
    assert exc.value.retryable is retryable
    assert exc.value.exit_code == returncode
    expected_message = stderr.splitlines()[0] if stderr else f"gh exited with status {returncode}"
    assert exc.value.message == expected_message


async def test_token_is_redacted_from_error_text() -> None:
    runner = StubRunner()
    runner.on(has("api"), stderr="gh: HTTP 401 token sekret rejected", returncode=1)
    adapter = make_adapter(runner, token=SecretStr("sekret"))
    with pytest.raises(GitHubError) as exc:
        await adapter.fetch_issues_by_states([StateLabel.TODO])
    assert "sekret" not in exc.value.message
    assert "***" in exc.value.message
    assert exc.value.stderr is not None and "sekret" not in exc.value.stderr
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_ghcli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.github.ghcli'`.

- [ ] **Step 4: Write the protocol**

`src/issuebot/github/adapter.py`:

```python
"""The protocol every GitHub implementation satisfies (the gh-backed adapter and the fake)."""

from collections.abc import Iterable
from typing import Protocol

from issuebot.config import GitHubLabels
from issuebot.github.models import (
    AuthStatus,
    Comment,
    Issue,
    LabelEnsured,
    RateLimit,
    RepoInfo,
    StateLabel,
)


class GitHubAdapter(Protocol):
    """Reads and writes for one repository. Every method may raise GitHubError."""

    @property
    def repo(self) -> str: ...

    @property
    def labels(self) -> GitHubLabels: ...

    async def fetch_issues_by_states(self, states: Iterable[StateLabel]) -> list[Issue]:
        """Open issues carrying any of the given state labels; [] for an empty input."""
        ...

    async def fetch_issues_by_ids(self, ids: Iterable[str]) -> list[Issue]:
        """Current snapshots; ids that no longer resolve to an issue are omitted."""
        ...

    async def fetch_terminal_issues(self) -> list[Issue]:
        """Closed issues that still carry any state label."""
        ...

    async def set_state(self, number: int, state: StateLabel) -> None:
        """Add the target state label and remove every other state label."""
        ...

    async def clear_state(self, number: int) -> None:
        """Remove every state label."""
        ...

    async def comment(self, number: int, body: str) -> Comment: ...

    async def find_workpad_comment(self, number: int) -> Comment | None: ...

    async def update_comment(self, comment_id: int, body: str) -> Comment: ...

    async def ensure_labels(self) -> list[LabelEnsured]:
        """Create or update the five state labels; idempotent."""
        ...

    async def missing_labels(self) -> list[str]:
        """Names of the configured state labels that do not exist in the repository."""
        ...

    async def rate_limit(self) -> RateLimit: ...

    async def auth_status(self) -> AuthStatus: ...

    async def repo_info(self) -> RepoInfo: ...
```

- [ ] **Step 5: Implement the adapter's reads and plumbing**

`src/issuebot/github/ghcli.py`:

```python
"""GitHubAdapter backed by the gh CLI: GraphQL for reads, gh subcommands for writes."""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from issuebot.config import GitHubLabels, GitHubSettings
from issuebot.github.errors import ErrorCategory, GitHubError
from issuebot.github.models import Issue, StateLabel
from issuebot.github.normalise import issue_from_node, label_name
from issuebot.github.runner import GhResult, GhRunner, GhRunnerLike
from issuebot.log import get_logger

PAGE_SIZE = 100
ID_BATCH_SIZE = 50

ISSUE_FIELDS = """fragment IssueFields on Issue {
  number title body state url createdAt updatedAt closedAt
  labels(first: 50) { nodes { name } }
  assignees(first: 20) { nodes { login } }
  closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
    nodes { number url state mergedAt }
  }
}"""


def _issues_query(states: str) -> str:
    return (
        "query($owner: String!, $name: String!, $label: String!, $cursor: String) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"    issues(labels: [$label], states: [{states}], first: {PAGE_SIZE}, after: $cursor,\n"
        "           orderBy: {field: CREATED_AT, direction: ASC}) {\n"
        "      nodes { ...IssueFields }\n"
        "      pageInfo { hasNextPage endCursor }\n"
        "    }\n"
        "  }\n"
        "}\n" + ISSUE_FIELDS
    )


OPEN_ISSUES_QUERY = _issues_query("OPEN")
CLOSED_ISSUES_QUERY = _issues_query("CLOSED")


def by_ids_query(numbers: Sequence[int]) -> str:
    aliases = "\n".join(f"    i{n}: issue(number: {n}) {{ ...IssueFields }}" for n in numbers)
    return (
        "query($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{aliases}\n"
        "  }\n"
        "}\n" + ISSUE_FIELDS
    )


_ERROR_RULES: tuple[tuple[ErrorCategory, re.Pattern[str]], ...] = (
    ("auth", re.compile(r"http 401|bad credentials|authentication|gh auth login")),
    ("not_found", re.compile(r"http 404|could not resolve to")),
    ("rate_limited", re.compile(r"http 429|rate limit|secondary rate")),
    (
        "transport",
        re.compile(r"http 5\d\d|connection|could not resolve host|timeout|\btls\b|dial tcp"),
    ),
    ("auth", re.compile(r"http 403")),
)


class GhCliAdapter:
    def __init__(self, settings: GitHubSettings, *, runner: GhRunnerLike | None = None) -> None:
        self._settings = settings
        self._runner: GhRunnerLike = runner or GhRunner(
            token=settings.token, timeout_ms=settings.request_timeout_ms
        )
        self._owner, self._name = settings.repo.split("/", 1)
        self._log = get_logger(__name__)

    @property
    def repo(self) -> str:
        return self._settings.repo

    @property
    def labels(self) -> GitHubLabels:
        return self._settings.labels

    # --- reads -------------------------------------------------------------------

    async def fetch_issues_by_states(self, states: Iterable[StateLabel]) -> list[Issue]:
        roles = list(dict.fromkeys(states))
        if not roles:
            return []
        return await self._collect(roles, OPEN_ISSUES_QUERY)

    async def fetch_terminal_issues(self) -> list[Issue]:
        return await self._collect(list(StateLabel), CLOSED_ISSUES_QUERY)

    async def fetch_issues_by_ids(self, ids: Iterable[str]) -> list[Issue]:
        numbers = sorted({int(value) for value in ids if str(value).isdigit()})
        issues: list[Issue] = []
        for start in range(0, len(numbers), ID_BATCH_SIZE):
            batch = numbers[start : start + ID_BATCH_SIZE]
            data = await self._graphql(
                by_ids_query(batch),
                {"owner": self._owner, "name": self._name},
                allow_missing_aliases=True,
            )
            repository = data.get("repository")
            if not isinstance(repository, Mapping):
                raise GitHubError("response", "GraphQL response has no repository")
            for number in batch:
                node = repository.get(f"i{number}")
                if node is None:
                    continue
                issues.append(issue_from_node(node, repo=self.repo, labels=self.labels))
        return issues

    async def _collect(self, roles: Sequence[StateLabel], query: str) -> list[Issue]:
        found: dict[int, Issue] = {}
        for role in roles:
            for issue in await self._issues_with_label(label_name(self.labels, role), query):
                found.setdefault(issue.number, issue)
        return sorted(found.values(), key=lambda issue: (issue.created_at, issue.number))

    async def _issues_with_label(self, label: str, query: str) -> list[Issue]:
        issues: list[Issue] = []
        cursor: str | None = None
        while True:
            variables = {"owner": self._owner, "name": self._name, "label": label}
            if cursor:
                variables["cursor"] = cursor
            data = await self._graphql(query, variables)
            connection = _dig(data, "repository", "issues")
            if not isinstance(connection, Mapping):
                raise GitHubError("response", "GraphQL response has no repository.issues")
            for node in connection.get("nodes") or []:
                try:
                    issues.append(issue_from_node(node, repo=self.repo, labels=self.labels))
                except GitHubError as exc:
                    number = node.get("number") if isinstance(node, Mapping) else None
                    self._log.warning(
                        "issue_record_skipped", issue_number=number, reason=exc.message
                    )
            page = connection.get("pageInfo")
            page = page if isinstance(page, Mapping) else {}
            if not page.get("hasNextPage"):
                return issues
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubError("response", "GraphQL page has hasNextPage without endCursor")

    # --- plumbing ----------------------------------------------------------------

    async def _graphql(
        self,
        query: str,
        variables: Mapping[str, str],
        *,
        allow_missing_aliases: bool = False,
    ) -> Mapping[str, Any]:
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            args += ["-f", f"{key}={value}"]
        result = await self._runner.run(args)
        payload = _parse_json(result.stdout)
        errors = payload.get("errors") if isinstance(payload, Mapping) else None
        if isinstance(errors, list) and errors:
            self._raise_for_graphql_errors(errors, result, allow_missing_aliases)
        elif result.returncode != 0:
            raise self._error_for(result)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise GitHubError(
                "response",
                "GraphQL response has no data object",
                exit_code=result.returncode,
                stderr=result.stderr,
            )
        return data

    def _raise_for_graphql_errors(
        self, errors: list[Any], result: GhResult, allow_missing_aliases: bool
    ) -> None:
        entries = [entry for entry in errors if isinstance(entry, Mapping)]
        types = {entry.get("type") for entry in entries}
        messages = "; ".join(str(entry.get("message", "")) for entry in entries) or "GraphQL error"
        alias_level = all(
            isinstance(entry.get("path"), list) and len(entry["path"]) >= 2 for entry in entries
        )
        if types == {"NOT_FOUND"} and allow_missing_aliases and alias_level:
            return
        if "RATE_LIMITED" in types:
            raise GitHubError(
                "rate_limited", messages, exit_code=result.returncode, stderr=result.stderr
            )
        category: ErrorCategory = "not_found" if types == {"NOT_FOUND"} else "response"
        raise GitHubError(category, messages, exit_code=result.returncode, stderr=result.stderr)

    async def _gh(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult:
        result = await self._runner.run(args, stdin=stdin)
        if result.returncode != 0:
            raise self._error_for(result)
        return result

    def _error_for(self, result: GhResult) -> GitHubError:
        stderr = self._redact(result.stderr)
        first_line = next((line for line in stderr.splitlines() if line.strip()), "").strip()
        message = first_line or f"gh exited with status {result.returncode}"
        category: ErrorCategory = "status"
        if result.returncode == 4:
            category = "auth"
        else:
            lowered = stderr.lower()
            for candidate, pattern in _ERROR_RULES:
                if pattern.search(lowered):
                    category = candidate
                    break
        self._log.warning(
            "gh_failed", category=category, exit_code=result.returncode, message=message
        )
        return GitHubError(category, message, exit_code=result.returncode, stderr=stderr)

    def _redact(self, text: str) -> str:
        token = self._settings.token.get_secret_value() if self._settings.token else ""
        return text.replace(token, "***") if token else text


def _parse_json(text: str) -> Any:
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _dig(mapping: Any, *keys: str) -> Any:
    node = mapping
    for key in keys:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node
```

Extend `src/issuebot/github/__init__.py` with `from issuebot.github.adapter import GitHubAdapter` and `from issuebot.github.ghcli import GhCliAdapter`, adding `"GhCliAdapter"` and `"GitHubAdapter"` to `__all__`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_ghcli.py -v`
Expected: 25 passed (including the parametrised error-mapping cases).

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/github tests/test_github_ghcli.py tests/fixtures/gh
git commit -m "feat: add GitHubAdapter protocol and GhCliAdapter GraphQL reads"
```

---

### Task 5: `GhCliAdapter` writes, labels and probes

**Files:**
- Modify: `src/issuebot/github/ghcli.py`, `tests/test_github_ghcli.py`
- Create: `tests/fixtures/gh/comments.json`, `tests/fixtures/gh/comment.json`, `tests/fixtures/gh/labels.json`, `tests/fixtures/gh/rate_limit.json`, `tests/fixtures/gh/repo.json`

**Interfaces:**
- Consumes: Task 4's `GhCliAdapter`, `_gh`, `_parse_json`, `StubRunner`, `has`, `make_adapter`; `LABEL_STYLES`, `LabelStyle`, `WORKPAD_MARKER`, `Comment`, `LabelEnsured`, `RateLimit`, `RepoInfo`, `AuthStatus`.
- Produces: `set_state`, `clear_state`, `comment`, `find_workpad_comment`, `update_comment`, `ensure_labels`, `missing_labels`, `rate_limit`, `auth_status`, `repo_info` on `GhCliAdapter`, completing the `GitHubAdapter` protocol; module helpers `_comment_from(payload) -> Comment`, `_is_workpad(body) -> bool`.

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/gh/comments.json`:

```json
[
  {
    "id": 1001,
    "body": "Looks related to #12.",
    "html_url": "https://github.com/example/repo/issues/42#issuecomment-1001",
    "user": {"login": "jleavers"},
    "created_at": "2026-09-01T10:00:00Z",
    "updated_at": "2026-09-01T10:00:00Z"
  },
  {
    "id": 1002,
    "body": "## Issuebot Workpad\n\n### Plan\n\n- [ ] 1. Reproduce\n",
    "html_url": "https://github.com/example/repo/issues/42#issuecomment-1002",
    "user": {"login": "issuebot-agent"},
    "created_at": "2026-09-02T10:00:00Z",
    "updated_at": "2026-09-02T10:30:00Z"
  }
]
```

`tests/fixtures/gh/comment.json`:

```json
{
  "id": 1003,
  "body": "Blocked: turn budget exhausted.",
  "html_url": "https://github.com/example/repo/issues/42#issuecomment-1003",
  "user": {"login": "issuebot-bot"},
  "created_at": "2026-09-02T11:00:00Z",
  "updated_at": "2026-09-02T11:00:00Z"
}
```

`tests/fixtures/gh/labels.json`:

```json
[
  {"name": "bug", "color": "d73a4a", "description": "Something isn't working"},
  {"name": "issuebot/todo", "color": "0e8a16", "description": "Queued for issuebot; a human sets this"},
  {"name": "issuebot/review", "color": "000000", "description": "old text"}
]
```

`tests/fixtures/gh/rate_limit.json`:

```json
{"limit": 5000, "used": 12, "remaining": 4988, "reset": 1788000000}
```

`tests/fixtures/gh/repo.json`:

```json
{"full_name": "example/repo", "default_branch": "main", "private": false}
```

- [ ] **Step 2: Append the failing tests**

Append to `tests/test_github_ghcli.py` (add `from datetime import UTC, datetime` and `from issuebot.github.models import WORKPAD_MARKER` to the import block at the top):

```python
# --- writes --------------------------------------------------------------------------

REMOVE_ALL = "issuebot/todo,issuebot/in-progress,issuebot/review,issuebot/rework,issuebot/complete"


async def test_set_state_adds_target_and_removes_the_other_four() -> None:
    runner = StubRunner()
    runner.on(has("issue", "edit"))
    await make_adapter(runner).set_state(42, StateLabel.IN_PROGRESS)
    assert runner.argv(0) == [
        "issue",
        "edit",
        "42",
        "-R",
        "example/repo",
        "--add-label",
        "issuebot/in-progress",
        "--remove-label",
        "issuebot/todo,issuebot/review,issuebot/rework,issuebot/complete",
    ]


async def test_clear_state_removes_all_five() -> None:
    runner = StubRunner()
    runner.on(has("issue", "edit"))
    await make_adapter(runner).clear_state(42)
    assert runner.argv(0) == [
        "issue",
        "edit",
        "42",
        "-R",
        "example/repo",
        "--remove-label",
        REMOVE_ALL,
    ]


async def test_set_state_with_missing_label_hints_at_labels_ensure() -> None:
    runner = StubRunner()
    runner.on(has("issue", "edit"), stderr="'issuebot/in-progress' not found", returncode=1)
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).set_state(42, StateLabel.IN_PROGRESS)
    assert exc.value.category == "not_found"
    assert "run issuebot labels ensure" in exc.value.message


async def test_comment_posts_json_body_and_parses_response() -> None:
    runner = StubRunner()
    runner.on(has("POST", "repos/example/repo/issues/42/comments"), stdout=fixture("comment.json"))
    comment = await make_adapter(runner).comment(42, "Blocked: turn budget exhausted.")
    argv, stdin = runner.calls[0]
    assert argv == ["api", "-X", "POST", "repos/example/repo/issues/42/comments", "--input", "-"]
    assert json.loads(stdin or "") == {"body": "Blocked: turn budget exhausted."}
    assert comment.id == 1003
    assert comment.author == "issuebot-bot"
    assert comment.url.endswith("#issuecomment-1003")
    assert comment.created_at == datetime(2026, 9, 2, 11, 0, tzinfo=UTC)


async def test_find_workpad_comment_returns_marker_comment_or_none() -> None:
    runner = StubRunner()
    runner.on(has("issues/42/comments?per_page=100"), stdout=fixture("comments.json"))
    runner.on(has("issues/43/comments?per_page=100"), stdout="[]")
    adapter = make_adapter(runner)
    found = await adapter.find_workpad_comment(42)
    assert found is not None
    assert found.id == 1002
    assert found.body.startswith(WORKPAD_MARKER)
    assert found.updated_at == datetime(2026, 9, 2, 10, 30, tzinfo=UTC)
    assert await adapter.find_workpad_comment(43) is None
    assert runner.argv(0) == ["api", "repos/example/repo/issues/42/comments?per_page=100"]


async def test_update_comment_patches_body() -> None:
    runner = StubRunner()
    runner.on(
        has("PATCH", "repos/example/repo/issues/comments/1002"), stdout=fixture("comment.json")
    )
    await make_adapter(runner).update_comment(1002, "new body")
    argv, stdin = runner.calls[0]
    assert argv == ["api", "-X", "PATCH", "repos/example/repo/issues/comments/1002", "--input", "-"]
    assert json.loads(stdin or "") == {"body": "new body"}


async def test_comment_response_that_is_not_an_object_raises_response() -> None:
    runner = StubRunner()
    runner.on(has("POST"), stdout="[]")
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).comment(42, "x")
    assert exc.value.category == "response"


# --- labels ----------------------------------------------------------------------------


async def test_ensure_labels_creates_updates_and_leaves_unchanged() -> None:
    runner = StubRunner()
    runner.on(has("label", "list"), stdout=fixture("labels.json"))
    runner.on(has("label", "create"))
    results = await make_adapter(runner).ensure_labels()
    assert [(r.name, r.outcome) for r in results] == [
        ("issuebot/todo", "unchanged"),
        ("issuebot/in-progress", "created"),
        ("issuebot/review", "updated"),
        ("issuebot/rework", "created"),
        ("issuebot/complete", "created"),
    ]
    assert runner.argv(0) == [
        "label",
        "list",
        "-R",
        "example/repo",
        "--json",
        "name,color,description",
        "--limit",
        "200",
    ]
    creates = [argv for argv, _ in runner.calls if argv[:2] == ["label", "create"]]
    assert len(creates) == 4
    assert creates[0] == [
        "label",
        "create",
        "issuebot/in-progress",
        "-R",
        "example/repo",
        "--color",
        "FBCA04",
        "--description",
        "An issuebot agent is working on it",
    ]
    review = next(argv for argv in creates if argv[2] == "issuebot/review")
    assert review[-1] == "--force"
    assert all("--force" not in argv for argv in creates if argv[2] != "issuebot/review")


async def test_missing_labels_lists_absent_names_in_role_order() -> None:
    runner = StubRunner()
    runner.on(has("label", "list"), stdout=fixture("labels.json"))
    missing = await make_adapter(runner).missing_labels()
    assert missing == ["issuebot/in-progress", "issuebot/rework", "issuebot/complete"]


async def test_label_list_that_is_not_a_list_raises_response() -> None:
    runner = StubRunner()
    runner.on(has("label", "list"), stdout="{}")
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).missing_labels()
    assert exc.value.category == "response"


# --- probes ----------------------------------------------------------------------------


async def test_rate_limit_parses_graphql_budget() -> None:
    runner = StubRunner()
    runner.on(has("rate_limit"), stdout=fixture("rate_limit.json"))
    limit = await make_adapter(runner).rate_limit()
    assert runner.argv(0) == ["api", "rate_limit", "--jq", ".resources.graphql"]
    assert (limit.limit, limit.remaining, limit.used) == (5000, 4988, 12)
    assert limit.reset_at == datetime.fromtimestamp(1788000000, UTC)


async def test_auth_status_reads_login() -> None:
    runner = StubRunner()
    runner.on(has("api", "user"), stdout="jleavers\n")
    status = await make_adapter(runner).auth_status()
    assert runner.argv(0) == ["api", "user", "--jq", ".login"]
    assert status.login == "jleavers"


async def test_repo_info_parses_fields() -> None:
    runner = StubRunner()
    runner.on(has("repos/example/repo"), stdout=fixture("repo.json"))
    info = await make_adapter(runner).repo_info()
    assert runner.argv(0) == [
        "api",
        "repos/example/repo",
        "--jq",
        "{full_name,default_branch,private}",
    ]
    assert (info.full_name, info.default_branch, info.private) == ("example/repo", "main", False)


@pytest.mark.parametrize("stdout", ["", "null", "{}", "[1]"])
async def test_probe_responses_are_validated(stdout: str) -> None:
    runner = StubRunner()
    runner.on(has("api"), stdout=stdout)
    adapter = make_adapter(runner)
    for call in (adapter.rate_limit, adapter.auth_status, adapter.repo_info):
        with pytest.raises(GitHubError) as exc:
            await call()
        assert exc.value.category == "response"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_ghcli.py -v -k "set_state or clear_state or comment or label or rate_limit or auth_status or repo_info or probe"`
Expected: FAIL with `AttributeError: 'GhCliAdapter' object has no attribute 'set_state'` (and similar).

- [ ] **Step 4: Implement the writes, labels and probes**

In `src/issuebot/github/ghcli.py`:

1. Extend the imports:

```python
from datetime import UTC, datetime

from issuebot.github.models import (
    WORKPAD_MARKER,
    AuthStatus,
    Comment,
    Issue,
    LabelEnsured,
    RateLimit,
    RepoInfo,
    StateLabel,
)
from issuebot.github.state import LABEL_STYLES, LabelStyle
```

2. Change the `not_found` rule in `_ERROR_RULES` to
   `re.compile(r"http 404|could not resolve to|\bnot found\b")`.

3. Add these methods to `GhCliAdapter` after `fetch_issues_by_ids` and before `_collect`:

```python
# --- writes ------------------------------------------------------------------


async def set_state(self, number: int, state: StateLabel) -> None:
    target = label_name(self.labels, state)
    others = [label_name(self.labels, role) for role in StateLabel if role is not state]
    self._log.debug("set_state", issue_number=number, state=state.value)
    await self._edit_labels(number, add=target, remove=others)


async def clear_state(self, number: int) -> None:
    self._log.debug("clear_state", issue_number=number)
    await self._edit_labels(number, add=None, remove=list(self.labels.as_tuple()))


async def _edit_labels(self, number: int, *, add: str | None, remove: Sequence[str]) -> None:
    args = ["issue", "edit", str(number), "-R", self.repo]
    if add is not None:
        args += ["--add-label", add]
    args += ["--remove-label", ",".join(remove)]
    try:
        await self._gh(args)
    except GitHubError as exc:
        if exc.category == "not_found":
            raise GitHubError(
                "not_found",
                f"{exc.message}; run issuebot labels ensure",
                exit_code=exc.exit_code,
                stderr=exc.stderr,
            ) from exc
        raise


async def comment(self, number: int, body: str) -> Comment:
    result = await self._gh(
        ["api", "-X", "POST", f"repos/{self.repo}/issues/{number}/comments", "--input", "-"],
        stdin=json.dumps({"body": body}),
    )
    return _comment_from(_parse_json(result.stdout))


async def find_workpad_comment(self, number: int) -> Comment | None:
    result = await self._gh(["api", f"repos/{self.repo}/issues/{number}/comments?per_page=100"])
    payload = _parse_json(result.stdout)
    if not isinstance(payload, list):
        raise GitHubError("response", "comments response is not a list")
    for item in payload:
        if isinstance(item, Mapping) and _is_workpad(item.get("body")):
            return _comment_from(item)
    return None


async def update_comment(self, comment_id: int, body: str) -> Comment:
    result = await self._gh(
        ["api", "-X", "PATCH", f"repos/{self.repo}/issues/comments/{comment_id}", "--input", "-"],
        stdin=json.dumps({"body": body}),
    )
    return _comment_from(_parse_json(result.stdout))


# --- labels ------------------------------------------------------------------


async def ensure_labels(self) -> list[LabelEnsured]:
    existing = await self._repo_labels()
    results: list[LabelEnsured] = []
    for role in StateLabel:
        name = label_name(self.labels, role)
        style = LABEL_STYLES[role]
        current = existing.get(name.lower())
        if current is None:
            await self._create_label(name, style, force=False)
            results.append(LabelEnsured(name=name, outcome="created"))
        elif current != (style.color.lower(), style.description):
            await self._create_label(name, style, force=True)
            results.append(LabelEnsured(name=name, outcome="updated"))
        else:
            results.append(LabelEnsured(name=name, outcome="unchanged"))
    return results


async def missing_labels(self) -> list[str]:
    existing = await self._repo_labels()
    return [name for name in self.labels.as_tuple() if name.lower() not in existing]


async def _repo_labels(self) -> dict[str, tuple[str, str]]:
    """Existing labels keyed by lowercased name -> (lowercased colour, description)."""
    result = await self._gh(
        ["label", "list", "-R", self.repo, "--json", "name,color,description", "--limit", "200"]
    )
    payload = _parse_json(result.stdout)
    if not isinstance(payload, list):
        raise GitHubError("response", "label list response is not a list")
    labels: dict[str, tuple[str, str]] = {}
    for item in payload:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            color = str(item.get("color") or "").lower()
            labels[item["name"].lower()] = (color, str(item.get("description") or ""))
    return labels


async def _create_label(self, name: str, style: LabelStyle, *, force: bool) -> None:
    args = ["label", "create", name, "-R", self.repo]
    args += ["--color", style.color, "--description", style.description]
    if force:
        args.append("--force")
    await self._gh(args)


# --- probes ------------------------------------------------------------------


async def rate_limit(self) -> RateLimit:
    result = await self._gh(["api", "rate_limit", "--jq", ".resources.graphql"])
    payload = _parse_json(result.stdout)
    try:
        return RateLimit(
            limit=int(payload["limit"]),
            remaining=int(payload["remaining"]),
            used=int(payload["used"]),
            reset_at=datetime.fromtimestamp(int(payload["reset"]), UTC),
        )
    except (TypeError, KeyError, ValueError) as exc:
        raise GitHubError("response", "unexpected rate_limit response") from exc


async def auth_status(self) -> AuthStatus:
    result = await self._gh(["api", "user", "--jq", ".login"])
    login = result.stdout.strip()
    if not login or login in ("null", "{}", "[1]") or login.startswith(("{", "[")):
        raise GitHubError("response", "user response has no login")
    return AuthStatus(login=login)


async def repo_info(self) -> RepoInfo:
    result = await self._gh(
        ["api", f"repos/{self.repo}", "--jq", "{full_name,default_branch,private}"]
    )
    payload = _parse_json(result.stdout)
    try:
        return RepoInfo(
            full_name=str(payload["full_name"]),
            default_branch=str(payload["default_branch"]),
            private=bool(payload["private"]),
        )
    except (TypeError, KeyError) as exc:
        raise GitHubError("response", "unexpected repository response") from exc
```

4. Add these module-level helpers after `_dig`:

```python
def _is_workpad(body: Any) -> bool:
    if not isinstance(body, str) or not body.strip():
        return False
    return body.lstrip().splitlines()[0].strip() == WORKPAD_MARKER


def _comment_from(payload: Any) -> Comment:
    if not isinstance(payload, Mapping):
        raise GitHubError("response", "comment response is not an object")
    user = payload.get("user")
    author = user.get("login") if isinstance(user, Mapping) else None
    try:
        return Comment(
            id=int(payload["id"]),
            body=str(payload.get("body") or ""),
            url=str(payload["html_url"]),
            author=str(author or ""),
            created_at=datetime.fromisoformat(payload["created_at"]),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubError("response", "unexpected comment response") from exc
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_ghcli.py -v`
Expected: 42 passed (25 from Task 4 plus 17 new, counting parametrised cases). If `test_error_mapping`'s "not_found" rows changed behaviour because of the widened rule, re-check the table: the row `"gh: Not Found (HTTP 404)"` still maps to `not_found`; no other row contains "not found".

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/github/ghcli.py tests/test_github_ghcli.py tests/fixtures/gh
git commit -m "feat: add GhCliAdapter writes, label management and probes"
```

---

### Task 6: `FakeGitHub`

**Files:**
- Create: `src/issuebot/github/fake.py`, `tests/test_github_fake.py`
- Modify: `src/issuebot/github/__init__.py`

**Interfaces:**
- Consumes: `GitHubSettings`, models, `GitHubError`, `issue_from_node`, `label_name`, `role_for`, `LABEL_STYLES`, `LabelStyle`, `WORKPAD_MARKER`.
- Produces: `FakeGitHub(settings: GitHubSettings, *, preseed_labels: bool = True, now: Callable[[], datetime] | None = None)` implementing `GitHubAdapter`, with public test helpers `add_issue`, `human_set_state`, `human_add_label`, `human_remove_label`, `open_pr`, `merge_pr`, `close_pr`, `close_issue`, `reopen_issue`, `comments_for`, `issue`, `fail_next`, and attributes `calls: list[tuple[str, tuple[Any, ...]]]`, `repo_labels: dict[str, LabelStyle]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_github_fake.py`:

```python
"""Tests for the in-memory FakeGitHub: protocol conformance and GitHub-like semantics."""

from datetime import UTC, datetime, timedelta

import pytest

from issuebot.config import GitHubSettings
from issuebot.github.errors import GitHubError
from issuebot.github.fake import FakeGitHub
from issuebot.github.models import WORKPAD_MARKER, StateLabel
from issuebot.github.state import LABEL_STYLES, LabelStyle

SETTINGS = GitHubSettings(repo="example/repo")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub(SETTINGS, now=Clock())


def test_repo_and_labels_come_from_settings(fake: FakeGitHub) -> None:
    assert fake.repo == "example/repo"
    assert fake.labels.todo == "issuebot/todo"
    assert set(fake.repo_labels) == set(SETTINGS.labels.as_tuple())


def test_add_issue_produces_normalised_issue(fake: FakeGitHub) -> None:
    issue = fake.add_issue("Add retry backoff", body="Details", labels=("Bug", "issuebot/todo"))
    assert issue.number == 1
    assert issue.id == "1"
    assert issue.identifier == "repo-1"
    assert issue.url == "https://github.com/example/repo/issues/1"
    assert issue.labels == ("bug", "issuebot/todo")
    assert issue.state is StateLabel.TODO
    assert issue.dispatchable
    assert issue.created_at.tzinfo is UTC
    second = fake.add_issue("Second")
    assert second.number == 2
    assert second.state is None and not second.dispatchable


async def test_fetch_by_states_filters_open_issues_by_role(fake: FakeGitHub) -> None:
    todo = fake.add_issue("A", labels=("issuebot/todo",))
    fake.add_issue("B", labels=("issuebot/review",))
    fake.add_issue("C")
    closed = fake.add_issue("D", labels=("issuebot/todo",))
    fake.close_issue(closed.number)
    issues = await fake.fetch_issues_by_states([StateLabel.TODO, StateLabel.REWORK])
    assert [issue.number for issue in issues] == [todo.number]
    assert await fake.fetch_issues_by_states([]) == []
    assert fake.calls[0] == ("fetch_issues_by_states", ((StateLabel.TODO, StateLabel.REWORK),))


async def test_fetch_by_ids_omits_unknown_and_pull_request_numbers(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("issuebot/in-progress",))
    pr = fake.open_pr(issue.number)
    found = await fake.fetch_issues_by_ids([str(issue.number), str(pr.number), "999", "x"])
    assert [i.number for i in found] == [issue.number]
    assert found[0].linked_pr is not None and found[0].linked_pr.number == pr.number
    assert found[0].linked_pr.state == "open"


async def test_merge_pr_closes_issue_and_terminal_fetch_sees_it(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("issuebot/review",))
    pr = fake.open_pr(issue.number)
    fake.merge_pr(pr.number)
    snapshot = fake.issue(issue.number)
    assert snapshot.github_state == "closed"
    assert snapshot.closed_at is not None
    assert snapshot.linked_pr is not None and snapshot.linked_pr.state == "merged"
    assert snapshot.linked_pr.merged_at is not None
    terminal = await fake.fetch_terminal_issues()
    assert [i.number for i in terminal] == [issue.number]
    assert await fake.fetch_issues_by_states([StateLabel.REVIEW]) == []


def test_close_pr_does_not_close_issue(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("issuebot/review",))
    pr = fake.open_pr(issue.number)
    fake.close_pr(pr.number)
    snapshot = fake.issue(issue.number)
    assert snapshot.github_state == "open"
    assert snapshot.linked_pr is not None and snapshot.linked_pr.state == "closed"


async def test_set_state_is_exclusive_and_bumps_updated_at(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", labels=("bug", "issuebot/todo", "issuebot/review"))
    assert fake.issue(issue.number).state is None
    await fake.set_state(issue.number, StateLabel.IN_PROGRESS)
    snapshot = fake.issue(issue.number)
    assert snapshot.state is StateLabel.IN_PROGRESS
    assert snapshot.labels == ("bug", "issuebot/in-progress")
    assert snapshot.updated_at > issue.updated_at
    await fake.clear_state(issue.number)
    assert fake.issue(issue.number).labels == ("bug",)
    assert fake.issue(issue.number).state is None


async def test_set_state_requires_the_label_to_exist_in_the_repo(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A")
    del fake.repo_labels["issuebot/in-progress"]
    with pytest.raises(GitHubError) as exc:
        await fake.set_state(issue.number, StateLabel.IN_PROGRESS)
    assert exc.value.category == "not_found"
    assert "labels ensure" in exc.value.message


async def test_set_state_on_unknown_issue_raises_not_found(fake: FakeGitHub) -> None:
    with pytest.raises(GitHubError) as exc:
        await fake.set_state(999, StateLabel.TODO)
    assert exc.value.category == "not_found"


def test_human_helpers_change_labels_without_recording_calls(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A")
    fake.human_set_state(issue.number, StateLabel.TODO)
    fake.human_add_label(issue.number, "Bug")
    assert fake.issue(issue.number).labels == ("issuebot/todo", "bug")
    fake.human_remove_label(issue.number, "bug")
    fake.human_set_state(issue.number, StateLabel.REWORK)
    assert fake.issue(issue.number).state is StateLabel.REWORK
    assert fake.calls == []


async def test_comments_and_workpad(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A")
    assert await fake.find_workpad_comment(issue.number) is None
    first = await fake.comment(issue.number, "hello")
    pad = await fake.comment(issue.number, f"{WORKPAD_MARKER}\n\n### Plan\n")
    assert first.id != pad.id
    assert first.author == "issuebot"
    found = await fake.find_workpad_comment(issue.number)
    assert found is not None and found.id == pad.id
    updated = await fake.update_comment(pad.id, f"{WORKPAD_MARKER}\n\n- [x] done\n")
    assert updated.id == pad.id and "done" in updated.body
    assert updated.updated_at > pad.updated_at
    assert [c.id for c in fake.comments_for(issue.number)] == [first.id, pad.id]
    with pytest.raises(GitHubError) as exc:
        await fake.update_comment(12345, "x")
    assert exc.value.category == "not_found"


async def test_ensure_and_missing_labels(fake: FakeGitHub) -> None:
    assert await fake.missing_labels() == []
    assert [r.outcome for r in await fake.ensure_labels()] == ["unchanged"] * 5
    del fake.repo_labels["issuebot/rework"]
    fake.repo_labels["issuebot/review"] = LabelStyle("000000", "old")
    assert await fake.missing_labels() == ["issuebot/rework"]
    outcomes = {r.name: r.outcome for r in await fake.ensure_labels()}
    assert outcomes["issuebot/rework"] == "created"
    assert outcomes["issuebot/review"] == "updated"
    assert outcomes["issuebot/todo"] == "unchanged"
    assert fake.repo_labels["issuebot/review"] == LABEL_STYLES[StateLabel.REVIEW]


def test_preseed_labels_can_be_disabled() -> None:
    fake = FakeGitHub(SETTINGS, preseed_labels=False)
    assert fake.repo_labels == {}


async def test_probes(fake: FakeGitHub) -> None:
    assert (await fake.auth_status()).login == "fake-user"
    info = await fake.repo_info()
    assert (info.full_name, info.default_branch, info.private) == ("example/repo", "main", False)
    limit = await fake.rate_limit()
    assert limit.remaining == 4999 and limit.limit == 5000


async def test_fail_next_injects_errors_in_order(fake: FakeGitHub) -> None:
    fake.fail_next("transport", times=2)
    for _ in range(2):
        with pytest.raises(GitHubError) as exc:
            await fake.auth_status()
        assert exc.value.category == "transport"
    assert (await fake.auth_status()).login == "fake-user"
    assert [name for name, _ in fake.calls] == ["auth_status"] * 3


def test_reopen_issue_clears_closed_at(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A")
    fake.close_issue(issue.number)
    assert fake.issue(issue.number).closed_at is not None
    fake.reopen_issue(issue.number)
    assert fake.issue(issue.number).github_state == "open"
    assert fake.issue(issue.number).closed_at is None


def test_explicit_numbers_and_shared_numbering(fake: FakeGitHub) -> None:
    issue = fake.add_issue("A", number=10)
    pr = fake.open_pr(issue.number)
    assert pr.number == 11
    assert fake.add_issue("B").number == 12
    with pytest.raises(ValueError, match="already exists"):
        fake.add_issue("C", number=10)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_github_fake.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.github.fake'`.

- [ ] **Step 3: Implement the fake**

`src/issuebot/github/fake.py`:

```python
"""In-memory GitHubAdapter with GitHub-like semantics and helpers for tests."""

import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from issuebot.config import GitHubLabels, GitHubSettings
from issuebot.github.errors import ErrorCategory, GitHubError
from issuebot.github.models import (
    WORKPAD_MARKER,
    AuthStatus,
    Comment,
    Issue,
    LabelEnsured,
    LinkedPr,
    PrState,
    RateLimit,
    RepoInfo,
    StateLabel,
)
from issuebot.github.normalise import issue_from_node, label_name
from issuebot.github.state import LABEL_STYLES, LabelStyle

_PR_STATE_UPPER: dict[PrState, str] = {"open": "OPEN", "closed": "CLOSED", "merged": "MERGED"}


@dataclass
class _FakeIssue:
    number: int
    title: str
    body: str | None
    state: str
    labels: list[str]
    assignees: list[str]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    comments: list[Comment] = field(default_factory=list)


@dataclass
class _FakePr:
    number: int
    closes: int
    state: PrState = "open"
    merged_at: datetime | None = None


class FakeGitHub:
    """Implements GitHubAdapter in memory; produces Issue records through the same normaliser."""

    def __init__(
        self,
        settings: GitHubSettings,
        *,
        preseed_labels: bool = True,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._now = now or (lambda: datetime.now(UTC))
        self.repo_labels: dict[str, LabelStyle] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._issues: dict[int, _FakeIssue] = {}
        self._prs: dict[int, _FakePr] = {}
        self._next_number = 1
        self._next_comment_id = 1000
        self._failures: list[ErrorCategory] = []
        if preseed_labels:
            for role in StateLabel:
                self.repo_labels[label_name(settings.labels, role)] = LABEL_STYLES[role]

    # --- protocol: identity -------------------------------------------------------

    @property
    def repo(self) -> str:
        return self._settings.repo

    @property
    def labels(self) -> GitHubLabels:
        return self._settings.labels

    # --- protocol: reads ----------------------------------------------------------

    async def fetch_issues_by_states(self, states: Iterable[StateLabel]) -> list[Issue]:
        roles = tuple(dict.fromkeys(states))
        self._enter("fetch_issues_by_states", roles)
        if not roles:
            return []
        wanted = {label_name(self.labels, role).lower() for role in roles}
        return self._snapshots(
            record
            for record in self._issues.values()
            if record.state == "open" and wanted & {name.lower() for name in record.labels}
        )

    async def fetch_issues_by_ids(self, ids: Iterable[str]) -> list[Issue]:
        wanted = tuple(ids)
        self._enter("fetch_issues_by_ids", wanted)
        numbers = sorted({int(value) for value in wanted if str(value).isdigit()})
        return [self._snapshot(self._issues[n]) for n in numbers if n in self._issues]

    async def fetch_terminal_issues(self) -> list[Issue]:
        self._enter("fetch_terminal_issues")
        state_names = {name.lower() for name in self.labels.as_tuple()}
        return self._snapshots(
            record
            for record in self._issues.values()
            if record.state == "closed" and state_names & {name.lower() for name in record.labels}
        )

    # --- protocol: writes ---------------------------------------------------------

    async def set_state(self, number: int, state: StateLabel) -> None:
        self._enter("set_state", number, state)
        record = self._require_issue(number)
        target = label_name(self.labels, state)
        self._require_label(target)
        self._strip_state_labels(record)
        record.labels.append(target)
        record.updated_at = self._now()

    async def clear_state(self, number: int) -> None:
        self._enter("clear_state", number)
        record = self._require_issue(number)
        self._strip_state_labels(record)
        record.updated_at = self._now()

    async def comment(self, number: int, body: str) -> Comment:
        self._enter("comment", number, body)
        record = self._require_issue(number)
        stamp = self._now()
        comment = Comment(
            id=self._next_comment_id,
            body=body,
            url=f"{self._issue_url(number)}#issuecomment-{self._next_comment_id}",
            author="issuebot",
            created_at=stamp,
            updated_at=stamp,
        )
        self._next_comment_id += 1
        record.comments.append(comment)
        return comment

    async def find_workpad_comment(self, number: int) -> Comment | None:
        self._enter("find_workpad_comment", number)
        record = self._require_issue(number)
        for comment in record.comments:
            first_line = (
                comment.body.lstrip().splitlines()[0].strip() if comment.body.strip() else ""
            )
            if first_line == WORKPAD_MARKER:
                return comment
        return None

    async def update_comment(self, comment_id: int, body: str) -> Comment:
        self._enter("update_comment", comment_id, body)
        for record in self._issues.values():
            for index, comment in enumerate(record.comments):
                if comment.id == comment_id:
                    updated = Comment(
                        id=comment.id,
                        body=body,
                        url=comment.url,
                        author=comment.author,
                        created_at=comment.created_at,
                        updated_at=self._now(),
                    )
                    record.comments[index] = updated
                    return updated
        raise GitHubError("not_found", f"comment {comment_id} not found")

    # --- protocol: labels and probes ---------------------------------------------

    async def ensure_labels(self) -> list[LabelEnsured]:
        self._enter("ensure_labels")
        results: list[LabelEnsured] = []
        for role in StateLabel:
            name = label_name(self.labels, role)
            style = LABEL_STYLES[role]
            current = self.repo_labels.get(name)
            if current is None:
                outcome = "created"
            elif current != style:
                outcome = "updated"
            else:
                outcome = "unchanged"
            self.repo_labels[name] = style
            results.append(LabelEnsured(name=name, outcome=outcome))
        return results

    async def missing_labels(self) -> list[str]:
        self._enter("missing_labels")
        return [name for name in self.labels.as_tuple() if name not in self.repo_labels]

    async def rate_limit(self) -> RateLimit:
        self._enter("rate_limit")
        return RateLimit(
            limit=5000, remaining=4999, used=1, reset_at=self._now() + timedelta(hours=1)
        )

    async def auth_status(self) -> AuthStatus:
        self._enter("auth_status")
        return AuthStatus(login="fake-user")

    async def repo_info(self) -> RepoInfo:
        self._enter("repo_info")
        return RepoInfo(full_name=self.repo, default_branch="main", private=False)

    # --- test helpers (never recorded in `calls`) --------------------------------

    def add_issue(
        self,
        title: str,
        *,
        body: str | None = None,
        labels: Iterable[str] = (),
        number: int | None = None,
        assignees: Iterable[str] = (),
    ) -> Issue:
        if number is None:
            number = self._next_number
        elif number in self._issues or number in self._prs:
            raise ValueError(f"number {number} already exists")
        self._next_number = max(self._next_number, number + 1)
        stamp = self._now()
        record = _FakeIssue(
            number=number,
            title=title,
            body=body,
            state="open",
            labels=list(labels),
            assignees=list(assignees),
            created_at=stamp,
            updated_at=stamp,
        )
        self._issues[number] = record
        return self._snapshot(record)

    def human_set_state(self, number: int, state: StateLabel) -> None:
        record = self._require_issue(number)
        self._strip_state_labels(record)
        record.labels.append(label_name(self.labels, state))
        record.updated_at = self._now()

    def human_add_label(self, number: int, name: str) -> None:
        record = self._require_issue(number)
        if name.lower() not in {label.lower() for label in record.labels}:
            record.labels.append(name)
            record.updated_at = self._now()

    def human_remove_label(self, number: int, name: str) -> None:
        record = self._require_issue(number)
        record.labels = [label for label in record.labels if label.lower() != name.lower()]
        record.updated_at = self._now()

    def open_pr(self, issue_number: int, *, pr_number: int | None = None) -> LinkedPr:
        self._require_issue(issue_number)
        if pr_number is None:
            pr_number = self._next_number
        elif pr_number in self._issues or pr_number in self._prs:
            raise ValueError(f"number {pr_number} already exists")
        self._next_number = max(self._next_number, pr_number + 1)
        self._prs[pr_number] = _FakePr(number=pr_number, closes=issue_number)
        return self._linked_pr(self._prs[pr_number])

    def merge_pr(self, pr_number: int) -> None:
        pr = self._require_pr(pr_number)
        pr.state = "merged"
        pr.merged_at = self._now()
        self.close_issue(pr.closes)

    def close_pr(self, pr_number: int) -> None:
        self._require_pr(pr_number).state = "closed"

    def close_issue(self, number: int) -> None:
        record = self._require_issue(number)
        record.state = "closed"
        record.closed_at = self._now()
        record.updated_at = record.closed_at

    def reopen_issue(self, number: int) -> None:
        record = self._require_issue(number)
        record.state = "open"
        record.closed_at = None
        record.updated_at = self._now()

    def comments_for(self, number: int) -> list[Comment]:
        return list(self._require_issue(number).comments)

    def issue(self, number: int) -> Issue:
        return self._snapshot(self._require_issue(number))

    def fail_next(self, category: ErrorCategory, *, times: int = 1) -> None:
        self._failures.extend([category] * times)

    # --- internals ---------------------------------------------------------------

    def _enter(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self._failures:
            category = self._failures.pop(0)
            raise GitHubError(category, f"injected {category} failure")

    def _require_issue(self, number: int) -> _FakeIssue:
        record = self._issues.get(number)
        if record is None:
            raise GitHubError("not_found", f"issue #{number} not found")
        return record

    def _require_pr(self, number: int) -> _FakePr:
        pr = self._prs.get(number)
        if pr is None:
            raise ValueError(f"pull request #{number} does not exist")
        return pr

    def _require_label(self, name: str) -> None:
        if name not in self.repo_labels:
            raise GitHubError("not_found", f"'{name}' not found; run issuebot labels ensure")

    def _strip_state_labels(self, record: _FakeIssue) -> None:
        state_names = {name.lower() for name in self.labels.as_tuple()}
        record.labels = [label for label in record.labels if label.lower() not in state_names]

    def _issue_url(self, number: int) -> str:
        return f"https://github.com/{self.repo}/issues/{number}"

    def _linked_pr(self, pr: _FakePr) -> LinkedPr:
        return LinkedPr(
            number=pr.number,
            url=f"https://github.com/{self.repo}/pull/{pr.number}",
            state=pr.state,
            merged_at=pr.merged_at,
        )

    def _node(self, record: _FakeIssue) -> dict[str, Any]:
        """A GraphQL-shaped node so the fake and the real adapter normalise identically."""
        return {
            "number": record.number,
            "title": record.title,
            "body": record.body,
            "state": "OPEN" if record.state == "open" else "CLOSED",
            "url": self._issue_url(record.number),
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "closedAt": record.closed_at.isoformat() if record.closed_at else None,
            "labels": {"nodes": [{"name": name} for name in record.labels]},
            "assignees": {"nodes": [{"login": login} for login in record.assignees]},
            "closedByPullRequestsReferences": {
                "nodes": [
                    {
                        "number": pr.number,
                        "url": f"https://github.com/{self.repo}/pull/{pr.number}",
                        "state": _PR_STATE_UPPER[pr.state],
                        "mergedAt": pr.merged_at.isoformat() if pr.merged_at else None,
                    }
                    for pr in self._prs.values()
                    if pr.closes == record.number
                ]
            },
        }

    def _snapshot(self, record: _FakeIssue) -> Issue:
        return issue_from_node(
            copy.deepcopy(self._node(record)), repo=self.repo, labels=self.labels
        )

    def _snapshots(self, records: Iterable[_FakeIssue]) -> list[Issue]:
        issues = [self._snapshot(record) for record in records]
        return sorted(issues, key=lambda issue: (issue.created_at, issue.number))
```

Extend `src/issuebot/github/__init__.py` with `from issuebot.github.fake import FakeGitHub` and `"FakeGitHub"` in `__all__`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_fake.py -v`
Expected: 17 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/github tests/test_github_fake.py
git commit -m "feat: add FakeGitHub in-memory adapter for tests"
```

---

### Task 7: CLI — `labels ensure`, `issues list`, network checks in `validate`

**Files:**
- Modify: `src/issuebot/cli.py` (replaced in full below), `tests/test_cli.py`

**Interfaces:**
- Consumes: `GhCliAdapter`, `GitHubAdapter`, `GitHubError`, `Issue`, `StateLabel`, `FakeGitHub`, `GitHubSettings`.
- Produces: `issuebot labels ensure [--workflow]`, `issuebot issues list [--workflow] [--state ROLE]`, twelve-check `validate`; `render_issue_table(issues) -> str`; `run_checks(workflow, *, adapter=None)`; module-level `_adapter_factory: Callable[[GitHubSettings], GitHubAdapter]`.

- [ ] **Step 1: Update the existing CLI tests and add the new ones**

In `tests/test_cli.py`:

1. Extend the import block:

```python
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from issuebot import __version__
from issuebot.cli import main, render_issue_table
from issuebot.config import GitHubSettings
from issuebot.github import FakeGitHub, GitHubError, Issue, LinkedPr, StateLabel
```

2. Add this autouse fixture right after the `executables` fixture:

```python
class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture(autouse=True)
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """Every CLI command talks to this in-memory GitHub instead of the real gh."""
    fake = FakeGitHub(GitHubSettings(repo="example/repo"), now=_Clock())
    monkeypatch.setattr("issuebot.cli._adapter_factory", lambda settings: fake)
    return fake
```

3. In `test_validate_good_workflow_exits_zero`, replace the `9 checks` assertion with:

```python
    assert "[ OK ] gh auth: logged in as fake-user" in out
    assert "[ OK ] github.repo access: example/repo (default branch main)" in out
    assert "[ OK ] github.labels: 5 labels present" in out
    assert out.index("[ OK ] gh: ") < out.index("[ OK ] gh auth:") < out.index("[ OK ] database.url")
    assert out.rstrip().endswith("12 checks: 0 failed, 0 warnings")
```

4. In `test_validate_missing_executables_fail`, replace `assert "2 failed" in out` with:

```python
    assert "[WARN] gh auth: skipped (gh not found)" in out
    assert "[WARN] github.repo access: skipped (gh not found)" in out
    assert "[WARN] github.labels: skipped (gh not found)" in out
    assert "2 failed, 3 warnings" in out
```

5. Append these tests at the end of the file:

```python
# --- validate: network checks ----------------------------------------------------------


def test_validate_reports_auth_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.fail_next("auth")
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] gh auth: injected auth failure; run gh auth login or set GH_TOKEN" in out
    assert "[ OK ] github.repo access: example/repo (default branch main)" in out


def test_validate_reports_repo_access_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")

    async def failing_repo_info() -> object:
        raise GitHubError("not_found", "injected not_found failure")

    monkeypatch.setattr(fake_github, "repo_info", failing_repo_info)
    assert main(["validate", "--workflow", str(GOOD)]) == 1
    out = capsys.readouterr().out
    assert "[ OK ] gh auth: logged in as fake-user" in out
    assert "[FAIL] github.repo access: not_found: injected not_found failure" in out
    assert "[ OK ] github.labels: 5 labels present" in out


def test_validate_warns_about_missing_labels(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    del fake_github.repo_labels["issuebot/rework"]
    del fake_github.repo_labels["issuebot/complete"]
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert (
        "[WARN] github.labels: missing: issuebot/rework, issuebot/complete; "
        "run issuebot labels ensure" in out
    )
    assert "0 failed, 1 warnings" in out


# --- labels ensure -----------------------------------------------------------------------


def test_labels_ensure_reports_each_label(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.repo_labels.clear()
    assert main(["labels", "ensure", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "[ OK ] issuebot/todo: created",
        "[ OK ] issuebot/in-progress: created",
        "[ OK ] issuebot/review: created",
        "[ OK ] issuebot/rework: created",
        "[ OK ] issuebot/complete: created",
    ]
    assert main(["labels", "ensure", "--workflow", str(GOOD)]) == 0
    assert all(line.endswith(": unchanged") for line in capsys.readouterr().out.splitlines())


def test_labels_ensure_reports_github_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.fail_next("transport")
    assert main(["labels", "ensure", "--workflow", str(GOOD)]) == 1
    assert "[FAIL] labels: transport: injected transport failure" in capsys.readouterr().out


def test_labels_ensure_unloadable_workflow_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["labels", "ensure", "--workflow", str(INVALID)]) == 2
    assert capsys.readouterr().out.startswith("[FAIL] workflow: ")


def test_labels_without_subcommand_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["labels"])
    assert exc.value.code == 2


# --- issues list --------------------------------------------------------------------------


def test_issues_list_prints_table_sorted_by_role(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    review = fake_github.add_issue("Fix label parsing", labels=("issuebot/review",))
    fake_github.open_pr(review.number)
    fake_github.add_issue("Add retry backoff", labels=("issuebot/in-progress",))
    fake_github.add_issue("Untracked")
    assert main(["issues", "list", "--workflow", str(GOOD)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["NUMBER", "STATE", "PR", "UPDATED", "TITLE"]
    assert lines[1].startswith("3       in_progress  -        2026-09-02T")
    assert lines[1].endswith("  Add retry backoff")
    assert lines[2].startswith("1       review       #2 open  2026-09-02T")
    assert lines[2].endswith("  Fix label parsing")
    assert len(lines) == 3


def test_issues_list_filters_by_state_and_reports_empty(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.add_issue("Only review", labels=("issuebot/review",))
    assert main(["issues", "list", "--workflow", str(GOOD), "--state", "todo"]) == 0
    assert capsys.readouterr().out == "no tracked issues\n"
    assert main(["issues", "list", "--workflow", str(GOOD), "--state", "review"]) == 0
    assert "Only review" in capsys.readouterr().out
    assert fake_github.calls[-1] == ("fetch_issues_by_states", ((StateLabel.REVIEW,),))


def test_issues_list_reports_github_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.fail_next("rate_limited")
    assert main(["issues", "list", "--workflow", str(GOOD)]) == 1
    assert "[FAIL] issues: rate_limited: injected rate_limited failure" in capsys.readouterr().out


def test_render_issue_table_marks_conflicts_and_aligns(make_issue: Callable[..., Issue]) -> None:
    conflict = make_issue(number=5, state=None, state_labels=("issuebot/todo", "issuebot/review"))
    todo = make_issue(number=2, title="Second")
    review = make_issue(
        number=9,
        state=StateLabel.REVIEW,
        title="Third",
        linked_pr=LinkedPr(number=10, url="https://x/pull/10", state="merged", merged_at=None),
    )
    lines = render_issue_table([conflict, review, todo]).splitlines()
    assert [line.split()[0] for line in lines[1:]] == ["2", "9", "5"]
    assert "conflict" in lines[3]
    assert "#10 merged" in lines[2]
    assert all(line.startswith(("NUMBER", "2 ", "9 ", "5 ")) for line in lines)
```

(`FakeGitHub.fail_next` fails the *next* protocol call, which in `validate` is `auth_status`; to fail the second call the test replaces `repo_info` directly.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: the new `labels`/`issues` tests fail with `SystemExit: 2` (unknown command); the `12 checks` assertion and the network-check tests fail; `render_issue_table` import fails at collection until the CLI is replaced — that is expected at this step.

- [ ] **Step 3: Replace `src/issuebot/cli.py`**

```python
"""Command-line entry point for issuebot."""

import argparse
import asyncio
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Literal

import yaml

from issuebot import __version__
from issuebot.config import ConfigError, GitHubSettings, Settings, Workflow, load_workflow
from issuebot.config.resolve import ENV_REF
from issuebot.github import GhCliAdapter, GitHubAdapter, GitHubError, Issue, StateLabel
from issuebot.log import LOG_LEVELS, configure_logging

DEFAULT_WORKFLOW = "WORKFLOW.md"

# Module-level references so tests can substitute the executable lookup and the adapter.
_which = shutil.which
_adapter_factory: Callable[[GitHubSettings], GitHubAdapter] = GhCliAdapter

CheckStatus = Literal["ok", "warn", "fail"]
_TAGS: dict[CheckStatus, str] = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
_NETWORK_SUBJECTS = ("gh auth", "github.repo access", "github.labels")
_ROLE_ORDER: dict[StateLabel | None, int] = {role: index for index, role in enumerate(StateLabel)}


@dataclass(frozen=True)
class Check:
    subject: str
    status: CheckStatus
    detail: str

    def line(self) -> str:
        return f"{_TAGS[self.status]} {self.subject}: {self.detail}"


def _add_workflow_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workflow",
        type=Path,
        default=None,
        help="path to WORKFLOW.md (default: $ISSUEBOT_WORKFLOW or ./WORKFLOW.md)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issuebot",
        description="Issue-to-PR agent orchestrator for GitHub and Claude.",
    )
    parser.add_argument("--version", action="version", version=f"issuebot {__version__}")
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        type=str.upper,
        default=None,
        help="DEBUG, INFO, WARNING or ERROR (default: $ISSUEBOT_LOG_LEVEL or INFO)",
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "console"],
        default=None,
        help="log line format (default: $ISSUEBOT_LOG_FORMAT or json)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    validate = subparsers.add_parser(
        "validate", help="load WORKFLOW.md and check the runtime environment"
    )
    _add_workflow_option(validate)
    validate.add_argument(
        "--show-config", action="store_true", help="print the effective configuration as YAML"
    )
    validate.set_defaults(func=cmd_validate)

    labels = subparsers.add_parser("labels", help="manage the issuebot state labels")
    labels_sub = labels.add_subparsers(dest="labels_command", metavar="<subcommand>", required=True)
    ensure = labels_sub.add_parser(
        "ensure", help="create or update the five state labels in the repository"
    )
    _add_workflow_option(ensure)
    ensure.set_defaults(func=cmd_labels_ensure)

    issues = subparsers.add_parser("issues", help="inspect tracked issues")
    issues_sub = issues.add_subparsers(dest="issues_command", metavar="<subcommand>", required=True)
    issues_list = issues_sub.add_parser("list", help="list open issues carrying a state label")
    _add_workflow_option(issues_list)
    issues_list.add_argument(
        "--state",
        choices=[role.value for role in StateLabel],
        default=None,
        help="only issues in this state",
    )
    issues_list.set_defaults(func=cmd_issues_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        configure_logging(
            level=args.log_level or os.environ.get("ISSUEBOT_LOG_LEVEL", "INFO"),
            fmt=args.log_format or os.environ.get("ISSUEBOT_LOG_FORMAT", "json"),
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.command is None:
        parser.print_help()
        return 2
    return int(args.func(args))


def workflow_path(explicit: Path | None, environ: Mapping[str, str]) -> Path:
    if explicit is not None:
        return explicit
    return Path(environ.get("ISSUEBOT_WORKFLOW") or DEFAULT_WORKFLOW)


def _load_or_report(args: argparse.Namespace) -> Workflow | None:
    try:
        return load_workflow(workflow_path(args.workflow, os.environ))
    except ConfigError as exc:
        print(f"[FAIL] workflow: {exc}")
        return None


# --- validate ------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    adapter = _adapter_factory(workflow.config.github) if _which("gh") else None
    checks = run_checks(workflow, adapter=adapter)
    for check in checks:
        print(check.line())
    failed = sum(check.status == "fail" for check in checks)
    warned = sum(check.status == "warn" for check in checks)
    print(f"{len(checks)} checks: {failed} failed, {warned} warnings")
    if args.show_config:
        print(render_config(workflow.config), end="")
    return 1 if failed else 0


def run_checks(workflow: Workflow, *, adapter: GitHubAdapter | None = None) -> list[Check]:
    cfg = workflow.config
    checks = [
        Check("workflow", "ok", str(workflow.path)),
        Check("github.repo", "ok", cfg.github.repo),
        _token_check(workflow),
        _workspace_check(cfg.workspace.root),
        _executable_check("claude.command", cfg.claude.command),
        _executable_check("gh", "gh"),
    ]
    checks.extend(_github_checks(adapter))
    checks.append(
        Check(
            "database.url",
            "ok",
            "configured" if cfg.database.url else "not configured (history and dashboard disabled)",
        )
    )
    checks.append(
        Check(
            "notifications.slack",
            "ok",
            "configured" if cfg.notifications.slack.webhook_url else "not configured",
        )
    )
    body = workflow.prompt_template
    if body:
        checks.append(Check("prompt", "ok", f"{len(body)} characters"))
    else:
        checks.append(Check("prompt", "warn", "body is empty"))
    return checks


def _github_checks(adapter: GitHubAdapter | None) -> list[Check]:
    if adapter is None:
        return [Check(subject, "warn", "skipped (gh not found)") for subject in _NETWORK_SUBJECTS]
    return asyncio.run(_probe_github(adapter))


async def _probe_github(adapter: GitHubAdapter) -> list[Check]:
    checks: list[Check] = []
    try:
        auth = await adapter.auth_status()
        checks.append(Check("gh auth", "ok", f"logged in as {auth.login}"))
    except GitHubError as exc:
        checks.append(Check("gh auth", "fail", f"{exc.message}; run gh auth login or set GH_TOKEN"))
    try:
        info = await adapter.repo_info()
        detail = f"{info.full_name} (default branch {info.default_branch})"
        checks.append(Check("github.repo access", "ok", detail))
    except GitHubError as exc:
        checks.append(Check("github.repo access", "fail", str(exc)))
    try:
        missing = await adapter.missing_labels()
    except GitHubError as exc:
        checks.append(Check("github.labels", "fail", str(exc)))
    else:
        if missing:
            detail = f"missing: {', '.join(missing)}; run issuebot labels ensure"
            checks.append(Check("github.labels", "warn", detail))
        else:
            checks.append(Check("github.labels", "ok", "5 labels present"))
    return checks


def _token_check(workflow: Workflow) -> Check:
    if workflow.config.github.token is None:
        return Check("github.token", "fail", "not set; export GH_TOKEN or set github.token: $VAR")
    raw_github = workflow.raw_config.get("github")
    raw_token = raw_github.get("token") if isinstance(raw_github, dict) else None
    if raw_token is None:
        return Check("github.token", "ok", "set (from GH_TOKEN)")
    if isinstance(raw_token, str) and ENV_REF.match(raw_token):
        return Check("github.token", "ok", f"set (from {raw_token})")
    return Check("github.token", "warn", "literal value in WORKFLOW.md; prefer $VAR")


def _workspace_check(root: Path) -> Check:
    if root.parent.is_dir():
        return Check("workspace.root", "ok", str(root))
    return Check("workspace.root", "warn", f"{root} (parent directory does not exist)")


def _executable_check(subject: str, command: str) -> Check:
    found = _which(command)
    if found:
        return Check(subject, "ok", found)
    return Check(subject, "fail", f"{command!r} not found on PATH")


def render_config(settings: Settings) -> str:
    return yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False)


# --- labels ----------------------------------------------------------------------------


def cmd_labels_ensure(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    adapter = _adapter_factory(workflow.config.github)
    try:
        results = asyncio.run(adapter.ensure_labels())
    except GitHubError as exc:
        print(f"[FAIL] labels: {exc}")
        return 1
    for result in results:
        print(f"[ OK ] {result.name}: {result.outcome}")
    return 0


# --- issues ----------------------------------------------------------------------------


def cmd_issues_list(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    adapter = _adapter_factory(workflow.config.github)
    roles = [StateLabel(args.state)] if args.state else list(StateLabel)
    try:
        issues = asyncio.run(adapter.fetch_issues_by_states(roles))
    except GitHubError as exc:
        print(f"[FAIL] issues: {exc}")
        return 1
    print(render_issue_table(issues), end="")
    return 0


def render_issue_table(issues: Sequence[Issue]) -> str:
    if not issues:
        return "no tracked issues\n"
    rows: list[tuple[str, str, str, str, str]] = [("NUMBER", "STATE", "PR", "UPDATED", "TITLE")]
    ordered = sorted(
        issues, key=lambda issue: (_ROLE_ORDER.get(issue.state, len(StateLabel)), issue.number)
    )
    for issue in ordered:
        pr = f"#{issue.linked_pr.number} {issue.linked_pr.state}" if issue.linked_pr else "-"
        state = issue.state.value if issue.state else "conflict"
        updated = issue.updated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append((str(issue.number), state, pr, updated, issue.title))
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    lines = []
    for row in rows:
        cells = [row[column].ljust(widths[column]) for column in range(4)]
        lines.append("  ".join([*cells, row[4]]).rstrip())
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all pass (34 tests: 23 existing plus 11 new). Then `uv run pytest -q` for the whole suite.

- [ ] **Step 5: Manual check of the real commands (needs `GH_TOKEN` for `jleavers/issuebot`)**

Run (this is the spec's done-when; the repository has no `issuebot/*` labels yet):

```bash
uv run issuebot labels ensure
uv run issuebot labels ensure
uv run issuebot validate
uv run issuebot issues list
```

Expected: five `created` lines, then five `unchanged` lines; `validate` prints twelve checks with `[ OK ] gh auth: logged in as jleavers`, `[ OK ] github.repo access: jleavers/issuebot (default branch main)` and `[ OK ] github.labels: 5 labels present`; `issues list` prints `no tracked issues`. Paste the output in your report. If `GH_TOKEN` is not available, say so in the report and skip; the controller runs it.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
git add src/issuebot/cli.py tests/test_cli.py
git commit -m "feat: add labels ensure, issues list and validate network checks"
```

---

### Task 8: Documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `docs/superpowers/specs/2026-09-02-phase-1-foundations-design.md`, `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`

Edit these with the Write/Edit tools (the files mention the dot-env filename and contain code fences that hooks may reflow; include any hook rewrites in the commit).

- [ ] **Step 1: `CLAUDE.md`**

In the `## Commands` code block, add after the `uv run issuebot validate` line:

```
uv run issuebot labels ensure        # create/update the five state labels in github.repo
uv run issuebot issues list          # table of open issues carrying a state label
```

In `## Package layout`, add this bullet after the `issuebot.events` bullet:

```markdown
- `issuebot.github`: `StateLabel` roles and the transition table (`state.py`); frozen
  `Issue`/`LinkedPr`/`Comment` records (`models.py`); `GitHubAdapter` protocol (async);
  `GhCliAdapter` (GraphQL reads via `gh api graphql`, writes via `gh issue edit`,
  `gh label create`, `gh api`; `GhRunner` is the only subprocess boundary); `FakeGitHub`
  for tests (same normaliser, GitHub-like semantics, `fail_next`, `calls`).
```

and change the `issuebot.cli` bullet to:

```markdown
- `issuebot.cli`: argparse; `validate` (twelve checks, three of them network probes
  through the adapter), `labels ensure`, `issues list`; exit codes 0/1/2 (ok / failed /
  workflow unloadable). Tests substitute `_which` and `_adapter_factory`.
```

- [ ] **Step 2: `README.md`**

In the Development code block, after `uv run issuebot validate ...`, add:

```
uv run issuebot labels ensure     # once per repository: creates the issuebot/* labels
```

- [ ] **Step 3: Phase 1 spec and roadmap**

In `docs/superpowers/specs/2026-09-02-phase-1-foundations-design.md` §4.2, add this row after the `github.labels.complete` row:

```
| `github.request_timeout_ms` | int ≥ 1000 (added in Phase 2; bounds every `gh` call) | 30000 |
```

In `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md` §2.11, add `  request_timeout_ms: 30000` as the last line of the `github:` block (after the `labels:` mapping).

- [ ] **Step 4: Verify and commit**

Run: `uv run pre-commit run --all-files && uv run pytest -q`
Expected: clean; tests unchanged.

```bash
git add CLAUDE.md README.md docs/superpowers/specs
git commit -m "docs: describe issuebot.github, the new CLI commands and request_timeout_ms"
```

---

### Task 9: Push and open the pull request

**Files:** none.

- [ ] **Step 1: Final full check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files && uv run pytest -q`
Expected: everything passes.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin phase-2-github-adapter`

- [ ] **Step 3: Write the PR body to a temporary file (separate shell call from Step 4; use the Write tool)**

`/tmp/issuebot-phase-2-pr.md`:

```markdown
## Phase 2: GitHub adapter and label state machine

Implements `docs/superpowers/specs/2026-09-02-phase-2-github-adapter-design.md`.

- `issuebot.github`: `StateLabel` roles, transition table and helpers; frozen `Issue`, `LinkedPr`, `Comment` records; GraphQL normalisation shared by the real adapter and the fake
- `GhRunner`: the only subprocess boundary (asyncio, controlled environment, timeout, token never logged)
- `GhCliAdapter`: reads via `gh api graphql` (one paginated query per role, id batches of 50, linked PRs from `closedByPullRequestsReferences`), writes via `gh issue edit` / `gh api`, label management via `gh label`, probes for auth, repo and rate limit; error mapping to seven categories with `retryable`
- `FakeGitHub`: in-memory adapter with GitHub-like semantics (merge closes the issue, labels must exist), `fail_next` and `calls` for later phases' tests
- CLI: `labels ensure`, `issues list`, and three network checks in `validate` (twelve total)
- New setting `github.request_timeout_ms` (default 30000)

No Claude interaction yet; the agent runner is Phase 3.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Append the session link the executing harness requires after the generated-with line.

- [ ] **Step 4: Open the PR via the REST API (the CLI's `pr create` is blocked in this repo)**

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='Phase 2: GitHub adapter and label state machine' \
  -f head='phase-2-github-adapter' -f base='main' \
  -F body=@/tmp/issuebot-phase-2-pr.md
```

Then confirm with `gh pr view --json title --jq '.title'` and watch CI with `gh pr checks --watch`. CI must be green before handing over for human review. Do not merge.
