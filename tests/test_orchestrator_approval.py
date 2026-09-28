"""The approval check is pure: evidence in, a verdict out (GHSA-jm8h-q3j6-p8xp)."""

from datetime import UTC, datetime, timedelta

from issuebot.github import ApprovalEvidence, LabelApplied, TextEdit
from issuebot.orchestrator.approval import Approved, Unapproved, assess

T0 = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
TODO = "issuebot/todo"
REWORK = "issuebot/rework"


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def labelled(label: str, actor: str | None, minutes: int) -> LabelApplied:
    return LabelApplied(label=label, actor=actor, at=at(minutes))


def edited(what: str, editor: str | None, minutes: int) -> TextEdit:
    return TextEdit(what=what, editor=editor, at=at(minutes))  # type: ignore[arg-type]


def verdict(
    *events: LabelApplied, edits: tuple[TextEdit, ...] = (), admin: bool = False
) -> Approved | Unapproved:
    """The verdict for a deployment acting as ``bot``, which administers the repo if ``admin``."""
    return assess(
        ApprovalEvidence(label_events=events, edits=edits),
        todo_label=TODO,
        own_login="bot",
        own_labels_approve=admin,
    )


def test_a_labelled_issue_with_no_edits_is_approved() -> None:
    result = verdict(labelled(TODO, "maintainer", 0))
    assert result == Approved(approver="maintainer", at=at(0))


def test_an_edit_before_the_label_is_the_text_that_was_approved() -> None:
    result = verdict(labelled(TODO, "maintainer", 5), edits=(edited("body", "reporter", 1),))
    assert isinstance(result, Approved)


def test_an_edit_at_the_labels_own_second_counts_as_after_it() -> None:
    """GitHub stamps both to the second, so a tie cannot say which came first. The approver's
    own edits are exempt whatever their time, so a tie that matters is someone else's edit
    landing in the second the label went on -- and that is refused, not assumed read."""
    approval = labelled(TODO, "maintainer", 5)
    edit = edited("body", "reporter", 5)
    result = verdict(approval, edits=(edit,))
    assert result == Unapproved(
        reason=(
            "body edited at 2026-09-28T09:05:00Z by reporter, after maintainer applied "
            "`issuebot/todo` at 2026-09-28T09:05:00Z"
        ),
        approval=approval,
        edit=edit,
    )


def test_the_approvers_own_edit_in_the_labels_second_is_fine() -> None:
    result = verdict(labelled(TODO, "maintainer", 5), edits=(edited("body", "maintainer", 5),))
    assert result == Approved(approver="maintainer", at=at(5))


def test_the_authors_edit_after_the_label_un_approves() -> None:
    approval = labelled(TODO, "maintainer", 0)
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
    result = verdict(labelled(TODO, "maintainer", 0), edits=(edited("title", "reporter", 1),))
    assert isinstance(result, Unapproved) and result.reason.startswith("title edited at ")


def test_the_approver_editing_what_they_approved_is_fine() -> None:
    result = verdict(labelled(TODO, "maintainer", 0), edits=(edited("body", "MAINTAINER", 10),))
    assert isinstance(result, Approved)


def test_another_maintainers_edit_un_approves_too() -> None:
    """The false positive the spec names: one relabel, rather than a permission lookup."""
    result = verdict(labelled(TODO, "maintainer", 0), edits=(edited("body", "colleague", 10),))
    assert isinstance(result, Unapproved)


def test_an_edit_by_a_deleted_account_un_approves() -> None:
    result = verdict(labelled(TODO, "maintainer", 0), edits=(edited("body", None, 10),))
    assert isinstance(result, Unapproved) and " by an account GitHub has deleted, " in result.reason


def test_edits_are_assessed_in_time_order_whatever_order_they_arrive() -> None:
    """``userContentEdits`` answers newest first; only the edits after the approval matter."""
    result = verdict(
        labelled(TODO, "maintainer", 5),
        edits=(edited("body", "maintainer", 20), edited("body", "reporter", 1)),
    )
    assert isinstance(result, Approved)


def test_the_latest_todo_is_the_approval_whatever_order_the_events_arrive() -> None:
    """The approval is the greatest ``at``, not the last event in the list: taken positionally,
    the earlier ``todo`` would be the approval here and the edit between them would refuse it."""
    result = verdict(
        labelled(TODO, "second", 10),
        labelled(TODO, "first", 0),
        edits=(edited("body", "reporter", 5),),
    )
    assert result == Approved(approver="second", at=at(10))


def test_a_maintainers_later_todo_after_an_outsiders_edit_re_approves() -> None:
    """The way back: applying ``todo`` again approves the text as it now stands."""
    result = verdict(
        labelled(TODO, "maintainer", 0),
        labelled(TODO, "maintainer", 10),
        edits=(edited("body", "reporter", 5),),
    )
    assert result == Approved(approver="maintainer", at=at(10))


def test_a_human_rework_does_not_re_approve_an_edit_made_during_review() -> None:
    """``rework`` asks for changes to the pull request; it is not a reading of the issue's text,
    so the edit is still checked against the ``todo`` before it."""
    todo = labelled(TODO, "maintainer", 0)
    result = verdict(
        todo,
        labelled("issuebot/in-progress", "bot", 1),
        labelled("issuebot/review", "bot", 2),
        labelled(REWORK, "reviewer", 10),
        edits=(edited("body", "reporter", 5),),
    )
    assert isinstance(result, Unapproved) and result.approval == todo


def test_a_rework_with_no_todo_behind_it_is_not_approved() -> None:
    result = verdict(labelled(REWORK, "reviewer", 0))
    assert result == Unapproved(
        reason="no account has applied `issuebot/todo`", approval=None, edit=None
    )


def test_issuebots_own_todo_does_not_launder_an_edit() -> None:
    """A dedicated account's own ``todo`` after an outsider's edit is not a maintainer's."""
    todo = labelled(TODO, "maintainer", 0)
    result = verdict(todo, labelled(TODO, "BOT", 10), edits=(edited("body", "reporter", 5),))
    assert isinstance(result, Unapproved) and result.approval == todo


def test_the_operators_own_todo_approves_when_its_account_is_an_admin() -> None:
    """A deployment on the maintainer's own token: the only approver there is is issuebot's
    account, and it administers the repository, so its label is the maintainer's."""
    result = verdict(labelled(TODO, "bot", 0), admin=True)
    assert result == Approved(approver="bot", at=at(0))


def test_the_operators_own_todo_is_not_an_approval_without_admin() -> None:
    result = verdict(labelled(TODO, "Bot", 0), labelled("issuebot/in-progress", "bot", 1))
    assert result == Unapproved(
        reason="no account other than bot has applied `issuebot/todo`", approval=None, edit=None
    )


def test_a_todo_nobody_applied_is_not_approved() -> None:
    result = verdict(labelled("issuebot/in-progress", "bot", 0), admin=True)
    assert result == Unapproved(
        reason="no account has applied `issuebot/todo`", approval=None, edit=None
    )


def test_a_label_by_a_deleted_account_is_not_an_approval() -> None:
    result = verdict(labelled(TODO, None, 0))
    assert isinstance(result, Unapproved) and result.approval is None


def test_label_names_compare_case_insensitively() -> None:
    result = verdict(labelled("Issuebot/Todo", "maintainer", 0))
    assert isinstance(result, Approved)
