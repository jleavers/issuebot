# Phase 3: Agent runner

Status: Draft for review (2026-09-03)

Parent: [Issuebot phased design](2026-09-02-issuebot-phased-design.md), Phase 3.
Builds on: [Phase 1: Foundations](2026-09-02-phase-1-foundations-design.md) and
[Phase 2: GitHub adapter](2026-09-02-phase-2-github-adapter-design.md).
This spec owns the detail of Phase 3 only; the architecture, the run lifecycle
(§2.4), workspaces and branches (§2.5), the prompt policy (§2.6), the security
posture (§2.9) and the configuration schema (§2.11) live in the parent.

## 1. Goal

Run one Claude session for one issue in an isolated workspace, in the
foreground, and get a typed result back: which turns ran, what they cost, why
the session stopped, and where the issue's label ended up. This is the phase
where the prompt gets written. After it, the orchestrator (Phase 4) can be
written against `run_session`, `WorkspaceManager` and `ClaudeRunner` without
knowing anything about `claude -p` or `stream-json`.

In scope: the four modules of `issuebot.agent` (workspace, prompt, runner,
session) plus a small errors module; the default `WORKFLOW.md` for this
repository including the in-run self-review step; two settings
(`agent.self_review`, `claude.setting_sources`) and tighter label validation;
a fake `claude` executable and recorded `stream-json` fixtures; a paginating
`find_workpad_comment`; `issuebot run-once <number>`; a stronger `prompt`
check in `validate`.

Out of scope: polling, claims, retries, backoff, concurrency, the blocked
escape, stall detection (`claude.stall_timeout_ms` is orchestrator-side), the
Claude Code GitHub Action, PostgreSQL. All Phase 4 or later.

## 2. Layout after this phase

```
pyproject.toml                  + jinja2>=3.1 (uv.lock changes for this and nothing else)
Dockerfile                      CLAUDE_CODE_VERSION default 2.1.259 (--permission-prompts none needs it)
WORKFLOW.md                     the dogfood policy: real prompt body, model: opus, setting_sources: [project]
src/issuebot/
├── cli.py                      + run-once; prompt check renders the template; claude version check
├── config/settings.py          + agent.self_review, claude.setting_sources, label name rules
├── github/ghcli.py             find_workpad_comment paginates
└── agent/
    ├── __init__.py             re-exports
    ├── errors.py               AgentError, AgentErrorCategory, outcome_for
    ├── workspace.py            workspace_key, WorkspaceManager, hooks, SessionRecord
    ├── prompt.py               PromptContext, PromptRenderer, issue_variables, CONTINUATION_TEMPLATE
    ├── runner.py               agent_environment, ClaudeRunner, TurnResult, TurnEvent, TurnObserver
    └── session.py              run_session, RunResult, new_run_id
tests/
├── fakes/claude                fake `claude` executable (replays fixtures, records its invocation)
├── fakes/gh                    + `repo clone` creates a git repository at the target path
├── fixtures/claude/*.jsonl     recorded stream-json (one real capture, error variants derived from it)
├── fixtures/gh/comments_paged.json
├── test_agent_errors.py
├── test_agent_workspace.py
├── test_agent_prompt.py
├── test_agent_runner.py
├── test_agent_session.py
├── test_workflow_default.py    the committed WORKFLOW.md loads and renders
├── test_cli.py                 + run-once
├── test_settings.py            + new fields, label rules
└── test_github_ghcli.py        + paginated workpad lookup
```

`issuebot.agent` depends on `config`, `log`, `events` and `github`. Nothing in
`github` or `events` imports `agent`. Three subprocess boundaries exist after
this phase: `GhRunner` (Phase 2), `ClaudeRunner.run_turn`, and the hook runner
inside `workspace.py` (which also runs the built-in post-clone script).

## 3. Settings change

| Field | Type and constraint | Default |
|---|---|---|
| `agent.self_review` | bool | `true` |
| `claude.setting_sources` | `list[Literal["user", "project", "local"]] \| None`; non-empty and distinct when set | `None` |
| (labels) | the five names must be distinct **case-insensitively**; a name may not contain `,` and may not start with `-` | |

`self_review` is passed to the template as `self_review`. `setting_sources`,
when set, is passed to `claude` as `--setting-sources user,project` (comma
joined); `None` omits the flag and Claude Code uses its own default. The
committed `WORKFLOW.md` sets `[project]` so the agent loads the workspace
repository's `CLAUDE.md` and `.claude/settings.json` but not the operator's
personal hooks, plugins and skills (on the developer host those include a
SessionStart hook that tells the agent to wait for approval).

The label rules close the two Phase 2 gaps: matching is case-insensitive, so
distinctness must be too; `gh issue edit --remove-label a,b` splits on commas
and `gh label create -- -x` would be parsed as a flag. Both rules are enforced
in `GitHubLabels` and documented in the Phase 1 spec §4.2 table; the roadmap
§2.11 sample gains `setting_sources: null`.

## 4. Errors (`errors.py`)

```python
AgentErrorCategory = Literal[
    "claude_not_found",
    "invalid_workspace_cwd",
    "turn_timeout",
    "process_exit",
    "turn_failed",
    "budget_exceeded",
    "prompt_error",
    "workspace_error",
    "hook_error",
    "github_error",
    "cancelled",
]


class AgentError(Exception):
    category: AgentErrorCategory
    message: str


def outcome_for(category: AgentErrorCategory) -> RunOutcome:
    """turn_timeout -> timed_out; cancelled -> cancelled; everything else -> failed."""
```

The first seven categories come from roadmap §2.4. `workspace_error` covers
key derivation, containment, clone, post-clone setup and `after_create`;
`hook_error` is a failed or timed-out `before_run`; `github_error` is a
`GitHubError` while re-fetching the issue between turns; `cancelled` is the
cancel event. `stalled` in `RunOutcome` is reserved for Phase 4 and never
produced here.

## 5. Workspaces (`workspace.py`)

### 5.1 Keys and containment

```python
def workspace_key(identifier: str) -> str
```

Replace every character outside `[A-Za-z0-9._-]` with `_`. If that changed the
identifier, or the result is empty, `.` or `..`, append `-` and the first 16
hexadecimal characters of `sha256(identifier)` (64 bits, Symphony §4.2). For
`issuebot-42` the key is `issuebot-42`.

`WorkspaceManager.path_for(identifier)` returns `root / key` after resolving
both to absolute paths and checking that the result is strictly inside `root`
(`Path.is_relative_to` and not equal). A violation raises
`AgentError("workspace_error")`. `is_contained(path)` exposes the same check
for the runner (Symphony §9.5, invariant 2).

### 5.2 Records

```python
HookName = Literal["after_create", "before_run", "after_run", "before_remove"]


class Workspace:  # frozen
    key: str
    path: Path
    created: bool  # True when this call cloned it


class HookResult:  # frozen
    name: str
    returncode: int | None  # None when timed out
    timed_out: bool
    duration_ms: int
    stdout_tail: str  # last 2000 characters
    stderr_tail: str
    ok: bool  # property: returncode == 0 and not timed_out
    summary: str  # property: "exit status N: <last stderr line>" or "timed out after N ms"


class SessionRecord:  # frozen
    version: int  # 1
    issue_number: int
    issue_identifier: str
    run_id: str
    session_id: str
    attempt: int
    turn_number: int  # completed claude -p turns in this run
    last_outcome: RunOutcome | None  # None while a run is in flight
    updated_at: datetime
```

`session_path(workspace) == workspace / ".issuebot" / "session.json"` and
`run_log_dir(workspace, run_id) == workspace / ".issuebot" / "runs" / run_id`.

### 5.3 `WorkspaceManager`

```python
class WorkspaceManager:
    def __init__(
        self,
        settings: Settings,
        *,
        gh: GhRunnerLike | None = None,
        environ: Mapping[str, str] | None = None,
        hook_shell: Sequence[str] = ("bash", "-lc"),
    ) -> None: ...

    root: Path
    hook_shell: tuple[str, ...]

    def path_for(self, identifier: str) -> Path: ...
    def is_contained(self, path: Path) -> bool: ...
    async def create_or_reuse(self, issue: Issue) -> Workspace: ...
    async def run_hook(self, name: HookName, workspace: Path) -> HookResult | None: ...
    async def remove(self, identifier: str) -> bool: ...
    def read_session(self, workspace: Path) -> SessionRecord | None: ...
    def write_session(self, workspace: Path, record: SessionRecord) -> None: ...
```

**`create_or_reuse`.** `path_for(issue.identifier)`; if `path / ".git"` exists
the workspace is reused (`created=False`, no hooks). If `path` exists without
`.git` it is a remnant of a failed creation: it is removed with a warning and
recreated. Creation:

1. `mkdir -p root`; clone with the injected `gh` runner:
   `gh repo clone <owner/name> <path> -- --depth 1`. The runner is a
   `GhRunner(token=settings.github.token, timeout_ms=settings.hooks.timeout_ms)`
   by default: the built-in clone is bounded like the hook Symphony would use
   for it. A `GitHubError` or non-zero exit is a `workspace_error`. A
   workflow that wants full history runs `git fetch --unshallow` in
   `after_create`.
2. Run the built-in post-clone script through the hook runner (same shell,
   timeout and environment as a hook):

   ```sh
   git config --local --add credential.https://github.com.helper '' &&
   git config --local --add credential.https://github.com.helper '!gh auth git-credential' &&
   mkdir -p .git/info && printf '.issuebot/\n' >> .git/info/exclude
   ```

   This is what `gh auth setup-git` writes, scoped to the workspace, so the
   agent's `git push` authenticates with `GH_TOKEN` through `gh`; the exclude
   entry keeps `.issuebot/` out of `git status` and the agent's commits.
3. Create `path / ".issuebot"`.
4. Run `after_create` if configured. Failure or timeout is fatal (Symphony
   §9.4).

Any failure in steps 1 to 4 removes `path` and raises
`AgentError("workspace_error", ...)`.

*Amended by Phase 4 (spec §10):* reuse requires both `path/.git` and `path/.issuebot`;
`.issuebot` is created as the last creation step, after `after_create`, so a hook that
writes under it must `mkdir -p .issuebot` first; the root `mkdir`, the marker `mkdir`
and `remove()` raise `AgentError("workspace_error")` instead of a raw `OSError`.

**Hooks.** `run_hook` returns `None` when the hook is not configured.
Otherwise it spawns `[*hook_shell, script]` with `cwd=workspace`,
`start_new_session=True`, stdin closed, the environment from
`agent_environment()` (§7.2), and `settings.hooks.timeout_ms`. On timeout the
whole process group gets `SIGKILL` and `timed_out=True`. It logs
`hook_started` at DEBUG and `hook_finished` at INFO (name, exit code,
duration, output tails), or `hook_failed` at WARNING. Output is truncated to
2000 characters per stream before it reaches the log (Symphony §15.4).
`run_hook` never raises; the caller decides what a failure means:
`after_create` and `before_run` are fatal, `after_run` and `before_remove` are
best effort.

**`remove`.** `path_for`; `False` when the directory does not exist. Otherwise
run `before_remove` best effort, then `shutil.rmtree(path)`, `True`. The
containment check in `path_for` is the guard; the manager never removes
anything outside `root`.

**`session.json`.** `write_session` serialises the record as JSON
(`updated_at` ISO 8601) to `session.json.tmp` and `os.replace`s it into
place. `read_session` returns `None` for a missing, unreadable, unparseable or
wrong-version file, logging a warning for the last three. Roadmap §2.3's
restart resume (Phase 4) reads it for `session_id` and `attempt`.

## 6. Prompt (`prompt.py`)

Jinja2 with `undefined=StrictUndefined`, `autoescape=False`,
`trim_blocks=True`, `lstrip_blocks=True`, `keep_trailing_newline=True`.
A template that does not compile (syntax error, unknown filter or test) and a
render that touches an undefined variable both raise
`AgentError("prompt_error", <jinja message>)`.

```python
class PromptContext:  # frozen
    issue: Issue
    repo: str
    labels: GitHubLabels
    attempt: int
    turn_number: int
    max_turns: int
    rework: bool
    self_review: bool

    def to_variables(self) -> dict[str, Any]: ...


def issue_variables(issue: Issue) -> dict[str, Any]: ...


class PromptRenderer:
    def __init__(self, template: str) -> None: ...  # compiles once
    def render(self, context: PromptContext) -> str: ...
    def render_continuation(self, context: PromptContext) -> str: ...
```

Template variables (roadmap §2.6, extended):

| Name | Value |
|---|---|
| `issue` | mapping of every `Issue` field: `id`, `identifier`, `number`, `title`, `body` (may be `None`), `github_state`, `state` (role value or `None`), `state_label` (the raw label name, `None` if not exactly one), `labels` (list), `url`, `assignees` (list), `created_at`/`updated_at`/`closed_at` (ISO 8601 or `None`), `dispatchable`, and `pr` (`{number, url, state, merged_at}` or `None`) |
| `repo` | `owner/name` |
| `labels` | the five configured names keyed by role value: `labels.todo`, `labels.in_progress`, `labels.review`, `labels.rework`, `labels.complete` |
| `workpad_marker` | `issuebot.github.models.WORKPAD_MARKER` (`## Issuebot Workpad`) |
| `attempt` | 1-based worker session count for this issue |
| `turn_number`, `max_turns` | current `claude -p` turn and `agent.max_turns` |
| `rework` | `True` when the session was dispatched from `rework` |
| `self_review` | `agent.self_review` |

Rework context is `rework: True` plus `issue.pr`. The agent gathers the PR's
review comments itself with `gh` inside the workspace, which it must do anyway
for the feedback sweep before `review`; issuebot does not summarise comments.
This departs from the roadmap's "summarised from `gh`" wording and keeps the
frozen `GitHubAdapter` protocol untouched.

The continuation prompt is a built-in template (`CONTINUATION_TEMPLATE`)
rendered with the same variables, adapted from Symphony's Elixir reference:

```text
Continuation guidance:

- The previous turn ended normally, but issue {{ issue.identifier }} is still labelled `{{ labels.in_progress }}`.
- This is continuation turn {{ turn_number }} of {{ max_turns }} for the current agent run (attempt {{ attempt }}).
- Resume from the current workspace and workpad state instead of restarting from scratch.
- The original task instructions and prior turn context are already present in this session, so do not restate them before acting.
- If a pull request exists, check it for new review comments and failed checks and address them before anything else.
- Focus on the remaining work and do not end the turn while the issue stays `{{ labels.in_progress }}` unless you are truly blocked.
```

`validate`'s existing `prompt` check gains rendering: it compiles the body and
renders it against a sample `in_progress` issue (`attempt=1`, `turn_number=1`,
`rework=False`, the configured `self_review`) and reports `[ OK ] prompt: <n> characters, renders` or
`[FAIL] prompt: <jinja message>`. The empty-body warning stays. `validate`
still has twelve checks.

## 7. Runner (`runner.py`)

### 7.1 Command line

`ClaudeRunner.build_argv(session_id, resume)` is pure and returns, in this
order:

```
<claude.command> -p --output-format stream-json --verbose
  --permission-mode <claude.permission_mode> --permission-prompts none
  --max-budget-usd <claude.max_budget_usd>
  --session-id <uuid>            # first turn of a fresh conversation
  --resume <session_id>          # continuation turns, or the first turn when resuming
  [--model <claude.model>]
  [--setting-sources <comma-joined claude.setting_sources>]
  [--append-system-prompt <claude.append_system_prompt>]
  [--allowedTools <tool> ...]    # variadic flags last
  [--disallowedTools <tool> ...]
```

The prompt is written to **stdin**, not passed as an argument: it can exceed
a single argument's length limit, and it stays out of `ps` and the argv debug
log. `--verbose` is required by `stream-json` in `-p` mode.
`--permission-prompts none` (Claude Code 2.1.259) makes the unattended
posture explicit: anything that would prompt (an `ask` rule, a hook answering
"ask", the few calls that prompt even under `bypassPermissions`) is denied at
once while the permission mode keeps deciding everything else, so a turn can
never wait for a human. `--max-turns` (Claude's internal agentic turns) is not
set; `agent.max_turns` counts `claude -p` invocations. `--bare` is
deliberately not used: it skips the repository's `CLAUDE.md` and does not
read the OAuth login.

Because of that flag the minimum Claude Code version is **2.1.259**
(`MIN_CLAUDE_VERSION` in `runner.py`); the Dockerfile's `CLAUDE_CODE_VERSION`
build arg moves to it. `validate`'s `claude.command` check now also runs
`<command> --version` (ten-second timeout, through a module-level
`_claude_version` seam) and reports `[ OK ] claude.command: <path> (2.1.259)`,
or `[FAIL] claude.command: <path> is 2.1.240; issuebot needs 2.1.259 or
newer`, or `[WARN] claude.command: <path> (version unknown: <reason>)` when
the output cannot be parsed. Presence on `PATH` is still checked first.

### 7.2 Environment

```python
def agent_environment(environ: Mapping[str, str], *, token: SecretStr | None) -> dict[str, str]
```

The child (and every hook) sees exactly:

- from the parent: `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `TZ`,
  `TMPDIR`, `TERM` when set, plus every variable whose name starts with
  `ANTHROPIC_`, `CLAUDE_`, `GIT_AUTHOR_` or `GIT_COMMITTER_`;
- fixed: `GH_PROMPT_DISABLED=1`, `GH_NO_UPDATE_NOTIFIER=1`, `NO_COLOR=1`,
  `GH_PAGER=cat` (the agent uses `gh` itself), `DISABLE_AUTOUPDATER=1`;
- `GH_TOKEN=<github.token>` when configured.

Nothing else is inherited (roadmap §2.9: the workspace is the only thing the
agent needs). Claude auth arrives either as `ANTHROPIC_API_KEY` or through the
login stored under `HOME`; the git identity for the agent's commits arrives as
the four `GIT_AUTHOR_*`/`GIT_COMMITTER_*` variables (documented in the
environment example file). The environment is never logged.

### 7.3 Records

```python
TurnEventKind = Literal[
    "session_started",
    "turn_activity",
    "turn_completed",
    "turn_failed",
    "turn_timeout",
    "process_exit",
]


class TurnEvent:  # frozen
    kind: TurnEventKind
    turn_number: int
    at: datetime
    session_id: str | None = None
    message_type: str | None = None  # turn_activity: the stream-json "type"
    tool_name: str | None = None  # turn_activity: for assistant tool_use blocks
    detail: str | None = None  # model, exit code, error message


class TurnObserver(Protocol):
    def on_turn_event(self, event: TurnEvent) -> None: ...


class TurnResult:  # frozen
    turn_number: int
    session_id: str | None  # from system/init; None if it never arrived
    model: str | None
    api_key_source: str | None
    exit_code: int | None
    subtype: str | None  # result.subtype
    is_error: bool
    num_turns: int  # Claude's internal agentic turns
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int
    permission_denials: int
    result_text: str | None
    error_category: AgentErrorCategory | None
    error: str | None
    stdout_path: Path
    stderr_path: Path
    ok: bool  # property: error_category is None
    total_input_tokens: int  # property: input + cache creation + cache read
```

```python
class TurnRunner(Protocol):  # what run_session depends on; ClaudeRunner satisfies it
    async def run_turn(
        self,
        *,
        prompt,
        workspace,
        session_id,
        resume,
        turn_number,
        log_dir,
        observer=None,
        cancel=None,
    ) -> TurnResult: ...
```

`StreamParser` (feeds one line at a time, remembers `system/init` and
`result`, reports activity), `classify_result` (the table in §7.4) and
`parse_claude_version` are public, pure and unit-tested without a subprocess.

Runtime events are **internal**: they go to the optional `TurnObserver` and to
the log, not to the event bus, and `EVENT_KINDS` is unchanged. Phase 4's stall
detection and runtime snapshot consume the observer; Phase 6 and 7 read
per-run totals from `RunEnded`.

### 7.4 `run_turn`

```python
class ClaudeRunner:
    def __init__(self, settings: Settings, *, environ: Mapping[str, str] | None = None) -> None: ...
    def build_argv(self, *, session_id: str, resume: bool) -> list[str]: ...
    def child_environment(self) -> dict[str, str]: ...
    async def run_turn(
        self,
        *,
        prompt: str,
        workspace: Path,
        session_id: str,
        resume: bool,
        turn_number: int,
        log_dir: Path,
        observer: TurnObserver | None = None,
        cancel: asyncio.Event | None = None,
    ) -> TurnResult: ...
```

`run_turn` never raises for a turn-level failure; every failure is a
`TurnResult` with `error_category` set. Only `asyncio.CancelledError`
propagates (after the child is killed).

1. Preflight: `workspace` must be a directory strictly inside
   `settings.workspace.root` (`invalid_workspace_cwd`, Symphony §9.5
   invariant 1). Create `log_dir`. Write the prompt to
   `log_dir / f"turn-{n}.prompt.md"`.
2. Spawn `build_argv(...)` with `cwd=workspace`, `env=child_environment()`,
   `start_new_session=True`, `stdin=PIPE`, `stdout=PIPE` with
   `limit=10 * 1024 * 1024` (tool results can be large), `stderr=` an open
   handle on `log_dir / f"turn-{n}.stderr.log"`. `OSError` on spawn is
   `claude_not_found`. Log `claude_turn_started` (argv, never the prompt or
   the environment).
3. Write the prompt to stdin and close it, in a task, so a large prompt cannot
   deadlock against stdout.
4. Read stdout line by line. Every line is appended verbatim to
   `log_dir / f"turn-{n}.jsonl"` and resets the silence clock: each
   `readline()` is bounded by `claude.turn_timeout_ms` (Symphony §10.6:
   maximum silence interval, not a total cap). Per line:
   - blank: ignored;
   - not a JSON object: `turn_activity` with `message_type="unparseable"`, one
     WARNING with the line length;
   - `type == "system"` and `subtype == "init"`: `session_started` with
     `session_id`, `model` and `apiKeySource` (a `session_id` that differs
     from the requested one is logged at WARNING, not fatal);
   - `type == "assistant"`: `turn_activity` per message, with `tool_name` for
     each `tool_use` content block;
   - `type == "result"`: recorded; fields read defensively with zero/`None`
     fallbacks: `subtype`, `is_error`, `num_turns`, `duration_ms`,
     `total_cost_usd`, `usage.{input_tokens, cache_creation_input_tokens,
     cache_read_input_tokens, output_tokens}`, `len(permission_denials)`,
     `result`;
   - anything else (`user`, `rate_limit_event`, `system` with another
     subtype, future types): `turn_activity` with that `message_type`.
5. On silence timeout or when `cancel` is set: terminate (§7.5) and return
   `turn_timeout` or `cancelled`.
6. At EOF, `await process.wait()`, emit `process_exit` with the exit code,
   then classify:

   | Condition | Category |
   |---|---|
   | result present, `subtype == "success"`, `is_error` false, exit code 0 | none (ok) |
   | result present, `subtype == "error_max_budget_usd"` | `budget_exceeded` |
   | result present, any other `subtype` or `is_error` true | `turn_failed` (message: subtype and the first 500 characters of `result`) |
   | result present and successful but exit code non-zero | `process_exit` |
   | no result | `process_exit` (message: exit code and the last non-blank stderr line, 500 characters) |

   `turn_completed` or `turn_failed` is emitted accordingly, and
   `claude_turn_finished` logged at INFO with category, exit code, tokens,
   cost and duration.

### 7.5 Termination

Both timeout and cancellation terminate the same way: `SIGTERM` to the child
(Claude Code then kills the process trees of running Bash tools and runs its
`SessionEnd` hooks), wait up to 10 seconds, then `SIGKILL` the whole process
group, then reap. After reaping, `process_exit` is emitted with the exit code
(negative signal number when killed), so the observer sees that event once
per turn on every path. `CancelledError` raised into `run_turn` does the same
before propagating, so a cancelled task never leaves a `claude` behind.

*Amended by Phase 4 (spec §10):* the process group is SIGKILLed even when the leader has
already exited (a grandchild holding stdout no longer outlives the turn), and a `cancel`
event that is already set when `run_turn` starts returns `cancelled` without spawning,
creating the log directory or emitting any event.

## 8. Session (`session.py`)

```python
StopReason = Literal["issue_moved", "max_turns", "issue_missing", "failure", "cancelled"]


class RunResult:  # frozen
    run_id: str
    issue_number: int
    issue_identifier: str
    attempt: int
    session_id: str
    outcome: RunOutcome  # succeeded | failed | timed_out | cancelled
    stop_reason: StopReason
    error_category: AgentErrorCategory | None
    error: str | None
    turns: int  # completed claude -p turns, successful or not
    input_tokens: int  # sum of TurnResult.total_input_tokens
    output_tokens: int
    cost_usd: float
    duration_s: float
    final_state: StateLabel | None  # from the last refresh, else the input issue
    final_issue: Issue | None  # the last refreshed snapshot, None if never refreshed or missing
    workspace_path: Path | None
    log_dir: Path | None


def new_run_id(now: datetime | None = None) -> str: ...  # "20260903T081200Z-a1b2c3"


async def run_session(
    issue: Issue,
    workflow: Workflow,
    adapter: GitHubAdapter,
    bus: EventBus,
    *,
    workspaces: WorkspaceManager,
    runner: TurnRunner,
    attempt: int = 1,
    rework: bool = False,
    resume_session_id: str | None = None,
    cancel: asyncio.Event | None = None,
    observer: TurnObserver | None = None,
    run_id: str | None = None,
) -> RunResult: ...
```

`workflow` supplies both the settings and the prompt template; `rework` tells
the prompt that the orchestrator dispatched the issue from `rework`; `runner`
is any `TurnRunner` (§7.3), which `ClaudeRunner` satisfies and tests stub.

`run_session` is roadmap §2.4's worker session (Symphony §16.5):

1. Bind `issue_number`, `issue_identifier` and `session_id` log context;
   `run_id = run_id or new_run_id()`; `session_id = resume_session_id or
   uuid4()`.
2. Publish `RunStarted(run_id, attempt, session_id, workspace_path)` where
   `workspace_path` is `workspaces.path_for(issue.identifier)` (an empty
   string if derivation fails; the failure then ends the run at step 3).
3. `workspaces.create_or_reuse(issue)`; `AgentError` ends the run
   (`failure`).
4. `before_run`; a failed or timed-out hook ends the run with `hook_error`.
5. `write_session(turn_number=0, last_outcome=None)`.
6. Turn loop, `turn_number` from 1 to `agent.max_turns`:
   - Prompt: the full template on turn 1 of a fresh conversation
     (`PromptRenderer.render`); the continuation prompt on every other turn,
     including turn 1 when `resume_session_id` was given. `prompt_error`
     ends the run.
   - `runner.run_turn(...)` with `resume = turn_number > 1 or
     resume_session_id is not None`, `log_dir = run_log_dir(workspace,
     run_id)`.
   - `write_session(turn_number=n)`; accumulate tokens and cost; count the
     turn.
   - A failed turn ends the run with the turn's category (`failure`, or
     `cancelled` for the cancel event).
   - If `cancel` is set, stop with `cancelled`.
   - Refresh: `adapter.fetch_issues_by_ids([issue.id])`. `GitHubError` ends
     the run with `github_error` (the attempt fails so Phase 4 retries with
     backoff; the workspace keeps the work). An empty result stops with
     `issue_missing`. A snapshot whose `state != IN_PROGRESS` or whose
     `dispatchable` is false stops with `issue_moved`. Otherwise, if
     `turn_number == max_turns`, stop with `max_turns`; else continue.
7. `after_run` best effort (also on failure paths after step 3).
8. `write_session(last_outcome=outcome)`; publish `RunEnded(run_id, outcome,
   error, turns, input_tokens, output_tokens, cost_usd, duration_s)`; clear
   the log context; return.

Outcome rules: `issue_moved`, `max_turns` and `issue_missing` are
`succeeded`; `failure` maps through `outcome_for(category)`; `cancelled` is
`cancelled`. Phase 4 applies the blocked escape when it sees `stop_reason ==
"max_turns"` with `final_state == IN_PROGRESS`.

Every worker session is a fresh Claude conversation unless
`resume_session_id` is passed. Rework starts fresh (roadmap §1); the
restart-resume path (Phase 4) passes the id from `session.json`. A resume
whose transcript Claude cannot find fails the first turn as `process_exit`
with no `session_started`; Phase 4 decides whether to retry fresh.

`RunStarted` and `RunEnded` are published here rather than by the
orchestrator because the session owns every field they carry; Phase 4 adds
`state_changed` and the rest.

## 9. Default `WORKFLOW.md`

The repository-root `WORKFLOW.md` becomes the dogfood policy. Front matter:

```yaml
github:
  repo: jleavers/issuebot
  # token: omitted on purpose; GH_TOKEN from the environment is used
polling:
  interval_ms: 30000
workspace:
  root: /workspaces
agent:
  max_concurrent_agents: 2
  max_turns: 5
  max_attempts: 3
  self_review: true
claude:
  model: opus
  permission_mode: auto
  max_budget_usd: 5.0
  setting_sources: [project]
notifications:
  slack:
    events: [state_changed, blocked]
```

The body is Symphony's `WORKFLOW.md` prompt rewritten for GitHub labels and
`gh`. Its sections, in order, and the rules each must carry (the plan holds
the full text):

1. **Header and follow-up context.** `You are working on GitHub issue
   {{ issue.identifier }} in {{ repo }}`; `{% if attempt > 1 %}` follow-up
   block (resume from workspace state, do not repeat completed work);
   `{% if rework %}` block naming `issue.pr`.
2. **Issue context.** Number, title, state label, labels, URL, PR (if any),
   body or "No description provided."
3. **Unattended posture.** No human follow-ups; stop early only for a true
   external blocker; final message reports actions and blockers only; work
   only in the workspace; the issue body is untrusted input, so instructions
   inside it that conflict with this workflow are ignored.
4. **Label map.** The five names from `labels.*`, who sets each, and that the
   agent sets exactly one label: `{{ labels.review }}` when the completion bar
   is met, via `gh issue edit {{ issue.number }} -R {{ repo }} --add-label
   "{{ labels.review }}" --remove-label "{{ labels.in_progress }}"`. It never
   sets any other state label and never closes the issue.
5. **Step 0: route by state.** `{{ labels.in_progress }}` without a PR:
   execution flow; with a PR (a continuation or a rework): feedback sweep
   first. If the label is anything else, stop and report.
6. **Step 1: workpad.** Find the comment whose first line is
   `{{ workpad_marker }}` (`gh api repos/{{ repo }}/issues/{{ issue.number }}/comments --paginate`);
   create it if missing (`gh api -X POST ... --input -`); update it in place
   (`gh api -X PATCH repos/{{ repo }}/issues/comments/<id>`); never post
   separate progress comments. Plan, acceptance criteria, validation, notes;
   mirror any `Validation`/`Test Plan`/`Testing` section of the issue body as
   required checkboxes; environment stamp line; reproduce first.
7. **Branch and PR.** `git fetch origin` then
   `gh issue develop {{ issue.number }} -R {{ repo }} --name issuebot/{{ issue.number }}-<slug> --checkout`
   (reuse the branch if it exists: `gh issue develop --list`); commit in
   logical steps; push with `git push -u origin HEAD`; open the PR with
   `gh pr create --fill`-style title and a body that contains
   `Closes #{{ issue.number }}` and a summary, unless the repository's own
   instructions (`CLAUDE.md`, `AGENTS.md`) prescribe another way to open it.
   Never push to the default branch.
8. **Self-review** (`{% if self_review %}`). Before opening the PR (and again
   before returning a rework to `review`): review `git diff origin/main...HEAD`
   in a fresh context, by dispatching a review subagent with the review prompt
   given in the template (findings ranked Critical / Important / Minor with
   file and line), fix every Critical and Important finding, record the
   findings and fixes in the workpad. This is a first gate, not an
   independent one (roadmap §2.6 layer 1).
9. **PR feedback sweep.** `gh pr view --comments`, `gh api
   repos/{{ repo }}/pulls/<n>/comments`, `gh pr view --json reviews`; every
   actionable comment, bot or human, is blocking until addressed or
   explicitly rebutted on the thread; re-run validation and push after
   changes.
10. **Completion bar before `{{ labels.review }}`.** Workpad checklist
    complete; acceptance criteria met; validation green on the latest
    commit; PR checks green (`gh pr checks`); no actionable comments; branch
    pushed; PR links the issue. Only then the label change from section 4.
11. **Rework flow.** Re-read the issue and every human comment; keep the
    branch and the PR; run the feedback sweep; self-review again if enabled;
    push; back to the completion bar.
12. **Follow-up issues.** `gh issue create -R {{ repo }}` with a clear title,
    description and acceptance criteria, **no** state label, and a line
    `Related to #{{ issue.number }}` in the body.
13. **Guardrails.** Never `rm -rf`, `git reset --hard`, `git clean -fd`,
    force-push or push to the default branch; never merge or close PRs;
    temporary proof edits are reverted before commit; keep issue text concise.
14. **Workpad template** (code fence) starting with the marker line.

The rendered default prompt must contain `WORKPAD_MARKER`, `Closes #<n>`, the
configured `review` and `in_progress` names, and the self-review section only
when `self_review` is true; `test_workflow_default.py` asserts this.

## 10. Adapter change: paginated workpad lookup

`GhCliAdapter.find_workpad_comment` becomes
`gh api "repos/<repo>/issues/<n>/comments?per_page=100" --paginate --slurp`.
With `--slurp` the output is one JSON array of pages, each page an array of
comments; the adapter flattens them in order (oldest first) and returns the
first whose body satisfies `is_workpad_body`. A single page still arrives
wrapped in the outer array. The rule is now exact instead of tolerated:
Phase 4's blocked escape appends to the workpad and must find it on any
issue. `FakeGitHub` is unchanged.

## 11. CLI: `issuebot run-once <number> [--workflow PATH] [--show-prompt]`

The tool for iterating on the prompt (roadmap §3, Phase 3). It performs the
orchestrator's dispatch step by hand and then one worker session:

1. Load the workflow (exit 2 on failure). Build the adapter through
   `_adapter_factory`.
2. `fetch_issues_by_ids([number])`. Empty: `[FAIL] issue: #N not found`,
   exit 1.
3. The issue must be open, `dispatchable`, and in `todo`, `rework` or
   `in_progress`; otherwise `[FAIL] issue: #N is <state|unlabelled|closed>;
   label it <todo> or <rework> first`, exit 1.
4. `rework = state == REWORK`. `attempt` is 1, or one more than the
   `attempt` in the workspace's `session.json` when that file exists for the
   same issue (a second `run-once` on the same issue is a follow-up).
5. `--show-prompt`: render the turn-1 prompt with `turn_number=1` and print
   it to stdout, exit 0 (`[FAIL] prompt: <message>`, exit 1, on a
   `prompt_error`). No label change, no workspace, no `claude`.
6. If the state is not `in_progress`: `set_state(number, IN_PROGRESS)` and
   publish `StateChanged(from=<old name>, to=<in_progress name>,
   actor="issuebot")`; re-fetch the issue so the prompt sees the new label.
   `GitHubError` here: `[FAIL] claim: <error>`, exit 1.
7. `run_session` with `EventBus([LogSink()])`, a `WorkspaceManager` and a
   `ClaudeRunner` built from the settings, through the module-level
   `_run_session` seam.
8. Print a summary and exit 0 when `outcome == "succeeded"`, else 1:

```
run 20260903T081200Z-a1b2c3: succeeded (issue_moved) after 2 turns in 1m42s, $0.31, 45120 in / 3004 out
issue #7 is now review
logs: /workspaces/issuebot-scratch-7/.issuebot/runs/20260903T081200Z-a1b2c3
```

When `stop_reason == "max_turns"` the second line reads `turn budget
exhausted; issue #7 remains in_progress (the blocked escape is Phase 4)`. On
failure the second line is `error: <category>: <message>`. `run-once` never
sets `review`: that is the agent's transition, exactly as in production.
Logs go to stderr through the normal configuration; `--log-format console` is
the readable choice at a terminal.

## 12. Testing

All hermetic. No test contacts GitHub or Anthropic; no test runs the real
`gh`, `claude` or a login shell profile.

**Fake `claude`** (`tests/fakes/claude`, Python, POSIX shebang; the tests that
spawn it are `skipif(sys.platform == "win32")` like the `gh` runner tests):

- reads the prompt from stdin, records `{"argv", "stdin", "env": {selected
  names}, "cwd"}` as JSON to the path in `CLAUDE_FAKE_RECORD` when set (the
  knobs carry the `CLAUDE_` prefix because only that prefix passes through
  the runner's environment filter, §7.2);
- replays `tests/fixtures/claude/<scenario>.jsonl` line by line with the
  session id in the fixture replaced by the one in `--session-id`/`--resume`,
  sleeping `CLAUDE_FAKE_DELAY_MS` between lines when set;
- scenarios via `CLAUDE_FAKE_SCENARIO`: `success` (default), `error_result`
  (`error_during_execution`, `is_error` true, exit 1), `budget`
  (`error_max_budget_usd`), `crash_after_init` (init then exit 2 with a
  stderr line), `no_init` (one non-JSON line then exit 1), `silent` (init
  then sleep 30 s: turn timeout), `stubborn` (like `silent` but ignoring
  `SIGTERM`: the SIGKILL fallback), `slow` (success with a 200 ms delay per
  line: cancellation), `long_line` (success whose tool result is 200 KB);
- writes its pid to `CLAUDE_FAKE_PIDFILE` when set (kill assertions).

**Fixtures** (`tests/fixtures/claude/`): `success.jsonl` is the real capture
made during the design session (Claude Code 2.1.259, `claude-opus-5`, a
`Read` tool call, `rate_limit_event`, `result` with `usage`, `modelUsage` and
`permission_denials`), with the session id, paths and uuids normalised. The
error variants are the same file with the `result` line rewritten; the plan
provides every fixture's exact content.

**Fake `gh`** gains one behaviour: when `argv[:2] == ["repo", "clone"]` it
runs `git init -q <path>` so workspace tests get a real repository to assert
against (the credential helper and the exclude entry are checked with real
`git`).

| File | Covers |
|---|---|
| `test_settings.py` | `self_review` default and type; `setting_sources` default, values, empty list rejected, duplicates rejected; labels distinct case-insensitively; a comma or leading `-` rejected with the field named |
| `test_agent_workspace.py` | `workspace_key` for clean, dirty (hash suffix, 64 bits, stable), empty, `.` and `..` identifiers; `path_for` containment (traversal rejected, root itself rejected); `create_or_reuse` clones through the fake `gh` with the expected argv, writes the credential helper (asserted with `git config --local --get-all`) and the exclude entry, creates `.issuebot`, runs `after_create` with cwd and environment, `created` flag; reuse skips clone and hooks; remnant directory recreated; clone failure and `after_create` failure remove the directory and raise `workspace_error`; hook timeout kills the group (pidfile) and reports `timed_out`; output truncated to 2000 characters; unconfigured hook returns `None`; `remove` runs `before_remove` and deletes, returns `False` when absent; `session.json` round trip, atomic write, missing/unparseable/wrong-version → `None` |
| `test_agent_prompt.py` | `issue_variables` shape (ISO datetimes, `pr`, `state_label`, `None` body); every variable in §6 reachable; undefined variable and unknown filter → `prompt_error`; syntax error → `prompt_error` at construction; `trim_blocks` behaviour; continuation prompt content (turn, max, attempt, label name) |
| `test_agent_runner.py` | `build_argv` for every setting (fresh vs resume, model, tools, append prompt, setting sources joined, budget formatting); `agent_environment` pass-through list, prefixes, fixed values, token presence and absence, nothing else leaks; `run_turn` against the fake: prompt arrives on stdin, cwd is the workspace, recorded environment, `success` parses session id, model, api key source, tokens, cost, `num_turns`; observer sees `session_started`, `turn_activity` with `tool_name="Read"`, `turn_completed`, `process_exit` in order; stdout and stderr files written, prompt file written; `error_result` → `turn_failed`; `budget` → `budget_exceeded`; `crash_after_init` → `process_exit` with stderr tail; `no_init` → `process_exit`, `session_id None`, unparseable activity; `silent` with a 500 ms timeout → `turn_timeout` and the process is gone; `slow` cancelled via the event → `cancelled` and the process is gone; task cancellation kills and reaps; `long_line` parses; workspace outside the root → `invalid_workspace_cwd` without spawning; missing executable → `claude_not_found` |
| `test_agent_session.py` | with `FakeGitHub`, a stub runner (scripted `TurnResult`s) and a real `WorkspaceManager` on `tmp_path` with hooks disabled: one turn then the fake human moves the issue to `review` → `succeeded`/`issue_moved`, `RunStarted` and `RunEnded` published with the right fields, `session.json` final; issue stays `in_progress` for `max_turns` turns → `max_turns`, continuation prompts from turn 2, `resume=True` from turn 2; issue closed → `issue_moved`; issue deleted → `issue_missing`; `resume_session_id` → turn 1 uses `--resume` and the continuation prompt; a failed turn → `failed` with the category, `after_run` still runs, `RunEnded.outcome`; `turn_timeout` → `timed_out`; `github_error` on refresh; `prompt_error`; `before_run` failure → `hook_error`; workspace failure → `failure` before any turn; cancel event → `cancelled`; totals summed across turns; log context bound and cleared |
| `test_workflow_default.py` | `load_workflow("WORKFLOW.md")` succeeds with `GH_TOKEN` injected; renders for a `todo` issue and a `rework` issue with a PR; contains `WORKPAD_MARKER`, `Closes #`, the review and in-progress names, `gh issue develop`; self-review section present with `self_review=True` and absent with `False`; continuation renders |
| `test_cli.py` | `run-once`: not found, wrong state (`review`, `complete`, unlabelled, closed) messages and exit 1; claims `todo` and `rework` (`set_state` called, `StateChanged` logged) but not `in_progress`; `--show-prompt` prints the prompt and makes no calls beyond the fetch; summary lines for `succeeded`, `max_turns` and `failed` through a substituted `_run_session`; attempt increments from an existing `session.json`; one end-to-end run through the fake `claude` and fake `gh` on `tmp_path` (`skipif win32`) that ends with the fake human's label unchanged and exit 0; `validate` prompt check OK/FAIL |
| `test_github_ghcli.py` | `find_workpad_comment` argv includes `--paginate --slurp`; a two-page fixture where the workpad is on page two; a wrapped single page |
| `test_events.py` | `EVENT_KINDS` unchanged (the existing registry test keeps passing) |

## 13. Decisions made in this phase

1. **Runtime events are internal.** `TurnEvent`s go to a `TurnObserver` and
   the log; `EVENT_KINDS` gains nothing. `run_session` publishes `RunStarted`
   and `RunEnded`.
2. **The prompt travels on stdin**, never in argv.
3. **The agent gathers rework context itself** (`rework` flag plus
   `issue.pr`); issuebot does not fetch or summarise PR comments and the
   adapter protocol stays frozen.
4. **The agent finds or creates the workpad**; issuebot does not pre-create
   it. `find_workpad_comment` paginates so issuebot's own lookups are exact.
5. **Every worker session is a fresh conversation** unless
   `resume_session_id` is passed; continuation turns inside a session use
   `--resume`.
6. **Silence-based turn timeout**, no total cap; `stall_timeout_ms` is
   Phase 4's.
7. **SIGTERM, ten seconds, SIGKILL of the process group** for timeout,
   cancel event and task cancellation alike.
8. **Hooks run as `bash -lc`** (Symphony §9.4's conforming default) with the
   agent's minimal environment; the built-in post-clone setup uses the same
   mechanism and timeout.
9. **Shallow clone via `gh repo clone -- --depth 1`**, bounded by
   `hooks.timeout_ms`.
10. **Repo-local credential helper and `.git/info/exclude`** instead of
    global git configuration or a committed `.gitignore` change.
11. **Git identity comes from `GIT_AUTHOR_*`/`GIT_COMMITTER_*`** pass-through;
    no new settings for it.
12. **`claude.setting_sources`** exists and the dogfood workflow sets
    `[project]`; `--bare` is not used.
13. **Default workflow pins `model: opus`, `permission_mode: auto`.** In `-p`
    mode a classifier-blocked action is denied and the run continues; it
    never waits for a human.
14. **`run-once` claims `in_progress` but never sets `review`**, and reports
    an exhausted turn budget instead of applying the blocked escape.
15. **Label validation tightened now** (case-insensitive distinctness, no
    `,`, no leading `-`) rather than deferred to Phase 4.
16. **The scratch repository for live checks is `jleavers/issuebot-scratch`**,
    created by the Phase 3 execution session.
17. **The real `claude` run is paid for by the operator's subscription login**
    on the developer host (no `ANTHROPIC_API_KEY` in the environment);
    `claude.max_budget_usd` caps the estimated cost per turn either way.
18. **`--permission-prompts none` is always passed** (decided 2026-09-03 on
    the 2.1.259 changelog entry); the Docker pin moves to 2.1.259 and
    `validate` enforces the minimum version.
19. **Per-issue clones, not git worktrees.** The agent never runs in the
    operator's checkout, so the multi-session conflicts worktrees solve
    cannot arise; worktrees would share one `.git` between concurrent agents
    (ref-lock contention on fetch, a base repository to prune) for a disk and
    clone-time saving. Recorded under the roadmap's Later as an optimisation.

## 14. Done when

- `uv run pytest -q` passes (no network, no real `gh` or `claude`); ruff and
  pre-commit clean; CI green; `docker compose build` succeeds with the new
  lock file and Claude Code 2.1.259.
- `uv run issuebot validate` reports `prompt: ... renders` and
  `claude.command: ... (2.1.259)` for the committed `WORKFLOW.md`.
- Live check, from the developer host with `export GH_TOKEN=$(gh auth token)`
  and no `ANTHROPIC_API_KEY` set:
  1. `gh repo create jleavers/issuebot-scratch --private` seeded with a
     README, a tiny Python module with one function and a `pyproject.toml`;
     `uv run issuebot labels ensure --workflow <scratch WORKFLOW.md>` where
     that file is the committed one with `github.repo: jleavers/issuebot-scratch`
     and `workspace.root` pointing at a directory on the host that is not
     inside any existing checkout (for example `~/issuebot-workspaces`);
  2. one issue labelled `issuebot/todo` asking for a trivial, testable change
     (a second function plus its test);
  3. `uv run issuebot run-once <number> --workflow <scratch WORKFLOW.md>`
     exits 0 with `issue #<n> is now review`; the repository has a branch
     `issuebot/<n>-...`, an open PR whose body contains `Closes #<n>`, and
     the issue carries only `issuebot/review`; `turn-1.jsonl` under the run's
     log directory shows the review subagent's tool use before the `gh pr
     create` (or equivalent) call; the workpad comment exists and was edited
     in place.
- `CLAUDE.md` describes `issuebot.agent` and `run-once`; `README.md` lists
  `run-once`; the environment example file documents the git identity
  variables; the Phase 1 spec §4.2 table and the roadmap §2.11 sample carry
  the new settings.
