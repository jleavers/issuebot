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
from issuebot.github.ghcli import ID_BATCH_SIZE, GhCliAdapter, by_ids_query
from issuebot.github.models import WORKPAD_MARKER, StateLabel
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
