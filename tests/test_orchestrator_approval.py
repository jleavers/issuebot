"""The approval check is pure: evidence in, a verdict out (GHSA-jm8h-q3j6-p8xp)."""

from datetime import UTC, datetime, timedelta

from issuebot.github import ApprovalEvidence, LabelApplied, TextEdit
from issuebot.orchestrator.approval import Approved, Unapproved, assess

T0 = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
ADMITTING = ("issuebot/todo", "issuebot/rework")


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def labelled(label: str, actor: str | None, minutes: int) -> LabelApplied:
    return LabelApplied(label=label, actor=actor, at=at(minutes))


def edited(what: str, editor: str | None, minutes: int) -> TextEdit:
    return TextEdit(what=what, editor=editor, at=at(minutes))  # type: ignore[arg-type]


def verdict(*events: LabelApplied, edits: tuple[TextEdit, ...] = ()) -> Approved | Unapproved:
    return assess(
        ApprovalEvidence(label_events=events, edits=edits), admitting=ADMITTING, own_login="bot"
    )


def test_a_labelled_issue_with_no_edits_is_approved() -> None:
    result = verdict(labelled("issuebot/todo", "maintainer", 0))
    assert result == Approved(approver="maintainer", at=at(0))


def test_an_edit_before_the_label_is_the_text_that_was_approved() -> None:
    result = verdict(
        labelled("issuebot/todo", "maintainer", 5), edits=(edited("body", "reporter", 1),)
    )
    assert isinstance(result, Approved)


def test_an_edit_at_the_labels_own_second_counts_as_before_it() -> None:
    """GitHub stamps both to the second; "edit, then label" in one second is not an edit after."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 5), edits=(edited("body", "reporter", 5),)
    )
    assert isinstance(result, Approved)


def test_the_authors_edit_after_the_label_un_approves() -> None:
    approval = labelled("issuebot/todo", "maintainer", 0)
    edit = edited("body", "reporter", 10)
    result = verdict(approval, edits=(edit,))
    assert result == Unapproved(
        reason=(
            "body edited at 2026-09-28T09:10:00Z by reporter, after maintainer applied "
            "`issuebot/todo` at 2026-09-28T09:00:00Z"
        ),
        approval=approval,
        edit=edit,
    )


def test_a_title_rename_after_the_label_un_approves() -> None:
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0), edits=(edited("title", "reporter", 1),)
    )
    assert isinstance(result, Unapproved) and result.reason.startswith("title edited at ")


def test_the_approver_editing_what_they_approved_is_fine() -> None:
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0), edits=(edited("body", "MAINTAINER", 10),)
    )
    assert isinstance(result, Approved)


def test_another_maintainers_edit_un_approves_too() -> None:
    """The false positive the spec names: one relabel, rather than a permission lookup."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0), edits=(edited("body", "colleague", 10),)
    )
    assert isinstance(result, Unapproved)


def test_an_edit_by_a_deleted_account_un_approves() -> None:
    result = verdict(labelled("issuebot/todo", "maintainer", 0), edits=(edited("body", None, 10),))
    assert isinstance(result, Unapproved) and " by an account GitHub has deleted, " in result.reason


def test_edits_are_assessed_in_time_order_whatever_order_they_arrive() -> None:
    """``userContentEdits`` answers newest first; only the edits after the approval matter."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 5),
        edits=(edited("body", "maintainer", 20), edited("body", "reporter", 1)),
    )
    assert isinstance(result, Approved)


def test_the_latest_human_admitting_label_is_the_approval() -> None:
    """todo, an edit, then rework by a human: the rework event approves the edited text."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0),
        labelled("issuebot/in-progress", "bot", 1),
        labelled("issuebot/review", "bot", 2),
        labelled("issuebot/rework", "reviewer", 10),
        edits=(edited("body", "reporter", 5),),
    )
    assert result == Approved(approver="reviewer", at=at(10))


def test_issuebots_own_label_events_are_not_approvals() -> None:
    """The conflict bounce applies rework itself; that must not launder an edit."""
    result = verdict(
        labelled("issuebot/todo", "maintainer", 0),
        labelled("issuebot/rework", "BOT", 10),
        edits=(edited("body", "reporter", 5),),
    )
    assert isinstance(result, Unapproved) and result.approval == labelled(
        "issuebot/todo", "maintainer", 0
    )


def test_an_admitting_label_nobody_applied_is_not_approved() -> None:
    result = verdict(labelled("issuebot/in-progress", "bot", 0))
    assert result == Unapproved(
        reason="no account other than bot has applied `issuebot/todo` or `issuebot/rework`",
        approval=None,
        edit=None,
    )


def test_a_label_by_a_deleted_account_is_not_an_approval() -> None:
    result = verdict(labelled("issuebot/todo", None, 0))
    assert isinstance(result, Unapproved) and result.approval is None


def test_label_names_compare_case_insensitively() -> None:
    result = verdict(labelled("Issuebot/Todo", "maintainer", 0))
    assert isinstance(result, Approved)
