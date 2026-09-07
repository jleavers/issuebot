"""The committed WORKFLOW.md loads and renders."""

from collections.abc import Callable
from pathlib import Path

from issuebot.agent.prompt import PromptContext, PromptRenderer
from issuebot.config import Workflow, load_workflow
from issuebot.github.models import WORKPAD_MARKER, Issue, LinkedPr, StateLabel

WORKFLOW = Path(__file__).parent.parent / "WORKFLOW.md"
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


def test_front_matter_pins_the_dogfood_settings() -> None:
    cfg = load().config
    assert cfg.github.repo == "jleavers/issuebot"
    assert cfg.claude.model == "opus"
    assert cfg.claude.permission_mode == "auto"
    assert cfg.claude.setting_sources == ["project"]
    # max_budget_usd is not pinned: it is an operator preference that varies by repository
    # and by plan (an API key makes it a spend guard, a subscription an effort limit), so
    # the committed number is a starting point, not a contract.
    assert cfg.agent.self_review is True
    assert cfg.agent.max_turns == 5
    assert cfg.agent.max_concurrent_agents == 2


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
    assert (
        "gh issue edit 42 -R jleavers/issuebot --add-label "
        '"issuebot/review" --remove-label "issuebot/in-progress"' in text
    )
    assert "gh issue develop 42 -R jleavers/issuebot --name issuebot/42-" in text
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
