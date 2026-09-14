"""GitHubAdapter backed by the gh CLI: GraphQL for reads, gh subcommands for writes."""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from issuebot.config import GitHubLabels, GitHubSettings
from issuebot.github.errors import ErrorCategory, GitHubError
from issuebot.github.models import (
    AuthStatus,
    Comment,
    Issue,
    LabelEnsured,
    RateLimit,
    RepoInfo,
    StateLabel,
    is_workpad_body,
)
from issuebot.github.normalise import issue_from_node, label_name
from issuebot.github.runner import GhResult, GhRunner, GhRunnerLike
from issuebot.github.state import LABEL_STYLES, LabelStyle, marker_label_styles
from issuebot.log import get_logger

PAGE_SIZE = 100
ID_BATCH_SIZE = 50
# How many pages of an issue's comments ``find_workpad_comment`` reads before giving up (#110):
# the workpad is the account's first comment, so it sits among the earliest, and every comment
# past it is someone else's to add. A thread that has outgrown this is reported, never
# scanned to its end, and never read as "no workpad" -- that answer would have the session
# open another one.
MAX_COMMENT_PAGES = 10

ISSUE_FIELDS = """fragment IssueFields on Issue {
  number title body state url createdAt updatedAt closedAt
  author { login }
  labels(first: 50) { nodes { name } }
  assignees(first: 20) { nodes { login } }
  closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
    nodes { number url state mergedAt mergeable isCrossRepository author { login } }
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
    """The gh-backed adapter.

    ``login`` is the account the token belongs to, for a caller that already knows it; left
    ``None``, the adapter asks ``gh api user`` once, the first time it needs it, and keeps the
    answer for its lifetime (``auth_status`` fills the same cache, so the worker's startup
    probe pays for it). It is what the two records issuebot treats as its own state are
    resolved by (#77): the issue's pull request and the workpad comment are the ones *this
    account* wrote, never the ones whose text says so.
    """

    def __init__(
        self,
        settings: GitHubSettings,
        *,
        runner: GhRunnerLike | None = None,
        login: str | None = None,
    ) -> None:
        self._settings = settings
        self._runner: GhRunnerLike = runner or GhRunner(
            token=settings.token, timeout_ms=settings.request_timeout_ms
        )
        self._owner, self._name = settings.repo.split("/", 1)
        self._login = login
        self._ignored_workpads: set[tuple[int, int]] = set()
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
        self._log.debug("fetch_issues_by_states", states=[role.value for role in roles])
        if not roles:
            return []
        return await self._collect(roles, OPEN_ISSUES_QUERY)

    async def fetch_terminal_issues(self) -> list[Issue]:
        self._log.debug("fetch_terminal_issues")
        return await self._collect(list(StateLabel), CLOSED_ISSUES_QUERY)

    async def fetch_issues_by_ids(self, ids: Iterable[str]) -> list[Issue]:
        numbers = sorted(
            {int(value) for value in ids if str(value).isascii() and str(value).isdigit()}
        )
        self._log.debug("fetch_issues_by_ids", ids=numbers)
        if not numbers:
            return []
        login = await self.own_login()
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
                if not isinstance(node, Mapping):
                    raise GitHubError("response", f"malformed issue record for alias i{number}")
                issues.append(self._issue(node, login))
        return issues

    def _issue(self, node: Mapping[str, Any], login: str) -> Issue:
        return issue_from_node(node, repo=self.repo, labels=self.labels, login=login)

    async def own_login(self) -> str:
        """The login of the account the adapter acts as; probed once and then remembered."""
        if self._login is None:
            self._login = (await self.auth_status()).login
        return self._login

    # --- writes ------------------------------------------------------------------

    async def set_state(
        self, number: int, state: StateLabel, *, clear_markers: bool = False
    ) -> None:
        target = label_name(self.labels, state)
        others = [label_name(self.labels, role) for role in StateLabel if role is not state]
        if clear_markers:
            others += list(self.labels.markers())
        self._log.debug(
            "set_state", issue_number=number, state=state.value, clear_markers=clear_markers
        )
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
            if (
                exc.category == "not_found"
                and "not found" in exc.message.lower()
                and "could not resolve to" not in exc.message.lower()
            ):
                raise GitHubError(
                    "not_found",
                    f"{exc.message}; run issuebot labels ensure",
                    exit_code=exc.exit_code,
                    stderr=exc.stderr,
                ) from exc
            raise

    async def comment(self, number: int, body: str) -> Comment:
        self._log.debug("comment", issue_number=number)
        result = await self._gh(
            ["api", "-X", "POST", f"repos/{self.repo}/issues/{number}/comments", "--input", "-"],
            stdin=json.dumps({"body": body}),
        )
        return _comment_from(_parse_json(result.stdout))

    async def find_workpad_comment(self, number: int) -> Comment | None:
        """The account's own comment whose first line is the marker, lowest id first.

        The marker is public and anyone can open a comment with it, so a match on the text
        alone would let any commenter hand the agent its "prior state" (#77). A marker comment
        by anyone else is passed over, and logged once per adapter: the session asks every
        turn, and one impostor is one finding, not a warning per turn for as long as it stays.

        The pages are asked for one at a time, oldest first, and the read stops at the first
        match (#110): the thread's length past the workpad is anyone's to grow, so it is not
        what the lookup's cost follows. ``MAX_COMMENT_PAGES`` bounds the rest, and a thread
        longer than that with no workpad in it is a ``response`` error, not ``None``.
        """
        self._log.debug("find_workpad_comment", issue_number=number)
        login = await self.own_login()
        for page_number in range(1, MAX_COMMENT_PAGES + 1):
            result = await self._gh(
                [
                    "api",
                    f"repos/{self.repo}/issues/{number}/comments"
                    f"?per_page={PAGE_SIZE}&page={page_number}",
                ]
            )
            page = _parse_json(result.stdout)
            if not isinstance(page, list):
                raise GitHubError("response", "comments page is not a list")
            for item in page:
                if not isinstance(item, Mapping):
                    raise GitHubError("response", "comments page item is not an object")
                if not is_workpad_body(item.get("body")):
                    continue
                comment = _comment_from(item)
                if comment.author.lower() == login.lower():
                    return comment
                if (number, comment.id) not in self._ignored_workpads:
                    self._ignored_workpads.add((number, comment.id))
                    self._log.warning(
                        "workpad_comment_ignored",
                        issue_number=number,
                        comment_id=comment.id,
                        author=comment.author,
                        reason=f"not written by {login}",
                    )
            if len(page) < PAGE_SIZE:
                return None
        raise GitHubError(
            "response",
            f"no workpad comment by {login} among the first {MAX_COMMENT_PAGES * PAGE_SIZE} "
            f"comments of #{number}",
        )

    async def update_comment(self, comment_id: int, body: str) -> Comment:
        self._log.debug("update_comment", comment_id=comment_id)
        result = await self._gh(
            [
                "api",
                "-X",
                "PATCH",
                f"repos/{self.repo}/issues/comments/{comment_id}",
                "--input",
                "-",
            ],
            stdin=json.dumps({"body": body}),
        )
        return _comment_from(_parse_json(result.stdout))

    # --- labels ------------------------------------------------------------------

    async def ensure_labels(
        self, extra: Mapping[str, LabelStyle] | None = None
    ) -> list[LabelEnsured]:
        self._log.debug("ensure_labels")
        existing = await self._repo_labels()
        wanted = [(label_name(self.labels, role), LABEL_STYLES[role]) for role in StateLabel]
        wanted += list(marker_label_styles(self.labels).items())
        wanted += list((extra or {}).items())
        results: list[LabelEnsured] = []
        for name, style in wanted:
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

    async def missing_labels(self, extra: Sequence[str] = ()) -> list[str]:
        self._log.debug("missing_labels")
        existing = await self._repo_labels()
        wanted = (*self.labels.as_tuple(), *self.labels.markers(), *extra)
        return [name for name in wanted if name.lower() not in existing]

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
        self._log.debug("rate_limit")
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
        self._log.debug("auth_status")
        result = await self._gh(["api", "user", "--jq", ".login"])
        login = result.stdout.strip()
        if not login or login in ("null", "{}", "[1]") or login.startswith(("{", "[")):
            raise GitHubError("response", "user response has no login")
        if self._login is None:
            self._login = login
        return AuthStatus(login=login)

    async def repo_info(self) -> RepoInfo:
        self._log.debug("repo_info")
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

    async def _collect(self, roles: Sequence[StateLabel], query: str) -> list[Issue]:
        login = await self.own_login()
        found: dict[int, Issue] = {}
        for role in roles:
            label = label_name(self.labels, role)
            for issue in await self._issues_with_label(label, query, login):
                found.setdefault(issue.number, issue)
        return sorted(found.values(), key=lambda issue: (issue.created_at, issue.number))

    async def _issues_with_label(self, label: str, query: str, login: str) -> list[Issue]:
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
                if not isinstance(node, Mapping):
                    self._log.warning(
                        "issue_record_skipped", issue_number=None, reason="record is not an object"
                    )
                    continue
                try:
                    issues.append(self._issue(node, login))
                except GitHubError as exc:
                    number = node.get("number")
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
        stderr = self._redact(result.stderr)
        payload = _parse_json(result.stdout)
        errors = payload.get("errors") if isinstance(payload, Mapping) else None
        if isinstance(errors, list) and errors:
            self._raise_for_graphql_errors(errors, result, stderr, allow_missing_aliases)
        elif result.returncode != 0:
            raise self._error_for(result)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise GitHubError(
                "response",
                "GraphQL response has no data object",
                exit_code=result.returncode,
                stderr=stderr,
            )
        return data

    def _raise_for_graphql_errors(
        self, errors: list[Any], result: GhResult, stderr: str, allow_missing_aliases: bool
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
            raise GitHubError("rate_limited", messages, exit_code=result.returncode, stderr=stderr)
        category: ErrorCategory = "not_found" if types == {"NOT_FOUND"} else "response"
        raise GitHubError(category, messages, exit_code=result.returncode, stderr=stderr)

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
