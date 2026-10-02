# GitHub Actions Minutes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the billing account's GitHub Actions minutes as a third window in the dashboard's limits tile, and post a Slack alert when this month's use reaches 75%, 90% and 100% of the plan's allowance.

**Architecture:** The hub's `web` process runs an hourly background poller (`issuebot.web.actions`) that reads GitHub's billing usage summary through the existing `GhRunner`, using a dedicated `user`-scope token. It upserts one row per account into a new `actions_minutes` table, which `issuebot web` migrates itself before serving, and it claims and then posts a Slack alert when a reading crosses a threshold not yet posted this month. The dashboard reads the row whose account owns the viewed repository and draws the window; `/state` carries the same figures as JSON.

**Tech Stack:** Python 3.14, FastAPI + Jinja2, psycopg 3, the `gh` CLI, PostgreSQL 18, pytest with pytest-asyncio (`asyncio_mode = "auto"`), docker compose.

**Spec:** `docs/superpowers/specs/2026-10-02-actions-minutes-design.md`

## Global Constraints

- Environment variable names, exactly: `ISSUEBOT_GITHUB_BILLING_TOKEN`, `ISSUEBOT_ACTIONS_INCLUDED_MINUTES`, `SLACK_WEBHOOK_URL`.
- Endpoints, exactly: `gh api /user`, then `gh api /users/{account}/settings/billing/usage/summary?product=actions`.
- "Used" is the sum of `grossQuantity` over items whose `unitType` is `minutes` compared case-insensitively. No runner multipliers; storage is excluded.
- `POLL_INTERVAL_S = 3600.0` and `THRESHOLDS = (75, 90, 100)` are constants, not settings.
- GitHub is reached only through `gh` via `GhRunner`/`GhRunnerLike`. The billing token travels as `GH_TOKEN` in the child's environment, never in an argument, a log line, the database, a page or the JSON.
- The billing token reaches the `web` service only. The worker's compose `environment:` sets it to `""`.
- The window is drawn only on repositories whose owner equals the row's `account`, compared case-insensitively.
- The window's label is `Actions`. Its tooltip format is `2,308 of 3,000 min used, 692 left, resets 1 Nov, read 12 min ago`.
- `jleavers/issuebot` is public: never name a private repository anywhere in the tree. Screenshots use the placeholder account `acme`.
- Edit `.env.example` with the Edit tool, not shell commands. A Bash hook blocks commands that mention dot-env files.
- `docs/package-layout.md` has about 17 KB of headroom under its 160 KiB budget (`tests/test_instruction_bounds.py`). Keep this plan's additions there under 3 KB.
- Never push to `main`, never run `git reset --hard`, `git clean -fd` or `rm -rf`. Work on branch `dashboard/actions-minutes`.
- Every commit message ends with these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
  ```
- Run the suite with `uv run pytest`. The DB tests skip without `DATABASE_URL`; Tasks 2 and 9 run them against a throwaway `test-db`.
- `docker compose` interpolates the whole file before it filters by profile, so every compose command here needs `ISSUEBOT_DB_PASSWORD` set, though neither `test-db` nor `config --quiet` uses its value. The main checkout's env file sets it. In a worktree, which has no env file, prefix each compose command with `ISSUEBOT_DB_PASSWORD=placeholder`. Never pass `ISSUEBOT_DB_PORT` inline, and never run `docker compose down`, which would stop the live hub (CLAUDE.md).

## Review Focus

1. **GitHub answers an older `timePeriod` after a newer one** (lag across a month boundary). The stored reading and the alert memory must stay on the newer month, and no alert is posted. Pinned in Task 2 (`test_a_reading_for_an_older_month_is_ignored`) and Task 4 (`test_an_older_month_from_github_posts_nothing`).
2. **A token that belongs to a different account from the repositories' owner** (for example one minted on a bot account). No window appears anywhere, and the web's log names the account it is reading, so the operator can see why. Pinned in Task 4 (`test_the_account_read_is_logged_once`).
3. **The web restarts while above a threshold.** No alert repeats. Pinned in Task 4 (`test_a_restart_above_a_threshold_does_not_post_again`).
4. **A webhook that always fails.** The claim is released every time, the stored memory is unchanged, and the next cycle retries. Pinned in Task 4 (`test_a_failed_post_releases_the_claim_and_the_next_cycle_retries`).
5. **Token or allowance values with surrounding whitespace or a newline from `.env`.** They are stripped and accepted. Pinned in Task 4 (`test_settings_strip_what_an_env_file_leaves_around_a_value`).

---

### Task 1: Shared `gh` failure categorisation, and the billing reads

**Files:**
- Modify: `src/issuebot/github/errors.py` (add `RATE_LIMITED`, `ERROR_RULES`, `categorise`)
- Modify: `src/issuebot/github/ghcli.py:172-182` (delete the two private rule tables), `:843` (`_RATE_LIMITED` → `RATE_LIMITED`), `:854-870` (`_error_for` uses `categorise`)
- Create: `src/issuebot/github/billing.py`
- Create: `tests/fixtures/gh/billing_summary.json`
- Create: `tests/test_github_errors.py`
- Create: `tests/test_github_billing.py`

**Interfaces:**
- Consumes: `GhRunnerLike.run(args: Sequence[str], *, stdin: str | None = None) -> GhResult`, `GhResult(returncode, stdout, stderr)` (`src/issuebot/github/runner.py`); `GitHubError(category, message, *, exit_code=None, stderr=None)` with `.category`, `.message`.
- Produces:
  - `issuebot.github.errors.categorise(returncode: int, stderr: str) -> ErrorCategory`
  - `issuebot.github.errors.RATE_LIMITED: re.Pattern[str]`
  - `issuebot.github.billing.ActionsUsage(period: date, used_minutes: float)`, a frozen dataclass
  - `issuebot.github.billing.parse_summary(document: object) -> ActionsUsage`
  - `issuebot.github.billing.next_period(period: date) -> date`
  - `async issuebot.github.billing.fetch_login(runner: GhRunnerLike) -> str`
  - `async issuebot.github.billing.fetch_actions_usage(runner: GhRunnerLike, account: str) -> ActionsUsage`
  - `issuebot.github.billing.describe_failure(error: GitHubError) -> str`

- [ ] **Step 1: Confirm nothing else imports the private rule tables**

Run: `grep -rn "_ERROR_RULES\|_RATE_LIMITED" src tests`
Expected: matches only in `src/issuebot/github/ghcli.py`. If a test imports either, change that import to the public name from `issuebot.github.errors` in Step 4.

- [ ] **Step 2: Write the failing test for `categorise`**

Create `tests/test_github_errors.py`:

```python
"""The one classification of a failed gh invocation, shared by the adapter and the billing reads."""

import pytest

from issuebot.github.errors import categorise


@pytest.mark.parametrize(
    ("returncode", "stderr", "category"),
    [
        (4, "", "auth"),
        (1, "gh: Bad credentials (HTTP 401)", "auth"),
        (1, "gh: Not Found (HTTP 404)", "not_found"),
        (1, "gh: API rate limit exceeded (HTTP 403)", "rate_limited"),
        (1, "HTTP 429", "rate_limited"),
        (1, "gh: Bad Gateway (HTTP 502)", "transport"),
        (
            1,
            'Post "https://api.github.com/": dial tcp: lookup api.github.com: no such host',
            "transport",
        ),
        (1, "gh: Resource not accessible by personal access token (HTTP 403)", "auth"),
        (1, "gh: I'm a teapot (HTTP 418)", "status"),
    ],
)
def test_categorise(returncode: int, stderr: str, category: str) -> None:
    assert categorise(returncode, stderr) == category
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_github_errors.py -q`
Expected: FAIL with `ImportError: cannot import name 'categorise'`.

- [ ] **Step 4: Move the rules into `errors.py` and use them from `ghcli.py`**

In `src/issuebot/github/errors.py`, add `import re` beside the existing `from typing import Literal`, and append after `RETRYABLE_CATEGORIES`:

```python
# How a failed `gh` invocation says what went wrong: exit code 4 is gh's own "authentication
# required", and otherwise its stderr decides, first rule to match. Shared by the adapter and
# the dashboard's billing reads, so the two cannot drift on what a 404 is.
RATE_LIMITED = re.compile(r"http 429|rate limit|secondary rate")
ERROR_RULES: tuple[tuple[ErrorCategory, re.Pattern[str]], ...] = (
    ("auth", re.compile(r"http 401|bad credentials|authentication|gh auth login")),
    ("not_found", re.compile(r"http 404|could not resolve to|\bnot found\b")),
    ("rate_limited", RATE_LIMITED),
    (
        "transport",
        re.compile(r"http 5\d\d|connection|could not resolve host|timeout|\btls\b|dial tcp"),
    ),
    ("auth", re.compile(r"http 403")),
)


def categorise(returncode: int, stderr: str) -> ErrorCategory:
    """The category of a failed ``gh`` invocation; ``status`` when nothing more is known."""
    if returncode == 4:
        return "auth"
    lowered = stderr.lower()
    for candidate, pattern in ERROR_RULES:
        if pattern.search(lowered):
            return candidate
    return "status"
```

In `src/issuebot/github/ghcli.py`:
- Delete the `_RATE_LIMITED = ...` line (172) and the whole `_ERROR_RULES = (...)` tuple (173-182).
- Add `RATE_LIMITED` and `categorise` to the existing `from issuebot.github.errors import ...` line.
- At line 843, change `_RATE_LIMITED.search(messages.lower())` to `RATE_LIMITED.search(messages.lower())`.
- Replace `_error_for` (854-870) with:

```
    def _error_for(self, result: GhResult) -> GitHubError:
        stderr = self._redact(result.stderr)
        first_line = next((line for line in stderr.splitlines() if line.strip()), "").strip()
        message = first_line or f"gh exited with status {result.returncode}"
        category = categorise(result.returncode, stderr)
        self._log.warning(
            "gh_failed", category=category, exit_code=result.returncode, message=message
        )
        return GitHubError(category, message, exit_code=result.returncode, stderr=stderr)
```

Run `uv run ruff check src/issuebot/github`. If `re` or `ErrorCategory` is now unused in `ghcli.py`, ruff names it; remove only what it names.

- [ ] **Step 5: Run the categorisation tests and the adapter's own suite**

Run: `uv run pytest tests/test_github_errors.py tests/test_github_ghcli.py -q`
Expected: PASS. The adapter tests are the regression proof that the move changed nothing.

- [ ] **Step 6: Add the recorded fixture**

Create `tests/fixtures/gh/billing_summary.json` with exactly this content. It is the response from 2026-10-02's probe, and it names no repository:

```json
{
  "timePeriod": {
    "year": 2026,
    "month": 10
  },
  "user": "jleavers",
  "product": "Actions",
  "usageItems": [
    {
      "product": "Actions",
      "sku": "actions_linux",
      "grossQuantity": 2192.0,
      "discountQuantity": 2192.0,
      "netQuantity": 0.0,
      "grossAmount": 13.152,
      "discountAmount": 13.152,
      "netAmount": 0.0,
      "pricePerUnit": 0.006,
      "unitType": "minutes"
    },
    {
      "product": "Actions",
      "sku": "actions_storage",
      "grossQuantity": 0.815204913,
      "discountQuantity": 0.815204913,
      "netQuantity": 0.0,
      "grossAmount": 0.000273846,
      "discountAmount": 0.000273846,
      "netAmount": 0.0,
      "pricePerUnit": 0.00033602,
      "unitType": "gigabyte-hours"
    },
    {
      "product": "Actions",
      "sku": "actions_windows",
      "grossQuantity": 114.0,
      "discountQuantity": 114.0,
      "netQuantity": 0.0,
      "grossAmount": 1.14,
      "discountAmount": 1.14,
      "netAmount": 0.0,
      "pricePerUnit": 0.01,
      "unitType": "minutes"
    }
  ]
}
```

- [ ] **Step 7: Write the failing billing tests**

Create `tests/test_github_billing.py`:

```python
"""The dashboard's billing reads: the summary's arithmetic, the two fetches, a failure's words."""

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from issuebot.github.billing import (
    ActionsUsage,
    describe_failure,
    fetch_actions_usage,
    fetch_login,
    next_period,
    parse_summary,
)
from issuebot.github.errors import GitHubError
from issuebot.github.runner import GhResult

FIXTURE = Path(__file__).parent / "fixtures" / "gh" / "billing_summary.json"
SUMMARY = "/users/jleavers/settings/billing/usage/summary?product=actions"


class StubRunner:
    """Returns the given results in order and records every argument list."""

    def __init__(self, *results: GhResult) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results)

    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult:
        self.calls.append(list(args))
        return self._results.pop(0)


def ok(document: object) -> GhResult:
    return GhResult(returncode=0, stdout=json.dumps(document), stderr="")


def month(*items: object, year: object = 2026, number: object = 10) -> dict[str, object]:
    return {"timePeriod": {"year": year, "month": number}, "usageItems": list(items)}


def test_the_recorded_summary_sums_to_what_the_billing_page_showed() -> None:
    # 2,192 Linux + 114 Windows: the page read 2,308 a minute later. Storage is measured in
    # gigabyte-hours, not minutes, and is left out.
    usage = parse_summary(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert usage == ActionsUsage(period=date(2026, 10, 1), used_minutes=2306.0)


def test_the_unit_is_compared_in_any_case() -> None:
    document = month(
        {"unitType": "Minutes", "grossQuantity": 5},
        {"unitType": "GigabyteHours", "grossQuantity": 99.5},
    )
    assert parse_summary(document).used_minutes == 5.0


def test_an_empty_month_is_zero() -> None:
    assert parse_summary(month(number=11)) == ActionsUsage(
        period=date(2026, 11, 1), used_minutes=0.0
    )


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"usageItems": []},
        {"timePeriod": {"year": 2026}, "usageItems": []},
        month(number=13),
        month(number=0),
        month(year=True),
        month(year="2026"),
        {"timePeriod": {"year": 2026, "month": 10}},
        month("not an object"),
        month({"unitType": "minutes", "grossQuantity": "5"}),
        month({"unitType": "minutes", "grossQuantity": True}),
    ],
)
def test_a_summary_it_cannot_read_is_a_response_error(document: object) -> None:
    with pytest.raises(GitHubError) as caught:
        parse_summary(document)
    assert caught.value.category == "response"


def test_next_period_is_the_first_of_the_following_month() -> None:
    assert next_period(date(2026, 10, 1)) == date(2026, 11, 1)
    assert next_period(date(2026, 12, 1)) == date(2027, 1, 1)


async def test_fetch_login_reads_whose_token_it_is() -> None:
    runner = StubRunner(ok({"login": "jleavers", "plan": {"name": "pro"}}))
    assert await fetch_login(runner) == "jleavers"
    assert runner.calls == [["api", "/user"]]


@pytest.mark.parametrize("document", [{}, {"login": ""}, {"login": "a/b"}, ["jleavers"]])
async def test_a_user_without_a_usable_login_is_a_response_error(document: object) -> None:
    with pytest.raises(GitHubError) as caught:
        await fetch_login(StubRunner(ok(document)))
    assert caught.value.category == "response"


async def test_fetch_actions_usage_asks_for_the_actions_summary_alone() -> None:
    runner = StubRunner(
        GhResult(returncode=0, stdout=FIXTURE.read_text(encoding="utf-8"), stderr="")
    )
    usage = await fetch_actions_usage(runner, "jleavers")
    assert usage.used_minutes == 2306.0
    # The token is the runner's GH_TOKEN, so no argument carries it.
    assert runner.calls == [["api", SUMMARY]]


@pytest.mark.parametrize("account", ["", "-leading", "a/b", "a?b", "x" * 40])
async def test_an_account_that_is_not_a_login_is_never_sent(account: str) -> None:
    runner = StubRunner()
    with pytest.raises(GitHubError) as caught:
        await fetch_actions_usage(runner, account)
    assert caught.value.category == "config" and runner.calls == []


@pytest.mark.parametrize(
    ("stderr", "category"),
    [
        ("gh: Bad credentials (HTTP 401)", "auth"),
        ("gh: Not Found (HTTP 404)", "not_found"),
        ("gh: I'm a teapot (HTTP 418)", "status"),
    ],
)
async def test_a_failed_read_carries_the_adapter_s_category(stderr: str, category: str) -> None:
    runner = StubRunner(GhResult(returncode=1, stdout="", stderr=stderr))
    with pytest.raises(GitHubError) as caught:
        await fetch_actions_usage(runner, "jleavers")
    assert caught.value.category == category
    assert caught.value.message == stderr


async def test_a_body_that_is_not_json_is_a_response_error() -> None:
    runner = StubRunner(GhResult(returncode=0, stdout="<html>", stderr=""))
    with pytest.raises(GitHubError) as caught:
        await fetch_login(runner)
    assert caught.value.category == "response"


@pytest.mark.parametrize(
    ("category", "words"),
    [
        ("auth", "token rejected"),
        ("not_found", "needs the user scope"),
        ("rate_limited", "rate limited"),
        ("transport", "could not be reached"),
        ("response", "not understood"),
        ("config", "gh could not be run"),
        ("status", "refused"),
    ],
)
def test_a_failure_is_described_in_issuebot_s_words_not_gh_s(category: str, words: str) -> None:
    error = GitHubError(category, "gh: stderr that says ghp_abc", stderr="ghp_abc")  # type: ignore[arg-type]
    text = describe_failure(error)
    assert words in text and "ghp_abc" not in text
```

- [ ] **Step 8: Run them to verify they fail**

Run: `uv run pytest tests/test_github_billing.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.github.billing'`.

- [ ] **Step 9: Write `billing.py`**

Create `src/issuebot/github/billing.py`:

```python
"""The billing account's GitHub Actions minutes: whose token it is, and what the month has used.

Read by the dashboard's poller (``issuebot.web.actions``) with a token of its own -- a classic
token carrying the ``user`` scope, the only credential GitHub's billing usage endpoints accept
-- and never by the worker. Spec: docs/superpowers/specs/2026-10-02-actions-minutes-design.md.

"Used" is what the billing page shows, established against it on 2026-10-02: every minute of
every Actions SKU, public repositories included, Windows at 1x -- the sum of ``grossQuantity``
over the items measured in minutes, which leaves storage out. The plan's allowance is not in
the API at all; it is a setting.
"""

import json
import re
from dataclasses import dataclass
from datetime import date

from issuebot.github.errors import GitHubError, categorise
from issuebot.github.runner import GhRunnerLike

USER_PATH = "/user"
SUMMARY_PATH = "/users/{account}/settings/billing/usage/summary?product=actions"
# GitHub's login alphabet. The account is interpolated into a path, so anything else is refused
# rather than sent.
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


@dataclass(frozen=True, slots=True)
class ActionsUsage:
    period: date  # the first day of the month the figure is for (UTC)
    used_minutes: float


def next_period(period: date) -> date:
    """The first day of the month after ``period``: when its allowance resets."""
    if period.month == 12:
        return date(period.year + 1, 1, 1)
    return date(period.year, period.month + 1, 1)


def parse_summary(document: object) -> ActionsUsage:
    """``period`` from ``timePeriod``; ``used_minutes`` summed over the minute-measured items."""
    if not isinstance(document, dict):
        raise GitHubError("response", "billing summary is not a JSON object")
    period = document.get("timePeriod")
    year = period.get("year") if isinstance(period, dict) else None
    number = period.get("month") if isinstance(period, dict) else None
    if not (_whole(year) and _whole(number) and 1 <= number <= 12):
        raise GitHubError("response", "billing summary has no usable timePeriod")
    items = document.get("usageItems")
    if not isinstance(items, list):
        raise GitHubError("response", "billing summary has no usageItems")
    used = 0.0
    for item in items:
        if not isinstance(item, dict):
            raise GitHubError("response", "billing summary has a usage item that is not an object")
        unit = item.get("unitType")
        if not isinstance(unit, str) or unit.lower() != "minutes":
            continue
        quantity = item.get("grossQuantity")
        if not isinstance(quantity, int | float) or isinstance(quantity, bool):
            raise GitHubError("response", "billing summary has a non-numeric grossQuantity")
        used += float(quantity)
    return ActionsUsage(period=date(year, number, 1), used_minutes=used)


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def fetch_login(runner: GhRunnerLike) -> str:
    """The login the token belongs to, which is whose billing it can read."""
    document = await _get(runner, USER_PATH)
    login = document.get("login") if isinstance(document, dict) else None
    if not isinstance(login, str) or not _LOGIN.fullmatch(login):
        raise GitHubError("response", "GET /user returned no usable login")
    return login


async def fetch_actions_usage(runner: GhRunnerLike, account: str) -> ActionsUsage:
    """This month's Actions minutes for ``account``, which must be the token's own."""
    if not _LOGIN.fullmatch(account):
        raise GitHubError("config", "not a GitHub login")
    return parse_summary(await _get(runner, SUMMARY_PATH.format(account=account)))


async def _get(runner: GhRunnerLike, path: str) -> object:
    result = await runner.run(["api", path])
    if result.returncode != 0:
        first = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")
        raise GitHubError(
            categorise(result.returncode, result.stderr),
            first or f"gh exited with status {result.returncode}",
            exit_code=result.returncode,
            stderr=result.stderr,
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubError("response", f"{path.split('?')[0]} did not return JSON") from exc


def describe_failure(error: GitHubError) -> str:
    """issuebot's own words for a failed read: what the window's tooltip and the row carry.

    Never ``gh``'s stderr, which goes to the log; only the category reaches a page.
    """
    match error.category:
        case "auth":
            return "token rejected: it may have expired or been revoked"
        case "not_found":
            return (
                "no access to this account's billing (404): the token needs the user scope "
                "and must belong to the account"
            )
        case "rate_limited":
            return "rate limited by GitHub"
        case "transport":
            return "GitHub could not be reached"
        case "response":
            return "GitHub's answer was not understood"
        case "config":
            return "gh could not be run"
        case _:
            return "GitHub refused the request"
```

- [ ] **Step 10: Run the tests to verify they pass**

Run: `uv run pytest tests/test_github_billing.py tests/test_github_errors.py tests/test_github_ghcli.py -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: PASS, and ruff clean. If `ruff format --check` fails, run `uv run ruff format` on the named files and re-run.

- [ ] **Step 11: Commit**

```bash
git add src/issuebot/github/errors.py src/issuebot/github/ghcli.py src/issuebot/github/billing.py tests/fixtures/gh/billing_summary.json tests/test_github_errors.py tests/test_github_billing.py
git commit -F - <<'EOF'
github: read the billing account's Actions minutes, sharing the adapter's failure categories

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 2: The `actions_minutes` table, its read and its four writes

**Files:**
- Create: `src/issuebot/db/migrations/0005_actions_minutes.sql`
- Create: `src/issuebot/db/actions.py` (the write SQL)
- Modify: `src/issuebot/db/queries.py` (row type, read SQL, `RepoQueries.actions_minutes`)
- Modify: `src/issuebot/db/database.py` (four methods)
- Modify: `src/issuebot/db/__init__.py` (export `ActionsMinutesRow`)
- Modify: `tests/test_db_migrate.py:19-27, 33-45, 94-104, 203, 263`; `tests/test_db_database.py:113-125`
- Modify: `tests/fakes/database.py` (`FakeQueries.actions_rows`, `FakeRepoQueries.actions_minutes`)
- Create: `tests/test_db_actions.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `issuebot.db.ActionsMinutesRow`, a frozen keyword-only dataclass with fields `account: str`, `period: date | None`, `used_minutes: float | None`, `included_minutes: int | None`, `observed_at: datetime | None`, `alerted_percent: int`, `error: str | None`, `error_at: datetime | None`
  - `async RepoQueries.actions_minutes() -> ActionsMinutesRow | None`, the row whose `account` equals the repository's owner case-insensitively
  - `async Database.record_actions_reading(*, account: str, period: date, used_minutes: float, included_minutes: int | None, observed_at: datetime) -> int | None`. It returns `alerted_percent` after the upsert, or `None` when the reading is for an older period than the stored one and was ignored.
  - `async Database.record_actions_error(*, account: str, error: str, error_at: datetime) -> None`
  - `async Database.claim_actions_alert(*, account: str, period: date, target: int) -> bool`
  - `async Database.release_actions_alert(*, account: str, period: date, target: int, previous: int) -> None`
  - Fake: `FakeQueries.actions_rows: list[ActionsMinutesRow]`, and `FakeRepoQueries.actions_minutes()` with the same owner match.

- [ ] **Step 1: Update the migration tests to expect a fifth migration**

In `tests/test_db_migrate.py`:
- Add `"actions_minutes",` to `TABLES` (lines 19-27).
- Rename `test_the_package_ships_the_four_migrations` to `test_the_package_ships_the_five_migrations`. Append `"0005_actions_minutes",` to its label list, change the version list to `[1, 2, 3, 4, 5]`, and add `assert "CREATE TABLE actions_minutes" in migrations[4].sql` after the `repos` assertion.
- In `test_migrate_applies_every_migration_once`, change the applied tuple to `("0001_initial", "0002_run_turns", "0003_repos", "0004_run_turns_repo", "0005_actions_minutes")` and every `4` there to `5`. That is the tuple's version, `((), 5)`, and `schema_version(conn) == 5`.
- Line 203: `assert result.version == 5 and result.applied == ("0004_run_turns_repo", "0005_actions_minutes")`.
- Line 263: `assert result.version == 5 and result.applied == ("0003_repos", "0004_run_turns_repo", "0005_actions_minutes")`.

In `tests/test_db_database.py` (`test_probe_before_and_after_migrate`): `(0, 5, True)` for `before`, append `"0005_actions_minutes",` to `result.applied`, and `(5, False, False)` for `after`.

- [ ] **Step 2: Write the failing DB tests**

Create `tests/test_db_actions.py`:

```python
"""The actions_minutes table against a real PostgreSQL (skipped without DATABASE_URL)."""

from datetime import UTC, date, datetime, timedelta

from issuebot.db import ActionsMinutesRow, Database

OCT = date(2026, 10, 1)
NOV = date(2026, 11, 1)
AT = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)


async def _database(db_url: str) -> Database:
    database = Database(db_url)
    await database.migrate()
    return database


async def _row(database: Database, repo: str = "jleavers/issuebot") -> ActionsMinutesRow | None:
    async with database.queries() as queries:
        return await queries.scoped(repo).actions_minutes()


async def _read(database: Database, period: date = OCT, used: float = 2306.0) -> int | None:
    return await database.record_actions_reading(
        account="jleavers",
        period=period,
        used_minutes=used,
        included_minutes=3000,
        observed_at=AT,
    )


async def test_a_reading_is_stored_and_read_back_by_the_owner_s_repositories(db_url: str) -> None:
    database = await _database(db_url)
    assert await _read(database) == 0
    assert await _row(database) == ActionsMinutesRow(
        account="jleavers",
        period=OCT,
        used_minutes=2306.0,
        included_minutes=3000,
        observed_at=AT,
        alerted_percent=0,
        error=None,
        error_at=None,
    )


async def test_the_owner_is_matched_case_insensitively_and_nobody_else_s(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    assert await _row(database, "JLeavers/issuebot") is not None
    assert await _row(database, "acme/frontend") is None


async def test_an_error_keeps_the_reading_and_the_next_reading_clears_it(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    later = AT + timedelta(hours=1)
    await database.record_actions_error(account="jleavers", error="token rejected", error_at=later)
    row = await _row(database)
    assert row is not None
    assert (row.used_minutes, row.error, row.error_at) == (2306.0, "token rejected", later)
    await _read(database, used=2400.0)
    row = await _row(database)
    assert row is not None and (row.used_minutes, row.error, row.error_at) == (2400.0, None, None)


async def test_an_error_before_any_reading_makes_a_reading_less_row(db_url: str) -> None:
    database = await _database(db_url)
    await database.record_actions_error(account="jleavers", error="token rejected", error_at=AT)
    row = await _row(database)
    assert row is not None
    assert (row.period, row.used_minutes, row.error) == (None, None, "token rejected")


async def test_a_claim_is_taken_once_and_released_only_by_its_holder(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    assert await database.claim_actions_alert(account="jleavers", period=OCT, target=75) is True
    assert await database.claim_actions_alert(account="jleavers", period=OCT, target=75) is False
    await database.release_actions_alert(account="jleavers", period=OCT, target=90, previous=0)
    row = await _row(database)
    assert row is not None and row.alerted_percent == 75
    await database.release_actions_alert(account="jleavers", period=OCT, target=75, previous=0)
    row = await _row(database)
    assert row is not None and row.alerted_percent == 0


async def test_a_claim_for_another_period_takes_nothing(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    assert await database.claim_actions_alert(account="jleavers", period=NOV, target=75) is False


async def test_the_same_month_keeps_the_alert_memory(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    await database.claim_actions_alert(account="jleavers", period=OCT, target=90)
    assert await _read(database, used=2800.0) == 90


async def test_a_new_month_resets_the_alert_memory(db_url: str) -> None:
    database = await _database(db_url)
    await _read(database)
    await database.claim_actions_alert(account="jleavers", period=OCT, target=90)
    assert await _read(database, period=NOV, used=10.0) == 0
    row = await _row(database)
    assert row is not None and (row.period, row.alerted_percent) == (NOV, 0)


async def test_a_reading_for_an_older_month_is_ignored(db_url: str) -> None:
    """GitHub lagging across a month boundary must not undo the new month or its alerts."""
    database = await _database(db_url)
    await _read(database, period=NOV, used=10.0)
    await database.claim_actions_alert(account="jleavers", period=NOV, target=75)
    assert await _read(database, period=OCT, used=3012.0) is None
    row = await _row(database)
    assert row is not None and (row.period, row.used_minutes, row.alerted_percent) == (
        NOV,
        10.0,
        75,
    )
```

- [ ] **Step 3: Run the DB tests to verify they fail**

Run: `uv run pytest tests/test_db_actions.py tests/test_db_migrate.py -q`
Expected without `DATABASE_URL`: the collection fails with `ImportError: cannot import name 'ActionsMinutesRow'` (the DB tests themselves would skip). `test_the_package_ships_the_five_migrations` also fails, because it needs no database.

- [ ] **Step 4: Write the migration**

Create `src/issuebot/db/migrations/0005_actions_minutes.sql`:

```sql
-- The billing account's GitHub Actions minutes (spec 2026-10-02): one row per account, written
-- hourly by the hub's web service and read by the dashboard's limits tile. alerted_percent is
-- the Slack alert's memory -- the highest threshold posted for `period` -- kept here so that a
-- restart, which every upgrade is, never posts the same alert twice.

CREATE TABLE actions_minutes (
    account          text PRIMARY KEY,           -- the token's login, as GET /user returned it
    period           date,                       -- first day of the month the reading is for
    used_minutes     double precision,           -- null until the first successful read
    included_minutes integer,                    -- ISSUEBOT_ACTIONS_INCLUDED_MINUTES at that read
    observed_at      timestamptz,                -- when issuebot read it
    alerted_percent  integer NOT NULL DEFAULT 0,
    error            text,                       -- the last failure, in issuebot's own words
    error_at         timestamptz
);
```

- [ ] **Step 5: Write the SQL module for the writes**

Create `src/issuebot/db/actions.py`:

```python
"""The actions_minutes table's writes. The web's poller is their one caller (spec 2026-10-02).

A reading for an older month than the stored one is ignored rather than written: GitHub lagging
across a month boundary must neither overwrite the new month nor reset ``alerted_percent`` and
repeat last month's alerts. ``RECORD_READING`` then returns no row, which the caller reads as
"ignored".
"""

RECORD_READING = """
INSERT INTO actions_minutes AS a
    (account, period, used_minutes, included_minutes, observed_at, alerted_percent, error, error_at)
VALUES
    (%(account)s, %(period)s, %(used_minutes)s, %(included_minutes)s, %(observed_at)s, 0, NULL, NULL)
ON CONFLICT (account) DO UPDATE SET
    period = EXCLUDED.period,
    used_minutes = EXCLUDED.used_minutes,
    included_minutes = EXCLUDED.included_minutes,
    observed_at = EXCLUDED.observed_at,
    alerted_percent = CASE WHEN a.period = EXCLUDED.period THEN a.alerted_percent ELSE 0 END,
    error = NULL,
    error_at = NULL
WHERE a.period IS NULL OR EXCLUDED.period >= a.period
RETURNING alerted_percent
"""

RECORD_ERROR = """
INSERT INTO actions_minutes (account, error, error_at)
VALUES (%(account)s, %(error)s, %(error_at)s)
ON CONFLICT (account) DO UPDATE SET error = EXCLUDED.error, error_at = EXCLUDED.error_at
"""

CLAIM_ALERT = """
UPDATE actions_minutes SET alerted_percent = %(target)s
WHERE account = %(account)s AND period = %(period)s AND alerted_percent < %(target)s
"""

RELEASE_ALERT = """
UPDATE actions_minutes SET alerted_percent = %(previous)s
WHERE account = %(account)s AND period = %(period)s AND alerted_percent = %(target)s
"""
```

- [ ] **Step 6: Add the row type and the read to `queries.py`**

In `src/issuebot/db/queries.py`, add this dataclass after `RepoRow` (around line 168), matching the file's other row types:

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class ActionsMinutesRow:
    """The billing account's Actions minutes, as the web's poller last recorded them."""

    account: str
    period: date | None
    used_minutes: float | None
    included_minutes: int | None
    observed_at: datetime | None
    alerted_percent: int
    error: str | None
    error_at: datetime | None
```

Next to the other column lists (around line 171), add `ACTIONS_COLUMNS = ", ".join(f.name for f in fields(ActionsMinutesRow))`. Next to `REPO = ...` (around line 285), add:

```python
# The account that owns the repository, compared case-insensitively as GitHub compares logins.
ACTIONS_MINUTES = (
    f"SELECT {ACTIONS_COLUMNS} FROM actions_minutes WHERE lower(account) = lower(%(owner)s)"
)
```

At the end of `class RepoQueries` (after `snapshot`), add:

```
    async def actions_minutes(self) -> ActionsMinutesRow | None:
        """The Actions minutes of the account that owns this repository, if the web reads them."""
        owner = self.repo.split("/", 1)[0]
        rows = await self._rows(ACTIONS_MINUTES, {"owner": owner})
        return ActionsMinutesRow(**rows[0]) if rows else None
```

Check that `date` is imported in `queries.py`. `DailyPoint.day: date` already uses it, so it should be. Export `ActionsMinutesRow` from `src/issuebot/db/__init__.py`: add it to the `from issuebot.db.queries import (...)` list and to `__all__`, both in alphabetical position.

- [ ] **Step 7: Add the four writes to `Database`**

In `src/issuebot/db/database.py`, add `from datetime import date, datetime` and `from issuebot.db.actions import CLAIM_ALERT, RECORD_ERROR, RECORD_READING, RELEASE_ALERT`. Then, after `notify_refresh`, add:

```
    async def record_actions_reading(
        self,
        *,
        account: str,
        period: date,
        used_minutes: float,
        included_minutes: int | None,
        observed_at: datetime,
    ) -> int | None:
        """Upsert the account's reading and clear its error. Returns ``alerted_percent`` after
        it (a new month has reset it to 0), or None when the reading was for an older month
        than the stored one and was ignored."""
        params = {
            "account": account,
            "period": period,
            "used_minutes": used_minutes,
            "included_minutes": included_minutes,
            "observed_at": observed_at,
        }
        async with self._open() as conn:
            row = await (await conn.execute(RECORD_READING, params)).fetchone()
        return int(row[0]) if row is not None else None

    async def record_actions_error(self, *, account: str, error: str, error_at: datetime) -> None:
        """Record a failed read, keeping the last reading; a row without one if there is none."""
        async with self._open() as conn:
            await conn.execute(
                RECORD_ERROR, {"account": account, "error": error, "error_at": error_at}
            )

    async def claim_actions_alert(self, *, account: str, period: date, target: int) -> bool:
        """Take ``target`` as the month's alert before posting it; False if it was taken."""
        params = {"account": account, "period": period, "target": target}
        async with self._open() as conn:
            cursor = await conn.execute(CLAIM_ALERT, params)
            return cursor.rowcount == 1

    async def release_actions_alert(
        self, *, account: str, period: date, target: int, previous: int
    ) -> None:
        """Give a claim back after a failed post, so the next cycle tries again."""
        params = {"account": account, "period": period, "target": target, "previous": previous}
        async with self._open() as conn:
            await conn.execute(RELEASE_ALERT, params)
```

- [ ] **Step 8: Teach the fake queries the read**

In `tests/fakes/database.py`, import `ActionsMinutesRow` from `issuebot.db.queries` alongside the other row types. In `FakeQueries.__init__`, add `self.actions_rows: list[ActionsMinutesRow] = []`. In `FakeRepoQueries`, after `snapshot`, add:

```
    async def actions_minutes(self) -> ActionsMinutesRow | None:
        self._check("actions_minutes")
        owner = self.repo.split("/", 1)[0].lower()
        return next(
            (row for row in self._parent.actions_rows if row.account.lower() == owner), None
        )
```

- [ ] **Step 9: Run the hermetic suite, then the DB tests against a throwaway server**

Run: `uv run pytest -q`
Expected: PASS, with the DB tests skipped.

Then run:
```bash
docker compose --profile test up -d --wait test-db
DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest tests/test_db_actions.py tests/test_db_migrate.py tests/test_db_database.py -q
```
Expected: PASS. Leave `test-db` running for later tasks; Task 9 removes it. If `docker compose` refuses because `ISSUEBOT_DB_PASSWORD` is unset, that variable has to be set in this checkout's env file, with any value (see CLAUDE.md); report it rather than work around it.

- [ ] **Step 10: Commit**

```bash
git add src/issuebot/db tests/test_db_actions.py tests/test_db_migrate.py tests/test_db_database.py tests/fakes/database.py
git commit -F - <<'EOF'
db: an actions_minutes table, read by the repository's owner and written by the web

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 3: The limits tile's Actions window and the `/state` member

**Files:**
- Modify: `src/issuebot/web/views.py` (`actions_document`, `actions_window`, two new keyword parameters)
- Modify: `src/issuebot/web/templates/partials/dashboard.html:59-75` (the limits tile)
- Modify: `src/issuebot/web/app.py:414-437` (`live_context`), `:604-609` (`api_state`)
- Modify: `tests/fakes/web.py` (an `actions_row` builder)
- Modify: `tests/test_web_pages.py` (view and page tests; the `test_dashboard_context` hero dict)
- Modify: `tests/test_web_app.py` (JSON tests)

**Interfaces:**
- Consumes: `ActionsMinutesRow`, `RepoQueries.actions_minutes()` and `FakeQueries.actions_rows` (Task 2); `next_period(period: date) -> date` (Task 1).
- Produces:
  - `issuebot.web.views.actions_document(row: ActionsMinutesRow | None, now: datetime) -> dict[str, Any] | None`
  - `issuebot.web.views.actions_window(row: ActionsMinutesRow | None, now: datetime) -> dict[str, Any] | None`, returning keys `value: str`, `percent: int | None` and `title: str`
  - `dashboard_context(..., actions: ActionsMinutesRow | None = None)`, whose hero gains `"actions"`
  - `state_document(row, now, *, actions: ActionsMinutesRow | None = None)`, which gains `"actions_minutes"`
  - Test builder `fakes.web.actions_row(**overrides) -> ActionsMinutesRow`

- [ ] **Step 1: Add the test builder**

In `tests/fakes/web.py`, add `date` to the `from datetime import ...` line and `ActionsMinutesRow` to the `from issuebot.db.queries import (...)` list. Then add, after `limits`:

```python
def actions_row(**overrides: Any) -> ActionsMinutesRow:
    """A reading for ``example``, the owner of ``REPO``: 2,308 of 3,000 this month, 12 min old."""
    fields: dict[str, Any] = {
        "account": "example",
        "period": date(2026, 9, 1),
        "used_minutes": 2308.0,
        "included_minutes": 3000,
        "observed_at": NOW - timedelta(minutes=12),
        "alerted_percent": 75,
        "error": None,
        "error_at": None,
    }
    fields.update(overrides)
    return ActionsMinutesRow(**fields)
```

- [ ] **Step 2: Write the failing view tests**

In `tests/test_web_pages.py`, add `actions_row` to the `from fakes.web import (...)` list, `date` to the datetime import, and `actions_document, actions_window` to the `from issuebot.web.views import (...)` list. In `test_dashboard_context`, add `"actions": None,` as the last key of the expected `live["hero"]` dict. Then append:

```python
# --- the Actions window ------------------------------------------------------------------------


def test_a_reading_is_a_percentage_with_the_minutes_in_its_tooltip() -> None:
    assert actions_window(actions_row(), NOW) == {
        "value": "77%",
        "percent": 77,
        "title": "2,308 of 3,000 min used, 692 left, resets 1 Oct, read 12 min ago",
    }


def test_past_the_allowance_it_reads_full_and_says_by_how_much() -> None:
    window = actions_window(actions_row(used_minutes=3012.0), NOW)
    assert window is not None and (window["value"], window["percent"]) == ("100%", 100)
    assert window["title"] == (
        "3,012 of 3,000 min used, 12 min over the included 3,000, resets 1 Oct, read 12 min ago"
    )


def test_a_reading_from_last_month_reads_empty_until_this_month_s_first() -> None:
    row = actions_row(period=date(2026, 8, 1), observed_at=NOW - timedelta(days=4))
    assert actions_window(row, NOW) == {
        "value": "0%",
        "percent": 0,
        "title": "0 of 3,000 min used, 3,000 left, resets 1 Oct, read 4 d ago",
    }


def test_without_an_allowance_it_is_a_dash_that_names_the_setting() -> None:
    window = actions_window(actions_row(included_minutes=None), NOW)
    assert window is not None and (window["value"], window["percent"]) == ("—", None)
    assert window["title"].startswith("2,308 min used this month; set ")
    assert "ISSUEBOT_ACTIONS_INCLUDED_MINUTES" in window["title"]


def test_an_error_with_no_reading_is_a_dash_whose_tooltip_is_the_error() -> None:
    row = actions_row(
        period=None,
        used_minutes=None,
        observed_at=None,
        error="token rejected: it may have expired or been revoked",
        error_at=NOW - timedelta(minutes=3),
    )
    assert actions_window(row, NOW) == {
        "value": "—",
        "percent": None,
        "title": "token rejected: it may have expired or been revoked",
    }


def test_a_failed_refresh_keeps_the_figure_and_says_so() -> None:
    row = actions_row(error="GitHub could not be reached", error_at=NOW - timedelta(minutes=3))
    window = actions_window(row, NOW)
    assert window is not None and window["value"] == "77%"
    assert window["title"].endswith("; last refresh failed 3 min ago: GitHub could not be reached")


def test_no_row_draws_no_window_and_no_document() -> None:
    assert actions_window(None, NOW) is None and actions_document(None, NOW) is None


def test_the_document_carries_the_figures() -> None:
    assert actions_document(actions_row(), NOW) == {
        "account": "example",
        "period": "2026-09",
        "used_minutes": 2308,
        "included_minutes": 3000,
        "remaining_minutes": 692,
        "percent": 77,
        "resets_at": "2026-10-01T00:00:00+00:00",
        "observed_at": (NOW - timedelta(minutes=12)).isoformat(),
        "error": None,
        "error_at": None,
    }


def test_a_rolled_over_document_is_this_month_with_nothing_used() -> None:
    document = actions_document(actions_row(period=date(2026, 8, 1)), NOW)
    assert document is not None
    assert (document["period"], document["used_minutes"], document["percent"]) == ("2026-09", 0, 0)


def test_the_limits_tile_gains_an_actions_window_on_the_owner_s_repository(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(rate_limits=limits())
    h.queries.actions_rows = [actions_row()]
    section = hero(html(h.client.get(f"{BASE}/partials/dashboard")))
    assert section.count('<div class="window"') == 13
    assert section.count('<div class="span">Actions</div>') == 1
    assert 'value="77" max="100"' in section
    assert 'title="2,308 of 3,000 min used, 692 left, resets 1 Oct, read 12 min ago"' in section


def test_another_owner_s_repository_draws_no_actions_window(h: Harness) -> None:
    h.register("acme/frontend")
    h.queries.actions_rows = [actions_row()]
    section = hero(html(h.client.get("/r/acme/frontend/partials/dashboard")))
    assert "Actions" not in section and section.count('<div class="window"') == 12


def test_the_actions_window_follows_n_a_claude_windows(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(credential="api_key", rate_limits=limits())
    h.queries.actions_rows = [actions_row()]
    section = hero(html(h.client.get(f"{BASE}/partials/dashboard")))
    assert section.count(">N/A<") == 2
    assert section.count('<div class="span">Actions</div>') == 1


def test_an_actions_dash_draws_no_meter(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(credential="api_key")
    h.queries.actions_rows = [actions_row(included_minutes=None)]
    section = hero(html(h.client.get(f"{BASE}/partials/dashboard")))
    assert 'class="meter"' not in section and section.count(">—<") == 1
```

If `Harness`, `limits` or `BASE` are not already imported in `tests/test_web_pages.py`, add them to the `from fakes.web import (...)` list. `ruff check` names any that are missing.

In `tests/test_web_app.py`, add `actions_row` to the `from fakes.web import (...)` list and append:

```python
def test_the_state_document_carries_the_owner_s_actions_minutes(h: Harness) -> None:
    h.queries.actions_rows = [actions_row()]
    body = h.client.get(f"{API}/state").json()
    assert body["actions_minutes"]["percent"] == 77
    assert body["actions_minutes"]["remaining_minutes"] == 692


def test_the_state_document_has_null_actions_minutes_without_a_row(h: Harness) -> None:
    assert h.client.get(f"{API}/state").json()["actions_minutes"] is None
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_web_pages.py tests/test_web_app.py -q`
Expected: FAIL with `ImportError: cannot import name 'actions_document'`.

- [ ] **Step 4: Write the view functions**

In `src/issuebot/web/views.py`, add `ActionsMinutesRow` to the `from issuebot.db.queries import (...)` list and `from issuebot.github.billing import next_period`. Then add after `limits_unavailable`:

```python
# The limits tile's third window: the billing account's GitHub Actions minutes (spec
# 2026-10-02). Unlike the two Claude windows it is not in the snapshot; it is the row the web's
# poller keeps for the account that owns the repository.
_ALLOWANCE_UNSET = "set ISSUEBOT_ACTIONS_INCLUDED_MINUTES to the plan's allowance to see the share"


def _month(moment: datetime) -> date:
    moment = moment.astimezone(UTC)
    return date(moment.year, moment.month, 1)


def actions_document(row: ActionsMinutesRow | None, now: datetime) -> dict[str, Any] | None:
    """GET .../state's ``actions_minutes``, and what the hero's Actions window is drawn from.

    None without a row for this repository's owner. A reading from an earlier month reads as
    this month with nothing used: the allowance has reset and nothing has been read since to
    spend it, the rule the Claude windows follow once their reset time has passed.
    """
    if row is None:
        return None
    period = row.period
    used: int | None = None
    if period is not None and row.used_minutes is not None:
        current = _month(now)
        if period < current:
            period, used = current, 0
        else:
            used = round(row.used_minutes)
    included = row.included_minutes
    percent = remaining = None
    if used is not None and included:
        percent = round(min(max(used / included, 0.0), 1.0) * 100)
        remaining = max(included - used, 0)
    resets = next_period(period) if period is not None else None
    return {
        "account": row.account,
        "period": f"{period:%Y-%m}" if period is not None else None,
        "used_minutes": used,
        "included_minutes": included,
        "remaining_minutes": remaining,
        "percent": percent,
        "resets_at": iso(datetime(resets.year, resets.month, 1, tzinfo=UTC)) if resets else None,
        "observed_at": iso(row.observed_at),
        "error": row.error,
        "error_at": iso(row.error_at),
    }


def actions_window(row: ActionsMinutesRow | None, now: datetime) -> dict[str, Any] | None:
    """The hero's Actions window: ``value``, ``percent`` (None draws no meter) and ``title``."""
    document = actions_document(row, now)
    if row is None or document is None:
        return None
    used, included = document["used_minutes"], document["included_minutes"]
    if used is None:
        return {"value": "—", "percent": None, "title": row.error or "no reading yet"}
    if not included:
        title = f"{used:,} min used this month; {_ALLOWANCE_UNSET}"
        return {"value": "—", "percent": None, "title": title}
    resets = next_period(date.fromisoformat(f"{document['period']}-01"))
    if used > included:
        figures = f"{used:,} of {included:,} min used, {used - included:,} min over the included {included:,}"
    else:
        figures = f"{used:,} of {included:,} min used, {included - used:,} left"
    title = f"{figures}, resets {resets.day} {resets:%b}, read {age_text(row.observed_at, now)}"
    if row.error:
        title += f"; last refresh failed {age_text(row.error_at, now)}: {row.error}"
    return {"value": f"{document['percent']}%", "percent": document["percent"], "title": title}
```

The month is written as `resets.day` plus `%b` because the zero-less `%-d` is glibc-only, and the repository is also used from Windows hosts.

Add a keyword parameter `actions: ActionsMinutesRow | None = None` to `dashboard_context` (after `labels`) and add `"actions": actions_window(actions, now),` as the last key of its `"hero"` dict. Change `state_document`'s signature to `def state_document(row: SnapshotRow | None, now: datetime, *, actions: ActionsMinutesRow | None = None) -> dict[str, Any]:`, and add `"actions_minutes": actions_document(actions, now),` after `"rate_limits"` in its return dict.

- [ ] **Step 5: Draw the window**

In `src/issuebot/web/templates/partials/dashboard.html`, inside the limits tile, insert the following after the `{% endif %}` that closes the `limits` / `limits_unavailable` branch and before the `</div>` that closes `<div class="windows">`:

```jinja
      {% if live.hero.actions %}
      <div class="window" title="{{ live.hero.actions.title }}">
        <div class="value">{{ live.hero.actions.value }}</div>
        {% if live.hero.actions.percent is not none %}
        <progress class="meter" value="{{ live.hero.actions.percent }}" max="100">{{ live.hero.actions.percent }}%</progress>
        {% endif %}
        <div class="span">Actions</div>
      </div>
      {% endif %}
```

- [ ] **Step 6: Read the row in the two routes**

In `src/issuebot/web/app.py`'s `live_context`, add `actions = await queries.actions_minutes()` after `totals_7d = ...`, and pass `actions=actions,` to `dashboard_context(...)`. In `api_state`, add `actions = await scope.queries.actions_minutes()` after `row = await scope.queries.snapshot()` inside the `async with`, and return `JSONResponse(state_document(row, now(), actions=actions))`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_pages.py tests/test_web_app.py tests/test_web_app_db.py -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: PASS. If ruff reports the `figures` line as too long (E501), wrap the f-string in parentheses across two lines; do not shorten the wording.

- [ ] **Step 8: Commit**

```bash
git add src/issuebot/web tests/fakes/web.py tests/test_web_pages.py tests/test_web_app.py
git commit -F - <<'EOF'
web: an Actions window in the limits tile, and actions_minutes in the state document

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 4: The poller, its Slack alert, and the app's lifespan

**Files:**
- Modify: `src/issuebot/notifications/messages.py` (`format_actions_alert`)
- Create: `src/issuebot/web/actions.py`
- Modify: `src/issuebot/web/app.py:300-316` (`create_app` takes `actions`, and gains a lifespan)
- Modify: `tests/test_notifications_messages.py`
- Create: `tests/test_web_actions.py`

**Interfaces:**
- Consumes:
  - `fetch_login`, `fetch_actions_usage`, `describe_failure`, `next_period` and `ActionsUsage` (Task 1)
  - the four `Database` writes (Task 2), through the `ActionsStore` protocol below
  - `issuebot.notifications.slack.urllib_post`, `slack_payload(text) -> bytes`, `Poster`, `PostResult(status, retry_after_s=None, error=None)` with `.ok`, and `POST_TIMEOUT_S`
  - `issuebot.db.errors.DatabaseError` with `.message`
- Produces:
  - `issuebot.notifications.messages.format_actions_alert(*, account: str, used: int, included: int, resets_on: date) -> str`
  - `issuebot.web.actions`: the constants `TOKEN_ENV`, `ALLOWANCE_ENV`, `WEBHOOK_ENV`, `POLL_INTERVAL_S` and `THRESHOLDS`
  - `ActionsSettings(token: SecretStr, included_minutes: int | None, webhook_url: SecretStr | None)`
  - `actions_settings(environ: Mapping[str, str]) -> ActionsSettings | None`, which raises `ValueError`
  - `alert_threshold(used: int, included: int) -> int`
  - `ActionsStore` (Protocol)
  - `ActionsPoller(settings, *, store, runner, post=urllib_post, now=..., interval_s=POLL_INTERVAL_S)` with `start()`, `async stop()` and `async poll_once()`
  - `create_app(database, *, password, clock=..., now=..., actions: ActionsPoller | None = None)`, which sets `app.state.actions`

- [ ] **Step 1: Write the failing message tests**

Append to `tests/test_notifications_messages.py` (add `from datetime import date` and `format_actions_alert` to its imports):

```python
def test_an_actions_alert_below_the_allowance_says_what_is_left() -> None:
    text = format_actions_alert(
        account="jleavers", used=2308, included=3000, resets_on=date(2026, 11, 1)
    )
    assert text == (
        ":warning: GitHub Actions: jleavers has used 2,308 of 3,000 included minutes this "
        "month (77%); 692 left until 1 Nov."
    )


def test_an_actions_alert_at_the_allowance_says_what_happens_next() -> None:
    text = format_actions_alert(
        account="jleavers", used=3012, included=3000, resets_on=date(2026, 11, 1)
    )
    assert text == (
        ":rotating_light: GitHub Actions: jleavers has used all 3,000 included minutes this "
        "month (3,012 used). Runs in private repositories are now billed or refused, depending "
        "on the account's budget, until 1 Nov."
    )


def test_an_actions_alert_escapes_the_account() -> None:
    text = format_actions_alert(
        account="<!channel>", used=1, included=4, resets_on=date(2026, 11, 1)
    )
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_notifications_messages.py -q`
Expected: FAIL with `ImportError: cannot import name 'format_actions_alert'`.

- [ ] **Step 3: Write `format_actions_alert`**

In `src/issuebot/notifications/messages.py`, add `from datetime import date` and append:

```python
def format_actions_alert(*, account: str, used: int, included: int, resets_on: date) -> str:
    """The dashboard's low-minutes line (spec 2026-10-02): whole minutes, one mrkdwn line."""
    who = _escape(account)
    resets = f"{resets_on.day} {resets_on:%b}"
    if used >= included:
        return (
            f":rotating_light: GitHub Actions: {who} has used all {included:,} included minutes "
            f"this month ({used:,} used). Runs in private repositories are now billed or "
            f"refused, depending on the account's budget, until {resets}."
        )
    percent = round(used / included * 100)
    return (
        f":warning: GitHub Actions: {who} has used {used:,} of {included:,} included minutes "
        f"this month ({percent}%); {included - used:,} left until {resets}."
    )
```

Run: `uv run pytest tests/test_notifications_messages.py -q`
Expected: PASS.

- [ ] **Step 4: Write the failing poller tests**

Create `tests/test_web_actions.py`:

```python
"""The Actions minutes poller and its Slack alert, against a fake gh, store and webhook."""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from structlog.testing import capture_logs

from fakes.database import FakeDatabase
from fakes.web import PASSWORD
from issuebot.db import StoreUnavailableError
from issuebot.github.runner import GhResult
from issuebot.notifications.slack import PostResult
from issuebot.web import create_app
from issuebot.web.actions import (
    ALLOWANCE_ENV,
    TOKEN_ENV,
    WEBHOOK_ENV,
    ActionsPoller,
    ActionsSettings,
    actions_settings,
    alert_threshold,
)

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
OCT = date(2026, 10, 1)
NOV = date(2026, 11, 1)
TOKEN = "ghp_billingtokenfortests000000000000"
WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"
LOGIN = GhResult(returncode=0, stdout=json.dumps({"login": "jleavers"}), stderr="")


def summary(used: float, *, number: int = 10) -> GhResult:
    document = {
        "timePeriod": {"year": 2026, "month": number},
        "usageItems": [{"unitType": "minutes", "grossQuantity": used}],
    }
    return GhResult(returncode=0, stdout=json.dumps(document), stderr="")


def rejected(stderr: str = "gh: Bad credentials (HTTP 401)") -> GhResult:
    return GhResult(returncode=1, stdout="", stderr=stderr)


class Runner:
    """Answers /user with ``login`` and each summary call with the next queued result; the last
    one repeats, so a loop that runs on does not exhaust it."""

    def __init__(self, *summaries: GhResult, login: GhResult = LOGIN) -> None:
        self.login = login
        self.summaries = list(summaries)
        self.calls: list[list[str]] = []

    async def run(self, args: Sequence[str], *, stdin: str | None = None) -> GhResult:
        self.calls.append(list(args))
        if args[1] == "/user":
            return self.login
        return self.summaries.pop(0) if len(self.summaries) > 1 else self.summaries[0]


class Store:
    """The table's semantics in memory, one account: an older month is ignored, a new one
    resets the memory, and the claim is conditional."""

    def __init__(self) -> None:
        self.period: date | None = None
        self.used: float | None = None
        self.alerted = 0
        self.error: str | None = None
        self.claims: list[int] = []

    async def record_actions_reading(
        self,
        *,
        account: str,
        period: date,
        used_minutes: float,
        included_minutes: int | None,
        observed_at: datetime,
    ) -> int | None:
        if self.period is not None and period < self.period:
            return None
        if period != self.period:
            self.alerted = 0
        self.period, self.used, self.error = period, used_minutes, None
        return self.alerted

    async def record_actions_error(self, *, account: str, error: str, error_at: datetime) -> None:
        self.error = error

    async def claim_actions_alert(self, *, account: str, period: date, target: int) -> bool:
        if period != self.period or self.alerted >= target:
            return False
        self.alerted = target
        self.claims.append(target)
        return True

    async def release_actions_alert(
        self, *, account: str, period: date, target: int, previous: int
    ) -> None:
        if period == self.period and self.alerted == target:
            self.alerted = previous


class Webhook:
    def __init__(self, status: int | None = 200) -> None:
        self.status = status
        self.texts: list[str] = []

    async def __call__(self, url: str, payload: bytes, *, timeout_s: float) -> PostResult:
        assert url == WEBHOOK
        self.texts.append(json.loads(payload)["text"])
        ok = self.status == 200
        return PostResult(status=self.status, error=None if ok else "HTTP Error 500")


def settings(*, included: int | None = 3000, webhook: str | None = WEBHOOK) -> ActionsSettings:
    return ActionsSettings(
        token=SecretStr(TOKEN),
        included_minutes=included,
        webhook_url=SecretStr(webhook) if webhook else None,
    )


def make(
    runner: Runner,
    store: Store,
    webhook: Webhook | None = None,
    *,
    config: ActionsSettings | None = None,
    interval_s: float = 3600.0,
) -> ActionsPoller:
    return ActionsPoller(
        config or settings(),
        store=store,
        runner=runner,
        post=webhook or Webhook(),
        now=lambda: NOW,
        interval_s=interval_s,
    )


# --- settings ----------------------------------------------------------------------------------


def test_no_token_turns_the_feature_off() -> None:
    assert actions_settings({ALLOWANCE_ENV: "3000", WEBHOOK_ENV: WEBHOOK}) is None


def test_settings_carry_the_three_values() -> None:
    config = actions_settings({TOKEN_ENV: TOKEN, ALLOWANCE_ENV: "3000", WEBHOOK_ENV: WEBHOOK})
    assert config is not None
    assert config.token.get_secret_value() == TOKEN
    assert config.included_minutes == 3000
    assert config.webhook_url is not None and config.webhook_url.get_secret_value() == WEBHOOK


def test_settings_strip_what_an_env_file_leaves_around_a_value() -> None:
    config = actions_settings(
        {TOKEN_ENV: f" {TOKEN}\n", ALLOWANCE_ENV: " 3000 ", WEBHOOK_ENV: "  "}
    )
    assert config is not None
    assert (config.token.get_secret_value(), config.included_minutes, config.webhook_url) == (
        TOKEN,
        3000,
        None,
    )


def test_an_unset_allowance_is_none_not_a_guess() -> None:
    config = actions_settings({TOKEN_ENV: TOKEN})
    assert config is not None and config.included_minutes is None


@pytest.mark.parametrize("value", ["three thousand", "0", "-5", "3000.5"])
def test_a_malformed_allowance_raises_even_without_a_token(value: str) -> None:
    with pytest.raises(ValueError, match=ALLOWANCE_ENV):
        actions_settings({ALLOWANCE_ENV: value})


@pytest.mark.parametrize(
    ("used", "threshold"),
    [(0, 0), (2249, 0), (2250, 75), (2699, 75), (2700, 90), (3000, 100), (9000, 100)],
)
def test_alert_threshold(used: int, threshold: int) -> None:
    assert alert_threshold(used, 3000) == threshold


# --- a cycle -----------------------------------------------------------------------------------


async def test_a_cycle_stores_the_month_s_reading() -> None:
    store = Store()
    await make(Runner(summary(2306.0)), store).poll_once()
    assert (store.period, store.used, store.error) == (OCT, 2306.0, None)


async def test_the_login_is_resolved_once() -> None:
    runner = Runner(summary(10.0))
    poller = make(runner, Store())
    await poller.poll_once()
    await poller.poll_once()
    assert [call[1] for call in runner.calls].count("/user") == 1


async def test_the_account_read_is_logged_once() -> None:
    """A token minted on the wrong account draws no window anywhere; this line says why."""
    with capture_logs() as logs:
        poller = make(Runner(summary(10.0)), Store())
        await poller.poll_once()
        await poller.poll_once()
    accounts = [entry for entry in logs if entry["event"] == "actions_minutes_account"]
    assert [entry["account"] for entry in accounts] == ["jleavers"]


async def test_a_failed_read_records_the_error_and_keeps_the_reading() -> None:
    store = Store()
    runner = Runner(summary(2306.0), rejected())
    poller = make(runner, store)
    await poller.poll_once()
    await poller.poll_once()
    assert store.used == 2306.0
    assert store.error == "token rejected: it may have expired or been revoked"


async def test_a_failed_login_writes_nothing_and_the_next_cycle_retries() -> None:
    store = Store()
    runner = Runner(summary(10.0), login=rejected())
    poller = make(runner, store)
    await poller.poll_once()
    assert (store.period, store.error) == (None, None)
    runner.login = LOGIN
    await poller.poll_once()
    assert store.used == 10.0


async def test_a_failure_is_logged_when_it_starts_and_when_it_clears() -> None:
    with capture_logs() as logs:
        poller = make(Runner(rejected(), rejected(), rejected(), summary(10.0)), Store())
        for _ in range(4):
            await poller.poll_once()
    events = [entry["event"] for entry in logs if entry["event"].startswith("actions_minutes_")]
    assert events.count("actions_minutes_failed") == 1
    assert events.count("actions_minutes_recovered") == 1


async def test_the_token_never_reaches_a_log_line() -> None:
    with capture_logs() as logs:
        poller = make(Runner(rejected(f"gh: Bad credentials for {TOKEN} (HTTP 401)")), Store())
        await poller.poll_once()
    assert TOKEN not in repr(logs)


# --- the alert ---------------------------------------------------------------------------------


async def test_crossing_a_threshold_posts_once() -> None:
    store, webhook = Store(), Webhook()
    poller = make(Runner(summary(2306.0), summary(2310.0)), store, webhook)
    await poller.poll_once()
    await poller.poll_once()
    assert len(webhook.texts) == 1 and webhook.texts[0].startswith(":warning:")
    assert "(77%)" in webhook.texts[0] and store.alerted == 75


async def test_a_jump_past_several_thresholds_posts_the_highest_alone() -> None:
    store, webhook = Store(), Webhook()
    await make(Runner(summary(2950.0)), store, webhook).poll_once()
    assert store.claims == [90] and len(webhook.texts) == 1 and "(98%)" in webhook.texts[0]


async def test_the_allowance_used_up_posts_the_last_alert() -> None:
    webhook = Webhook()
    await make(Runner(summary(3012.0)), Store(), webhook).poll_once()
    assert webhook.texts[0].startswith(":rotating_light:") and "until 1 Nov" in webhook.texts[0]


async def test_a_restart_above_a_threshold_does_not_post_again() -> None:
    store, webhook = Store(), Webhook()
    await make(Runner(summary(2306.0)), store, webhook).poll_once()
    await make(Runner(summary(2320.0)), store, webhook).poll_once()  # a new process, same table
    assert len(webhook.texts) == 1


async def test_a_failed_post_releases_the_claim_and_the_next_cycle_retries() -> None:
    store, webhook = Store(), Webhook(status=500)
    poller = make(Runner(summary(2306.0)), store, webhook)
    with capture_logs() as logs:
        await poller.poll_once()
    assert store.alerted == 0 and len(webhook.texts) == 1
    assert any(entry["event"] == "actions_minutes_alert_failed" for entry in logs)
    webhook.status = 200
    await poller.poll_once()
    assert store.alerted == 75 and len(webhook.texts) == 2


async def test_an_older_month_from_github_posts_nothing() -> None:
    store, webhook = Store(), Webhook()
    store.period, store.used, store.alerted = NOV, 10.0, 0
    await make(Runner(summary(3012.0, number=10)), store, webhook).poll_once()
    assert (store.period, store.used, webhook.texts) == (NOV, 10.0, [])


@pytest.mark.parametrize(
    "config", [settings(webhook=None), settings(included=None)], ids=["no-webhook", "no-allowance"]
)
async def test_without_a_webhook_or_an_allowance_nothing_is_claimed_or_posted(
    config: ActionsSettings,
) -> None:
    store, webhook = Store(), Webhook()
    await make(Runner(summary(3012.0)), store, webhook, config=config).poll_once()
    assert store.used == 3012.0 and store.claims == [] and webhook.texts == []


# --- the loop and the app ----------------------------------------------------------------------


async def test_start_runs_a_cycle_and_stop_ends_the_loop() -> None:
    store = Store()
    poller = make(Runner(summary(10.0)), store)
    poller.start()
    poller.start()  # a second start is a no-op, not a second loop
    for _ in range(50):
        if store.used is not None:
            break
        await asyncio.sleep(0)
    assert store.used == 10.0
    await poller.stop()
    await poller.stop()


async def test_a_database_error_is_logged_and_the_next_cycle_retries() -> None:
    class Flaky(Store):
        failures = 1

        async def record_actions_reading(self, **kwargs: object) -> int | None:  # type: ignore[override]
            if self.failures:
                self.failures -= 1
                raise StoreUnavailableError("cannot connect: refused")
            return await super().record_actions_reading(**kwargs)  # type: ignore[arg-type]

    store = Flaky()
    with capture_logs() as logs:
        poller = make(Runner(summary(10.0)), store, interval_s=0)
        poller.start()
        for _ in range(100):
            if store.used is not None:
                break
            await asyncio.sleep(0)
        await poller.stop()
    assert store.used == 10.0
    assert any(entry["event"] == "actions_minutes_store_failed" for entry in logs)


class FakePoller:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")


def test_the_app_starts_and_stops_the_poller_with_its_lifespan() -> None:
    poller = FakePoller()
    app = create_app(FakeDatabase(), password=PASSWORD, actions=poller)  # type: ignore[arg-type]
    assert app.state.actions is poller
    with TestClient(app):
        assert poller.events == ["start"]
    assert poller.events == ["start", "stop"]


def test_an_app_without_a_poller_has_none() -> None:
    assert create_app(FakeDatabase(), password=PASSWORD).state.actions is None
```

- [ ] **Step 5: Run them to verify they fail**

Run: `uv run pytest tests/test_web_actions.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'issuebot.web.actions'`.

- [ ] **Step 6: Write the poller**

Create `src/issuebot/web/actions.py`:

```python
"""The dashboard's GitHub Actions minutes poller, and the Slack alert it posts (spec 2026-10-02).

Runs in the hub's web process and nowhere else: the token it holds is the billing account's own,
a classic token with the ``user`` scope, and the web runs no session. One cycle at start, then
one an hour: learn the token's login once, read the month's summary, upsert the account's row,
and post an alert when the reading reaches a threshold not yet posted this month. The row is the
alert's memory, claimed before the post and given back if the post fails, so a restart -- which
every upgrade is -- never posts twice and a failed post is retried an hour later.
"""

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from pydantic import SecretStr

from issuebot.db.errors import DatabaseError
from issuebot.github.billing import (
    ActionsUsage,
    describe_failure,
    fetch_actions_usage,
    fetch_login,
    next_period,
)
from issuebot.github.errors import GitHubError
from issuebot.github.runner import GhRunnerLike
from issuebot.log import get_logger
from issuebot.notifications.messages import format_actions_alert
from issuebot.notifications.slack import POST_TIMEOUT_S, Poster, slack_payload, urllib_post

TOKEN_ENV = "ISSUEBOT_GITHUB_BILLING_TOKEN"
ALLOWANCE_ENV = "ISSUEBOT_ACTIONS_INCLUDED_MINUTES"
WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
POLL_INTERVAL_S = 3600.0
# Percent of the allowance, each posted at most once a month. Constants, as SlackSink's retry
# policy is: a deployment that wants others is a code change, not a setting.
THRESHOLDS: tuple[int, ...] = (75, 90, 100)


@dataclass(frozen=True, slots=True)
class ActionsSettings:
    token: SecretStr
    included_minutes: int | None
    webhook_url: SecretStr | None


def actions_settings(environ: Mapping[str, str]) -> ActionsSettings | None:
    """The poller's settings, or None when the token is unset and the feature is off.

    A malformed allowance raises ``ValueError`` whether or not the token is set: it is an
    operator's mistake, and refusing to start names it where a dash on the tile would not.
    """
    raw = environ.get(ALLOWANCE_ENV, "").strip()
    included: int | None = None
    if raw:
        try:
            included = int(raw)
        except ValueError:
            included = 0
        if included <= 0:
            raise ValueError(
                f"{ALLOWANCE_ENV} must be a positive whole number of minutes, such as 3000"
            )
    token = environ.get(TOKEN_ENV, "").strip()
    if not token:
        return None
    webhook = environ.get(WEBHOOK_ENV, "").strip()
    return ActionsSettings(
        token=SecretStr(token),
        included_minutes=included,
        webhook_url=SecretStr(webhook) if webhook else None,
    )


def alert_threshold(used: int, included: int) -> int:
    """The highest threshold ``used`` whole minutes have reached; 0 for none."""
    return max((t for t in THRESHOLDS if used * 100 >= t * included), default=0)


class ActionsStore(Protocol):
    """The poller's writes; ``issuebot.db.Database`` is the real one."""

    async def record_actions_reading(
        self,
        *,
        account: str,
        period: date,
        used_minutes: float,
        included_minutes: int | None,
        observed_at: datetime,
    ) -> int | None: ...

    async def record_actions_error(
        self, *, account: str, error: str, error_at: datetime
    ) -> None: ...

    async def claim_actions_alert(self, *, account: str, period: date, target: int) -> bool: ...

    async def release_actions_alert(
        self, *, account: str, period: date, target: int, previous: int
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ActionsPoller:
    """One cycle at start, then one every ``interval_s``; ``poll_once`` is the test seam."""

    def __init__(
        self,
        settings: ActionsSettings,
        *,
        store: ActionsStore,
        runner: GhRunnerLike,
        post: Poster = urllib_post,
        now: Callable[[], datetime] = _utcnow,
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._settings = settings
        self._store = store
        self._runner = runner
        self._post = post
        self._now = now
        self._interval_s = interval_s
        self._account: str | None = None
        self._failure: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger(__name__)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="actions-minutes")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except DatabaseError as exc:
                self._log.warning("actions_minutes_store_failed", error=exc.message)
            except Exception:
                # Never let the poller die quietly: the next cycle is the retry.
                self._log.exception("actions_minutes_poll_crashed")
            await asyncio.sleep(self._interval_s)

    async def poll_once(self) -> None:
        if self._account is None:
            try:
                self._account = await fetch_login(self._runner)
            except GitHubError as exc:
                self._failed(exc)
                return
            # Which account is read is the first thing to check when no window appears: a token
            # of any other account than the repositories' owner draws nothing anywhere.
            self._log.info("actions_minutes_account", account=self._account)
        account = self._account
        try:
            usage = await fetch_actions_usage(self._runner, account)
        except GitHubError as exc:
            self._failed(exc)
            await self._store.record_actions_error(
                account=account, error=describe_failure(exc), error_at=self._now()
            )
            return
        self._recovered()
        alerted = await self._store.record_actions_reading(
            account=account,
            period=usage.period,
            used_minutes=usage.used_minutes,
            included_minutes=self._settings.included_minutes,
            observed_at=self._now(),
        )
        if alerted is None:
            self._log.info(
                "actions_minutes_older_period_ignored",
                account=account,
                period=usage.period.isoformat(),
            )
            return
        await self._alert(account, usage, alerted)

    async def _alert(self, account: str, usage: ActionsUsage, alerted: int) -> None:
        included = self._settings.included_minutes
        webhook = self._settings.webhook_url
        if included is None or webhook is None:
            return
        used = round(usage.used_minutes)
        target = alert_threshold(used, included)
        if target <= alerted:
            return
        claimed = await self._store.claim_actions_alert(
            account=account, period=usage.period, target=target
        )
        if not claimed:
            return
        text = format_actions_alert(
            account=account, used=used, included=included, resets_on=next_period(usage.period)
        )
        result = await self._post(
            webhook.get_secret_value(), slack_payload(text), timeout_s=POST_TIMEOUT_S
        )
        if result.ok:
            self._log.info(
                "actions_minutes_alert_sent",
                account=account,
                threshold=target,
                used=used,
                included=included,
            )
            return
        await self._store.release_actions_alert(
            account=account, period=usage.period, target=target, previous=alerted
        )
        self._log.warning(
            "actions_minutes_alert_failed",
            account=account,
            threshold=target,
            status=result.status,
            error=result.error,
        )

    def _failed(self, exc: GitHubError) -> None:
        """Log a failure when it starts or changes, not on every hourly cycle."""
        description = describe_failure(exc)
        if description == self._failure:
            return
        self._failure = description
        token = self._settings.token.get_secret_value()
        self._log.warning(
            "actions_minutes_failed",
            category=exc.category,
            error=description,
            detail=exc.message.replace(token, "***"),
        )

    def _recovered(self) -> None:
        if self._failure is not None:
            self._failure = None
            self._log.info("actions_minutes_recovered")
```

- [ ] **Step 7: Give `create_app` the poller and a lifespan**

In `src/issuebot/web/app.py`:
- Add `AsyncIterator` to `from collections.abc import ...`, add `from contextlib import asynccontextmanager`, and add `from issuebot.web.actions import ActionsPoller`.
- Add the keyword parameter `actions: ActionsPoller | None = None,` after `now` in `create_app`'s signature, and append ` ``actions`` is the Actions minutes poller the CLI builds when the billing token is set; it starts and stops with the app.` to the docstring.
- Replace the line `app = FastAPI(title="issuebot", docs_url=None, redoc_url=None, openapi_url=None)` with:

```
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The Actions minutes poller lives as long as the server does (spec 2026-10-02).
        if actions is not None:
            actions.start()
        try:
            yield
        finally:
            if actions is not None:
                await actions.stop()

    app = FastAPI(
        title="issuebot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.actions = actions
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_actions.py tests/test_notifications_messages.py tests/test_web_app.py tests/test_web_pages.py -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: PASS. If a `capture_logs` test sees no entries, the logger was bound before capture began; build the poller inside the `with capture_logs()` block, as the tests above already do.

- [ ] **Step 9: Commit**

```bash
git add src/issuebot/notifications/messages.py src/issuebot/web/actions.py src/issuebot/web/app.py tests/test_notifications_messages.py tests/test_web_actions.py
git commit -F - <<'EOF'
web: poll the Actions minutes hourly and alert Slack at 75, 90 and 100 percent

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 5: `issuebot web` builds the poller from its environment

**Files:**
- Modify: `src/issuebot/cli.py:2456-2483` (`cmd_web`, `_run_web`) and its imports
- Modify: `tests/conftest.py:23-46` (`_ENV_VARS`)
- Modify: `tests/test_cli.py` (after `test_web_gates_the_app_with_the_password_from_the_environment`, around line 3600)

**Interfaces:**
- Consumes: `actions_settings`, `ActionsPoller`, `ActionsSettings` and `ALLOWANCE_ENV` (Task 4); `GhRunner(token=SecretStr | None, ...)` (`src/issuebot/github/runner.py`); `create_app(..., actions=...)` and `app.state.actions` (Task 4).
- Produces: `_run_web(url, *, password, port, bind, actions: ActionsSettings | None = None) -> int`.

- [ ] **Step 1: Keep the suite hermetic**

In `tests/conftest.py`, add these two lines to `_ENV_VARS`, after `"SLACK_WEBHOOK_URL",`, so a host that exports them cannot change a test's outcome:

```
    "ISSUEBOT_GITHUB_BILLING_TOKEN",
    "ISSUEBOT_ACTIONS_INCLUDED_MINUTES",
```

- [ ] **Step 2: Write the failing CLI tests**

In `tests/test_cli.py`, add `from issuebot.web.actions import ActionsPoller` to the imports, and add after `test_web_gates_the_app_with_the_password_from_the_environment`:

```python
BILLING_TOKEN = "ghp_" + "b" * 36


def test_web_builds_no_actions_poller_without_the_billing_token(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    assert main(["web"]) == 0
    ((app, _host, _port),) = fake_serve.calls
    assert app.state.actions is None  # type: ignore[attr-defined]


def test_web_builds_the_actions_poller_from_the_environment(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("ISSUEBOT_GITHUB_BILLING_TOKEN", BILLING_TOKEN)
    monkeypatch.setenv("ISSUEBOT_ACTIONS_INCLUDED_MINUTES", "3000")
    assert main(["web"]) == 0
    ((app, _host, _port),) = fake_serve.calls
    assert isinstance(app.state.actions, ActionsPoller)  # type: ignore[attr-defined]
    err = capsys.readouterr().err
    assert "web_started" in err and BILLING_TOKEN not in err
    assert "actions_minutes_allowance_unset" not in err


def test_web_warns_when_the_allowance_is_unset(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("ISSUEBOT_GITHUB_BILLING_TOKEN", BILLING_TOKEN)
    assert main(["web"]) == 0
    assert "actions_minutes_allowance_unset" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["three thousand", "0", "-5", "3000.5"])
def test_web_refuses_a_malformed_allowance_before_migrating(
    value: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
    web_password: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("ISSUEBOT_ACTIONS_INCLUDED_MINUTES", value)
    assert main(["web"]) == 1
    assert capsys.readouterr().out == (
        "[FAIL] web: ISSUEBOT_ACTIONS_INCLUDED_MINUTES must be a positive whole number of "
        "minutes, such as 3000\n"
    )
    assert fake_serve.calls == [] and fake_database.migrations == 0
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "web_" -q`
Expected: `test_web_builds_the_actions_poller_from_the_environment`, `test_web_warns_when_the_allowance_is_unset` and the four malformed-allowance cases FAIL. `test_web_builds_no_actions_poller_without_the_billing_token` already passes, because Task 4's `create_app` sets `app.state.actions` to None; it pins that the CLI keeps it that way. The existing `web` tests still pass.

- [ ] **Step 4: Wire the CLI**

In `src/issuebot/cli.py`, import `GhRunner` from `issuebot.github.runner` if it is not already imported (`grep -n "GhRunner" src/issuebot/cli.py`), and add `from issuebot.web.actions import ALLOWANCE_ENV, ActionsPoller, ActionsSettings, actions_settings`. Replace `cmd_web`'s final `return` and `_run_web` with:

```
    try:
        actions = actions_settings(os.environ)
    except ValueError as exc:
        print(f"[FAIL] web: {exc}")
        return 1
    return asyncio.run(
        _run_web(url, password=password, port=args.port, bind=args.bind, actions=actions)
    )


async def _run_web(
    url: str, *, password: str, port: int, bind: str, actions: ActionsSettings | None = None
) -> int:
    """Migrate, build the app and serve it until a stop signal; a failed bind is uvicorn's
    error line and exit 1. The web reads no workflow: everything it shows is in the database,
    and the Actions minutes poller, when the billing token is set, is what puts them there."""
    if not 0 <= port <= 65535:
        print("[FAIL] web: --port must be between 0 and 65535")
        return 1
    try:
        database = await _migrate_database(url)
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    log = get_logger(__name__)
    poller = None
    if actions is not None:
        poller = ActionsPoller(actions, store=database, runner=GhRunner(token=actions.token))
        if actions.included_minutes is None:
            log.warning("actions_minutes_allowance_unset", setting=ALLOWANCE_ENV)
    log.info(
        "web_started",
        bind=bind,
        port=port,
        database=database.description,
        actions_minutes=poller is not None,
    )
    try:
        await _serve(create_app(database, password=password, actions=poller), host=bind, port=port)
    except SystemExit as exc:  # uvicorn's startup() exits 3 when the bind fails
        return 1 if exc.code else 0
    return 0
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q && uv run ruff check src tests && uv run ruff format --check src tests`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/issuebot/cli.py tests/conftest.py tests/test_cli.py
git commit -F - <<'EOF'
cli: issuebot web builds the Actions minutes poller when the billing token is set

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 6: Compose, `.env.example`, and keeping the token out of the worker

**Files:**
- Modify: `compose.yaml` (the `web` service's environment and comment; the `worker` service's environment)
- Modify: `.env.example` (after the `ISSUEBOT_WEB_PASSWORD=` block, around line 79). Use the Edit tool.
- Modify: `tests/test_compose_credentials.py`
- Modify: `tests/test_agent_runner.py` (beside `test_agent_environment_passes_only_the_allowed_names`, line 389)

**Interfaces:**
- Consumes: the variable names from Task 4.
- Produces: no code interfaces.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_compose_credentials.py`:

```python
BILLING = ("ISSUEBOT_GITHUB_BILLING_TOKEN", "ISSUEBOT_ACTIONS_INCLUDED_MINUTES")


def test_the_actions_minutes_settings_reach_the_web_as_pass_throughs() -> None:
    web = _env(_services()["web"])
    for name in (*BILLING, "SLACK_WEBHOOK_URL"):
        assert web[name] == f"${{{name}:-}}", (name, web.get(name))


def test_the_billing_token_reaches_the_web_alone() -> None:
    """The account owner's credential (spec 2026-10-02). The worker's env_file loads the hub's
    whole .env, so its environment map empties the token, and environment wins over env_file;
    no other service names it at all."""
    services = _services()
    assert _env(services["worker"])["ISSUEBOT_GITHUB_BILLING_TOKEN"] == ""
    for name, service in services.items():
        if name not in ("web", "worker"):
            assert "ISSUEBOT_GITHUB_BILLING_TOKEN" not in str(service.get("environment", "")), name
    uses = re.findall(r"\$\{ISSUEBOT_GITHUB_BILLING_TOKEN[^}]*\}", COMPOSE.read_text())
    assert uses == ["${ISSUEBOT_GITHUB_BILLING_TOKEN:-}"], uses


def test_env_example_ships_the_actions_minutes_settings_empty() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    for name in BILLING:
        assignments = [line for line in lines if line.startswith(f"{name}=")]
        assert assignments == [f"{name}="], assignments
```

Add, beside the other `agent_environment` tests in `tests/test_agent_runner.py`:

```python
def test_agent_environment_never_carries_the_dashboard_s_billing_token() -> None:
    parent = {"PATH": "/usr/bin", "ISSUEBOT_GITHUB_BILLING_TOKEN": "ghp_" + "c" * 36}
    assert "ISSUEBOT_GITHUB_BILLING_TOKEN" not in agent_environment(parent, token=None)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_compose_credentials.py tests/test_agent_runner.py -k "actions_minutes or billing" -q`
Expected: the compose and `.env.example` tests FAIL with `KeyError: 'ISSUEBOT_GITHUB_BILLING_TOKEN'`. The `agent_environment` test PASSES already, because the allow-list holds; it is a pin, not a fix.

- [ ] **Step 3: Edit `compose.yaml`**

In the `web` service, replace the two comment lines

```
    # The dashboard and the JSON API for every repository on this database. Reads nothing but
    # DATABASE_URL and its own password; no workflow, no GitHub, Claude or Slack credential.
```

with

```
    # The dashboard and the JSON API for every repository on this database. Reads DATABASE_URL,
    # its own password and, when the hub sets them, the GitHub Actions minutes settings below;
    # no workflow and no Claude credential.
```

and append to its `environment:` map, after `ISSUEBOT_WEB_PASSWORD`:

```yaml
      # The dashboard's GitHub Actions minutes (docs/dashboard.md): the billing account's own
      # classic token with only the `user` scope, its plan's monthly allowance, and the webhook
      # the low-minutes alert posts to. Optional pass-throughs like the password above; with no
      # token the web polls nothing and the limits tile keeps its two Claude windows.
      ISSUEBOT_GITHUB_BILLING_TOKEN: ${ISSUEBOT_GITHUB_BILLING_TOKEN:-}
      ISSUEBOT_ACTIONS_INCLUDED_MINUTES: ${ISSUEBOT_ACTIONS_INCLUDED_MINUTES:-}
      SLACK_WEBHOOK_URL: ${SLACK_WEBHOOK_URL:-}
```

In the `worker` service's `environment:` map, after `ISSUEBOT_WORKFLOW: /configs/WORKFLOW.md`, add:

```yaml
      # The billing account's token is the dashboard's alone (docs/security-model.md). env_file
      # above loads the whole .env, which in a hub checkout holds it, so it is emptied here --
      # environment wins over env_file -- and the owner's credential never sits in the
      # container where sessions run.
      ISSUEBOT_GITHUB_BILLING_TOKEN: ""
```

- [ ] **Step 4: Edit `.env.example` (Edit tool)**

After the `ISSUEBOT_WEB_PASSWORD=` line, add:

```
# Optional, hub only: the dashboard's GitHub Actions minutes, a third window in the limits tile
# (docs/dashboard.md). A classic token of the account that owns the repositories with only the
# `user` scope ticked -- GitHub's billing endpoints accept nothing narrower, and that scope can
# also edit the account's profile and read its private email addresses, so it goes to the web
# and never to a worker -- and the plan's monthly allowance in minutes (3000 on GitHub Pro, 2000
# on Free). With SLACK_WEBHOOK_URL set, the dashboard also posts when 75%, 90% and 100% of the
# allowance is used.
ISSUEBOT_GITHUB_BILLING_TOKEN=
ISSUEBOT_ACTIONS_INCLUDED_MINUTES=
```

- [ ] **Step 5: Run the tests, and prove compose still parses under every profile**

Run: `uv run pytest tests/test_compose_credentials.py tests/test_agent_runner.py -q`
Expected: PASS.

Run: `for p in hub worker hub,worker; do COMPOSE_PROFILES=$p docker compose config --quiet && echo "$p ok"; done`
Expected: `hub ok`, `worker ok`, `hub,worker ok`.

- [ ] **Step 6: Commit**

```bash
git add compose.yaml .env.example tests/test_compose_credentials.py tests/test_agent_runner.py
git commit -F - <<'EOF'
compose: pass the Actions minutes settings to the web, and empty the token in the worker

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 7: Documentation

**Files:**
- Modify: `docs/dashboard.md` (the tiles sentence at line 66; a new `## GitHub Actions minutes` section before `## What "issues closed" counts`)
- Modify: `docs/security-model.md` (a new `## The dashboard's billing token` section before `## Checking that the credential took`)
- Modify: `docs/operations.md` (the end of `### Checks that never ran`)
- Modify: `README.md` (Step 1's `.env` paragraph, around line 256; Development's `issuebot web` sentence, around line 616)
- Modify: `docs/package-layout.md` (the `issuebot.github`, `issuebot.notifications`, `issuebot.db` and `issuebot.web` sections)
- Modify: `CLAUDE.md` (the `issuebot web` command line; the `docs/dashboard.md` map entry)

**Interfaces:** none.

- [ ] **Step 1: `docs/dashboard.md`**

Replace the sentence `Closed, agents run, cost, tokens, limits and activity, each showing two figures: 1 day and 7 days for the first four, the two usage windows for limits, and running against retrying for activity.` (it spans lines 66-68) with:

```
Closed, agents run, cost, tokens, limits and activity, each showing
two figures: 1 day and 7 days for the first four, the two usage windows for limits (and a
third, [GitHub Actions minutes](#github-actions-minutes), where the hub is given a billing
token), and running against retrying for activity.
```

Insert before `## What "issues closed" counts`:

```
## GitHub Actions minutes

Where the hub's `web` is given a billing token, the limits tile carries a third window,
`Actions`: the share of the billing account's monthly Actions allowance used so far, with the
same depletion bar as the Claude windows, and the figures in its tooltip -- `2,308 of 3,000 min
used, 692 left, resets 1 Nov, read 12 min ago`. When the allowance runs out, workflows in
private repositories stop starting and a pull request's checks fail with zero steps ([Checks
that never ran](operations.md#checks-that-never-ran)); this window is where that shows first.

Three settings in the hub checkout's `.env`, read by `web` alone:

- `ISSUEBOT_GITHUB_BILLING_TOKEN`: a classic token of the account that owns the repositories,
  with only `user` ticked. GitHub's billing endpoints take no fine-grained token and nothing
  narrower, and that scope can also edit the account's profile and read its private email
  addresses, so it goes to the dashboard and never to a worker ([The dashboard's billing
  token](security-model.md#the-dashboards-billing-token)). Give it an expiry; a lapsed one shows
  as the window's error.
- `ISSUEBOT_ACTIONS_INCLUDED_MINUTES`: the plan's monthly allowance, 3000 on GitHub Pro and 2000
  on Free. GitHub's API does not report it. Without it the window is a dash that names it; a
  value that is not a positive whole number stops `issuebot web` from starting.
- `SLACK_WEBHOOK_URL`, the worker's own: with it the dashboard posts to Slack when the month's
  use reaches 75%, 90% and 100% of the allowance, each once a month. `notifications.slack.events`
  does not govern it: that list is the worker's, and the web reads no workflow.

The web reads the account's usage summary once an hour -- about 25 REST requests a day on the
token's own budget, and none of the workers' GraphQL points. "Used" is what the billing page
counts: every Actions minute, public repositories included, Windows at 1x. The window is drawn
only on repositories that account owns, reads 0% from the first of the month (UTC) until the
month's first reading, and keeps its last figure through a failed read, adding the failure to
the tooltip. The web's log names the account it reads (`actions_minutes_account`), which is the
first thing to check when no window appears.

The reading is kept in the `actions_minutes` table, so it survives a restart and an alert is
never posted twice. To take the window away, remove the token, restart `web`, and delete the
row: `docker compose exec db psql -U issuebot -c 'DELETE FROM actions_minutes'`.
```

- [ ] **Step 2: `docs/security-model.md`**

Insert before `## Checking that the credential took`:

```
## The dashboard's billing token

The hub's `web` may hold one GitHub credential, for the limits tile's [GitHub Actions
minutes](dashboard.md#github-actions-minutes): `ISSUEBOT_GITHUB_BILLING_TOKEN`, a classic token
of the account that owns the repositories, with the `user` scope and nothing else. It is not a
deployment's token. A deployment's bot account cannot read its owner's billing at all, and the
billing endpoints take no fine-grained token, so this is a second token of the *owner's*. It
reaches no repository, but it can edit the owner's profile, read their private email addresses,
follow and unfollow, and read their billing.

So it lives where no session does. The web runs no session, answers only behind HTTP Basic on a
loopback-published port, and already holds the database DSN. The worker's container is where a
hostile issue's session runs, and keeping the owner's credentials out of it is why each
deployment acts as its own bot account ([The account a session acts
as](#the-account-a-session-acts-as)). The worker loads the whole `.env` through `env_file`, so
compose empties the billing token in the worker's `environment:`, which wins; and
`agent_environment`'s allow-list would not pass it to a session in any case. The token travels to
`gh` as `GH_TOKEN` in the child's environment, never as an argument, and no log line, row, page
or JSON response carries it: the error the window shows is issuebot's own wording.

Give it an expiry. A lapsed token shows as the window's error and as one
`actions_minutes_failed` line in the web's log; a new one is an `.env` edit and `docker compose
up -d web`.
```

- [ ] **Step 3: `docs/operations.md`**

Append a paragraph at the end of `### Checks that never ran`, immediately before the next `###` heading:

```
Where the hub is given a billing token, the dashboard's limits tile shows the billing account's
[GitHub Actions minutes](dashboard.md#github-actions-minutes), and Slack hears at 75%, 90% and
100%; a window at 100% is this cause before it is any other.
```

- [ ] **Step 4: `README.md`**

In Step 1, change `values, and optionally `SLACK_WEBHOOK_URL`.` to:

```
values, and optionally `SLACK_WEBHOOK_URL`. On the hub, `ISSUEBOT_GITHUB_BILLING_TOKEN` and
`ISSUEBOT_ACTIONS_INCLUDED_MINUTES` add the billing account's [GitHub Actions
minutes](docs/dashboard.md#github-actions-minutes) to the dashboard: the token is a classic one
of the account that owns the repositories with only `user` ticked, the narrowest GitHub offers,
which can also edit that account's profile and read its private email addresses.
```

In Development, change ``reads `DATABASE_URL` and `ISSUEBOT_WEB_PASSWORD` and nothing else`` to:

```
reads `DATABASE_URL` and `ISSUEBOT_WEB_PASSWORD`, plus the optional [GitHub Actions
minutes](docs/dashboard.md#github-actions-minutes) settings, and nothing else
```

- [ ] **Step 5: `docs/package-layout.md`** (keep the four additions under 3 KB in total)

Append a paragraph at the end of each section:

- `issuebot.github`:
  ```
  `billing.py`: the dashboard's Actions minutes reads, made with the billing token through any
  `GhRunnerLike` -- `fetch_login`, `fetch_actions_usage`, `parse_summary` (period from
  `timePeriod`; `grossQuantity` summed over the items whose unit is minutes, which is the figure
  the billing page shows), `next_period`, and `describe_failure`, issuebot's own words for a
  failure, since `gh`'s stderr never reaches a page. `errors.categorise` is the stderr
  classification it shares with `ghcli`.
  ```
- `issuebot.notifications`: change `imported by `cli` only` at the section's start to `imported by `cli`, and by `web` for one message`, and append:
  ```
  `format_actions_alert` is the dashboard's low-minutes line, posted by `web.actions` through
  `urllib_post` with a single attempt; the next hourly cycle is the retry.
  ```
- `issuebot.db`:
  ```
  `0005_actions_minutes.sql` and `actions.py`: one row per billing account, written only by the
  web's poller. `Database.record_actions_reading` ignores a reading for an older month (GitHub
  lagging across a boundary) and resets `alerted_percent` on a new one; `record_actions_error`
  keeps the reading; `claim_actions_alert`/`release_actions_alert` are the alert's
  claim-then-post. `RepoQueries.actions_minutes()` reads the repository owner's row,
  case-insensitively.
  ```
- `issuebot.web`:
  ```
  `actions.py`: `actions_settings` (the three environment variables; a malformed allowance
  raises, so `issuebot web` refuses to start), `ActionsPoller` (hourly; `create_app(actions=)`
  starts and stops it with the lifespan), `alert_threshold` over `THRESHOLDS` (75, 90, 100).
  `views.actions_document`/`actions_window` draw the limits tile's third window and `/state`'s
  `actions_minutes` from the row.
  ```

- [ ] **Step 6: `CLAUDE.md`**

After the line `                                     #   ISSUEBOT_WEB_PASSWORD, reads no workflow; binds 127.0.0.1 by default)`, add:

```
                                     #   + the Actions minutes with ISSUEBOT_GITHUB_BILLING_TOKEN, docs/dashboard.md
```

In the doc map's `docs/dashboard.md` entry, change `the hero's six tiles, and what "issues closed" counts.` to `the hero's six tiles, the GitHub Actions minutes window and its Slack alert, and what "issues closed" counts.`

- [ ] **Step 7: Run the documentation tests**

Run: `uv run pytest tests/test_doc_pointers.py tests/test_readme_bounds.py tests/test_instruction_bounds.py tests/test_package_layout.py tests/test_compose_credentials.py -q && uv run pre-commit run --all-files`
Expected: PASS. If `test_doc_pointers` rejects an anchor, compare it with the heading's GitHub slug: `The dashboard's billing token` becomes `the-dashboards-billing-token`. If `test_readme_bounds` fails, the edit moved a pinned passage; restore that passage's wording and put the new sentence after it.

- [ ] **Step 8: Commit**

```bash
git add docs/dashboard.md docs/security-model.md docs/operations.md README.md docs/package-layout.md CLAUDE.md
git commit -F - <<'EOF'
docs: the dashboard's GitHub Actions minutes, its alert, and its billing token

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 8: Seed an Actions reading and regenerate the README's images

**Files:**
- Modify: `tools/screenshots/seed.py` (`main`)
- Regenerate: `docs/images/dashboard.png`, `docs/images/issue-journey.gif`

**Interfaces:**
- Consumes: `Database.record_actions_reading` (Task 2).

- [ ] **Step 1: Seed a reading for the placeholder account**

In `tools/screenshots/seed.py` no new import is needed: `NOW.date()` gives the date, and `timedelta` is already imported. In `main`, after the `await database.register_repo(...)` line, add:

```
    # The limits tile's Actions window (spec 2026-10-02), for `acme`, the owner of REPO: a
    # comfortable 58% so the image shows the window without implying an alert.
    await database.record_actions_reading(
        account="acme",
        period=NOW.date().replace(day=1),
        used_minutes=1740.0,
        included_minutes=3000,
        observed_at=NOW - timedelta(minutes=12),
    )
```

- [ ] **Step 2: Regenerate the images by the recipe in `tools/screenshots/README.md`**

Run (once per machine): `uv run --with playwright playwright install chromium`

Then:
```bash
docker compose --profile test up -d --wait test-db
export DATABASE_URL="postgresql://issuebot@127.0.0.1:$(docker compose port test-db 5432 | cut -d: -f2)/issuebot"
uv run python tools/screenshots/seed.py history
ISSUEBOT_WEB_PASSWORD=screenshot uv run issuebot web --port 8099 &
uv run --with playwright --with pillow python tools/screenshots/capture.py --password screenshot --dsn "$DATABASE_URL"
kill %1
docker compose rm -sf test-db
```

Expected: the capture prints both sizes, each under 500 KB, and exits 0. Open `docs/images/dashboard.png` with the Read tool and confirm that the limits tile shows three windows, the third reading `58%` and labelled `Actions`. If the capture cannot run (no Playwright, no Chromium), stop and report it rather than committing the seed change alone with stale images.

Note that `docker compose rm -sf test-db` removes the store Task 2 left running. Task 9 brings up a fresh one.

- [ ] **Step 3: Commit**

```bash
git add tools/screenshots/seed.py docs/images/dashboard.png docs/images/issue-journey.gif
git commit -F - <<'EOF'
screenshots: seed an Actions minutes reading and regenerate the README's images

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VQwkBTve1HhNjpdVvASzSa
EOF
```

---

### Task 9: Whole-branch verification

**Files:** none changed; this task runs everything and reports.

- [ ] **Step 1: Lint and the hermetic suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files && uv run pytest -q`
Expected: all clean and PASS, with the DB tests skipped.

- [ ] **Step 2: The DB tests against a throwaway server**

```bash
docker compose --profile test up -d --wait test-db
DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest -q
docker compose rm -sf test-db
```
Expected: PASS with nothing skipped for want of a database. Use `rm -sf test-db`, never `docker compose down`.

- [ ] **Step 3: Compose under every profile**

Run: `for p in hub worker hub,worker; do COMPOSE_PROFILES=$p docker compose config --quiet && echo "$p ok"; done`
Expected: three `ok` lines.

- [ ] **Step 4: Report**

Report the commands run and their results, and the commits on the branch (`git log --oneline main..`). Do not push and do not open a pull request; that is the human's next step.

The live check needs the account owner's token, so it is theirs to run after merging and upgrading the hub:
1. Mint a classic token with only `user` ticked, with an expiry.
2. Put it and `ISSUEBOT_ACTIONS_INCLUDED_MINUTES=3000` in the hub checkout's env file.
3. Run `docker compose up -d web`.
4. Run `docker compose logs web | grep actions_minutes` and expect `actions_minutes_account` naming the account.
5. Check the dashboard: the limits tile shows the `Actions` window on that account's repositories, and its tooltip's figure matches the billing page to within a few minutes.
