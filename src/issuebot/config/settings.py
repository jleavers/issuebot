"""Typed runtime settings parsed from WORKFLOW.md front matter (after resolution)."""

import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from issuebot.events.types import EVENT_KINDS


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


RepoName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
NonEmptyStr = Annotated[str, Field(min_length=1)]
PermissionMode = Literal["auto", "acceptEdits", "dontAsk", "bypassPermissions"]
SettingSource = Literal["user", "project", "local"]


class GitHubLabels(_Model):
    """The five state label names, plus the markers that qualify them but are not states."""

    todo: NonEmptyStr = "issuebot/todo"
    in_progress: NonEmptyStr = "issuebot/in-progress"
    review: NonEmptyStr = "issuebot/review"
    rework: NonEmptyStr = "issuebot/rework"
    complete: NonEmptyStr = "issuebot/complete"
    no_fault: NonEmptyStr = "issuebot/no-fault"

    def as_tuple(self) -> tuple[str, ...]:
        """The five state labels, and only those: what ``clear_state`` strips."""
        return (self.todo, self.in_progress, self.review, self.rework, self.complete)

    def markers(self) -> tuple[str, ...]:
        """Labels issuebot owns that are not states, so a state change must not remove them."""
        return (self.no_fault,)

    @field_validator("todo", "in_progress", "review", "rework", "complete", "no_fault")
    @classmethod
    def _label_name_is_usable(cls, value: str) -> str:
        if "," in value:
            raise ValueError("state label names must not contain ','")
        if value.startswith("-"):
            raise ValueError("state label names must not start with '-'")
        return value

    @model_validator(mode="after")
    def _labels_are_distinct(self) -> Self:
        values = (*self.as_tuple(), *self.markers())
        if len({value.lower() for value in values}) != len(values):
            raise ValueError("label names must be distinct (compared case-insensitively)")
        return self


class GitHubSettings(_Model):
    repo: RepoName
    token: SecretStr | None = None
    labels: GitHubLabels = Field(default_factory=GitHubLabels)
    request_timeout_ms: int = Field(default=30_000, ge=1000)


class PollingSettings(_Model):
    interval_ms: int = Field(default=30_000, ge=1000)


# POSIX portable user names, plus the trailing ``$`` Samba accounts carry; never a `-u` option.
_ACCOUNT_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\$?")


class WorkspaceSettings(_Model):
    root: Path = Path("/workspaces")

    @field_validator("root")
    @classmethod
    def _root_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace.root must be absolute after resolution")
        return value


class HooksSettings(_Model):
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = Field(default=60_000, ge=1)


class AgentSettings(_Model):
    max_concurrent_agents: int = Field(default=3, ge=1)
    max_turns: int = Field(default=5, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    max_retry_backoff_ms: int = Field(default=300_000, ge=1000)
    # How many times the worker may move one issue from review to rework because its pull
    # request conflicts with the default branch; 0 turns the automatic bounce off.
    max_conflict_reworks: int = Field(default=3, ge=0)
    self_review: bool = True
    # The account the session runs as (#75): ``claude -p``, every hook, the clone and the
    # post-clone setup, through ``issuebot.agent.runas``. A different uid from the worker's
    # is what puts the worker's code, environment and state out of the session's reach; unset
    # (the host route, the tests) runs everything as the worker, which is the shared privilege
    # domain the image no longer has. Falls back to ``ISSUEBOT_AGENT_USER`` (``resolve.py``),
    # which the image sets to ``agent``.
    run_as: str | None = None

    @field_validator("run_as")
    @classmethod
    def _run_as_is_an_account_name(cls, value: str | None) -> str | None:
        if value is not None and not _ACCOUNT_NAME.fullmatch(value):
            raise ValueError("agent.run_as must be an account name")
        return value


class ClaudeSettings(_Model):
    command: NonEmptyStr = "claude"
    model: str | None = None
    permission_mode: PermissionMode = "auto"
    max_budget_usd: float = Field(default=5.0, gt=0)
    turn_timeout_ms: int = Field(default=3_600_000, ge=1)
    stall_timeout_ms: int = 300_000
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    append_system_prompt: str | None = None
    # Which of Claude Code's settings sources the session loads (#107). Always passed, never
    # claude's own default: with ``project`` or ``local`` in the list the clone's ``CLAUDE.md``,
    # ``.claude/`` (settings, hooks, skills, commands) and ``.mcp.json`` are configuration in
    # force for every session, and anyone who can merge to the watched repository can change
    # them. ``user`` alone is the deployment's own home and nothing from the clone; the clone's
    # ``CLAUDE.md`` and ``AGENTS.md`` reach the prompt as enveloped data instead.
    setting_sources: list[SettingSource] = Field(default_factory=lambda: ["user"])
    model_labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("model_labels")
    @classmethod
    def _model_labels_are_usable(cls, value: dict[str, str]) -> dict[str, str]:
        for name, model in value.items():
            if not name.strip():
                raise ValueError("each key must be a non-empty label name")
            if not model.strip():
                raise ValueError(f"label {name!r} must map to a non-empty model name")
        if len({name.strip().lower() for name in value}) != len(value):
            raise ValueError("label names must be distinct (compared case-insensitively)")
        return {name.strip(): model.strip() for name, model in value.items()}

    @field_validator("setting_sources")
    @classmethod
    def _setting_sources_are_usable(cls, value: list[SettingSource]) -> list[SettingSource]:
        if not value:
            raise ValueError("claude.setting_sources must name at least one source")
        if len(set(value)) != len(value):
            raise ValueError("claude.setting_sources must not repeat a source")
        return value

    @property
    def loads_clone_settings(self) -> bool:
        """True when a source names the clone: its files are then claude's configuration."""
        return any(source != "user" for source in self.setting_sources)


class DatabaseSettings(_Model):
    url: SecretStr | None = None


class SlackSettings(_Model):
    webhook_url: SecretStr | None = None
    events: list[str] = Field(default_factory=lambda: ["state_changed", "blocked"])

    @field_validator("events")
    @classmethod
    def _events_are_known(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - EVENT_KINDS)
        if unknown:
            known = ", ".join(sorted(EVENT_KINDS))
            raise ValueError(f"unknown event kinds: {', '.join(unknown)}; known kinds: {known}")
        return value


class NotificationsSettings(_Model):
    slack: SlackSettings = Field(default_factory=SlackSettings)


class Settings(_Model):
    github: GitHubSettings
    polling: PollingSettings = Field(default_factory=PollingSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    hooks: HooksSettings = Field(default_factory=HooksSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    notifications: NotificationsSettings = Field(default_factory=NotificationsSettings)
