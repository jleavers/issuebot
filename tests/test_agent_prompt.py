"""Tests for prompt rendering."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from issuebot.agent.errors import AgentError
from issuebot.agent.prompt import PromptContext, PromptRenderer, issue_variables
from issuebot.config import GitHubLabels
from issuebot.github.models import WORKPAD_MARKER, Issue, LinkedPr, StateLabel

PR = LinkedPr(
    number=51, url="https://github.com/example/repo/pull/51", state="open", merged_at=None
)


def context(issue: Issue, **overrides: object) -> PromptContext:
    fields: dict[str, object] = {
        "issue": issue,
        "repo": "example/repo",
        "labels": GitHubLabels(),
        "attempt": 1,
        "turn_number": 1,
        "max_turns": 5,
        "rework": False,
        "self_review": True,
    }
    fields.update(overrides)
    return PromptContext(**fields)  # type: ignore[arg-type]


def test_issue_variables_are_plain_values(make_issue: Callable[..., Issue]) -> None:
    issue = make_issue(
        body="Do the thing",
        state=StateLabel.IN_PROGRESS,
        state_labels=("issuebot/in-progress",),
        labels=("issuebot/in-progress", "bug"),
        assignees=("jleavers",),
        linked_pr=PR,
        closed_at=datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
    )
    variables = issue_variables(issue)
    assert variables["number"] == 42
    assert variables["identifier"] == "repo-42"
    assert variables["body"] == "Do the thing"
    assert variables["state"] == "in_progress"
    assert variables["state_label"] == "issuebot/in-progress"
    assert variables["labels"] == ["issuebot/in-progress", "bug"]
    assert variables["assignees"] == ["jleavers"]
    assert variables["created_at"] == "2026-09-01T09:00:00+00:00"
    assert variables["closed_at"] == "2026-09-03T08:00:00+00:00"
    assert variables["pr"] == {
        "number": 51,
        "url": "https://github.com/example/repo/pull/51",
        "state": "open",
        "merged_at": None,
    }
    assert variables["dispatchable"] is True


def test_issue_variables_handle_missing_values(make_issue: Callable[..., Issue]) -> None:
    issue = make_issue(state=None, state_labels=("issuebot/todo", "issuebot/review"))
    variables = issue_variables(issue)
    assert variables["body"] is None
    assert variables["state"] is None
    assert variables["state_label"] is None
    assert variables["closed_at"] is None
    assert variables["pr"] is None


def test_every_documented_variable_is_reachable(make_issue: Callable[..., Issue]) -> None:
    template = (
        "{{ issue.identifier }}|{{ repo }}|{{ labels.todo }}|{{ labels.in_progress }}|"
        "{{ labels.review }}|{{ labels.rework }}|{{ labels.complete }}|{{ workpad_marker }}|"
        "{{ attempt }}|{{ turn_number }}|{{ max_turns }}|{{ rework }}|{{ self_review }}"
    )
    rendered = PromptRenderer(template).render(
        context(make_issue(), attempt=2, turn_number=3, max_turns=5, rework=True)
    )
    assert rendered == (
        "repo-42|example/repo|issuebot/todo|issuebot/in-progress|issuebot/review|"
        f"issuebot/rework|issuebot/complete|{WORKPAD_MARKER}|2|3|5|True|True"
    )


def test_undefined_variable_is_a_prompt_error(make_issue: Callable[..., Issue]) -> None:
    renderer = PromptRenderer("{{ issue.nope }}")
    with pytest.raises(AgentError) as exc:
        renderer.render(context(make_issue()))
    assert exc.value.category == "prompt_error"
    assert "nope" in exc.value.message


def test_unknown_filter_fails_at_construction() -> None:
    with pytest.raises(AgentError) as exc:
        PromptRenderer("{{ issue.title | shout }}")
    assert exc.value.category == "prompt_error"
    assert "shout" in exc.value.message


def test_syntax_error_fails_at_construction() -> None:
    with pytest.raises(AgentError) as exc:
        PromptRenderer("{% if issue.body %}unterminated")
    assert exc.value.category == "prompt_error"


def test_blocks_do_not_leave_blank_lines(make_issue: Callable[..., Issue]) -> None:
    template = "a\n{% if rework %}\nrework\n{% endif %}\nb\n"
    assert PromptRenderer(template).render(context(make_issue())) == "a\nb\n"
    rendered = PromptRenderer(template).render(context(make_issue(), rework=True))
    assert rendered == "a\nrework\nb\n"


def test_none_body_renders_through_a_guard(make_issue: Callable[..., Issue]) -> None:
    template = "{% if issue.body %}{{ issue.body }}{% else %}No description provided.{% endif %}"
    assert PromptRenderer(template).render(context(make_issue())) == "No description provided."


def test_continuation_prompt_names_turn_and_label(make_issue: Callable[..., Issue]) -> None:
    rendered = PromptRenderer("unused").render_continuation(
        context(make_issue(), attempt=2, turn_number=3, max_turns=5)
    )
    assert rendered.startswith("Continuation guidance:")
    assert "continuation turn 3 of 5" in rendered
    assert "(attempt 2)" in rendered
    assert "`issuebot/in-progress`" in rendered
    assert "repo-42" in rendered
