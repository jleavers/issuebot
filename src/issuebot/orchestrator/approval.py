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

Triage and the parent review have a second rule, for the ``triage-todo`` auto-triage applies,
which has no person behind it: authorship (spec 2026-10-25, section 3). The author's
association is a maintainer's, as the dispatching tick read it, and every title and body edit
since the issue was opened is the author's -- so the text is a maintainer's own wording, and
the author tightening it is fine, as it is for an approver. ``assess_triage`` asks rule 1
first, and an approval there stands: on the operator's own admin token issuebot's label *is*
the operator's, the worker cannot tell their hand from auto-triage's, and refusing it would
refuse the operator. Only when rule 1 refuses does the latest ``triage-todo`` say whether
authorship is asked, because the label is the record of who vouched for the text. A person's
means a person read it, so an edit after their reading stays refused however the author
stands; authorship decides only a label issuebot's own account applied, and a tie in one
second goes to the person's, which is the reading that never admits more. When authorship
refuses as well, and rule 1 had counted a label and refused for an edit after it, the
hand-back keeps rule 1's reason, which names the edit. ``assess_triage`` is
never called for ``todo``: authorship passes an issue issuebot's own account opened, which
holds only because the triage and parent-review sessions are read-only and hold no
credential, so a work run's approval is rule 1 alone.

Pure. The orchestrator and ``run-once`` fetch the evidence and act on the verdict.
"""

from dataclasses import dataclass
from datetime import datetime

from issuebot.github import MAINTAINER_ASSOCIATIONS, ApprovalEvidence, LabelApplied, TextEdit
from issuebot.orchestrator.stamps import utc_stamp

DELETED_ACCOUNT = "an account GitHub has deleted"
# The two ways an issue has no approval at all. The first is a deployment on a dedicated
# account whose own `todo` is the only one there is; the second is a `todo` no person GitHub
# still names applied -- an app, a bot, an Actions workflow, or an account since deleted.
NO_OTHER_APPROVER = "no account other than {own_login} has applied `{todo_label}`"
NO_APPROVER = "no account has applied `{todo_label}`"
# The two ways authorship refuses before it looks at an edit (spec 2026-10-25, section 3): an
# author GitHub no longer names, whose standing nobody can read, and one without write access.
AUTHOR_DELETED = "opened by an account GitHub has deleted"
NOT_A_MAINTAINER = "opened by {author}, whose association is {association}, not a maintainer's"


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
                f"{edit.what} edited at {utc_stamp(edit.at)} by {editor}, after {approver} "
                f"applied `{approval.label}` at {utc_stamp(approval.at)}"
            ),
            approval=approval,
            edit=edit,
        )
    return Approved(approver=approver, at=approval.at)


def assess_authorship(
    evidence: ApprovalEvidence,
    *,
    author: str | None,
    author_association: str | None,
    labelled: LabelApplied,
) -> Approved | Unapproved:
    """The authorship verdict for an issue whose latest ``triage-todo`` is ``labelled``.

    ``author`` and ``author_association`` are the issue's as the dispatching tick fetched it:
    the association is re-read at dispatch, never remembered (spec 2026-10-25, section 3). The
    checks run in this order -- a named author, a maintainer's association, then the edits --
    and every edit since the issue was opened counts, not only those after the label: nobody
    read the text when the label went on, so what is approved is the text its author wrote.
    """
    if author is None:
        return Unapproved(reason=AUTHOR_DELETED, approval=labelled, edit=None)
    if author_association not in MAINTAINER_ASSOCIATIONS:
        return Unapproved(
            reason=NOT_A_MAINTAINER.format(
                author=author, association=author_association or "unknown"
            ),
            approval=labelled,
            edit=None,
        )
    for edit in sorted(evidence.edits, key=lambda item: item.at):
        if edit.editor is not None and edit.editor.lower() == author.lower():
            continue
        editor = edit.editor if edit.editor is not None else DELETED_ACCOUNT
        return Unapproved(
            reason=(
                f"{edit.what} edited at {utc_stamp(edit.at)} by {editor}, not by {author}, "
                "who opened it"
            ),
            approval=labelled,
            edit=edit,
        )
    return Approved(approver=author, at=labelled.at)


def assess_triage(
    evidence: ApprovalEvidence,
    *,
    triage_label: str,
    own_login: str,
    own_labels_approve: bool,
    author: str | None,
    author_association: str | None,
) -> Approved | Unapproved:
    """The verdict for a triage or parent-review run: rule 1, then authorship when the latest
    ``triage_label`` is issuebot's own (spec 2026-10-25, section 3).

    Authorship approving wins over rule 1's refusal (phase 4 ruling 22). Otherwise rule 1's
    refusal is the answer whenever authorship is not asked, and whenever authorship refuses too
    after rule 1 counted a label and refused for an edit after it -- a person's, or on an admin
    token the operator's hand-applied one, a stranger's issue included. So the reason a label
    someone applied was refused is the one the hand-back gives, naming who edited and when;
    authorship's own reason is given only where rule 1 counted no label at all.
    """
    verdict = assess(
        evidence,
        todo_label=triage_label,
        own_login=own_login,
        own_labels_approve=own_labels_approve,
    )
    if isinstance(verdict, Approved):
        return verdict
    wanted = triage_label.lower()
    own = own_login.lower()
    named = [
        (event.actor, event)
        for event in evidence.label_events
        if event.actor is not None and event.label.lower() == wanted
    ]
    if not named:
        return verdict
    # The greatest `at`, and in a tie a person's label over issuebot's, whatever order GitHub
    # lists them in: authorship decides only a latest label nobody but issuebot applied.
    actor, latest = max(named, key=lambda item: (item[1].at, item[0].lower() != own))
    if actor.lower() != own:
        return verdict
    authored = assess_authorship(
        evidence, author=author, author_association=author_association, labelled=latest
    )
    if isinstance(authored, Approved) or verdict.approval is None:
        return authored
    # Rule 1 counted a label and refused for an edit after it: on an admin token, the operator
    # hand-applying `triage-todo` to a stranger's issue, which the stranger then edited. That
    # edit is what un-approved it, so the hand-back names it, not the author's standing.
    return verdict
