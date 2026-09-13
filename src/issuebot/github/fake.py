"""In-memory GitHubAdapter with GitHub-like semantics and helpers for tests."""

import copy
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from issuebot.config import GitHubLabels, GitHubSettings
from issuebot.github.errors import ErrorCategory, GitHubError
from issuebot.github.models import (
    AuthStatus,
    Comment,
    Issue,
    LabelEnsured,
    LinkedPr,
    Mergeable,
    PrState,
    RateLimit,
    RepoInfo,
    StateLabel,
    is_workpad_body,
)
from issuebot.github.normalise import issue_from_node, label_name
from issuebot.github.state import LABEL_STYLES, LabelStyle, marker_label_styles

_PR_STATE_UPPER: dict[PrState, str] = {"open": "OPEN", "closed": "CLOSED", "merged": "MERGED"}


@dataclass
class _FakeIssue:
    number: int
    title: str
    body: str | None
    author: str | None
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
    mergeable: Mergeable = "mergeable"


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
            self.repo_labels.update(marker_label_styles(settings.labels))

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
        numbers = sorted(
            {int(value) for value in wanted if str(value).isascii() and str(value).isdigit()}
        )
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

    async def set_state(
        self, number: int, state: StateLabel, *, clear_markers: bool = False
    ) -> None:
        self._enter("set_state", number, state)
        record = self._require_issue(number)
        self._require_state_labels()
        target = label_name(self.labels, state)
        self._strip_state_labels(record)
        if clear_markers:
            markers = {name.lower() for name in self.labels.markers()}
            record.labels = [name for name in record.labels if name.lower() not in markers]
        record.labels.append(target)
        record.updated_at = self._now()

    async def clear_state(self, number: int) -> None:
        self._enter("clear_state", number)
        record = self._require_issue(number)
        self._require_state_labels()
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
            if is_workpad_body(comment.body):
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

    async def ensure_labels(
        self, extra: Mapping[str, LabelStyle] | None = None
    ) -> list[LabelEnsured]:
        self._enter("ensure_labels")
        wanted = [(label_name(self.labels, role), LABEL_STYLES[role]) for role in StateLabel]
        wanted += list(marker_label_styles(self.labels).items())
        wanted += list((extra or {}).items())
        results: list[LabelEnsured] = []
        for name, style in wanted:
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

    async def missing_labels(self, extra: Sequence[str] = ()) -> list[str]:
        self._enter("missing_labels")
        wanted = (*self.labels.as_tuple(), *self.labels.markers(), *extra)
        return [name for name in wanted if name not in self.repo_labels]

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
        author: str | None = "reporter",
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
            author=author,
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

    def set_pr_mergeable(self, pr_number: int, mergeable: Mergeable) -> None:
        """What GitHub's test merge would answer for the pull request from now on."""
        self._require_pr(pr_number).mergeable = mergeable

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

    def _require_state_labels(self) -> None:
        for role in StateLabel:
            self._require_label(label_name(self.labels, role))

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
            mergeable=pr.mergeable,
        )

    def _node(self, record: _FakeIssue) -> dict[str, Any]:
        """A GraphQL-shaped node so the fake and the real adapter normalise identically."""
        return {
            "number": record.number,
            "title": record.title,
            "body": record.body,
            "author": {"login": record.author} if record.author is not None else None,
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
                        "mergeable": pr.mergeable.upper(),
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
