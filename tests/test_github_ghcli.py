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
