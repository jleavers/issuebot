from datetime import UTC, datetime

from issuebot.github import ApprovalEvidence, LabelApplied, TextEdit


def test_approval_evidence_is_frozen_and_keyword_only() -> None:
    at = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
    evidence = ApprovalEvidence(
        label_events=(LabelApplied(label="issuebot/todo", actor="maintainer", at=at),),
        edits=(TextEdit(what="body", editor=None, at=at),),
    )
    assert evidence.label_events[0].actor == "maintainer"
    assert evidence.edits[0].editor is None
