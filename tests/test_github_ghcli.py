"""Tests for GhCliAdapter against a stub runner and recorded gh output."""

import io
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from issuebot.config import GitHubSettings
from issuebot.github.errors import GitHubError
from issuebot.github.ghcli import (
    ID_BATCH_SIZE,
    ISSUE_FIELDS,
    MAX_COMMENT_PAGES,
    MAX_ISSUE_PAGES,
    MAX_TERMINAL_PAGES,
    MAX_TIMELINE_PAGES,
    PAGE_SIZE,
    GhCliAdapter,
    by_ids_query,
)
from issuebot.github.models import WORKPAD_MARKER, StateLabel
from issuebot.github.runner import GhResult
from issuebot.github.state import LabelStyle
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


LOGIN = "issuebot-agent"
"""The account the fixtures' workpad and pull requests were written by."""


def make_adapter(
    runner: StubRunner, *, login: str | None = LOGIN, **overrides: object
) -> GhCliAdapter:
    settings = GitHubSettings(repo="example/repo", **overrides)  # type: ignore[arg-type]
    return GhCliAdapter(settings, runner=runner, login=login)


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


def _issues_page(number: int, *, end_cursor: str | None) -> str:
    """One board page carrying one ordinary issue, saying there is always another."""
    node = {
        "number": number,
        "title": "t",
        "body": "b",
        "state": "OPEN",
        "url": f"https://example/{number}",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "closedAt": None,
        "author": {"login": "someone"},
        "labels": {"nodes": [{"name": "issuebot/todo"}]},
        "assignees": {"nodes": []},
        "closedByPullRequestsReferences": {"nodes": []},
    }
    return json.dumps(
        {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [node],
                        "pageInfo": {
                            "hasNextPage": end_cursor is not None,
                            "endCursor": end_cursor,
                        },
                    }
                }
            }
        }
    )


def _endless_board(runner: StubRunner, label: str, pages: int) -> None:
    """``pages`` board pages for ``label``, every one of them saying there is another."""
    runner.on(
        both(has(f"label={label}"), lacks("cursor=")), stdout=_issues_page(1, end_cursor="c1")
    )
    for page in range(1, pages + 2):
        runner.on(
            both(has(f"label={label}"), has(f"cursor=c{page}")),
            stdout=_issues_page(page + 1, end_cursor=f"c{page + 1}"),
        )


async def test_board_poll_gives_up_past_the_page_cap() -> None:
    """A board that keeps saying ``hasNextPage`` is a failed read, not a short board (#139).

    The read fails rather than returning what it has: the query is oldest first, so a
    truncated answer would silently starve the newest issues while the worker claimed from
    it believing it had seen everything. A ``response`` error is what #88's consecutive-failure
    counter turns into a ``github`` dispatch hold, which says so where an operator looks.
    """
    runner = StubRunner()
    _endless_board(runner, "issuebot/todo", MAX_ISSUE_PAGES)
    with pytest.raises(GitHubError) as excinfo:
        await make_adapter(runner).fetch_issues_by_states([StateLabel.TODO])
    assert excinfo.value.category == "response"
    assert not excinfo.value.retryable
    assert excinfo.value.message == (
        f"more than {MAX_ISSUE_PAGES * PAGE_SIZE} issues carry issuebot/todo"
    )
    assert len(runner.calls) == MAX_ISSUE_PAGES


async def test_board_poll_reads_a_full_ceiling_of_pages() -> None:
    """The page before the ceiling is still answered: the cap refuses, it does not shorten."""
    runner = StubRunner()
    runner.on(
        both(has("label=issuebot/todo"), lacks("cursor=")), stdout=_issues_page(1, end_cursor="c1")
    )
    for page in range(1, MAX_ISSUE_PAGES):
        last = page == MAX_ISSUE_PAGES - 1
        runner.on(
            both(has("label=issuebot/todo"), has(f"cursor=c{page}")),
            stdout=_issues_page(page + 1, end_cursor=None if last else f"c{page + 1}"),
        )
    issues = await make_adapter(runner).fetch_issues_by_states([StateLabel.TODO])
    assert len(issues) == MAX_ISSUE_PAGES
    assert len(runner.calls) == MAX_ISSUE_PAGES


async def test_terminal_sweep_skips_one_over_ceiling_role_and_keeps_the_rest() -> None:
    """One role over its ceiling does not void the sweep's other four (#139).

    The role that can actually reach it is ``complete`` -- it grows with everything issuebot
    has finished -- and its issues are the ones the sweep classifies ``unchanged`` and does
    nothing with. Letting it refuse the whole read would stop the sweep closing issues out,
    removing workspaces and releasing session accounts, which is worse than the cost the cap
    is for. The board poll keeps the all-or-nothing rule: there, four roles are not a board.
    """
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)
    runner = StubRunner()
    _endless_board(runner, "issuebot/complete", MAX_TERMINAL_PAGES)
    empty = _issues_page(0, end_cursor=None)
    for role in ("todo", "in-progress", "review", "rework"):
        runner.on(has(f"label=issuebot/{role}"), stdout=empty)

    issues = await make_adapter(runner).fetch_terminal_issues()

    assert [issue.number for issue in issues] == [0]
    skipped = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "issue_role_skipped"
    ]
    assert [record["label"] for record in skipped] == ["issuebot/complete"]
    assert str(MAX_TERMINAL_PAGES * PAGE_SIZE) in skipped[0]["reason"]


async def test_terminal_sweep_still_fails_on_an_error_that_is_not_the_cap() -> None:
    """Only a ``response`` error is the cap's: a transport failure is the whole read's to
    fail on, as it was before, so the sweep does not quietly work from four roles."""
    runner = StubRunner()
    runner.on(has("label=issuebot/todo"), stdout="", stderr="dial tcp: timeout", returncode=1)
    with pytest.raises(GitHubError) as excinfo:
        await make_adapter(runner).fetch_terminal_issues()
    assert excinfo.value.category != "response"


async def test_terminal_sweep_carries_its_own_looser_ceiling() -> None:
    """The closed read is a different resource, so it is a different number (#139).

    ``complete`` rests on a closed issue for ever, so the sweep's pages grow with everything
    issuebot has ever finished -- not with a working set a human drains, which is what bounds
    the open board.
    """
    assert MAX_TERMINAL_PAGES > MAX_ISSUE_PAGES
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)
    runner = StubRunner()
    _endless_board(runner, "issuebot/todo", MAX_TERMINAL_PAGES)
    runner.on(has("label=issuebot/"), stdout=_issues_page(0, end_cursor=None))

    await make_adapter(runner).fetch_terminal_issues()

    # The ceiling was the terminal one and not the board's: it read every page up to it.
    reason = json.loads(stream.getvalue().splitlines()[0])["reason"]
    assert reason == f"more than {MAX_TERMINAL_PAGES * PAGE_SIZE} issues carry issuebot/todo"
    todo_calls = [argv for argv, _ in runner.calls if has("label=issuebot/todo")(argv)]
    assert len(todo_calls) == MAX_TERMINAL_PAGES


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


async def test_fetch_by_ids_non_object_alias_raises_response() -> None:
    runner = StubRunner()
    runner.on(has("i7: issue"), stdout=json.dumps({"data": {"repository": {"i7": "nope"}}}))
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).fetch_issues_by_ids(["7"])
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
        (1, "gh: authentication required", "auth", False),
        (1, "gh: You have exceeded a secondary rate limit", "rate_limited", True),
        (1, "could not resolve host: api.github.com", "transport", True),
        (1, "gh: request timeout after 30s", "transport", True),
        (1, "gh: TLS handshake failed", "transport", True),
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


# --- debug logging and advanced error handling ----------------------------------


async def test_read_methods_log_debug_on_entry() -> None:
    stream = io.StringIO()
    configure_logging(level="DEBUG", stream=stream)
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
    adapter = make_adapter(runner)
    await adapter.fetch_issues_by_states([StateLabel.TODO])
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert any(record.get("event") == "fetch_issues_by_states" for record in records)
    debug_record = next(r for r in records if r.get("event") == "fetch_issues_by_states")
    assert debug_record.get("states") == ["todo"]


async def test_token_is_redacted_from_graphql_error_stderr() -> None:
    runner = StubRunner()
    runner.on(
        has("api"),
        stdout=json.dumps({"errors": [{"message": "boom"}]}),
        stderr="gh: something sekret happened",
        returncode=1,
    )
    adapter = make_adapter(runner, token=SecretStr("sekret"))
    with pytest.raises(GitHubError) as exc:
        await adapter.fetch_issues_by_states([StateLabel.TODO])
    assert exc.value.category == "response"
    assert exc.value.stderr is not None
    assert "sekret" not in exc.value.stderr
    assert "***" in exc.value.stderr


async def test_null_list_node_is_skipped_and_logged() -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)
    runner = StubRunner()
    page = json.loads(fixture("list_todo_page2.json"))
    page["data"]["repository"]["issues"]["nodes"] = [
        None,
        {
            "number": 44,
            "title": "Write docs",
            "body": "",
            "state": "OPEN",
            "url": "https://github.com/example/repo/issues/44",
            "createdAt": "2026-09-01T11:00:00Z",
            "updatedAt": "2026-09-01T11:00:00Z",
            "closedAt": None,
            "labels": {"nodes": [{"name": "issuebot/todo"}]},
            "assignees": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": []},
        },
    ]
    runner.on(has("label=issuebot/todo"), stdout=json.dumps(page))
    issues = await make_adapter(runner).fetch_issues_by_states([StateLabel.TODO])
    assert [issue.number for issue in issues] == [44]
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    skipped = next(r for r in records if r.get("event") == "issue_record_skipped")
    assert skipped.get("issue_number") is None
    assert "not an object" in skipped.get("reason", "")


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


async def test_set_state_removes_the_markers_only_when_asked() -> None:
    """#34: `claim` drops the no-fault marker; every other `set_state` must preserve it."""
    runner = StubRunner()
    runner.on(has("issue", "edit"))
    await make_adapter(runner).set_state(42, StateLabel.IN_PROGRESS, clear_markers=True)
    assert runner.argv(0)[-1] == (
        "issuebot/todo,issuebot/review,issuebot/rework,issuebot/complete,issuebot/no-fault"
    )


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


async def test_set_state_on_missing_issue_does_not_hint_at_labels_ensure() -> None:
    runner = StubRunner()
    runner.on(
        has("issue", "edit"),
        stderr="Could not resolve to an issue (repository.issue)",
        returncode=1,
    )
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).set_state(999, StateLabel.IN_PROGRESS)
    assert exc.value.category == "not_found"
    assert "labels ensure" not in exc.value.message


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


def comment_page(ids: range, *, workpad_at: int | None = None) -> str:
    """A page of comments by a stranger, one of them the account's workpad when asked."""
    items = []
    for comment_id in ids:
        own = comment_id == workpad_at
        items.append(
            {
                "id": comment_id,
                "body": f"{WORKPAD_MARKER}\n\nstate" if own else f"comment {comment_id}",
                "html_url": f"https://github.com/example/repo/issues/42#issuecomment-{comment_id}",
                "user": {"login": LOGIN if own else "someone"},
                "created_at": "2026-09-02T10:00:00Z",
                "updated_at": "2026-09-02T10:00:00Z",
            }
        )
    return json.dumps(items)


async def test_find_workpad_comment_reads_one_page_and_returns_the_marker_comment_or_none() -> None:
    runner = StubRunner()
    runner.on(has("issues/42/comments?per_page=100&page=1"), stdout=fixture("comments.json"))
    runner.on(has("issues/43/comments?per_page=100&page=1"), stdout="[]")
    adapter = make_adapter(runner)
    found = await adapter.find_workpad_comment(42)
    assert found is not None and found.author == LOGIN
    assert found.id == 1002
    assert found.body.startswith(WORKPAD_MARKER)
    assert found.updated_at == datetime(2026, 9, 2, 10, 30, tzinfo=UTC)
    # A page shorter than PAGE_SIZE is the last one: no second request.
    assert [argv for argv, _ in runner.calls if "comments" in argv[1]] == [
        ["api", "repos/example/repo/issues/42/comments?per_page=100&page=1"]
    ]
    assert await adapter.find_workpad_comment(43) is None


async def test_find_workpad_comment_pages_until_the_marker_and_no_further() -> None:
    runner = StubRunner()
    runner.on(has("comments?per_page=100&page=1"), stdout=comment_page(range(1, 101)))
    runner.on(
        has("comments?per_page=100&page=2"), stdout=comment_page(range(101, 201), workpad_at=150)
    )
    runner.on(has("comments?per_page=100&page=3"), stdout=comment_page(range(201, 205)))
    found = await make_adapter(runner).find_workpad_comment(42)
    assert found is not None and found.id == 150
    pages = [argv[1].rsplit("=", 1)[1] for argv, _ in runner.calls if "comments" in argv[1]]
    assert pages == ["1", "2"]


async def test_find_workpad_comment_stops_at_the_page_cap_with_an_error_not_none() -> None:
    runner = StubRunner()
    for page in range(1, MAX_COMMENT_PAGES + 2):
        first = (page - 1) * 100 + 1
        runner.on(
            has(f"comments?per_page=100&page={page}"),
            stdout=comment_page(range(first, first + 100)),
        )
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).find_workpad_comment(42)
    assert exc.value.category == "response"
    assert not exc.value.retryable
    assert exc.value.message == (
        f"no workpad comment by {LOGIN} among the first {MAX_COMMENT_PAGES * 100} comments of #42"
    )
    pages = [argv for argv, _ in runner.calls if "comments" in argv[1]]
    assert len(pages) == MAX_COMMENT_PAGES


async def test_find_workpad_comment_skips_a_marker_comment_by_anyone_else() -> None:
    """The marker is public; the author is the provenance (#77). Logged, and passed over."""
    stream = io.StringIO()
    configure_logging(level="DEBUG", stream=stream)
    runner = StubRunner()
    runner.on(has("issues/42/comments"), stdout=fixture("comments_impostor.json"))
    adapter = make_adapter(runner)
    found = await adapter.find_workpad_comment(42)
    assert found is not None
    assert found.id == 1002 and found.author == LOGIN
    ignored = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"workpad_comment_ignored"' in line
    ]
    assert [(r["comment_id"], r["author"]) for r in ignored] == [(1000, "mallory")]
    assert ignored[0]["reason"] == f"not written by {LOGIN}"
    # The session asks every turn; one impostor is logged once per adapter, not per call.
    assert (await adapter.find_workpad_comment(42)) is not None
    assert stream.getvalue().count('"workpad_comment_ignored"') == 1

    runner = StubRunner()
    runner.on(has("issues/42/comments"), stdout=fixture("comments_impostor_only.json"))
    assert await make_adapter(runner).find_workpad_comment(42) is None


async def test_find_workpad_comment_matches_the_login_case_insensitively() -> None:
    runner = StubRunner()
    runner.on(has("issues/42/comments"), stdout=fixture("comments.json"))
    found = await make_adapter(runner, login="Issuebot-Agent").find_workpad_comment(42)
    assert found is not None and found.id == 1002


async def test_find_workpad_comment_rejects_a_wrapped_page() -> None:
    """``--slurp`` wrapped the pages in a list; a page is a list of comments, nothing else."""
    runner = StubRunner()
    runner.on(has("issues/42/comments"), stdout="[" + fixture("comments.json") + "]")
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).find_workpad_comment(42)
    assert exc.value.category == "response"
    runner = StubRunner()
    runner.on(has("issues/42/comments"), stdout='{"comments": []}')
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).find_workpad_comment(42)
    assert exc.value.category == "response"


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


async def test_ensure_labels_creates_the_extra_labels_too() -> None:
    runner = StubRunner()
    runner.on(has("label", "list"), stdout=fixture("labels.json"))
    runner.on(has("label", "create"))
    style = LabelStyle("BFD4F2", "Run this issue with the sonnet model")
    results = await make_adapter(runner).ensure_labels({"issuebot/model/sonnet": style})
    assert (results[-1].name, results[-1].outcome) == ("issuebot/model/sonnet", "created")
    creates = [argv for argv, _ in runner.calls if argv[:2] == ["label", "create"]]
    assert creates[-1] == [
        "label",
        "create",
        "issuebot/model/sonnet",
        "-R",
        "example/repo",
        "--color",
        "BFD4F2",
        "--description",
        "Run this issue with the sonnet model",
    ]


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
        ("issuebot/no-fault", "created"),
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
    assert len(creates) == 5
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
    assert missing == [
        "issuebot/in-progress",
        "issuebot/rework",
        "issuebot/complete",
        "issuebot/no-fault",
    ]


async def test_missing_labels_reports_the_extra_names_after_the_state_ones() -> None:
    runner = StubRunner()
    runner.on(has("label", "list"), stdout=fixture("labels.json"))
    missing = await make_adapter(runner).missing_labels(("issuebot/model/sonnet",))
    assert missing == [
        "issuebot/in-progress",
        "issuebot/rework",
        "issuebot/complete",
        "issuebot/no-fault",
        "issuebot/model/sonnet",
    ]


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


def test_issue_fields_fetch_the_author() -> None:
    """The prompt's envelope names the author, so the fragment has to ask for one (#76)."""
    assert "author { login }" in ISSUE_FIELDS


def test_fragment_asks_who_opened_each_pull_request_and_from_where() -> None:
    """``_select_pr`` resolves the linked PR by author and head repository (#77)."""
    pr_nodes = ISSUE_FIELDS.split("closedByPullRequestsReferences", 1)[1]
    assert "author { login }" in pr_nodes
    assert "isCrossRepository" in pr_nodes


async def test_own_login_is_probed_once_and_remembered() -> None:
    """Without a login the adapter asks ``gh api user`` before its first read, then never again."""
    runner = StubRunner()
    runner.on(has("api", "user"), stdout="Issuebot-Agent\n")
    runner.on(has("graphql"), stdout=fixture("by_ids.json"))
    runner.on(has("issues/42/comments"), stdout=fixture("comments.json"))
    adapter = make_adapter(runner, login=None)
    issues = await adapter.fetch_issues_by_ids(["7"])
    assert runner.argv(0) == ["api", "user", "--jq", ".login"]
    assert issues[0].linked_pr is not None, "the login is matched case-insensitively"
    await adapter.fetch_issues_by_ids(["7"])
    assert await adapter.find_workpad_comment(42) is not None
    assert await adapter.own_login() == "Issuebot-Agent"
    assert sum(argv[:2] == ["api", "user"] for argv, _ in runner.calls) == 1


async def test_an_explicit_login_is_not_overwritten_by_the_probe() -> None:
    runner = StubRunner()
    runner.on(has("api", "user"), stdout="jleavers\n")
    adapter = make_adapter(runner, login=LOGIN)
    assert (await adapter.auth_status()).login == "jleavers"
    assert await adapter.own_login() == LOGIN


async def test_auth_status_fills_the_login_cache() -> None:
    runner = StubRunner()
    runner.on(has("api", "user"), stdout="jleavers\n")
    runner.on(has("graphql"), stdout=fixture("by_ids.json"))
    adapter = make_adapter(runner, login=None)
    assert (await adapter.auth_status()).login == "jleavers"
    issues = await adapter.fetch_issues_by_ids(["7"])
    assert issues[0].linked_pr is None, "the fixture's PR was opened by issuebot-agent"
    assert sum(argv[:2] == ["api", "user"] for argv, _ in runner.calls) == 1


async def test_a_failed_login_probe_fails_the_read() -> None:
    """No provenance, no board: a read that cannot tell its own PRs apart must not answer."""
    runner = StubRunner()
    runner.on(has("api", "user"), stdout="", stderr="HTTP 401: Bad credentials", returncode=1)
    adapter = make_adapter(runner, login=None)
    with pytest.raises(GitHubError) as exc:
        await adapter.fetch_issues_by_states([StateLabel.TODO])
    assert exc.value.category == "auth"
    assert not any("graphql" in arg for argv, _ in runner.calls for arg in argv)


async def test_empty_reads_do_not_probe_the_login() -> None:
    runner = StubRunner()
    adapter = make_adapter(runner, login=None)
    assert await adapter.fetch_issues_by_states([]) == []
    assert await adapter.fetch_issues_by_ids([]) == []
    assert runner.calls == []


# --- reads: the label history --------------------------------------------------------------


def _timeline_page(nodes: list[dict[str, object]], *, end_cursor: str | None) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "issue": {
                        "timelineItems": {
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


async def test_count_own_label_additions_reads_the_timeline_and_paginates() -> None:
    """The bounce count comes from GitHub's own record of who added the label (#104)."""
    runner = StubRunner()
    runner.on(
        both(has("LABELED_EVENT"), lacks("cursor=")),
        stdout=_timeline_page(
            [
                {"actor": {"login": LOGIN}, "label": {"name": "issuebot/rework"}},
                {"actor": {"login": "reviewer"}, "label": {"name": "issuebot/rework"}},
                {"actor": {"login": LOGIN.upper()}, "label": {"name": "Issuebot/Rework"}},
                {"actor": {"login": LOGIN}, "label": {"name": "issuebot/review"}},
                {"actor": None, "label": {"name": "issuebot/rework"}},  # a deleted account
                {"actor": {"login": LOGIN}, "label": None},  # a deleted label
                {},  # not a labeled event at all
            ],
            end_cursor="Y3Vyc29yOjE=",
        ),
    )
    runner.on(
        both(has("LABELED_EVENT"), has("cursor=Y3Vyc29yOjE=")),
        stdout=_timeline_page(
            [{"actor": {"login": LOGIN}, "label": {"name": "issuebot/rework"}}], end_cursor=None
        ),
    )
    adapter = make_adapter(runner)
    assert await adapter.count_own_label_additions(42, "issuebot/rework") == 3
    first = runner.argv(0)
    assert first[:3] == ["api", "graphql", "-f"]
    assert "timelineItems(itemTypes: [LABELED_EVENT], first: 100, after: $cursor)" in query_of(
        first
    )
    # `-F`, not `-f`: the issue number is an `Int!` and gh has to send it typed.
    assert first[first.index("number=42") - 1] == "-F"
    assert first[first.index("owner=example") - 1] == "-f"
    assert len(runner.calls) == 2


async def test_count_own_label_additions_gives_up_past_the_page_cap() -> None:
    """A label history longer than ``MAX_TIMELINE_PAGES`` is an error, not a longer read (#110)."""
    runner = StubRunner()
    runner.on(
        both(has("LABELED_EVENT"), lacks("cursor=")),
        stdout=_timeline_page(
            [{"actor": {"login": LOGIN}, "label": {"name": "issuebot/rework"}}], end_cursor="c1"
        ),
    )
    for page in range(1, MAX_TIMELINE_PAGES + 2):
        runner.on(
            both(has("LABELED_EVENT"), has(f"cursor=c{page}")),
            stdout=_timeline_page(
                [{"actor": {"login": LOGIN}, "label": {"name": "issuebot/rework"}}],
                end_cursor=f"c{page + 1}",
            ),
        )
    with pytest.raises(GitHubError) as excinfo:
        await make_adapter(runner).count_own_label_additions(42, "issuebot/rework")
    assert excinfo.value.category == "response"
    assert str(excinfo.value).endswith(
        f"label history of #42 runs past {MAX_TIMELINE_PAGES * 100} events"
    )
    assert len(runner.calls) == MAX_TIMELINE_PAGES


async def test_count_own_label_additions_rejects_a_malformed_response() -> None:
    runner = StubRunner()
    runner.on(has("LABELED_EVENT"), stdout='{"data": {"repository": {"issue": null}}}')
    with pytest.raises(GitHubError) as exc:
        await make_adapter(runner).count_own_label_additions(42, "issuebot/rework")
    assert exc.value.category == "response"
    runner = StubRunner()
    runner.on(
        has("LABELED_EVENT"),
        stdout='{"data": {"repository": {"issue": {"timelineItems": {"nodes": [], '
        '"pageInfo": {"hasNextPage": true, "endCursor": null}}}}}}',
    )
    with pytest.raises(GitHubError, match="hasNextPage without endCursor"):
        await make_adapter(runner).count_own_label_additions(42, "issuebot/rework")


async def test_count_own_label_additions_probes_the_login_once() -> None:
    runner = StubRunner()
    runner.on(has("api", "user"), stdout="Issuebot-Agent\n")
    runner.on(
        has("LABELED_EVENT"),
        stdout=_timeline_page(
            [{"actor": {"login": "issuebot-agent"}, "label": {"name": "issuebot/rework"}}],
            end_cursor=None,
        ),
    )
    adapter = make_adapter(runner, login=None)
    assert await adapter.count_own_label_additions(42, "issuebot/rework") == 1
    assert await adapter.count_own_label_additions(42, "issuebot/rework") == 1
    assert sum(argv[:2] == ["api", "user"] for argv, _ in runner.calls) == 1
