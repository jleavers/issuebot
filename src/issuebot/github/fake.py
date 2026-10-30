"""In-memory GitHubAdapter with GitHub-like semantics and helpers for tests."""

import copy
import html
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from issuebot.config import GitHubLabels, GitHubSettings
from issuebot.github.errors import ErrorCategory, GitHubError
from issuebot.github.models import (
    ApprovalEvidence,
    AuthStatus,
    BranchRules,
    Comment,
    Issue,
    LabelApplied,
    LabelEnsured,
    LinkedPr,
    Mergeable,
    ParentSteps,
    PrReview,
    PrState,
    RateLimit,
    RepoInfo,
    ReviewStep,
    StateLabel,
    SubIssue,
    TextEdit,
    TrackerComment,
    UnreadableParent,
    is_workpad_body,
)
from issuebot.github.normalise import (
    issue_from_node,
    label_name,
    parent_record_from_node,
    review_step_from_node,
)
from issuebot.github.state import (
    LABEL_STYLES,
    TERMINAL_SWEEP_ROLES,
    LabelStyle,
    marker_label_styles,
)
from issuebot.invocation import run_hint

_PR_STATE_UPPER: dict[PrState, str] = {"open": "OPEN", "closed": "CLOSED", "merged": "MERGED"}


_FENCE = re.compile(
    r"^```[ \t]*([^\s`]*)[^\n]*\n(.*?)(?:^```[ \t]*$\n?|\Z)", re.DOTALL | re.MULTILINE
)
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)


def render_body_html(body: str | None) -> str | None:
    """A minimal stand-in for GitHub's render (GHSA-f3fm-r55f-2vgm): paragraphs and fenced
    code, everything else escaped, HTML comments dropped. Enough for the hermetic suite to
    exercise the same path production takes; the real renderer is GitHub's. Limits: fences are
    split before comments are dropped, so a fence inside a comment surfaces, and ``~~~`` fences
    are prose, so a comment inside one is dropped where GitHub shows it as code. The first errs
    toward revealing, the second toward hiding."""
    if body is None:
        return None
    parts: list[str] = []

    def prose(text: str) -> None:
        for paragraph in re.split(r"\n\s*\n", _COMMENT.sub("", text)):
            if paragraph.strip():
                parts.append(f'<p dir="auto">{html.escape(paragraph.strip())}</p>')

    position = 0
    for match in _FENCE.finditer(body):
        prose(body[position : match.start()])
        lang = f' lang="{html.escape(match.group(1))}"' if match.group(1) else ""
        parts.append(f"<pre{lang}><code>{html.escape(match.group(2))}</code></pre>")
        position = match.end()
    prose(body[position:])
    return "\n".join(parts) + "\n" if parts else ""


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
    # GitHub's `stateReason`: set when the issue closes, `None` while it is open.
    state_reason: str | None = None
    author_association: str = "NONE"
    comments: list[Comment] = field(default_factory=list)
    # (actor, label, at) for every label added, oldest first: GitHub's timeline, in miniature.
    label_events: list[tuple[str, str, datetime]] = field(default_factory=list)
    # Body edits and title renames, oldest first, as ``approval_evidence`` reports them.
    edits: list[TextEdit] = field(default_factory=list)
    # The sub-issue link, both ways, as GitHub keeps it. ``parent_repo`` is the parent's
    # repository when it is not this one (#359); ``None`` means this repository.
    parent: int | None = None
    parent_repo: str | None = None
    children: list[int] = field(default_factory=list)
    # What ``Comment`` cannot carry, by comment id: (author or None once deleted, association).
    tracker_authors: dict[int, tuple[str | None, str]] = field(default_factory=dict)


@dataclass
class _FakePr:
    number: int
    closes: int
    author: str
    state: PrState = "open"
    merged_at: datetime | None = None
    mergeable: Mergeable = "mergeable"
    cross_repository: bool = False
    title: str = ""
    body: str = ""
    reviews: list[PrReview] = field(default_factory=list)


class FakeGitHub:
    """Implements GitHubAdapter in memory; produces Issue records through the same normaliser.

    ``login`` is the account the fake acts as: what ``auth_status`` reports, who ``comment``
    writes as, whose pull requests ``open_pr`` opens by default, and the provenance the
    normaliser resolves ``linked_pr`` and ``find_workpad_comment`` by (#77). ``admin`` is what
    ``repo_info`` says about that account's standing on the repository: whether its own label
    events approve (GHSA-jm8h-q3j6-p8xp).
    """

    def __init__(
        self,
        settings: GitHubSettings,
        *,
        preseed_labels: bool = True,
        now: Callable[[], datetime] | None = None,
        login: str = "issuebot",
        admin: bool = False,
    ) -> None:
        self._settings = settings
        self._now = now or (lambda: datetime.now(UTC))
        self.login = login
        self.admin = admin
        self.branch_rules_result: BranchRules | None = None
        self.repo_labels: dict[str, LabelStyle] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._issues: dict[int, _FakeIssue] = {}
        self._prs: dict[int, _FakePr] = {}
        self._next_number = 1
        self._next_comment_id = 1000
        # [category, method or None, matching calls still to let through], one per failure.
        self._failures: list[list[Any]] = []
        # Parent number -> the fields ``parent_steps`` leaves out of its record (#358).
        self._malformed_parents: dict[int, tuple[str, ...]] = {}
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

    async def fetch_issues_with_label(self, label: str) -> list[Issue]:
        self._enter("fetch_issues_with_label", label)
        return self._snapshots(
            record
            for record in self._issues.values()
            if record.state == "open" and label.lower() in {name.lower() for name in record.labels}
        )

    async def fetch_issues_with_any_label(self, labels: Sequence[str]) -> list[Issue]:
        self._enter("fetch_issues_with_any_label", tuple(labels))
        wanted = {label.lower() for label in labels}
        return self._snapshots(
            record
            for record in self._issues.values()
            if record.state == "open" and wanted & {name.lower() for name in record.labels}
        )

    async def fetch_issues_opened_since(self, since: datetime) -> list[Issue]:
        self._enter("fetch_issues_opened_since", since)
        return self._snapshots(
            record
            for record in self._issues.values()
            if record.state == "open" and record.created_at > since
        )

    async def fetch_issues_by_ids(self, ids: Iterable[str]) -> list[Issue]:
        wanted = tuple(ids)
        self._enter("fetch_issues_by_ids", wanted)
        numbers = sorted(
            {int(value) for value in wanted if str(value).isascii() and str(value).isdigit()}
        )
        return [self._snapshot(self._issues[n]) for n in numbers if n in self._issues]

    async def fetch_terminal_issues(self) -> list[Issue]:
        """The sweep's read: closed issues under every state role but ``complete`` (#149).

        ``complete`` is where a closed issue rests, so the real adapter stopped asking for it;
        the fake has to stop too, or every orchestrator test would be exercising a read the
        worker no longer makes.
        """
        self._enter("fetch_terminal_issues")
        state_names = {label_name(self.labels, role).lower() for role in TERMINAL_SWEEP_ROLES}
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
            markers = {name.lower() for name in self.labels.claim_clears()}
            record.labels = [name for name in record.labels if name.lower() not in markers]
        record.labels.append(target)
        record.updated_at = self._now()
        record.label_events.append((self.login, target, record.updated_at))

    async def clear_state(self, number: int) -> None:
        self._enter("clear_state", number)
        record = self._require_issue(number)
        self._require_state_labels()
        self._strip_state_labels(record)
        record.updated_at = self._now()

    async def edit_labels(
        self, number: int, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        self._enter("edit_labels", number, tuple(add), tuple(remove))
        if not add and not remove:
            return
        record = self._require_issue(number)
        for name in (*add, *remove):
            self._require_label(name)
        gone = {name.lower() for name in remove}
        record.labels = [name for name in record.labels if name.lower() not in gone]
        record.updated_at = self._now()
        for name in add:
            if name.lower() not in {held.lower() for held in record.labels}:
                record.labels.append(name)
                record.label_events.append((self.login, name, record.updated_at))

    async def fetch_comments(self, number: int) -> list[TrackerComment]:
        self._enter("fetch_comments", number)
        record = self._require_issue(number)
        found: list[TrackerComment] = []
        for comment in record.comments:
            author, association = record.tracker_authors.get(comment.id, (comment.author, "NONE"))
            found.append(
                TrackerComment(
                    id=comment.id,
                    url=comment.url,
                    author=author,
                    association=association,
                    body_html=render_body_html(comment.body) or "",
                    created_at=comment.created_at,
                )
            )
        return found

    async def sub_issues(self, number: int) -> list[SubIssue]:
        self._enter("sub_issues", number)
        record = self._require_issue(number)
        return [self._sub_issue(self._issues[child]) for child in record.children]

    async def parent_steps(
        self, numbers: Iterable[int]
    ) -> dict[int, ParentSteps | UnreadableParent]:
        wanted = sorted(set(numbers))
        self._enter("parent_steps", wanted)
        found: dict[int, ParentSteps | UnreadableParent] = {}
        for number in wanted:
            record = self._issues.get(number)
            if record is None:
                continue
            node = {
                "id": self._node_id(number),
                "number": number,
                "state": "OPEN" if record.state == "open" else "CLOSED",
                "labels": {"nodes": [{"name": name} for name in record.labels]},
                "subIssues": {
                    "nodes": [
                        {
                            "number": child.number,
                            "title": child.title,
                            "url": self._issue_url(child.number),
                            "state": "OPEN" if child.state == "open" else "CLOSED",
                        }
                        for child in (self._issues[n] for n in record.children)
                    ]
                },
            }
            for name in self._malformed_parents.get(number, ()):
                node.pop(name, None)
            found[number] = parent_record_from_node(number, node)
        return found

    async def parent_review_steps(self, number: int) -> list[ReviewStep]:
        self._enter("parent_review_steps", number)
        record = self._require_issue(number)
        return [
            review_step_from_node(copy.deepcopy(self._step_node(self._issues[child])))
            for child in record.children
        ]

    async def close_as_completed(self, number: int) -> None:
        self._enter("close_as_completed", number)
        self._require_issue(number)
        self.close_issue(number)

    async def create_sub_issue(self, parent: int, *, title: str, body: str) -> SubIssue:
        self._enter("create_sub_issue", parent, title, body)
        record = self._require_issue(parent)
        child = self._issues[self.add_issue(title, body=body, author=self.login).number]
        child.parent = parent
        record.children.append(child.number)
        return self._sub_issue(child)

    async def comment(self, number: int, body: str) -> Comment:
        self._enter("comment", number, body)
        record = self._require_issue(number)
        stamp = self._now()
        comment = Comment(
            id=self._next_comment_id,
            body=body,
            url=f"{self._issue_url(number)}#issuecomment-{self._next_comment_id}",
            author=self.login,
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
            if is_workpad_body(comment.body) and comment.author.lower() == self.login.lower():
                return comment
        return None

    async def count_own_label_additions(self, number: int, label: str) -> int:
        self._enter("count_own_label_additions", number, label)
        record = self._require_issue(number)
        return sum(
            1
            for actor, name, _at in record.label_events
            if actor.lower() == self.login.lower() and name.lower() == label.lower()
        )

    async def own_login(self) -> str:
        self._enter("own_login")
        return self.login

    async def approval_evidence(self, number: int) -> ApprovalEvidence:
        self._enter("approval_evidence", number)
        record = self._require_issue(number)
        return ApprovalEvidence(
            label_events=tuple(
                LabelApplied(label=name, actor=actor, at=at)
                for actor, name, at in record.label_events
            ),
            edits=tuple(record.edits),
        )

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
        return AuthStatus(login=self.login)

    async def repo_info(self) -> RepoInfo:
        self._enter("repo_info")
        return RepoInfo(full_name=self.repo, default_branch="main", private=False, admin=self.admin)

    async def branch_rules(self, branch: str) -> BranchRules:
        self._enter("branch_rules", branch)
        if self.branch_rules_result is not None:
            return self.branch_rules_result
        return BranchRules(
            branch=branch,
            required_approving_reviews=1,
            require_last_push_approval=True,
            require_code_owner_review=True,
            dismiss_stale_reviews_on_push=True,
            bypassable=(),
        )

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
        author_association: str = "NONE",
        created_at: datetime | None = None,
    ) -> Issue:
        if number is None:
            number = self._next_number
        elif number in self._issues or number in self._prs:
            raise ValueError(f"number {number} already exists")
        self._next_number = max(self._next_number, number + 1)
        stamp = created_at if created_at is not None else self._now()
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
            author_association=author_association,
        )
        self._issues[number] = record
        return self._snapshot(record)

    def link_sub_issue(self, parent: int, child: int) -> None:
        """Link an existing issue under ``parent``, last in its order, as GitHub's UI does."""
        parent_record = self._require_issue(parent)
        child_record = self._require_issue(child)
        if child_record.parent is not None:
            raise ValueError(f"#{child} already has a parent: #{child_record.parent}")
        child_record.parent = parent
        parent_record.children.append(child)

    def link_foreign_parent(self, child: int, *, repo: str, parent: int) -> None:
        """Link an existing issue under issue ``parent`` of another repository, which GitHub
        allows (#359). The fake holds only this repository, so the parent has no record."""
        if repo.lower() == self.repo.lower():
            raise ValueError(f"{repo} is this repository: use link_sub_issue")
        child_record = self._require_issue(child)
        if child_record.parent is not None:
            raise ValueError(f"#{child} already has a parent: #{child_record.parent}")
        child_record.parent = parent
        child_record.parent_repo = repo

    def human_set_state(self, number: int, state: StateLabel, *, actor: str = "reporter") -> None:
        """A state label set by someone else; ``actor`` is who the timeline credits."""
        record = self._require_issue(number)
        self._strip_state_labels(record)
        target = label_name(self.labels, state)
        record.labels.append(target)
        record.updated_at = self._now()
        record.label_events.append((actor, target, record.updated_at))

    def human_add_label(self, number: int, name: str, *, actor: str = "reporter") -> None:
        record = self._require_issue(number)
        if name.lower() not in {label.lower() for label in record.labels}:
            record.labels.append(name)
            record.updated_at = self._now()
            record.label_events.append((actor, name, record.updated_at))

    def human_record_label(self, number: int, name: str, *, actor: str = "reporter") -> None:
        """Records the event for a label that is already on the issue, without adding it."""
        record = self._require_issue(number)
        record.label_events.append((actor, name, self._now()))

    def human_remove_label(self, number: int, name: str) -> None:
        record = self._require_issue(number)
        record.labels = [label for label in record.labels if label.lower() != name.lower()]
        record.updated_at = self._now()

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

    def add_comment(self, number: int, body: str, *, author: str) -> Comment:
        """A comment by someone else (a human, another bot): what ``comment`` cannot write."""
        record = self._require_issue(number)
        stamp = self._now()
        comment = Comment(
            id=self._next_comment_id,
            body=body,
            url=f"{self._issue_url(number)}#issuecomment-{self._next_comment_id}",
            author=author,
            created_at=stamp,
            updated_at=stamp,
        )
        self._next_comment_id += 1
        record.comments.append(comment)
        return comment

    def add_tracker_comment(
        self, number: int, *, body: str, author: str | None, association: str
    ) -> Comment:
        """A comment with the association (and, for a deleted account, no author) that
        ``fetch_comments`` reports; ``add_comment`` leaves both at their defaults."""
        comment = self.add_comment(number, body, author=author or "")
        self._require_issue(number).tracker_authors[comment.id] = (author, association)
        return comment

    def open_pr(
        self,
        issue_number: int,
        *,
        pr_number: int | None = None,
        author: str | None = None,
        cross_repository: bool = False,
        title: str = "",
        body: str = "",
    ) -> LinkedPr:
        """A pull request whose body closes ``issue_number``; issuebot's own unless said otherwise.

        Returns the record as the normaliser would build it, which is not the same as saying
        the issue now links to it: one by another ``author``, or from a fork, closes the issue
        on GitHub and is still nobody's as far as ``Issue.linked_pr`` is concerned.
        """
        self._require_issue(issue_number)
        if pr_number is None:
            pr_number = self._next_number
        elif pr_number in self._issues or pr_number in self._prs:
            raise ValueError(f"number {pr_number} already exists")
        self._next_number = max(self._next_number, pr_number + 1)
        self._prs[pr_number] = _FakePr(
            number=pr_number,
            closes=issue_number,
            author=self.login if author is None else author,
            cross_repository=cross_repository,
            title=title,
            body=body,
        )
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

    def add_pr_review(
        self,
        pr_number: int,
        *,
        author: str,
        association: str,
        state: str = "APPROVED",
        body: str = "",
    ) -> None:
        """A review of the pull request by someone else."""
        pr = self._require_pr(pr_number)
        anchor = f"pullrequestreview-{len(pr.reviews) + 1}"
        pr.reviews.append(
            PrReview(
                author=author,
                association=association,
                state=state,
                body_html=render_body_html(body) or "",
                url=f"https://github.com/{self.repo}/pull/{pr_number}#{anchor}",
            )
        )

    def close_issue(self, number: int, *, reason: str = "COMPLETED") -> None:
        """Close the issue, recording GitHub's ``stateReason`` (``COMPLETED``, ``NOT_PLANNED``)."""
        record = self._require_issue(number)
        record.state = "closed"
        record.state_reason = reason
        record.closed_at = self._now()
        record.updated_at = record.closed_at

    def reopen_issue(self, number: int) -> None:
        record = self._require_issue(number)
        record.state = "open"
        record.state_reason = None
        record.closed_at = None
        record.updated_at = self._now()

    def comments_for(self, number: int) -> list[Comment]:
        return list(self._require_issue(number).comments)

    def issue(self, number: int) -> Issue:
        return self._snapshot(self._require_issue(number))

    def malform_parent(self, number: int, *fields: str) -> None:
        """Make ``parent_steps`` serve parent ``number``'s record without ``fields`` (``"id"``,
        ``"subIssues"``, ...), through the same normaliser as ever; no fields puts it right."""
        if fields:
            self._malformed_parents[number] = fields
        else:
            self._malformed_parents.pop(number, None)

    def fail_next(
        self, category: ErrorCategory, *, times: int = 1, on: str | None = None, after: int = 0
    ) -> None:
        """Make calls fail with ``category``, ``times`` in a row.

        Bare, it is the very next call of any method. ``on`` narrows it to calls of that
        method (the name ``_enter`` receives): the others pass through and do not consume it.
        ``after=N`` lets N matching calls succeed first, once, before the run of ``times``
        failures: ``on="create_sub_issue", after=1`` fails the second creation.
        """
        self._failures.extend([[category, on, after if i == 0 else 0] for i in range(times)])

    # --- internals ---------------------------------------------------------------

    def _enter(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        for failure in self._failures:
            category, only, after = failure
            if only is not None and only != name:
                continue
            if after:
                failure[2] -= 1
                return
            self._failures.remove(failure)
            raise GitHubError(category, f"injected {category} failure")
        return

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
            raise GitHubError("not_found", f"'{name}' not found; {run_hint('labels ensure')}")

    def _require_state_labels(self) -> None:
        for role in StateLabel:
            self._require_label(label_name(self.labels, role))

    def _strip_state_labels(self, record: _FakeIssue) -> None:
        state_names = {name.lower() for name in self.labels.as_tuple()}
        record.labels = [label for label in record.labels if label.lower() not in state_names]

    def _issue_url(self, number: int) -> str:
        return f"https://github.com/{self.repo}/issues/{number}"

    def _sub_issue(self, record: _FakeIssue) -> SubIssue:
        return SubIssue(
            number=record.number,
            title=record.title,
            url=self._issue_url(record.number),
            state="open" if record.state == "open" else "closed",
        )

    def _node_id(self, number: int) -> str:
        return f"I_{number}"

    def _step_node(self, record: _FakeIssue) -> dict[str, Any]:
        """A GraphQL-shaped step of ``parent_review_steps_query``: the issue, how it closed and,
        when a merged pull request that closes it did, that pull request."""
        closer = next(
            (
                pr
                for pr in reversed(list(self._prs.values()))
                if pr.closes == record.number and pr.state == "merged"
            ),
            None,
        )
        events: list[dict[str, Any]] = []
        if record.state == "closed" and closer is not None:
            events.append(
                {
                    "closer": {
                        "__typename": "PullRequest",
                        "number": closer.number,
                        "title": closer.title,
                        "url": f"https://github.com/{self.repo}/pull/{closer.number}",
                        "bodyHTML": render_body_html(closer.body) or "",
                        "merged": True,
                        "authorAssociation": "NONE",
                        "author": {"login": closer.author},
                        "reviews": {
                            "nodes": [
                                {
                                    "url": review.url,
                                    "state": review.state,
                                    "bodyHTML": review.body_html,
                                    "authorAssociation": review.association,
                                    "author": {"login": review.author},
                                }
                                for review in closer.reviews
                            ]
                        },
                    }
                }
            )
        return {
            "number": record.number,
            "title": record.title,
            "url": self._issue_url(record.number),
            "bodyHTML": render_body_html(record.body) or "",
            "state": "OPEN" if record.state == "open" else "CLOSED",
            "stateReason": record.state_reason,
            "authorAssociation": record.author_association,
            "author": {"login": record.author} if record.author is not None else None,
            "labels": {"nodes": [{"name": name} for name in record.labels]},
            "timelineItems": {"nodes": events},
        }

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
            "bodyHTML": render_body_html(record.body),
            "author": {"login": record.author} if record.author is not None else None,
            "state": "OPEN" if record.state == "open" else "CLOSED",
            "url": self._issue_url(record.number),
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "closedAt": record.closed_at.isoformat() if record.closed_at else None,
            "authorAssociation": record.author_association,
            "parent": (
                {
                    "number": record.parent,
                    "repository": {"nameWithOwner": record.parent_repo or self.repo},
                }
                if record.parent is not None
                else None
            ),
            "subIssuesSummary": {"total": len(record.children)},
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
                        "isCrossRepository": pr.cross_repository,
                        "author": {"login": pr.author},
                    }
                    for pr in self._prs.values()
                    if pr.closes == record.number
                ]
            },
        }

    def _snapshot(self, record: _FakeIssue) -> Issue:
        return issue_from_node(
            copy.deepcopy(self._node(record)), repo=self.repo, labels=self.labels, login=self.login
        )

    def _snapshots(self, records: Iterable[_FakeIssue]) -> list[Issue]:
        issues = [self._snapshot(record) for record in records]
        return sorted(issues, key=lambda issue: (issue.created_at, issue.number))
