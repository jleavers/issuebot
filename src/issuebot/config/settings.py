"""Typed runtime settings parsed from WORKFLOW.md front matter (after resolution)."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from issuebot.events.types import EVENT_KINDS


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


RepoName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
NonEmptyStr = Annotated[str, Field(min_length=1)]
PermissionMode = Literal["auto", "acceptEdits", "dontAsk", "bypassPermissions"]


class GitHubLabels(_Model):
    todo: NonEmptyStr = "issuebot/todo"
    in_progress: NonEmptyStr = "issuebot/in-progress"
    review: NonEmptyStr = "issuebot/review"
    rework: NonEmptyStr = "issuebot/rework"
    complete: NonEmptyStr = "issuebot/complete"

    def as_tuple(self) -> tuple[str, ...]:
        return (self.todo, self.in_progress, self.review, self.rework, self.complete)

    @model_validator(mode="after")
    def _labels_are_distinct(self) -> Self:
        values = self.as_tuple()
        if len(set(values)) != len(values):
            raise ValueError("state labels must be distinct")
        return self


class GitHubSettings(_Model):
    repo: RepoName
    token: SecretStr | None = None
    labels: GitHubLabels = Field(default_factory=GitHubLabels)
    request_timeout_ms: int = Field(default=30_000, ge=1000)


class PollingSettings(_Model):
    interval_ms: int = Field(default=30_000, ge=1000)


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


class ServerSettings(_Model):
    port: int = Field(default=8080, ge=0, le=65535)
    bind: NonEmptyStr = "0.0.0.0"


class Settings(_Model):
    github: GitHubSettings
    polling: PollingSettings = Field(default_factory=PollingSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    hooks: HooksSettings = Field(default_factory=HooksSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    notifications: NotificationsSettings = Field(default_factory=NotificationsSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
