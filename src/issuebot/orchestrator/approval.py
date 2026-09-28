"""Is the text a session would act on the text a human approved? (GHSA-jm8h-q3j6-p8xp)

A human applying ``issuebot/todo`` or ``issuebot/rework`` approves the issue's title and body
*as they stand*. Nothing pins that text, so this module reads GitHub's own record instead:
the latest human application of an admitting label is the approval, and any edit after it by
anyone but the approver un-approves the issue. The approver editing what they approved is
fine -- a solo operator labels and then tightens the wording. Another maintainer's edit is
refused too, which is a false positive the design names: one relabel, rather than a
permission lookup per editor, where admitting an outsider's edit would be the advisory.

Pure. The orchestrator fetches the evidence and acts on the verdict.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from issuebot.github import ApprovalEvidence, LabelApplied, TextEdit

DELETED_ACCOUNT = "an account GitHub has deleted"


@dataclass(frozen=True, kw_only=True, slots=True)
class Approved:
    approver: str
    at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class Unapproved:
    """Why the issue is not dispatched. ``reason`` is one line and safe to publish."""

    reason: str
    approval: LabelApplied | None
    edit: TextEdit | None


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def assess(
    evidence: ApprovalEvidence, *, admitting: Sequence[str], own_login: str
) -> Approved | Unapproved:
    """The verdict for one issue.

    ``admitting`` names the labels a human applies to hand an issue to issuebot; ``own_login``
    is the account issuebot acts as, whose own label events (the conflict bounce's ``rework``)
    are never approvals. Comparisons are case-insensitive, as the rest of the adapter's are.
    """
    wanted = {label.lower() for label in admitting}
    own = own_login.lower()
    approvals = [
        event
        for event in evidence.label_events
        if event.label.lower() in wanted and event.actor is not None and event.actor.lower() != own
    ]
    if not approvals:
        names = " or ".join(f"`{label}`" for label in admitting)
        return Unapproved(
            reason=f"no account other than {own_login} has applied {names}",
            approval=None,
            edit=None,
        )
    approval = approvals[-1]
    assert approval.actor is not None  # filtered above; for the type checker
    for edit in sorted(evidence.edits, key=lambda item: item.at):
        if edit.at <= approval.at:
            continue
        if edit.editor is not None and edit.editor.lower() == approval.actor.lower():
            continue
        editor = edit.editor if edit.editor is not None else DELETED_ACCOUNT
        return Unapproved(
            reason=(
                f"{edit.what} edited at {_stamp(edit.at)} by {editor}, after {approval.actor} "
                f"applied `{approval.label}` at {_stamp(approval.at)}"
            ),
            approval=approval,
            edit=edit,
        )
    return Approved(approver=approval.actor, at=approval.at)
