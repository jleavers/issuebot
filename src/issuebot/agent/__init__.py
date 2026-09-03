"""Agent execution: workspaces, prompt rendering, the claude -p runner and the worker session."""

from issuebot.agent.errors import AgentError, AgentErrorCategory, outcome_for
from issuebot.agent.prompt import (
    CONTINUATION_TEMPLATE,
    PromptContext,
    PromptRenderer,
    issue_variables,
)
from issuebot.agent.runner import (
    MIN_CLAUDE_VERSION,
    ClaudeRunner,
    StreamParser,
    TurnEvent,
    TurnObserver,
    TurnResult,
    TurnRunner,
    agent_environment,
    classify_result,
    parse_claude_version,
)
from issuebot.agent.session import RunResult, StopReason, new_run_id, run_session
from issuebot.agent.workspace import (
    HookResult,
    SessionRecord,
    Workspace,
    WorkspaceManager,
    run_log_dir,
    session_path,
    workspace_key,
)

__all__ = [
    "CONTINUATION_TEMPLATE",
    "MIN_CLAUDE_VERSION",
    "AgentError",
    "AgentErrorCategory",
    "ClaudeRunner",
    "HookResult",
    "PromptContext",
    "PromptRenderer",
    "RunResult",
    "SessionRecord",
    "StopReason",
    "StreamParser",
    "TurnEvent",
    "TurnObserver",
    "TurnResult",
    "TurnRunner",
    "Workspace",
    "WorkspaceManager",
    "agent_environment",
    "classify_result",
    "issue_variables",
    "new_run_id",
    "outcome_for",
    "parse_claude_version",
    "run_log_dir",
    "run_session",
    "session_path",
    "workspace_key",
]
