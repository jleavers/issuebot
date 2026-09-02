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
    ("not_found", re.compile(r"http 404|could not resolve to|\bnot found\b")),
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
