"""The committed configs/WORKFLOW.md loads and renders."""

from collections.abc import Callable
from pathlib import Path

from issuebot.agent.prompt import PromptContext, PromptRenderer
from issuebot.config import Workflow, load_workflow
from issuebot.github.models import WORKPAD_MARKER, Issue, LinkedPr, StateLabel

WORKFLOW = Path(__file__).parent.parent / "configs" / "WORKFLOW.md"
PR = LinkedPr(
    number=51, url="https://github.com/jleavers/issuebot/pull/51", state="open", merged_at=None
)


def load() -> Workflow:
    return load_workflow(WORKFLOW, environ={"GH_TOKEN": "t"})


def context(workflow: Workflow, issue: Issue, **overrides: object) -> PromptContext:
    fields: dict[str, object] = {
        "issue": issue,
        "repo": workflow.config.github.repo,
        "labels": workflow.config.github.labels,
        "attempt": 1,
        "turn_number": 1,
        "max_turns": workflow.config.agent.max_turns,
        "rework": False,
        "self_review": workflow.config.agent.self_review,
    }
    fields.update(overrides)
    return PromptContext(**fields)  # type: ignore[arg-type]


def dispatched(make_issue: Callable[..., Issue], **overrides: object) -> Issue:
    fields: dict[str, object] = {
        "identifier": "issuebot-42",
        "state": StateLabel.IN_PROGRESS,
        "state_labels": ("issuebot/in-progress",),
        "labels": ("issuebot/in-progress", "bug"),
        "body": "Add a subtract function.",
        "url": "https://github.com/jleavers/issuebot/issues/42",
    }
    fields.update(overrides)
    return make_issue(**fields)


def test_front_matter_holds_what_the_design_needs() -> None:
    """Only settings the shipped file must have, never an operator's preferences.

    `github.repo`, `claude.model`, the budget, the turn and agent limits and `self_review`
    are all things a copy of this file is expected to change, so pinning them here would
    make following step 1 of the README a test failure.
    """
    cfg = load().config
    # Nobody can answer a permission prompt in an unattended run.
    assert cfg.claude.permission_mode == "auto"
    # The README promises the agent reads the target repository's .claude/settings.json.
    assert cfg.claude.setting_sources == ["project"]


def test_renders_for_a_fresh_issue(make_issue: Callable[..., Issue]) -> None:
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, dispatched(make_issue))
    )
    assert text.startswith("You are working on GitHub issue `issuebot-42` (#42)")
    assert WORKPAD_MARKER in text
    assert "Closes #42" in text
    assert "Add a subtract function." in text
    assert "Labels: issuebot/in-progress, bug" in text
    repo = workflow.config.github.repo
    assert (
        f"gh issue edit 42 -R {repo} --add-label "
        '"issuebot/review" --remove-label "issuebot/in-progress"' in text
    )
    assert f"gh issue develop 42 -R {repo} --name issuebot/42-" in text
    assert "## Step 4: self-review" in text
    assert "## Follow-up context" not in text
    assert "## Rework context" not in text
    assert "{{" not in text
    assert "{%" not in text


def test_self_review_can_be_switched_off(make_issue: Callable[..., Issue]) -> None:
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, dispatched(make_issue), self_review=False)
    )
    assert "## Step 4: self-review" not in text
    assert "self-review them" not in text
    assert "The self-review ran" not in text
    assert "## Step 5: pull request" in text


def test_follow_up_and_rework_context(make_issue: Callable[..., Issue]) -> None:
    workflow = load()
    issue = dispatched(make_issue, linked_pr=PR)
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, issue, attempt=2, rework=True)
    )
    assert "## Follow-up context" in text
    assert "This is attempt 2 for this issue" in text
    assert "worker session #" not in text
    assert "## Rework context" in text
    assert "The pull request is #51 (open)" in text
    assert "Linked pull request: #51 (open)" in text


def test_missing_body_and_pr_render_fallbacks(make_issue: Callable[..., Issue]) -> None:
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, dispatched(make_issue, body=None, linked_pr=None), rework=True)
    )
    assert "No description provided." in text
    assert "No linked pull request was found" in text


def test_no_fault_found_hands_over_without_a_pull_request(
    make_issue: Callable[..., Issue],
) -> None:
    """The third outcome: a defect that no longer happens is reported, not invented around.

    Without this route the only exit the prompt offers is a pull request, so an already-fixed
    issue is worked until the turn budget runs out and the blocked escape calls it a timeout.
    """
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, dispatched(make_issue))
    )
    assert "## No fault found" in text
    # It ends at `review` like the change route; it never invents a change to open a PR with.
    assert "Change no code, open no pull request" in text
    # And it costs evidence, so it cannot become the cheap way out of a hard issue.
    assert "Reproduction attempted" in text


def test_no_fault_found_hands_over_with_the_marker_label(
    make_issue: Callable[..., Issue],
) -> None:
    """The route has to leave a signal, or the close cannot be told from an abandonment."""
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, dispatched(make_issue))
    )
    assert (
        '--add-label "issuebot/review" --add-label "issuebot/no-fault" '
        '--remove-label "issuebot/in-progress"' in text
    )
    # And the marker must not read as a sixth state the session could set on its own.
    assert "`issuebot/no-fault` is not a state" in text


def test_no_fault_found_is_not_contradicted_on_a_later_attempt(
    make_issue: Callable[..., Issue],
) -> None:
    """Attempt 2's nudge to keep working must not close the route attempt 1 could take."""
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render(
        context(workflow, dispatched(make_issue), attempt=2)
    )
    assert "## Follow-up context" in text
    assert "or the issue meets the No fault found bar" in text


def test_continuation_renders(make_issue: Callable[..., Issue]) -> None:
    workflow = load()
    text = PromptRenderer(workflow.prompt_template).render_continuation(
        context(workflow, dispatched(make_issue), turn_number=2)
    )
    assert "continuation turn 2 of 5" in text


def test_after_create_unshallows_a_shallow_clone() -> None:
    hook = load().config.hooks.after_create
    assert hook is not None
    assert "git rev-parse --is-shallow-repository" in hook
    assert "git fetch --unshallow" in hook
