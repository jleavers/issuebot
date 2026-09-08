"""Prompt rendering: Jinja2 with strict undefined variables, plus the continuation prompt."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jinja2 import Environment, StrictUndefined, Template, TemplateError

from issuebot.agent.errors import AgentError
from issuebot.config import GitHubLabels
from issuebot.github.models import WORKPAD_MARKER, Issue, LinkedPr, StateLabel

CONTINUATION_TEMPLATE = """\
Continuation guidance:

- The previous turn ended normally, but issue {{ issue.identifier }} is still labelled \
`{{ labels.in_progress }}`.
- This is continuation turn {{ turn_number }} of {{ max_turns }} for the current agent run \
(attempt {{ attempt }}).
- Resume from the current workspace and workpad state instead of restarting from scratch.
- The original task instructions and prior turn context are already present in this session, \
so do not restate them before acting.
- If a pull request exists, check it for new review comments and failed checks and address \
them before anything else.
- Focus on the remaining work and do not end the turn while the issue stays \
`{{ labels.in_progress }}` unless you are truly blocked.
"""


@dataclass(frozen=True, kw_only=True, slots=True)
class PromptContext:
    """Everything a template can see for one turn."""

    issue: Issue
    repo: str
    labels: GitHubLabels
    attempt: int
    turn_number: int
    max_turns: int
    rework: bool
    self_review: bool

    def to_variables(self) -> dict[str, Any]:
        return {
            "issue": issue_variables(self.issue),
            "repo": self.repo,
            "labels": {
                **{role.value: getattr(self.labels, role.value) for role in StateLabel},
                "no_fault": self.labels.no_fault,
            },
            "workpad_marker": WORKPAD_MARKER,
            "attempt": self.attempt,
            "turn_number": self.turn_number,
            "max_turns": self.max_turns,
            "rework": self.rework,
            "self_review": self.self_review,
        }


def issue_variables(issue: Issue) -> dict[str, Any]:
    """The issue as plain values: roles and datetimes as strings, the linked PR as ``pr``."""
    return {
        "id": issue.id,
        "identifier": issue.identifier,
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "github_state": issue.github_state,
        "state": issue.state.value if issue.state is not None else None,
        "state_label": issue.state_labels[0] if len(issue.state_labels) == 1 else None,
        "labels": list(issue.labels),
        "url": issue.url,
        "assignees": list(issue.assignees),
        "created_at": _iso(issue.created_at),
        "updated_at": _iso(issue.updated_at),
        "closed_at": _iso(issue.closed_at),
        "dispatchable": issue.dispatchable,
        "pr": _pr_variables(issue.linked_pr),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _pr_variables(pr: LinkedPr | None) -> dict[str, Any] | None:
    if pr is None:
        return None
    return {"number": pr.number, "url": pr.url, "state": pr.state, "merged_at": _iso(pr.merged_at)}


class PromptRenderer:
    """Compiles the workflow body once and renders it, and the continuation prompt, strictly."""

    def __init__(self, template: str) -> None:
        self._env = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self._template = self._compile(template)
        self._continuation = self._compile(CONTINUATION_TEMPLATE)

    def render(self, context: PromptContext) -> str:
        return self._render(self._template, context)

    def render_continuation(self, context: PromptContext) -> str:
        return self._render(self._continuation, context)

    def _compile(self, source: str) -> Template:
        try:
            return self._env.from_string(source)
        except TemplateError as exc:
            raise AgentError("prompt_error", f"template does not compile: {exc}") from exc

    @staticmethod
    def _render(template: Template, context: PromptContext) -> str:
        try:
            return template.render(context.to_variables())
        except TemplateError as exc:
            raise AgentError("prompt_error", f"template does not render: {exc}") from exc
