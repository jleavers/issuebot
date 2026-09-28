"""Normalised GitHub records shared by the adapter, the fake, the orchestrator and the CLI."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

WORKPAD_MARKER = "## Issuebot Workpad"


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


@dataclass(frozen=True, kw_only=True, slots=True)
class Comment:
    id: int
    body: str
    url: str
    author: str
    created_at: datetime
    updated_at: datetime


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
    bypass -- so the count and the two flags are the union over the rulesets that do bind
    (the largest count; either flag where any of them sets it), and ``bypassable`` names the
    rulesets carrying a ``pull_request`` rule that the caller may bypass, whose requirements
    hold it to nothing and are therefore not counted (GHSA-jm8h-q3j6-p8xp).

    ``require_last_push_approval`` is the ``pull_request`` rule's own
    ``require_last_push_approval`` parameter (API name; "Require approval of the most recent
    reviewable push" in the UI). Without it an approval survives a later push, so a session can
    push to an already-approved pull request and merge it (GHSA-jm8h-q3j6-p8xp) -- the review
    count alone does not stop that.

    ``require_code_owner_review`` is the rule's ``require_code_owner_review`` parameter
    ("Require review from Code Owners"). Without it any account with write approves, the
    session's own included: on a pull request someone else opened -- from a fork, where they
    are the last pusher -- the session's approval satisfies the rule, latest push and all, and
    the session can then merge it. With it, the approval that counts is a code owner's, which
    keeps it a human's only if ``CODEOWNERS`` names only humans; nothing here reads that file.
    """

    branch: str
    required_approving_reviews: int | None
    require_last_push_approval: bool = False
    require_code_owner_review: bool = False
    bypassable: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True, slots=True)
class AuthStatus:
    login: str


@dataclass(frozen=True, kw_only=True, slots=True)
class LabelEnsured:
    name: str
    outcome: LabelOutcome
