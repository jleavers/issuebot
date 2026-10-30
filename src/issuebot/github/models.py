"""Normalised GitHub records shared by the adapter, the fake, the orchestrator and the CLI."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

WORKPAD_MARKER = "## Issuebot Workpad"

MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
"""The ``authorAssociation`` values GitHub gives someone with write access or better."""


def is_workpad_body(body: object) -> bool:
    """True when a comment body's first non-blank line is the workpad marker."""
    if not isinstance(body, str) or not body.strip():
        return False
    return body.lstrip().splitlines()[0].strip() == WORKPAD_MARKER


class StateLabel(StrEnum):
    """The five roles of the label state machine; values equal GitHubLabels field names."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    REWORK = "rework"
    COMPLETE = "complete"


GitHubState = Literal["open", "closed"]
PrState = Literal["open", "closed", "merged"]
Mergeable = Literal["mergeable", "conflicting", "unknown"]
LabelOutcome = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True, kw_only=True, slots=True)
class LinkedPr:
    number: int
    url: str
    state: PrState
    merged_at: datetime | None
    # GitHub's MergeableState, lowercased; "unknown" when it has not been computed or asked for.
    mergeable: Mergeable = "unknown"


@dataclass(frozen=True, kw_only=True, slots=True)
class Issue:
    """One tracked issue, normalised as in the Symphony spec (§4.1.1, §11.3)."""

    id: str
    identifier: str
    number: int
    title: str
    body: str | None
    author: str | None  # the login that opened it; None once GitHub has deleted the account
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
    # GitHub's ``CommentAuthorAssociation`` for the account that opened the issue, upper-case
    # as GitHub spells it (``OWNER``, ``MEMBER``, ``COLLABORATOR``, ``CONTRIBUTOR``, ``NONE``,
    # ...); ``None`` when the record carries none. What the prompt's envelope reports, so a
    # session can tell a maintainer's text from anyone else's (GHSA-jm8h-q3j6-p8xp).
    author_association: str | None = None
    # The parent issue's number when this one is a sub-issue; ``None`` for a top-level issue,
    # and for one whose parent is in another repository (#359), which ``parent_elsewhere`` says.
    parent: int | None = None
    # A sub-issue of an issue in another repository: no parent here, but still a step of
    # someone's split, so auto-triage passes it over as it does any sub-issue (#359).
    parent_elsewhere: bool = False
    # GitHub's sanitised render of `body` (`bodyHTML`): what the prompt shows a session
    # (GHSA-f3fm-r55f-2vgm). `None` exactly when `body` is.
    body_html: str | None = None
    # How many sub-issues it has (`subIssuesSummary { total }`), open or closed: what tells the
    # worker an issue is a parent without a read of its own.
    sub_issue_count: int = 0


@dataclass(frozen=True, kw_only=True, slots=True)
class Comment:
    id: int
    body: str
    url: str
    author: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class TrackerComment:
    """One comment as the triage prompt shows it: GitHub's sanitised render, and who wrote it.

    ``association`` is GitHub's ``CommentAuthorAssociation`` (``OWNER``, ``MEMBER``, ``NONE``,
    ...), the signal that tells a maintainer's text from a stranger's; ``author`` is ``None``
    once GitHub has deleted the account. ``body_html`` and not the raw body, for the reason the
    issue's own text is (GHSA-f3fm-r55f-2vgm): hidden markup is not rendered.
    """

    id: int
    url: str
    author: str | None
    association: str | None
    body_html: str
    created_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class SubIssue:
    """A child of an issue, as GitHub links it."""

    number: int
    title: str
    url: str
    state: GitHubState


@dataclass(frozen=True, kw_only=True, slots=True)
class ParentSteps:
    """A parent issue as the order of its steps needs it: its labels (lower-cased, as
    ``Issue.labels``) and its sub-issues in GitHub's order."""

    number: int
    labels: tuple[str, ...]
    sub_issues: tuple[SubIssue, ...]
    id: str  # the parent's GraphQL node id
    state: GitHubState


@dataclass(frozen=True, kw_only=True, slots=True)
class UnreadableParent:
    """A parent ``parent_steps`` asked for whose record came back malformed (#358).

    It stands in that parent's place in the result, so one bad record costs that parent and
    not the batch, and a reader can tell it from a parent GitHub did not return at all: that
    one is absent. ``reason`` is the normaliser's message."""

    number: int
    reason: str


@dataclass(frozen=True, kw_only=True, slots=True)
class PrReview:
    """One review of a pull request. ``association`` is GitHub's ``CommentAuthorAssociation``,
    upper-case; ``state`` is GitHub's ``PullRequestReviewState`` (``APPROVED``, ...)."""

    author: str | None  # None once GitHub has deleted the account
    association: str | None
    state: str
    body_html: str
    url: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ClosingPr:
    """The merged pull request that closed a step, with its reviews."""

    number: int
    title: str
    url: str
    body_html: str
    author: str | None
    association: str | None
    reviews: tuple[PrReview, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class ReviewStep:
    """One sub-issue as a parent's review needs it: what it asked, how it closed and, when a
    merged pull request closed it, that pull request. ``labels`` are lower-cased;
    ``state_reason`` is GitHub's ``stateReason`` (``COMPLETED``, ``NOT_PLANNED``, ``DUPLICATE``)
    and ``None`` while the step is open. ``closed_by`` is set only for a merged pull request:
    a closing commit, an unmerged pull request or no closer is ``None``."""

    number: int
    title: str
    url: str
    body_html: str
    author: str | None
    association: str | None
    labels: tuple[str, ...]
    state: GitHubState
    state_reason: str | None
    closed_by: ClosingPr | None


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
    # Whether the token's account administers the repository. Such an account can bypass or
    # rewrite the branch ruleset, and its label events are a maintainer's, so the approval
    # check counts its own `todo` when this is true (GHSA-jm8h-q3j6-p8xp).
    admin: bool = False


@dataclass(frozen=True, kw_only=True, slots=True)
class BranchRules:
    """What the repository's rulesets require of the caller's pull request into ``branch``.

    ``required_approving_reviews`` is ``None`` when no ``pull_request`` rule binds the caller.
    Rulesets only: classic branch protection is readable by admins alone, and the check that
    reads this must work for the account it recommends, which is not one.

    A rule binds the caller only when its ruleset says the caller cannot bypass it. GitHub
    lists a branch's rules for everyone -- it does not leave out the ones the caller may
    bypass -- so the count and the three flags are the union over the rulesets that do bind
    (the largest count; each flag where any of them sets it), and ``bypassable`` names the
    rulesets carrying a ``pull_request`` rule that the caller may bypass, whose requirements
    hold it to nothing and are therefore not counted (GHSA-jm8h-q3j6-p8xp).

    ``require_last_push_approval`` is the ``pull_request`` rule's own
    ``require_last_push_approval`` parameter (API name; "Require approval of the most recent
    reviewable push" in the UI). Without it an approval of an earlier push still satisfies the
    rule after a later one, so a session can push to an already-approved pull request and merge
    it (GHSA-jm8h-q3j6-p8xp) -- the review count alone does not stop that.

    ``require_code_owner_review`` is the rule's ``require_code_owner_review`` parameter
    ("Require review from Code Owners"). Without it any account with write approves, the
    session's own included: on a pull request someone else opened -- from a fork, where they
    are the last pusher -- the session's approval satisfies the rule, latest push and all, and
    the session can then merge it. With it, the approval that counts is a code owner's, which
    keeps it a human's only if ``CODEOWNERS`` names only humans and names one for every path:
    code-owner review binds only the paths that have an owner, so with no ``CODEOWNERS``, or
    none matching a path (the recipes' ``* @<you>`` line is what matches them all), any write
    approval counts there again. Nothing here reads that file.

    ``dismiss_stale_reviews_on_push`` is the rule's ``dismiss_stale_reviews_on_push`` parameter
    ("Dismiss stale pull request approvals when new commits are pushed"). GitHub checks the two
    above separately -- latest-push review wants an approval of the latest push from anyone but
    its pusher, code-owner review any code owner's approval not yet dismissed -- so without it
    a maintainer's approval of an earlier push survives the next one, the session's own
    approval of that push completes the pair, and the session can merge what no human saw.
    """

    branch: str
    required_approving_reviews: int | None
    require_last_push_approval: bool = False
    require_code_owner_review: bool = False
    dismiss_stale_reviews_on_push: bool = False
    bypassable: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True, slots=True)
class AuthStatus:
    login: str


@dataclass(frozen=True, kw_only=True, slots=True)
class LabelEnsured:
    name: str
    outcome: LabelOutcome
