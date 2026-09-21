# Phase 2: GitHub adapter and label state machine

Status: Draft for review (2026-09-02)

Parent: [Issuebot phased design](2026-09-02-issuebot-phased-design.md), Phase 2.
Builds on: [Phase 1: Foundations](2026-09-02-phase-1-foundations-design.md).
This spec owns the detail of Phase 2 only; the architecture, the label state
machine's meaning and the configuration schema live in the parent.

## 1. Goal

Everything issuebot needs to read and write issue state through the `gh` CLI,
behind a small async adapter protocol, with an in-memory fake that later phases
test against. After this phase the orchestrator (Phase 4) and the agent runner
(Phase 3) can be written against `Issue`, `StateLabel` and `GitHubAdapter`
without knowing anything about `gh`.

In scope: the normalised issue model, the label state machine as data, the
`gh`-backed adapter (reads via GraphQL, writes via `gh issue`/`gh label`/
`gh api`), the fake, three CLI additions (`labels ensure`, `issues list`, and
network checks in `validate`), and one new setting.

Out of scope: running Claude, workspaces, scheduling, comments authored by the
agent (the agent uses `gh` directly inside its workspace in Phase 3), PostgreSQL.

## 2. Layout after this phase

```
src/issuebot/
├── cli.py                      + labels ensure, issues list, validate network checks
├── config/settings.py          + github.request_timeout_ms
└── github/
    ├── __init__.py             re-exports
    ├── models.py               StateLabel, Issue, LinkedPr, Comment, RateLimit, RepoInfo, AuthStatus, LabelEnsured
    ├── state.py                transitions, is_active, is_terminal, next_state_for, classify_closed, LABEL_STYLES
    ├── errors.py               GitHubError and categories
    ├── adapter.py              GitHubAdapter protocol
    ├── runner.py               GhRunner: the subprocess boundary around `gh`
    ├── normalise.py            issue_from_node, role_for, label_name
    ├── ghcli.py                GhCliAdapter
    └── fake.py                 FakeGitHub
tests/
├── fixtures/gh/                recorded gh output (JSON and stderr samples)
├── fakes/gh                    fake `gh` executable used by the runner tests
├── test_github_state.py
├── test_github_normalise.py
├── test_github_runner.py
├── test_github_ghcli.py
├── test_github_fake.py
└── test_cli.py                 extended
```

The `github` package depends on `config` (for `GitHubSettings`/`GitHubLabels`),
`log`, and nothing else. Nothing in it imports `events`; publishing events on
label changes is the orchestrator's job in Phase 4.

## 3. Settings change

One field is added to `GitHubSettings` (spec Phase 1 §4.2 table):

| Field | Type and constraint | Default |
|---|---|---|
| `github.request_timeout_ms` | int ≥ 1000 | 30000 |

It bounds every `gh` invocation. Nothing else in the schema changes.

## 4. Domain model (`models.py`)

All types are frozen, keyword-only dataclasses (`slots=True` is fine; no
pydantic).

### 4.1 `StateLabel`

The five *roles* of the state machine, independent of the label *names*
configured in `github.labels`:

```python
class StateLabel(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    REWORK = "rework"
    COMPLETE = "complete"
```

The enum values equal the field names of `GitHubLabels`, so
`label_name(labels, role) == getattr(labels, role.value)`.

### 4.2 `Issue`

Normalised as in Symphony §4.1.1 and §11.3.

| Field | Type | Meaning |
|---|---|---|
| `id` | `str` | opaque dispatch identity: the issue number as a string |
| `identifier` | `str` | human key and workspace key: `<repo-name>-<number>`, e.g. `issuebot-42` |
| `number` | `int` | |
| `title` | `str` | |
| `body` | `str \| None` | |
| `github_state` | `"open" \| "closed"` | |
| `state` | `StateLabel \| None` | the role of the single `issuebot/*` state label present; `None` if none or more than one |
| `state_labels` | `tuple[str, ...]` | the raw names of every state label found, in role order (length ≠ 1 means the invariant is broken) |
| `labels` | `tuple[str, ...]` | every label name, lowercased, deduplicated, in GitHub order |
| `url` | `str` | |
| `assignees` | `tuple[str, ...]` | logins |
| `created_at`, `updated_at` | `datetime` | aware UTC |
| `closed_at` | `datetime \| None` | |
| `linked_pr` | `LinkedPr \| None` | see 4.3 |
| `dispatchable` | `bool` | `github_state == "open" and state is not None` |

Required for a record to be well-formed: `number`, `title`, `state`
(GitHub's), `url`, `createdAt`, `updatedAt`. Anything else that is unusable
normalises to `None`/empty per Symphony §11.3.

### 4.3 `LinkedPr`

```python
class LinkedPr:  # frozen
    number: int
    url: str
    state: Literal["open", "closed", "merged"]
    merged_at: datetime | None
```

Chosen from the issue's `closedByPullRequestsReferences` (PRs whose body
closes this issue): a merged PR wins (latest `mergedAt`), else the open PR
with the highest number, else the closed PR with the highest number; `None`
when there are no references.

### 4.4 Small records

```python
class Comment:
    id: int
    body: str
    url: str
    author: str
    created_at: datetime
    updated_at: datetime


class RateLimit:
    limit: int
    remaining: int
    used: int
    reset_at: datetime


class RepoInfo:
    full_name: str
    default_branch: str
    private: bool


class AuthStatus:
    login: str


class LabelEnsured:
    name: str
    outcome: Literal["created", "updated", "unchanged"]
```

## 5. Label state machine (`state.py`)

Pure data and pure functions; no I/O.

```python
class Actor(StrEnum): HUMAN = "human"; ISSUEBOT = "issuebot"; AGENT = "agent"

ACTIVE_STATES   = frozenset({TODO, REWORK, IN_PROGRESS})
TERMINAL_STATES = frozenset({COMPLETE})

TRANSITIONS: frozenset[tuple[StateLabel | None, StateLabel, Actor]] = {
    (None,        TODO,        HUMAN),
    (TODO,        IN_PROGRESS, ISSUEBOT),   # dispatch
    (REWORK,      IN_PROGRESS, ISSUEBOT),   # dispatch with rework context
    (IN_PROGRESS, REVIEW,      AGENT),      # PR opened and validated
    (IN_PROGRESS, REVIEW,      ISSUEBOT),   # blocked escape
    (REVIEW,      REWORK,      HUMAN),
    (REVIEW,      TODO,        HUMAN),      # human restarts from scratch
    (IN_PROGRESS, TODO,        HUMAN),      # human pulls it back
    (REVIEW,      COMPLETE,    ISSUEBOT),   # linked PR merged
    (IN_PROGRESS, COMPLETE,    ISSUEBOT),   # merged before the agent moved it
}

def is_allowed(current, target, actor) -> bool
def is_active(state) -> bool
def is_terminal(state) -> bool
def next_state_for(issue) -> StateLabel | None
    # TODO or REWORK -> IN_PROGRESS; IN_PROGRESS -> IN_PROGRESS (resume, no label change); else None
def classify_closed(issue) -> ClosedOutcome   # Literal["complete", "cancelled"]
    # "complete" iff issue.linked_pr is not None and issue.linked_pr.state == "merged"

LABEL_STYLES: dict[StateLabel, LabelStyle]   # LabelStyle(color: str, description: str)
```

Colours and descriptions (used by `ensure_labels`):

| Role | Colour | Description |
|---|---|---|
| `todo` | `0E8A16` | Queued for issuebot; a human sets this |
| `in_progress` | `FBCA04` | An issuebot agent is working on it |
| `review` | `1D76DB` | PR opened; waiting for human review |
| `rework` | `D93F0B` | Reviewer wants changes; issuebot will pick it up |
| `complete` | `5319E7` | Closed by a merged issuebot PR |

`is_allowed` is used by Phase 4 to sanity-check its own writes and by the
fake to reject nonsensical test setups; it is not enforced against humans,
who may do anything through the GitHub UI.

## 6. Adapter protocol (`adapter.py`) and errors (`errors.py`)

### 6.1 Protocol

Async throughout: the orchestrator is asyncio and the runner is an asyncio
subprocess. Every method may raise `GitHubError`.

```python
class GitHubAdapter(Protocol):
    repo: str                       # owner/name
    labels: GitHubLabels

    async def fetch_issues_by_states(self, states: Iterable[StateLabel]) -> list[Issue]
        # open issues carrying any of the given state labels; deduplicated by number;
        # sorted by created_at then number; an empty input returns [] without a request
    async def fetch_issues_by_ids(self, ids: Iterable[str]) -> list[Issue]
        # current snapshots for the given ids; ids that no longer resolve to an issue
        # (deleted, transferred, or actually a pull request) are omitted; empty input -> []
    async def fetch_terminal_issues(self) -> list[Issue]
        # closed issues that still carry any state label (startup sweep and completion)
        # *Amended by #149:* any state label but `complete`, which is where a closed issue
        # comes to rest -- `TERMINAL_SWEEP_ROLES` in `github/state.py`.
    async def set_state(self, number: int, state: StateLabel) -> None
        # adds the target label and removes every other state label in one gh invocation
    async def clear_state(self, number: int) -> None
        # removes every state label (an issue closed without a merged PR)
    async def comment(self, number: int, body: str) -> Comment
    async def find_workpad_comment(self, number: int) -> Comment | None
        # the first comment whose first line is exactly "## Issuebot Workpad"
    async def update_comment(self, comment_id: int, body: str) -> Comment
    async def ensure_labels(self) -> list[LabelEnsured]
        # creates or updates the five labels to LABEL_STYLES; idempotent
    async def missing_labels(self) -> list[str]
        # configured state label names absent from the repository (validate uses this; no writes)
    async def rate_limit(self) -> RateLimit          # GraphQL budget
    async def auth_status(self) -> AuthStatus        # who the token is
    async def repo_info(self) -> RepoInfo
```

Malformed records follow Symphony §11.1: a state-list read omits a record it
cannot normalise and logs a warning with the issue number; an id read raises.

### 6.2 Errors

```python
ErrorCategory = Literal[
    "auth", "not_found", "rate_limited", "transport", "status", "response", "config"
]


class GitHubError(Exception):
    category: ErrorCategory
    message: str
    retryable: bool  # True for rate_limited and transport
    exit_code: int | None
    stderr: str | None  # first 500 characters, token-free
```

The orchestrator relies on success versus failure plus `retryable`; the
category and message are for logs and the CLI.

## 7. The `gh` runner (`runner.py`)

The only place that spawns a process.

```python
class GhResult:  returncode: int; stdout: str; stderr: str
class GhRunnerLike(Protocol):          # what GhCliAdapter depends on; tests pass a stub
    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult
class GhRunner:
    def __init__(self, *, command: str = "gh", token: SecretStr | None = None, timeout_ms: int = 30_000, environ: Mapping[str, str] | None = None)
    def child_environment(self) -> dict[str, str]
    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult
```

- Spawns `[command, *args]` with `asyncio.create_subprocess_exec`, no shell.
- Child environment: the parent's (or the injected `environ`) plus
  `GH_TOKEN=<token>` when a token is configured, `GH_PROMPT_DISABLED=1`,
  `GH_NO_UPDATE_NOTIFIER=1`, `NO_COLOR=1`, `GH_PAGER=cat`.
- `stdin`, when given, is written and closed; stdout and stderr are captured as
  UTF-8 with `errors="replace"`.
- On timeout the process is killed and `GitHubError("transport", retryable=True)`
  is raised. A missing executable is `GitHubError("config")`.
- Logs one DEBUG line per invocation: `argv` (never the environment), exit
  code, duration, stdout and stderr lengths. Never logs bodies or the token.

## 8. `GhCliAdapter` (`ghcli.py`)

Constructed from `GitHubSettings` (`repo`, `token`, `labels`,
`request_timeout_ms`) with an optional `GhRunner` (tests inject a stub).

### 8.1 Reads: GraphQL through `gh api graphql`

One shared fragment:

```graphql
fragment IssueFields on Issue {
  number title body state url createdAt updatedAt closedAt
  labels(first: 50) { nodes { name } }
  assignees(first: 20) { nodes { login } }
  closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
    nodes { number url state mergedAt }
  }
}
```

**By state.** One paginated query per requested role (GitHub's `labels`
filter semantics are not relied on for more than one label at a time):

```graphql
query($owner: String!, $name: String!, $label: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(labels: [$label], states: [OPEN], first: 100, after: $cursor,
           orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes { ...IssueFields }
      pageInfo { hasNextPage endCursor }
    }
  }
}
```

invoked as `gh api graphql -f query=<text> -f owner=<o> -f name=<n> -f label=<name> [-f cursor=<c>]`
(`-f`, never `-F`: every variable is a string, and `-F` would coerce a numeric-looking
repository name), following `pageInfo` until `hasNextPage` is false. Results across roles are
merged by number (first occurrence wins) and sorted. `fetch_terminal_issues`
is the same query with `states: [CLOSED]` over all five roles.
*Amended by #149:* over `TERMINAL_SWEEP_ROLES`, the four roles a closed issue has to be moved
off, and never `complete` -- which held everything issuebot had ever finished, so asking for
it made the sweep's cost grow with the deployment's own successful work. The reasoning, and
what the re-read was load-bearing for, is in `2026-09-14-resource-ceilings-design.md`,
"The sweep's repetition".

**By id.** Aliased lookups in batches of 50:

```graphql
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    i42: issue(number: 42) { ...IssueFields }
    i43: issue(number: 43) { ...IssueFields }
  }
}
```

`gh` exits 1 and still prints the JSON body when any alias fails. The adapter
parses stdout first: an `errors` entry with `type: NOT_FOUND` whose `path`
names an alias means that id is omitted; any other error is raised as
`response` (or `rate_limited` for `type: RATE_LIMITED`). Ids that are not
integers are omitted without a request.

### 8.2 Writes and other calls

| Method | Invocation |
|---|---|
| `set_state(n, role)` | `gh issue edit <n> -R <repo> --add-label <target> --remove-label <other1>,<other2>,...` (the four other names, always all four, so a broken invariant heals) |
| `clear_state(n)` | `gh issue edit <n> -R <repo> --remove-label <all five>` |
| `comment(n, body)` | `gh api -X POST repos/<repo>/issues/<n>/comments --input -` with `{"body": ...}` on stdin |
| `find_workpad_comment(n)` | `gh api "repos/<repo>/issues/<n>/comments?per_page=100"`; first comment whose first line, stripped, equals `## Issuebot Workpad` |
| `update_comment(id, body)` | `gh api -X PATCH repos/<repo>/issues/comments/<id> --input -` |
| `ensure_labels()` | `gh label list -R <repo> --json name,color,description --limit 200`, then per role: absent → `gh label create <name> -R <repo> --color <hex> --description <text>` (`created`); present with different colour/description → same command with `--force` (`updated`); else `unchanged` |
| `missing_labels()` | the same `gh label list`; returns configured names not present (case-insensitive) |
| `rate_limit()` | `gh api rate_limit --jq .resources.graphql` |
| `auth_status()` | `gh api user --jq .login` |
| `repo_info()` | `gh api repos/<repo> --jq '{full_name,default_branch,private}'` |

`gh issue edit` fails when a label does not exist in the repository; that
surfaces as `GitHubError("not_found")` whose message tells the operator to run
`issuebot labels ensure`.

### 8.3 Error mapping

Applied to every non-zero exit (after the GraphQL body rules in 8.1):

| Signal | Category | Retryable |
|---|---|---|
| exit code 4, or stderr contains `HTTP 401`, `Bad credentials`, `authentication`, `gh auth login` | `auth` | no |
| stderr contains `HTTP 404`, `Could not resolve to` or `not found` (gh's wording for an unknown label) | `not_found` | no |
| stderr contains `HTTP 429`, `rate limit`, or `secondary rate` | `rate_limited` | yes |
| stderr contains `HTTP 5`, `connection`, `could not resolve host`, `timeout`, `TLS`, or the runner timed out | `transport` | yes |
| stderr contains `HTTP 403` (not matched above) | `auth` | no |
| stdout is not the JSON the call expects | `response` | no |
| executable missing | `config` | no |
| anything else | `status` | no |

The message is the first line of stderr with the token (if it ever appeared)
replaced by `***`.

### 8.4 Logging

Every adapter method logs at DEBUG on entry with `issue_number` where
applicable; failures log at WARNING with `category`, `exit_code` and the
message; `fetch_issues_by_states` logs each omitted malformed record at
WARNING with its number and the missing field.

## 9. `FakeGitHub` (`fake.py`)

An in-memory `GitHubAdapter` used by every later phase's tests and by the CLI
tests. It models the GitHub behaviours issuebot depends on:

- Issues have a number, title, body, open/closed state, labels, assignees,
  comments and timestamps; pull requests are separate records that can close
  an issue.
- Labels must exist in the repository before they can be applied;
  `FakeGitHub(settings.github)` pre-creates the five state labels unless
  constructed with `preseed_labels=False`.
- `set_state` and `clear_state` behave like the real commands, including
  raising `not_found` for a missing label.
- Merging a PR that closes an issue closes the issue (`closed_at` set), as
  GitHub does on merge to the default branch. Closing a PR without merging does
  not.
- `fetch_issues_by_ids` omits unknown numbers and PR numbers.

Test helpers (not part of the protocol):

```python
add_issue(title, *, body=None, labels=(), number=None, assignees=()) -> Issue
human_set_state(number, role)                 # exclusive, like the UI
human_add_label(number, name) / human_remove_label(number, name)
open_pr(issue_number, *, pr_number=None) -> LinkedPr    # references "Closes #<n>"
merge_pr(pr_number) / close_pr(pr_number)
close_issue(number) / reopen_issue(number)
comments_for(number) -> list[Comment]
fail_next(category, *, times=1)               # the next adapter call(s) raise GitHubError(category)
calls: list[tuple[str, tuple[Any, ...]]]      # every protocol call, in order
issue(number) -> Issue                        # current snapshot
```

`fetch_issues_by_states` returns deep-copied snapshots so tests cannot mutate
the fake through a returned object (they are frozen anyway).

## 10. CLI additions

### 10.1 `issuebot labels ensure [--workflow PATH]`

Loads the workflow, builds the adapter, runs `ensure_labels()`, prints one
line per label: `[ OK ] issuebot/todo: created` / `updated` / `unchanged`.
Exit 0; on `GitHubError` prints `[FAIL] labels: <category>: <message>` and
exits 1; exit 2 if the workflow cannot be loaded.

### 10.2 `issuebot issues list [--workflow PATH] [--state ROLE]`

Fetches open issues across all five roles (or one), prints a table sorted by
role order then number:

```
NUMBER  STATE        PR        UPDATED               TITLE
42      in_progress  -         2026-09-02T10:11:12Z  Add retry backoff
43      review       #51 open  2026-09-02T09:00:00Z  Fix label parsing
```

`PR` shows `#<n> <state>` or `-`. Exit codes as above. Prints `no tracked
issues` when the list is empty.

### 10.3 `validate` network checks

Three checks are appended after the `gh` executable check, executed only when
`gh` was found on `PATH` (otherwise each reports `[WARN] <subject>: skipped
(gh not found)`):

| Subject | OK | FAIL / WARN |
|---|---|---|
| `gh auth` | `logged in as <login>` | FAIL `<message>; run gh auth login or set GH_TOKEN` |
| `github.repo access` | `<full_name> (default branch <name>)` | FAIL `<category>: <message>` |
| `github.labels` | `5 labels present` | WARN `missing: <names>; run issuebot labels ensure` |

`validate` therefore has twelve checks. A `GitHubError` in any of the three is
reported in that check's line; it never aborts the command.

### 10.4 Seams for tests

`issuebot.cli` gains a module-level `_adapter_factory: Callable[[GitHubSettings], GitHubAdapter]`
defaulting to `GhCliAdapter`. Tests substitute it (like `_which`) with a
factory returning a `FakeGitHub`. CLI commands call the adapter through
`asyncio.run`.

## 11. Testing

All hermetic. No test contacts GitHub.

| File | Covers |
|---|---|
| `test_github_state.py` | every transition in `TRANSITIONS` and a sample of disallowed ones; `is_active`/`is_terminal` for all five roles; `next_state_for` for each role and `None`; `classify_closed` for merged, open, closed and absent PRs; `LABEL_STYLES` covers all five roles with six-hex colours |
| `test_github_normalise.py` | `issue_from_node` on recorded nodes: full record, minimal record (nulls), two state labels (state `None`, `dispatchable` False), closed issue, PR selection (merged beats open beats closed), label case-folding and deduplication, malformed record raises `response` with the field name, `identifier`/`id` derivation |
| `test_github_runner.py` | against `tests/fakes/gh` (an executable script that records argv, echoes stdin, and behaves per an env-configured scenario): argv and stdin passthrough; `GH_TOKEN` present only when configured; `GH_PROMPT_DISABLED`/`NO_COLOR` set; timeout kills and raises `transport`; missing executable raises `config` |
| `test_github_ghcli.py` | with a `StubRunner` that maps expected argv prefixes to recorded results and records calls: by-state pagination (two pages) and merge across roles; by-id batching (>50 ids → two requests), NOT_FOUND omission, PR-number omission, other GraphQL errors raise; terminal fetch uses `states: [CLOSED]`; `set_state` argv (target added, all four others removed) and `clear_state`; comment/workpad/update argv and stdin JSON; `ensure_labels` outcomes (created/updated/unchanged); `rate_limit`, `auth_status`, `repo_info` parsing; every row of the error-mapping table; malformed list record omitted with a warning, malformed id record raises |
| `test_github_fake.py` | protocol conformance: the same scenario suite (`fetch`, `set_state`, `clear_state`, comments, labels) run against `FakeGitHub`; GitHub-like semantics: merge closes the issue, close does not, unknown label raises `not_found`, `fetch_issues_by_ids` omits PR numbers; `fail_next` and `calls` |
| `test_cli.py` | `labels ensure` lines and exit codes; `issues list` table, `--state`, empty case; `validate` twelve checks with a fake adapter: logged in, repo reachable, labels present/missing, `GitHubError` per check, and the `skipped` variants when `gh` is absent |
| `test_settings.py` | `request_timeout_ms` default and lower bound |

Recorded fixtures under `tests/fixtures/gh/` are captured once from the real
API (issue nodes, a two-page list, a by-id response with a NOT_FOUND alias,
comments, `rate_limit`, `user`, `repos`, `label list`) and committed; the plan
provides their exact content so no live capture is needed to execute it.

## 12. Decisions made in this phase

1. **Async adapter.** Every protocol method is `async`; the runner uses asyncio
   subprocesses. Sync callers (the CLI) use `asyncio.run`.
2. **GraphQL for reads, `gh` subcommands for writes.** One query shape gives
   labels, assignees and linked PRs in one round trip; `gh issue edit` and
   `gh label create` keep writes simple and idempotent.
3. **One query per role.** Never relies on the semantics of a multi-label
   `labels:` filter.
4. **State roles are an enum; names stay configurable.** `Issue.state` is a
   role; the adapter owns the name mapping.
5. **Two or more state labels means `state = None` and not dispatchable.**
   Nothing guesses which label a human meant; `set_state` heals it on the
   next transition because it always removes all four other names.
6. **Linked PR selection order**: merged, then open, then closed.
7. **`fetch_issues_by_ids` silently omits ids that resolve to a PR or nothing**
   (Symphony §11.1: omission means "no longer visible").
8. **Workpad marker is the first line `## Issuebot Workpad`.** Phase 3's prompt
   uses the same string.
9. **`validate` gains network checks without an opt-out flag**; a host without
   `gh` gets `skipped` warnings rather than failures for those three.
10. **Label colours and descriptions are fixed per role**, not configurable.

## 13. Done when

- `uv run pytest -q` passes (no network); ruff and pre-commit clean; CI green.
- With `GH_TOKEN` set for `jleavers/issuebot`: `uv run issuebot labels ensure`
  prints five `created` lines, then five `unchanged` lines on a second run;
  `uv run issuebot validate` reports twelve checks with `gh auth`,
  `github.repo access` and `github.labels` OK; after a human applies
  `issuebot/todo` to an issue, `uv run issuebot issues list` shows it under
  `todo`.
- `CLAUDE.md`'s package layout describes `issuebot.github`.
