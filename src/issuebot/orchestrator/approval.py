"""Is the text a session would act on the text a human approved? (GHSA-jm8h-q3j6-p8xp)

A maintainer applying ``issuebot/todo`` approves the issue's title and body *as they stand*.
Nothing pins that text, so this module reads GitHub's own record instead: the latest ``todo``
label event by an account that can approve is the approval, and any edit at or after it by
anyone but the approver un-approves the issue.

Only ``todo`` approves. ``rework`` never does, whoever applies it: issuebot's conflict bounce
applies it itself, and a reviewer's ``rework`` asks for changes to the *pull request* -- it is
not a statement that they re-read the issue. An edit made during review is therefore checked
against the ``todo`` before it, and the way to adopt it is applying ``todo`` again.

issuebot's own label events approve only when ``own_labels_approve`` says its account
administers the repository. On a dedicated bot account they never do, so its own relabel --
the bounce's, or a session's -- cannot launder an edit that came before it. On the
maintainer's own token they must, or a solo operator whose token *is* their account has no
approver at all and every issue is refused for ever; and an account that administers the
repository can rewrite the branch ruleset anyway, so its label is a maintainer's in every
sense the check cares about. An actor GitHub no longer names (``None``: a deleted account, or
an app or bot, which ``approval_evidence`` reports the same way) is never an approver.

The approver editing what they approved is fine -- a solo operator labels and then tightens
the wording. Another maintainer's edit is refused too, which is a false positive the design
names: one relabel, rather than a permission lookup per editor, where admitting an outsider's
edit would be the advisory. An edit stamped in the label's own second counts as *after* it:
GitHub records both to the second, the approver's own edits are exempt whatever their time,
so a tie that matters is someone else's edit landing in that second, and assuming the label
went on last would admit exactly the edit this check exists to refuse.

Pure. The orchestrator and ``run-once`` fetch the evidence and act on the verdict.
"""

from dataclasses import dataclass
from datetime import datetime

from issuebot.github import ApprovalEvidence, LabelApplied, TextEdit

DELETED_ACCOUNT = "an account GitHub has deleted"
# The two ways an issue has no approval at all. The first is a deployment on a dedicated
# account whose own `todo` is the only one there is; the second is a label nobody GitHub names
# as a person applied -- an app, a deleted account, or a label put on before anyone watched.
NO_OTHER_APPROVER = "no account other than {own_login} has applied `{todo_label}`"
NO_APPROVER = "no account has applied `{todo_label}`"


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
    evidence: ApprovalEvidence, *, todo_label: str, own_login: str, own_labels_approve: bool
) -> Approved | Unapproved:
    """The verdict for one issue.

    ``todo_label`` is the one label that approves; ``own_login`` is the account issuebot acts
    as, and ``own_labels_approve`` whether that account administers the repository (so its
    own ``todo`` is a maintainer's). Comparisons are case-insensitive, as the rest of the
    adapter's are.
    """
    wanted = todo_label.lower()
    own = own_login.lower()
    todos = [
        (event.actor, event)
        for event in evidence.label_events
        if event.actor is not None and event.label.lower() == wanted
    ]
    candidates = [
        (actor, event) for actor, event in todos if own_labels_approve or actor.lower() != own
    ]
    if not candidates:
        # Every named `todo` left out here is the account's own, so there is one to mention.
        shape = NO_OTHER_APPROVER if todos else NO_APPROVER
        return Unapproved(
            reason=shape.format(own_login=own_login, todo_label=todo_label),
            approval=None,
            edit=None,
        )
    approver, approval = max(candidates, key=lambda candidate: candidate[1].at)
    for edit in sorted(evidence.edits, key=lambda item: item.at):
        if edit.at < approval.at:
            continue
        if edit.editor is not None and edit.editor.lower() == approver.lower():
            continue
        editor = edit.editor if edit.editor is not None else DELETED_ACCOUNT
        return Unapproved(
            reason=(
                f"{edit.what} edited at {_stamp(edit.at)} by {editor}, after {approver} "
                f"applied `{approval.label}` at {_stamp(approval.at)}"
            ),
            approval=approval,
            edit=edit,
        )
    return Approved(approver=approver, at=approval.at)
