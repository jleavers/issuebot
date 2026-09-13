"""Turn GraphQL issue nodes into Issue records; shared by the gh adapter and the fake."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from issuebot.config import GitHubLabels
from issuebot.github.errors import GitHubError
from issuebot.github.models import Issue, LinkedPr, Mergeable, PrState, StateLabel

_PR_STATES: dict[str, PrState] = {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}
_PR_RANK: dict[PrState, int] = {"merged": 0, "open": 1, "closed": 2}
_MERGEABLE: dict[str, Mergeable] = {
    "MERGEABLE": "mergeable",
    "CONFLICTING": "conflicting",
    "UNKNOWN": "unknown",
}


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
        raw_mergeable = item.get("mergeable")
        mergeable = (
            _MERGEABLE.get(raw_mergeable, "unknown")
            if isinstance(raw_mergeable, str)
            else "unknown"
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
    if not candidates:
        return None

    def rank(candidate: LinkedPr) -> tuple[int, float, int]:
        merged = candidate.merged_at.timestamp() if candidate.merged_at else 0.0
        return (_PR_RANK[candidate.state], -merged, -candidate.number)

    return min(candidates, key=rank)
