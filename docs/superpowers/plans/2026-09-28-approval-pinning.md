# Approval Pinning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An issue whose title or body was edited after a maintainer approved it (by applying `issuebot/todo` or `issuebot/rework`) is handed back to a human instead of being dispatched, and GitHub's own edit and label history is the only record consulted.

**Architecture:** A new adapter read, `approval_evidence(number)`, returns the issue's label events and text edits from GitHub's timeline and `userContentEdits`. A pure module, `orchestrator/approval.py`, decides `Approved` or `Unapproved` from that evidence. `Orchestrator._dispatch` asks it before binding an account or claiming; an `Unapproved` verdict runs a new `actions.unapproved_escape` (workpad block, then the state label comes off, then a `Blocked` event), shaped like `blocked_escape`. Nothing is stored: re-approval is a fresh label event after the edit.

**Tech Stack:** Python 3.14, `uv`, pytest (hermetic), `gh api graphql` through the existing `GhCliAdapter` runner seam, the in-memory `FakeGitHub`.

**Spec:** `docs/superpowers/specs/2026-09-28-tracker-text-admission-design.md`, section 1.

## Global Constraints

- `uv run ruff check . && uv run ruff format --check .` clean; `uv run pytest` green (hermetic: no network, no Docker).
- Every adapter method exists on both `GhCliAdapter` (`src/issuebot/github/ghcli.py`) and `FakeGitHub` (`src/issuebot/github/fake.py`), and is declared on the `GitHubAdapter` protocol (`src/issuebot/github/adapter.py`).
- Escapes are label-first (#128, #157): a non-retryable failure to write the note still moves the label; a retryable one (`transport`, `rate_limited`) is raised for the next tick. Reuse `_escape_note` and `_report_note_failure` in `actions.py`; do not write a third copy.
- Bounded reads (#110): every paginated GraphQL read stops at `MAX_TIMELINE_PAGES` and raises `GitHubError("response", ...)` past it.
- Logins and label names compare case-insensitively, as `count_own_label_additions` does.
- Never name the private sibling repository from the advisory anywhere in code, tests, docs or commit messages; refer to the advisory as GHSA-jm8h-q3j6-p8xp.
- `CLAUDE.md` has a size budget (`tests/test_instruction_bounds.py`); module design goes in `docs/package-layout.md`, not `CLAUDE.md`.
- Commit messages end with the attribution lines the session was given; PRs are opened with `gh api repos/{owner}/{repo}/pulls -X POST` and a body file written in a separate Bash call, never `gh pr create`.

## Review Focus

1. **An edit at exactly the approval's timestamp** — GitHub records label and edit times to the second; an edit `at == approval.at` must count as *before* the approval (not after), or a maintainer's "edit, then label" in the same second is refused for ever. Pinned in Task 2.
2. **`userContentEdits` arrives newest-first** — the adapter must not assume chronological order; the assessment sorts by `at`. Pinned in Task 3 (the adapter test feeds edits out of order).
3. **A deleted editor account** (`editor: null`) — an edit nobody can be credited with is *not* the approver's, so it un-approves. Pinned in Task 2.
4. **An orphaned `in_progress` issue on resume** — the admitting event is the human's `todo`/`rework` *before* issuebot's own `in_progress`; issuebot's own label events are never approvals. Pinned in Task 2 and Task 6.
5. **The un-approval note fails non-retryably** — the label still comes off (label-first), and the `Blocked` event is still published; a second tick does not write a second block. Pinned in Task 5.

---

### Task 1: The evidence models and the adapter protocol

**Files:**
- Modify: `src/issuebot/github/models.py` (after `class Comment`)
- Modify: `src/issuebot/github/adapter.py` (imports; new method after `count_own_label_additions`)
- Modify: `src/issuebot/github/__init__.py` (export the three models)
- Test: `tests/test_github_models.py` (create if absent)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True, kw_only=True, slots=True)
  class LabelApplied:
      label: str
      actor: str | None  # None: GitHub has deleted the account
      at: datetime


  @dataclass(frozen=True, kw_only=True, slots=True)
  class TextEdit:
      what: Literal["body", "title"]
      editor: str | None  # None: GitHub has deleted the account
      at: datetime


  @dataclass(frozen=True, kw_only=True, slots=True)
  class ApprovalEvidence:
      label_events: tuple[LabelApplied, ...]  # as GitHub lists them, oldest first
      edits: tuple[TextEdit, ...]  # any order; the assessment sorts
  ```
  and on `GitHubAdapter`:
  ```python
  async def approval_evidence(self, number: int) -> ApprovalEvidence:
      """Every label addition and every title or body edit GitHub records for the issue."""
      ...


  async def own_login(self) -> str:
      """The login of the account the adapter acts as."""
      ...
  ```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_github_models.py
from datetime import UTC, datetime

from issuebot.github import ApprovalEvidence, LabelApplied, TextEdit


def test_approval_evidence_is_frozen_and_keyword_only() -> None:
    at = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
    evidence = ApprovalEvidence(
        label_events=(LabelApplied(label="issuebot/todo", actor="maintainer", at=at),),
        edits=(TextEdit(what="body", editor=None, at=at),),
    )
    assert evidence.label_events[0].actor == "maintainer"
    assert evidence.edits[0].editor is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_github_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'ApprovalEvidence'`

- [ ] **Step 3: Add the models, export them, and declare the protocol methods**

In `src/issuebot/github/models.py`, after `class Comment` (add `Literal` to the `typing` import if it is not already there):

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class LabelApplied:
    """One label addition as GitHub's timeline records it."""

    label: str
    actor: str | None  # None once GitHub has deleted the account
    at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class TextEdit:
    """One edit to the text a session is handed: the body (``userContentEdits``) or the title
    (a ``RenamedTitleEvent``)."""

    what: Literal["body", "title"]
    editor: str | None  # None once GitHub has deleted the account
    at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class ApprovalEvidence:
    """What decides whether the text a session would act on is the text a human approved.

    ``label_events`` are oldest first, as GitHub lists the timeline. ``edits`` carry no order
    promise -- ``userContentEdits`` answers newest first -- so the assessment sorts them.
    """

    label_events: tuple[LabelApplied, ...]
    edits: tuple[TextEdit, ...]
```

In `src/issuebot/github/__init__.py`, add `ApprovalEvidence`, `LabelApplied` and `TextEdit` to the `from issuebot.github.models import (...)` block and to `__all__`.

In `src/issuebot/github/adapter.py`, add `ApprovalEvidence` to the models import and, after `count_own_label_additions`:

```python
async def approval_evidence(self, number: int) -> ApprovalEvidence:
    """Every label addition and every title or body edit GitHub records for the issue.

    What the orchestrator's approval check reads (GHSA-jm8h-q3j6-p8xp): the record is
    GitHub's, so nothing a session writes can change it, and nothing issuebot stores can
    drift from it.
    """
    ...


async def own_login(self) -> str:
    """The login of the account the adapter acts as."""
    ...
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_github_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/github/models.py src/issuebot/github/adapter.py src/issuebot/github/__init__.py tests/test_github_models.py
git commit -m "github: the evidence an approval check reads"
```

---

### Task 2: The pure assessment

**Files:**
- Create: `src/issuebot/orchestrator/approval.py`
- Test: `tests/test_orchestrator_approval.py`

**Interfaces:**
- Consumes: `ApprovalEvidence`, `LabelApplied`, `TextEdit` from Task 1.
- Produces:
  ```python
  @dataclass(frozen=True, kw_only=True, slots=True)
  class Approved:
      approver: str
      at: datetime


  @dataclass(frozen=True, kw_only=True, slots=True)
  class Unapproved:
      reason: str  # one line, safe to publish
      approval: LabelApplied | None
      edit: TextEdit | None


  def assess(
      evidence: ApprovalEvidence, *, admitting: Sequence[str], own_login: str
  ) -> Approved | Unapproved: ...
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orchestrator_approval.py
"""The approval check is pure: evidence in, a verdict out (GHSA-jm8h-q3j6-p8xp)."""

from datetime import UTC, datetime, timedelta

from issuebot.github import ApprovalEvidence, LabelApplied, TextEdit
from issuebot.orchestrator.approval import Approved, Unapproved, assess

T0 = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
ADMITTING = ("issuebot/todo", "issuebot/rework")


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def labelled(label: str, actor: str | None, minutes: int) -> LabelApplied:
    return LabelApplied(label=label, actor=actor, at=at(minutes))


def edited(what: str, editor: str | None, minutes: int) -> TextEdit:
    return TextEdit(what=what, editor=editor, at=at(minutes))  # type: ignore[arg-type]


def verdict(*events: LabelApplied, edits: tuple[TextEdit, ...] = ()) -> Approved | Unapproved:
    return assess(
        ApprovalEvidence(label_events=events, edits=edits), admitting=ADMITTING, own_login="bot"
    )


def test_a_labelled_issue_with_no_edits_is_approved() -> None:
    result = verdict(labelled("issuebot/todo", "maintainer", 0))
    assert result == Approved(approver="maintainer", at=at(0))


def test_an_edit_before_the_label_is_the_text_that_was_approved() -> None:
    result = verdict(
        labelled("issuebot/todo", "maintainer", 5), edits=(edited("body", "reporter", 1),)
    )
    assert isinstance(result, Approved)


def test_an_edit_at_the_labels_own_second_counts_as_before_it() -> None:
    """GitHub stamps both to the second; "edit, then label" in one second is not an edit after."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 5), edits=(edited("body", "reporter", 5),)
    )
    assert isinstance(result, Approved)


def test_the_authors_edit_after_the_label_un_approves() -> None:
    approval = labelled("issuebot/todo", "maintainer", 0)
    edit = edited("body", "reporter", 10)
    result = verdict(approval, edits=(edit,))
    assert result == Unapproved(
        reason=(
            "body edited at 2026-09-28T09:10:00Z by reporter, after maintainer applied "
            "`issuebot/todo` at 2026-09-28T09:00:00Z"
        ),
        approval=approval,
        edit=edit,
    )


def test_a_title_rename_after_the_label_un_approves() -> None:
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0), edits=(edited("title", "reporter", 1),)
    )
    assert isinstance(result, Unapproved) and result.reason.startswith("title edited at ")


def test_the_approver_editing_what_they_approved_is_fine() -> None:
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0), edits=(edited("body", "MAINTAINER", 10),)
    )
    assert isinstance(result, Approved)


def test_another_maintainers_edit_un_approves_too() -> None:
    """The false positive the spec names: one relabel, rather than a permission lookup."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0), edits=(edited("body", "colleague", 10),)
    )
    assert isinstance(result, Unapproved)


def test_an_edit_by_a_deleted_account_un_approves() -> None:
    result = verdict(labelled("issuebot/todo", "maintainer", 0), edits=(edited("body", None, 10),))
    assert isinstance(result, Unapproved) and " by an account GitHub has deleted, " in result.reason


def test_edits_are_assessed_in_time_order_whatever_order_they_arrive() -> None:
    """``userContentEdits`` answers newest first; only the edits after the approval matter."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 5),
        edits=(edited("body", "maintainer", 20), edited("body", "reporter", 1)),
    )
    assert isinstance(result, Approved)


def test_the_latest_human_admitting_label_is_the_approval() -> None:
    """todo, an edit, then rework by a human: the rework event approves the edited text."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0),
        labelled("issuebot/in-progress", "bot", 1),
        labelled("issuebot/review", "bot", 2),
        labelled("issuebot/rework", "reviewer", 10),
        edits=(edited("body", "reporter", 5),),
    )
    assert result == Approved(approver="reviewer", at=at(10))


def test_issuebots_own_label_events_are_not_approvals() -> None:
    """The conflict bounce applies rework itself; that must not launder an edit."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0),
        labelled("issuebot/rework", "BOT", 10),
        edits=(edited("body", "reporter", 5),),
    )
    assert isinstance(result, Unapproved) and result.approval == labelled(
        "issuebot/todo", "maintainer", 0
    )


def test_an_admitting_label_nobody_applied_is_not_approved() -> None:
    result = verdict(labelled("issuebot/in-progress", "bot", 0))
    assert result == Unapproved(
        reason="no account other than bot has applied `issuebot/todo` or `issuebot/rework`",
        approval=None,
        edit=None,
    )


def test_a_label_by_a_deleted_account_is_not_an_approval() -> None:
    result = verdict(labelled("issuebot/todo", None, 0))
    assert isinstance(result, Unapproved) and result.approval is None


def test_label_names_compare_case_insensitively() -> None:
    result = verdict(labelled("Issuebot/Todo", "maintainer", 0))
    assert isinstance(result, Approved)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_orchestrator_approval.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.orchestrator.approval'`

- [ ] **Step 3: Write the module**

```python
# src/issuebot/orchestrator/approval.py
"""Is the text a session would act on the text a human approved? (GHSA-jm8h-q3j6-p8xp)

A human applying ``issuebot/todo`` or ``issuebot/rework`` approves the issue's title and body
*as they stand*. Nothing pins that text, so this module reads GitHub's own record instead:
the latest human application of an admitting label is the approval, and any edit after it by
anyone but the approver un-approves the issue. The approver editing what they approved is
fine -- a solo operator labels and then tightens the wording. Another maintainer's edit is
refused too, which is a false positive the design names: one relabel, rather than a
permission lookup per editor, where admitting an outsider's edit would be the advisory.

Pure. The orchestrator fetches the evidence and acts on the verdict.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from issuebot.github import ApprovalEvidence, LabelApplied, TextEdit

DELETED_ACCOUNT = "an account GitHub has deleted"


@dataclass(frozen=True, kw_only=True, slots=True)
class Approved:
    approver: str
    at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class Unapproved:
    """Why the issue is not dispatched. ``reason`` is one line and safe to publish."""

    reason: str
    approval: LabelApplied | None
    edit: TextEdit | None


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def assess(
    evidence: ApprovalEvidence, *, admitting: Sequence[str], own_login: str
) -> Approved | Unapproved:
    """The verdict for one issue.

    ``admitting`` names the labels a human applies to hand an issue to issuebot; ``own_login``
    is the account issuebot acts as, whose own label events (the conflict bounce's ``rework``)
    are never approvals. Comparisons are case-insensitive, as the rest of the adapter's are.
    """
    wanted = {label.lower() for label in admitting}
    own = own_login.lower()
    approvals = [
        event
        for event in evidence.label_events
        if event.label.lower() in wanted and event.actor is not None and event.actor.lower() != own
    ]
    if not approvals:
        names = " or ".join(f"`{label}`" for label in admitting)
        return Unapproved(
            reason=f"no account other than {own_login} has applied {names}",
            approval=None,
            edit=None,
        )
    approval = approvals[-1]
    assert approval.actor is not None  # filtered above; for the type checker
    for edit in sorted(evidence.edits, key=lambda item: item.at):
        if edit.at <= approval.at:
            continue
        if edit.editor is not None and edit.editor.lower() == approval.actor.lower():
            continue
        editor = edit.editor if edit.editor is not None else DELETED_ACCOUNT
        return Unapproved(
            reason=(
                f"{edit.what} edited at {_stamp(edit.at)} by {editor}, after {approval.actor} "
                f"applied `{approval.label}` at {_stamp(approval.at)}"
            ),
            approval=approval,
            edit=edit,
        )
    return Approved(approver=approval.actor, at=approval.at)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_approval.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/issuebot/orchestrator/approval.py tests/test_orchestrator_approval.py && uv run ruff format src/issuebot/orchestrator/approval.py tests/test_orchestrator_approval.py
git add src/issuebot/orchestrator/approval.py tests/test_orchestrator_approval.py
git commit -m "orchestrator: assess whether the text a session would act on is the text a human approved"
```

---

### Task 3: `approval_evidence` and `own_login` on the gh adapter

**Files:**
- Modify: `src/issuebot/github/ghcli.py` (queries near `LABEL_EVENTS_QUERY`; method after `count_own_label_additions`)
- Test: `tests/test_github_ghcli.py` (after the `count_own_label_additions` tests, ~line 1200)

**Interfaces:**
- Consumes: `ApprovalEvidence`, `LabelApplied`, `TextEdit` (Task 1); the existing `_graphql`, `_dig`, `MAX_TIMELINE_PAGES`, `PAGE_SIZE`; `own_login` already exists on `GhCliAdapter` (line ~237) and needs no change.
- Produces: `GhCliAdapter.approval_evidence(number) -> ApprovalEvidence`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_github_ghcli.py`, after `test_count_own_label_additions_probes_the_login_once`:

```python
# --- approval evidence ----------------------------------------------------------------


def _approval_timeline_page(nodes: list[dict[str, object]], *, end_cursor: str | None) -> str:
    return _timeline_page(nodes, end_cursor=end_cursor)


def _edits_page(nodes: list[dict[str, object]], *, end_cursor: str | None) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "issue": {
                        "userContentEdits": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": end_cursor is not None,
                                "endCursor": end_cursor,
                            },
                        }
                    }
                }
            }
        }
    )


async def test_approval_evidence_reads_labels_renames_and_edits() -> None:
    """Two bounded reads (GHSA-jm8h-q3j6-p8xp): the timeline for label additions and title
    renames, and ``userContentEdits`` for the body. Edits keep GitHub's order; the assessment
    sorts."""
    runner = StubRunner()
    runner.on(
        both(has("RENAMED_TITLE_EVENT"), lacks("cursor=")),
        stdout=_approval_timeline_page(
            [
                {
                    "__typename": "LabeledEvent",
                    "createdAt": "2026-09-28T09:00:00Z",
                    "actor": {"login": "maintainer"},
                    "label": {"name": "issuebot/todo"},
                },
                {
                    "__typename": "RenamedTitleEvent",
                    "createdAt": "2026-09-28T09:05:00Z",
                    "actor": None,
                },
                {
                    "__typename": "LabeledEvent",
                    "createdAt": "2026-09-28T09:06:00Z",
                    "actor": {"login": LOGIN},
                    "label": None,
                },
                {},
            ],
            end_cursor="t1",
        ),
    )
    runner.on(
        both(has("RENAMED_TITLE_EVENT"), arg("cursor=t1")),
        stdout=_approval_timeline_page(
            [
                {
                    "__typename": "LabeledEvent",
                    "createdAt": "2026-09-28T09:10:00Z",
                    "actor": {"login": LOGIN},
                    "label": {"name": "issuebot/in-progress"},
                }
            ],
            end_cursor=None,
        ),
    )
    runner.on(
        both(has("userContentEdits"), lacks("cursor=")),
        stdout=_edits_page(
            [
                {"editedAt": "2026-09-28T09:20:00Z", "editor": {"login": "reporter"}},
                {"editedAt": "2026-09-28T08:00:00Z", "editor": None},
                {"editor": {"login": "nobody"}},  # no timestamp: not an edit issuebot can place
            ],
            end_cursor=None,
        ),
    )
    evidence = await make_adapter(runner).approval_evidence(42)
    assert [(e.label, e.actor, e.at.isoformat()) for e in evidence.label_events] == [
        ("issuebot/todo", "maintainer", "2026-09-28T09:00:00+00:00"),
        ("issuebot/in-progress", LOGIN, "2026-09-28T09:10:00+00:00"),
    ]
    assert [(e.what, e.editor, e.at.isoformat()) for e in evidence.edits] == [
        ("title", None, "2026-09-28T09:05:00+00:00"),
        ("body", "reporter", "2026-09-28T09:20:00+00:00"),
        ("body", None, "2026-09-28T08:00:00+00:00"),
    ]
    first = runner.argv(0)
    assert first[:3] == ["api", "graphql", "-f"]
    assert (
        "timelineItems(itemTypes: [LABELED_EVENT, RENAMED_TITLE_EVENT], first: 100, after: $cursor)"
    ) in query_of(first)
    assert first[first.index("number=42") - 1] == "-F"
    assert len(runner.calls) == 3


async def test_approval_evidence_gives_up_past_the_page_cap() -> None:
    runner = StubRunner()
    for page in range(0, MAX_TIMELINE_PAGES + 2):
        predicate = lacks("cursor=") if page == 0 else arg(f"cursor=t{page}")
        runner.on(
            both(has("RENAMED_TITLE_EVENT"), predicate),
            stdout=_approval_timeline_page([], end_cursor=f"t{page + 1}"),
        )
    with pytest.raises(GitHubError) as excinfo:
        await make_adapter(runner).approval_evidence(42)
    assert excinfo.value.category == "response"
    assert str(excinfo.value).endswith(
        f"label and title history of #42 runs past {MAX_TIMELINE_PAGES * 100} events"
    )


async def test_approval_evidence_gives_up_past_the_edit_page_cap() -> None:
    runner = StubRunner()
    runner.on(has("RENAMED_TITLE_EVENT"), stdout=_approval_timeline_page([], end_cursor=None))
    for page in range(0, MAX_TIMELINE_PAGES + 2):
        predicate = lacks("cursor=") if page == 0 else arg(f"cursor=e{page}")
        runner.on(
            both(has("userContentEdits"), predicate),
            stdout=_edits_page([], end_cursor=f"e{page + 1}"),
        )
    with pytest.raises(GitHubError) as excinfo:
        await make_adapter(runner).approval_evidence(42)
    assert excinfo.value.category == "response"
    assert str(excinfo.value).endswith(
        f"edit history of #42 runs past {MAX_TIMELINE_PAGES * 100} edits"
    )


@pytest.mark.parametrize("stdout", ["{}", '{"data": {"repository": {"issue": null}}}'])
async def test_approval_evidence_rejects_a_malformed_response(stdout: str) -> None:
    runner = StubRunner()
    runner.on(has("graphql"), stdout=stdout)
    with pytest.raises(GitHubError) as excinfo:
        await make_adapter(runner).approval_evidence(42)
    assert excinfo.value.category == "response"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_github_ghcli.py -k approval_evidence -v`
Expected: FAIL with `AttributeError: 'GhCliAdapter' object has no attribute 'approval_evidence'`

- [ ] **Step 3: Add the queries and the method**

In `src/issuebot/github/ghcli.py`, after `LABEL_EVENTS_QUERY`:

```python
# The issue's label additions and title renames, oldest first, for the approval check
# (GHSA-jm8h-q3j6-p8xp): which human last handed the issue to issuebot, and whether the
# title moved after that.
APPROVAL_TIMELINE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $cursor: String) {\n"
    "  repository(owner: $owner, name: $name) {\n"
    "    issue(number: $number) {\n"
    "      timelineItems(itemTypes: [LABELED_EVENT, RENAMED_TITLE_EVENT], "
    f"first: {PAGE_SIZE}, after: $cursor) {{\n"
    "        nodes {\n"
    "          __typename\n"
    "          ... on LabeledEvent { createdAt actor { login } label { name } }\n"
    "          ... on RenamedTitleEvent { createdAt actor { login } }\n"
    "        }\n"
    "        pageInfo { hasNextPage endCursor }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}\n"
)
# The issue body's edit history: who changed the description, and when. GitHub answers it
# newest first; the assessment sorts, so the order here is not relied on.
CONTENT_EDITS_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $cursor: String) {\n"
    "  repository(owner: $owner, name: $name) {\n"
    "    issue(number: $number) {\n"
    f"      userContentEdits(first: {PAGE_SIZE}, after: $cursor) {{\n"
    "        nodes { editedAt editor { login } }\n"
    "        pageInfo { hasNextPage endCursor }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}\n"
)
```

After `count_own_label_additions`:

```python
async def approval_evidence(self, number: int) -> ApprovalEvidence:
    """Every label addition, title rename and body edit GitHub records for the issue.

    Two paginated reads, each bounded at ``MAX_TIMELINE_PAGES`` under #110's rule: the
    timeline for ``LabeledEvent`` and ``RenamedTitleEvent`` items, and ``userContentEdits``
    for the body. A node without a timestamp is skipped, since an edit issuebot cannot
    place in time is one it cannot assess; a deleted actor is kept as ``None``, because
    "somebody GitHub no longer names" is a fact the assessment acts on.
    """
    self._log.debug("approval_evidence", issue_number=number)
    label_events: list[LabelApplied] = []
    edits: list[TextEdit] = []
    async for node in self._paginate(
        APPROVAL_TIMELINE_QUERY,
        number,
        ("repository", "issue", "timelineItems"),
        overflow=f"label and title history of #{number} runs past "
        f"{MAX_TIMELINE_PAGES * PAGE_SIZE} events",
    ):
        at = _optional_timestamp(node.get("createdAt"))
        if at is None:
            continue
        actor = _dig(node, "actor", "login")
        login = actor if isinstance(actor, str) and actor else None
        if node.get("__typename") == "RenamedTitleEvent":
            edits.append(TextEdit(what="title", editor=login, at=at))
            continue
        name = _dig(node, "label", "name")
        if isinstance(name, str) and name:
            label_events.append(LabelApplied(label=name, actor=login, at=at))
    async for node in self._paginate(
        CONTENT_EDITS_QUERY,
        number,
        ("repository", "issue", "userContentEdits"),
        overflow=f"edit history of #{number} runs past {MAX_TIMELINE_PAGES * PAGE_SIZE} edits",
    ):
        at = _optional_timestamp(node.get("editedAt"))
        if at is None:
            continue
        editor = _dig(node, "editor", "login")
        edits.append(
            TextEdit(
                what="body", editor=editor if isinstance(editor, str) and editor else None, at=at
            )
        )
    return ApprovalEvidence(label_events=tuple(label_events), edits=tuple(edits))


async def _paginate(
    self, query: str, number: int, path: tuple[str, ...], *, overflow: str
) -> AsyncIterator[Mapping[str, Any]]:
    """The mapping nodes of one issue connection, page by page, under the timeline cap."""
    cursor: str | None = None
    for _page_number in range(MAX_TIMELINE_PAGES):
        variables: dict[str, str | int] = {
            "owner": self._owner,
            "name": self._name,
            "number": number,
        }
        if cursor:
            variables["cursor"] = cursor
        data = await self._graphql(query, variables)
        connection = _dig(data, *path)
        if not isinstance(connection, Mapping):
            raise GitHubError("response", f"GraphQL response has no {'.'.join(path[1:])}")
        for node in connection.get("nodes") or []:
            if isinstance(node, Mapping):
                yield node
        page = connection.get("pageInfo")
        page = page if isinstance(page, Mapping) else {}
        if not page.get("hasNextPage"):
            return
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise GitHubError("response", "GraphQL page has hasNextPage without endCursor")
    raise GitHubError("response", overflow)
```

Add `AsyncIterator` to the `collections.abc` import, `ApprovalEvidence`, `LabelApplied`, `TextEdit` to the models import, and import `_optional_timestamp` from `issuebot.github.normalise` (it is module-private there; rename it to `optional_timestamp` in `normalise.py` and update its two callers there, or add a one-line public alias `optional_timestamp = _optional_timestamp` — pick the rename).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_ghcli.py -k "approval_evidence or count_own_label" -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/issuebot/github tests/test_github_ghcli.py && uv run ruff format src/issuebot/github tests/test_github_ghcli.py
git add src/issuebot/github/ghcli.py src/issuebot/github/normalise.py tests/test_github_ghcli.py
git commit -m "github: read an issue's label, rename and edit history for the approval check"
```

---

### Task 4: The fake records timestamps, edits and renames

**Files:**
- Modify: `src/issuebot/github/fake.py` (`_FakeIssue`, `set_state`, `human_set_state`, `human_add_label`, `count_own_label_additions`; new `own_login`, `approval_evidence`, `human_edit_body`, `human_rename`)
- Test: `tests/test_github_fake.py`

**Interfaces:**
- Consumes: Task 1's models.
- Produces, on `FakeGitHub`:
  ```python
  async def own_login(self) -> str
  async def approval_evidence(self, number: int) -> ApprovalEvidence
  def human_edit_body(self, number: int, body: str | None, *, editor: str | None = "reporter") -> None
  def human_rename(self, number: int, title: str, *, actor: str | None = "reporter") -> None
  ```
  `_FakeIssue.label_events` becomes `list[tuple[str, str, datetime]]` — `(actor, label, at)`; `_FakeIssue.edits: list[TextEdit]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_github_fake.py`:

```python
async def test_approval_evidence_records_labels_edits_and_renames() -> None:
    clock = _Clock()  # if the file has no clock helper, use: now=lambda: stamps.pop(0)
    fake = FakeGitHub(GitHubSettings(repo="example/repo"), now=clock, login="bot")
    fake.add_issue("Task", body="original", number=42)
    clock.advance(60)
    fake.human_set_state(42, StateLabel.TODO, actor="maintainer")
    clock.advance(60)
    fake.human_edit_body(42, "edited", editor="reporter")
    clock.advance(60)
    fake.human_rename(42, "Renamed", actor=None)
    clock.advance(60)
    await fake.set_state(42, StateLabel.IN_PROGRESS)
    evidence = await fake.approval_evidence(42)
    assert [(e.label, e.actor) for e in evidence.label_events] == [
        ("issuebot/todo", "maintainer"),
        ("issuebot/in-progress", "bot"),
    ]
    assert evidence.label_events[0].at < evidence.label_events[1].at
    assert [(e.what, e.editor) for e in evidence.edits] == [("body", "reporter"), ("title", None)]
    assert fake.issue(42).body == "edited" and fake.issue(42).title == "Renamed"
    assert await fake.own_login() == "bot"
    assert await fake.count_own_label_additions(42, "issuebot/in-progress") == 1
```

Look at the top of `tests/test_github_fake.py` for how other tests drive the fake's clock; if there is no `_Clock`, define one in the test file:

```python
class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_github_fake.py -k approval_evidence -v`
Expected: FAIL with `AttributeError: 'FakeGitHub' object has no attribute 'human_edit_body'`

- [ ] **Step 3: Extend the fake**

In `src/issuebot/github/fake.py`:

```python
@dataclass
class _FakeIssue:
    ...
    # (actor, label, at) for every label added, oldest first: GitHub's timeline, in miniature.
    label_events: list[tuple[str, str, datetime]] = field(default_factory=list)
    # Body edits and title renames, oldest first, as ``approval_evidence`` reports them.
    edits: list[TextEdit] = field(default_factory=list)
```

Every `record.label_events.append((actor, name))` becomes `record.label_events.append((actor, name, self._now()))` (three sites: `set_state`, `human_set_state`, `human_add_label`), and `count_own_label_additions` unpacks three: `for actor, name, _at in record.label_events`.

New methods (after `count_own_label_additions`):

```python
async def own_login(self) -> str:
    return self.login


async def approval_evidence(self, number: int) -> ApprovalEvidence:
    self._enter("approval_evidence", number)
    record = self._require_issue(number)
    return ApprovalEvidence(
        label_events=tuple(
            LabelApplied(label=name, actor=actor, at=at) for actor, name, at in record.label_events
        ),
        edits=tuple(record.edits),
    )
```

And beside `human_remove_label`:

```python
def human_edit_body(
    self, number: int, body: str | None, *, editor: str | None = "reporter"
) -> None:
    """An edit to the description by someone else; ``editor`` is who GitHub credits."""
    record = self._require_issue(number)
    record.body = body
    record.updated_at = self._now()
    record.edits.append(TextEdit(what="body", editor=editor, at=record.updated_at))


def human_rename(self, number: int, title: str, *, actor: str | None = "reporter") -> None:
    record = self._require_issue(number)
    record.title = title
    record.updated_at = self._now()
    record.edits.append(TextEdit(what="title", editor=actor, at=record.updated_at))
```

Add `ApprovalEvidence`, `LabelApplied`, `TextEdit` to the models import.

- [ ] **Step 4: Run the fake's and the actions' tests**

Run: `uv run pytest tests/test_github_fake.py tests/test_orchestrator_actions.py -v`
Expected: PASS (the conflict-bounce tests still count label events through the three-tuple)

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/github/fake.py tests/test_github_fake.py
git commit -m "github/fake: timestamped label events, body edits and renames"
```

---

### Task 5: `unapproved_escape`

**Files:**
- Modify: `src/issuebot/orchestrator/actions.py` (after `blocked_escape`, before `BUDGET_HEADING`)
- Test: `tests/test_orchestrator_actions.py`

**Interfaces:**
- Consumes: `Unapproved` (Task 2), `_escape_note`, `_report_note_failure`, `_stamp`, `EscapeOutcome`, `state_label_name`, `pr_url` (all already in `actions.py`), `adapter.clear_state`.
- Produces:
  ```python
  UNAPPROVED_HEADING = "### Issuebot unapproved edit ("
  def unapproved_block(verdict: Unapproved, now: datetime, labels: GitHubLabels) -> str
  async def unapproved_escape(adapter, bus, issue: Issue, verdict: Unapproved, *, now: datetime) -> EscapeOutcome
  ```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator_actions.py` (imports: `Unapproved` from `issuebot.orchestrator.approval`, `LabelApplied`, `TextEdit` from `issuebot.github`, `UNAPPROVED_HEADING`, `unapproved_block`, `unapproved_escape` from `issuebot.orchestrator.actions`):

```python
# --- unapproved escape ----------------------------------------------------------------

EDIT_VERDICT = Unapproved(
    reason=(
        "body edited at 2026-09-28T09:10:00Z by reporter, after maintainer applied "
        "`issuebot/todo` at 2026-09-28T09:00:00Z"
    ),
    approval=LabelApplied(
        label="issuebot/todo", actor="maintainer", at=datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
    ),
    edit=TextEdit(what="body", editor="reporter", at=datetime(2026, 9, 28, 9, 10, tzinfo=UTC)),
)


def test_unapproved_block_names_the_edit_and_the_way_back() -> None:
    block = unapproved_block(EDIT_VERDICT, NOW, LABELS)
    assert block.startswith(UNAPPROVED_HEADING)
    assert EDIT_VERDICT.reason in block
    assert "issuebot has removed the label" in block
    assert "apply `issuebot/todo` again" in block


async def test_unapproved_escape_notes_and_removes_the_label(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    await h.github.comment(42, f"{WORKPAD_MARKER}\n\n### Plan\n")
    h.github.calls.clear()
    issue = h.github.issue(42)
    assert await unapproved_escape(h.github, h.bus, issue, EDIT_VERDICT, now=NOW) == "applied"
    body = h.github.comments_for(42)[0].body
    assert body.count(UNAPPROVED_HEADING) == 1 and EDIT_VERDICT.reason in body
    assert h.github.issue(42).state is None and h.github.issue(42).labels == ()
    assert h.recorder.kinds == ["state_changed", "blocked"]
    changed = h.recorder.events[0]
    assert isinstance(changed, StateChanged)
    assert (changed.from_label, changed.to_label, changed.actor) == (
        "issuebot/todo",
        None,
        "issuebot",
    )
    blocked = h.recorder.events[1]
    assert isinstance(blocked, Blocked) and blocked.reason == EDIT_VERDICT.reason


async def test_unapproved_escape_is_idempotent_per_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    issue = h.github.issue(42)
    with monkeypatch.context() as patch:
        fail_on(h, "clear_state", patch)
        assert await unapproved_escape(h.github, h.bus, issue, EDIT_VERDICT, now=NOW) == "failed"
    assert h.github.issue(42).state is StateLabel.TODO
    assert h.recorder.events == []
    first = h.github.comments_for(42)[0].body
    assert await unapproved_escape(h.github, h.bus, issue, EDIT_VERDICT, now=NOW) == "applied"
    assert h.github.comments_for(42)[0].body == first
    assert h.github.issue(42).state is None


@pytest.mark.parametrize("category", ["response", "not_found", "auth", "status", "config"])
async def test_unapproved_escape_removes_the_label_when_the_note_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, category: ErrorCategory
) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    issue = h.github.issue(42)
    with monkeypatch.context() as patch:
        fail_on(h, "comment", patch, category)
        assert await unapproved_escape(h.github, h.bus, issue, EDIT_VERDICT, now=NOW) == "applied"
    assert h.github.issue(42).state is None
    assert h.recorder.kinds == ["state_changed", "blocked"]


@pytest.mark.parametrize("category", ["transport", "rate_limited"])
async def test_unapproved_escape_retries_a_retryable_note_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, category: ErrorCategory
) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", labels=("issuebot/todo",), number=42)
    issue = h.github.issue(42)
    with monkeypatch.context() as patch:
        fail_on(h, "find_workpad_comment", patch, category)
        assert await unapproved_escape(h.github, h.bus, issue, EDIT_VERDICT, now=NOW) == "failed"
    assert h.github.issue(42).state is StateLabel.TODO
    assert h.recorder.events == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_orchestrator_actions.py -k unapproved -v`
Expected: FAIL with `ImportError: cannot import name 'unapproved_escape'`

- [ ] **Step 3: Write the escape**

In `src/issuebot/orchestrator/actions.py`, after `blocked_escape` and before the `BUDGET_HEADING` comment:

```python
# The heading of the block the approval check writes (GHSA-jm8h-q3j6-p8xp). The *reason* is
# what makes two blocks the same block, as with the budget escape: it names the edit and the
# approval to the second, so a later edit gets its own block and the same edit never two.
UNAPPROVED_HEADING = "### Issuebot unapproved edit ("


def unapproved_block(verdict: Unapproved, now: datetime, labels: GitHubLabels) -> str:
    admitting = f"`{labels.todo}`"
    if verdict.approval is not None and verdict.approval.label.lower() == labels.rework.lower():
        admitting = f"`{labels.rework}`"
    return (
        f"{UNAPPROVED_HEADING}{_stamp(now)})\n\n"
        f"{verdict.reason}. issuebot has removed the label: the text a session would act on is "
        "no longer the text that was approved. Read the current title and description; if they "
        f"are what you want done, apply {admitting} again.\n"
    )


async def unapproved_escape(
    adapter: GitHubAdapter,
    bus: EventBus,
    issue: Issue,
    verdict: Unapproved,
    *,
    now: datetime,
) -> EscapeOutcome:
    """Hand an issue whose approved text has changed back to a human: note, then no label.

    Label-first like ``blocked_escape`` (#128, #157): the note is best effort and the label
    removal is the point, since an issue nobody has approved must not be a candidate on the
    next tick. ``issue`` is the record the poll just returned; the evidence behind
    ``verdict`` was read a moment ago, so there is no second fetch here. Re-approval is the
    human applying the admitting label again, which is a new label event after the edit.
    """
    log = get_logger(__name__)
    block = unapproved_block(verdict, now, adapter.labels)
    try:
        note_failure = await _escape_note(
            adapter, issue.number, block, lambda body: verdict.reason in body
        )
        await adapter.clear_state(issue.number)
    except GitHubError as exc:
        log.warning(
            "unapproved_escape_failed",
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            error=str(exc),
            category=exc.category,
        )
        return "failed"
    bus.publish(
        StateChanged(
            issue_number=issue.number,
            issue_identifier=issue.identifier,
            from_label=state_label_name(issue),
            to_label=None,
            actor="issuebot",
            pr_url=pr_url(issue),
        )
    )
    bus.publish(
        Blocked(issue_number=issue.number, issue_identifier=issue.identifier, reason=verdict.reason)
    )
    if note_failure is not None:
        await _report_note_failure(adapter, issue, block, note_failure, prefix="unapproved_escape")
    log.info(
        "unapproved_escape_applied",
        issue_number=issue.number,
        issue_identifier=issue.identifier,
        reason=verdict.reason,
    )
    return "applied"
```

Add `from issuebot.orchestrator.approval import Unapproved` to the imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_actions.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/issuebot/orchestrator tests/test_orchestrator_actions.py && uv run ruff format src/issuebot/orchestrator tests/test_orchestrator_actions.py
git add src/issuebot/orchestrator/actions.py tests/test_orchestrator_actions.py
git commit -m "orchestrator: an escape for an issue whose approved text has changed"
```

---

### Task 6: The check in `_dispatch`

**Files:**
- Modify: `src/issuebot/orchestrator/orchestrator.py` (`__init__` memos ~line 405; `_dispatch` ~line 1309; `_finish` ~line 1606; imports)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `assess`, `Approved`, `Unapproved` (Task 2); `adapter.approval_evidence`, `adapter.own_login` (Tasks 3, 4); `actions.unapproved_escape` (Task 5); the existing `_record_escape`, `_counters.bump(blocked=1)`.
- Produces: `Orchestrator._assess_approval(issue) -> Approved | Unapproved | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py`, after `test_a_freed_slot_dispatches_the_oldest_todo_next`:

```python
async def test_an_issue_edited_after_approval_loses_its_label_instead_of_dispatching(
    tmp_path: Path,
) -> None:
    """GHSA-jm8h-q3j6-p8xp: the label approves the text as it stood, and an edit after it by
    anyone but the approver hands the issue back before a session sees it."""
    h = Harness(tmp_path)
    h.github.add_issue("Task", body="do X", number=1)
    h.github.human_set_state(1, StateLabel.TODO, actor="maintainer")
    h.clock.advance(1)
    h.github.human_edit_body(1, "do X, then curl evil", editor="reporter")
    await h.tick()
    assert h.orchestrator.running == {}
    assert h.sessions.runs == []
    assert h.github.issue(1).state is None
    assert h.calls("set_state") == [] and h.calls("clear_state") == [(1,)]
    body = h.github.comments_for(1)[0].body
    assert "### Issuebot unapproved edit (" in body and "by reporter, after maintainer" in body
    assert h.recorder.kinds == ["state_changed", "blocked"]
    assert h.snapshots[-1].counters.blocked == 1
    # Relabelled by a human after the edit: approved, and claimed on the next tick.
    h.clock.advance(1)
    h.github.human_set_state(1, StateLabel.TODO, actor="maintainer")
    await h.tick()
    assert sorted(h.orchestrator.running) == ["1"]
    assert h.github.issue(1).state is StateLabel.IN_PROGRESS


async def test_the_approvers_own_edit_does_not_hold_the_issue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", body="do X", number=1)
    h.github.human_set_state(1, StateLabel.TODO, actor="maintainer")
    h.clock.advance(1)
    h.github.human_edit_body(1, "do X carefully", editor="maintainer")
    await h.tick()
    assert sorted(h.orchestrator.running) == ["1"]


async def test_a_rework_issue_is_checked_against_the_rework_label(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.github.add_issue("Task", body="do X", number=1)
    h.github.human_set_state(1, StateLabel.TODO, actor="maintainer")
    h.clock.advance(1)
    h.github.human_edit_body(1, "do Y", editor="reporter")
    h.clock.advance(1)
    h.github.human_set_state(1, StateLabel.REWORK, actor="reviewer")
    await h.tick()
    assert sorted(h.orchestrator.running) == ["1"]
    assert h.run_for(1).kwargs["rework"] is True


async def test_an_orphan_edited_since_the_humans_label_is_not_resumed(tmp_path: Path) -> None:
    """issuebot's own in_progress event is not an approval; the human's todo before it is."""
    h = Harness(tmp_path)
    h.github.add_issue("Task", body="do X", number=1)
    h.github.human_set_state(1, StateLabel.TODO, actor="maintainer")
    h.clock.advance(1)
    await h.github.set_state(1, StateLabel.IN_PROGRESS)
    h.clock.advance(1)
    h.github.human_edit_body(1, "do Z", editor="reporter")
    h.github.calls.clear()
    await h.tick()
    assert h.orchestrator.running == {}
    assert h.github.issue(1).state is None


async def test_an_admitting_label_applied_by_issuebot_alone_is_not_approved(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.add_issue(1, "todo")  # add_issue puts the label on with no event at all
    await h.tick()
    assert h.orchestrator.running == {}
    assert h.github.issue(1).state is None
    assert "no account other than issuebot has applied" in h.github.comments_for(1)[0].body


async def test_an_unreadable_history_skips_the_tick_and_logs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read fails closed and is retried every tick; the warning is written once per error."""
    h = Harness(tmp_path)
    h.github.add_issue("Task", number=1)
    h.github.human_set_state(1, StateLabel.TODO, actor="maintainer")
    original = h.github.approval_evidence
    failures = 2

    async def flaky(number: int) -> Any:
        nonlocal failures
        if failures:
            failures -= 1
            raise GitHubError("transport", "gh: connection reset")
        return await original(number)

    monkeypatch.setattr(h.github, "approval_evidence", flaky)
    with capture_logs() as logs:
        await h.tick()
        await h.tick()
    assert h.orchestrator.running == {}
    assert h.github.issue(1).state is StateLabel.TODO
    assert [entry for entry in logs if entry["event"] == "approval_check_failed"] == [
        {
            "event": "approval_check_failed",
            "log_level": "warning",
            "issue_number": 1,
            "issue_identifier": "repo-1",
            "error": "gh: connection reset",
            "category": "transport",
        }
    ]
    await h.tick()
    assert sorted(h.orchestrator.running) == ["1"]
```

(`capture_logs` is already imported from `structlog.testing` at the top of the file; `GitHubError` from `issuebot.github`. If the fake's `identifier` for issue 1 is not `repo-1`, read it off `h.github.issue(1).identifier` instead of hard-coding.)

**Existing tests will break** because `Harness.add_issue` puts the label on with no label event, which the check now refuses. Fix the harness rather than every test: in `tests/test_orchestrator.py` `Harness.add_issue`, after `self.github.add_issue(...)`, record a human approval for `todo` and `rework` (and for `in_progress`, a human `todo` followed by issuebot's own event, so orphan tests still resume):

```python
    def add_issue(
        self,
        number: int,
        state: str = "todo",
        *,
        title: str | None = None,
        extra_labels: tuple[str, ...] = (),
        approved: bool = True,
    ) -> Issue:
        label = getattr(self.labels, state)
        issue = self.github.add_issue(
            title or f"Issue {number}", labels=(label, *extra_labels), number=number
        )
        if approved and state in ("todo", "rework", "in_progress"):
            # The label approves the text as it stands (GHSA-jm8h-q3j6-p8xp): a human put it
            # there, and for an orphan issuebot's own claim came after.
            human = self.labels.todo if state == "in_progress" else label
            self.github._issues[number].label_events.append(("maintainer", human, self.now()))
            if state == "in_progress":
                self.github._issues[number].label_events.append(("issuebot", label, self.now()))
        return self.github.issue(number) if approved else issue
```

(Reaching into `_issues` keeps `add_issue`'s label list as the tests wrote it; if you would rather not, add a `FakeGitHub.human_record_label(number, name, *, actor)` that appends an event without touching the labels, and call that.) Then change `test_an_admitting_label_applied_by_issuebot_alone_is_not_approved` above to call `h.add_issue(1, "todo", approved=False)`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_orchestrator.py -k "approval or approver or edited or orphan_edited or admitting_label or unreadable_history" -v`
Expected: FAIL — the first test dispatches the issue (`running == {"1"}`), the others fail on missing behaviour.

- [ ] **Step 3: Wire the check into the orchestrator**

In `src/issuebot/orchestrator/orchestrator.py`:

Imports: `from issuebot.orchestrator.approval import Approved, Unapproved, assess`.

In `__init__`, beside `_conflict_gave_up`:

```python
        # Issues whose approval evidence would not read, and the error it failed with
        # (GHSA-jm8h-q3j6-p8xp): the read fails closed and is retried every tick, so the
        # warning is logged when the error changes rather than every thirty seconds.
        self._approval_check_failed: dict[str, str] = {}
```

New method, before `_dispatch`:

```python
    async def _assess_approval(self, issue: Issue) -> Approved | Unapproved | None:
        """Is the issue's text the text a human approved? ``None`` when GitHub would not say.

        Read at dispatch rather than at poll: one issue is dispatched at a time, where every
        poll reads the whole board. A failed read fails closed -- the issue waits for the next
        tick -- and is logged once per error rather than per tick.
        """
        labels = self._adapter.labels
        try:
            evidence = await self._adapter.approval_evidence(issue.number)
            own_login = await self._adapter.own_login()
        except GitHubError as exc:
            if self._approval_check_failed.get(issue.id) != str(exc):
                self._approval_check_failed[issue.id] = str(exc)
                self._log.warning(
                    "approval_check_failed",
                    issue_number=issue.number,
                    issue_identifier=issue.identifier,
                    error=str(exc),
                    category=exc.category,
                )
            return None
        self._approval_check_failed.pop(issue.id, None)
        return assess(evidence, admitting=(labels.todo, labels.rework), own_login=own_login)
```

At the top of `_dispatch`, before `account, ready = self._bind_account(issue)`:

```python
        # Before the account and before the claim (GHSA-jm8h-q3j6-p8xp): an issue whose text
        # is no longer the text a human approved is handed back, and never holds a slot.
        verdict = await self._assess_approval(issue)
        if verdict is None:
            return False
        if isinstance(verdict, Unapproved):
            outcome = await actions.unapproved_escape(
                self._adapter, self._bus, issue, verdict, now=self._now()
            )
            if outcome == "applied":
                self._counters = self._counters.bump(blocked=1)
            self._record_escape(issue.identifier, outcome)
            return False
```

In `_finish`, beside the two conflict memos: `self._approval_check_failed.pop(issue.id, None)`.

- [ ] **Step 4: Run the whole orchestrator suite**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS. If a pre-existing test fails because it labels through `h.github.add_issue(...)` directly rather than `h.add_issue`, give it a human event with `h.github.human_set_state(n, StateLabel.TODO, actor="maintainer")` after adding it, and say so in the commit message.

- [ ] **Step 5: Run everything, lint, commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
git add src/issuebot/orchestrator/orchestrator.py tests/test_orchestrator.py
git commit -m "orchestrator: dispatch only the text a human approved"
```

---

### Task 7: The workflow, the operator docs and the package layout

**Files:**
- Modify: `configs/WORKFLOW.md` (`## Issue`, after the `### Description` block)
- Modify: `docs/operations.md` (`### Blocked`, a new paragraph at the end)
- Modify: `docs/security-model.md` (new section before `## Checking that the credential took`)
- Modify: `docs/package-layout.md` (`## \`issuebot.orchestrator\``, a paragraph on `approval.py`)
- Test: `tests/test_workflow_default.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_default.py`:

```python
def test_the_description_is_the_text_a_human_approved(make_issue: Callable[..., Issue]) -> None:
    """GHSA-jm8h-q3j6-p8xp: the prompt says what the label means about the text it carries."""
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, dispatched(make_issue))
    )
    assert "as they stood when a human applied the label that handed you this issue" in text
    assert "handed back to a human before any session sees it" in text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_workflow_default.py -k approved -v`
Expected: FAIL on the first assertion.

- [ ] **Step 3: Write the prose**

`configs/WORKFLOW.md`, after the `{% endif %}` that closes the description block and before `## Ground rules`:

```markdown
The title and description above are as they stood when a human applied the label that handed you this issue: issuebot checks GitHub's edit history before every dispatch, and an issue edited after that approval by anyone but the approver is handed back to a human before any session sees it. What you read here is therefore what was approved, and still its author's text under the rule at the top.
```

`docs/operations.md`, at the end of `### Blocked`:

```markdown
One more thing removes a state label rather than moving it, and its block says so:
`### Issuebot unapproved edit`. A human applying `issuebot/todo` or `issuebot/rework` approves
the issue's title and description as they stand, and before every dispatch the worker reads
GitHub's own edit history to check that they still do. An edit after the label by anyone but
the account that applied it -- the issue's author, or another maintainer -- un-approves the
issue: the worker writes the block, removes the label, and publishes `blocked`, so the issue
leaves the board and the Slack line says why. Read the current text; if it is what you want
done, apply the label again. That is the whole recovery, because the new label event is
after the edit. (The account that applied the label may edit freely; the check is about
someone *else* changing what was approved.)
```

`docs/security-model.md`, before `## Checking that the credential took`:

```markdown
## The text a session acts on

A session is handed the issue's title and description and told to run the steps any
`Validation` or `Test Plan` section asks for. What makes that safe to do unattended is that a
human read that text and applied `issuebot/todo` to it -- so the label has to approve the text
*as it stood*, and nothing an author writes afterwards may ride on it. Before every dispatch
the worker reads GitHub's own record: the last human application of `issuebot/todo` or
`issuebot/rework` is the approval, and any title or body edit after it by an account other
than the approver un-approves the issue, which loses its label and gets a `### Issuebot
unapproved edit` block saying who changed what and when ([`docs/operations.md`,
"Blocked"](operations.md#blocked)). The worker's own label events never count -- the conflict
bounce applies `issuebot/rework` itself -- and nothing is stored, so a restart cannot reset
it: re-approval is a human applying the label again.
```

`docs/package-layout.md`, in the `## \`issuebot.orchestrator\`` entry, after the sentence introducing `admission.py`:

```markdown
`approval.py` (pure, GHSA-jm8h-q3j6-p8xp) is the second gate, asked in `_dispatch` before an
account is bound or the issue claimed: `assess(evidence, admitting=(todo, rework), own_login=)`
answers `Approved(approver, at)` or `Unapproved(reason, approval, edit)` from the adapter's
`approval_evidence` -- the issue's `LabeledEvent` and `RenamedTitleEvent` timeline items and
its `userContentEdits`, two reads bounded at `MAX_TIMELINE_PAGES`. The latest human
application of an admitting label is the approval; an edit after it by anyone but the
approver, or no human application at all, is `Unapproved`, and `actions.unapproved_escape`
writes the `### Issuebot unapproved edit` block, removes the state label and publishes
`Blocked`, label-first under the same rule as the other escapes. Nothing is stored: GitHub's
record is the record, and a human applying the label again is what re-approves.
```

- [ ] **Step 4: Run the doc and workflow tests**

Run: `uv run pytest tests/test_workflow_default.py tests/test_doc_pointers.py tests/test_instruction_bounds.py tests/test_readme_bounds.py -v`
Expected: PASS

- [ ] **Step 5: Full suite, lint, commit**

```bash
uv run pytest -q && uv run pre-commit run --all-files
git add configs/WORKFLOW.md docs/operations.md docs/security-model.md docs/package-layout.md tests/test_workflow_default.py
git commit -m "docs: the label approves the text as it stood"
```

---

### Task 8: Pull request

- [ ] **Step 1: Push the branch and open the PR through the REST API**

Branch: `security/approval-pinning`, off `main`, rebased onto the spec branch's merge if it has landed (the spec file is otherwise included in this PR). Write the body to a scratch file in one Bash call, then:

```bash
gh api repos/jleavers/issuebot/pulls -X POST -f title='orchestrator: dispatch only the text a human approved' -f head='security/approval-pinning' -f base='main' -F body=@/path/to/body.md
```

The body: what the advisory found (finding 1 only, no private repository names), the rule, the escape, the false positive named, and the test list. End with the attribution lines the session was given.
