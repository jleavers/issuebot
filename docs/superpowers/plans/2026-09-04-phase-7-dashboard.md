# Phase 7: Web dashboard and the per-issue log viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The blueprint's dashboard over the Phase 6 database: a Kanban of the five label columns, hero stats, two 30-day charts, the running agents, a page per issue with its runs and the transcript of every captured turn, a Symphony-shaped JSON API, `POST /api/v1/refresh` as a throttled `NOTIFY`, `GET /healthz`, `issuebot web`, and a real compose `web` service; plus the turn-log capture that makes the log viewer possible after the workspace is gone.

**Architecture:** A new `issuebot.web` package (FastAPI app factory, pure view-model builders, a transcript parser, Jinja2 templates with autoescape and `StrictUndefined`, one stylesheet, one script, vendored htmx and Chart.js) that reads PostgreSQL through Phase 6's `Database` facade, one connection per request, and writes nothing but `NOTIFY`. Turn logs reach the database through the existing sink: when the drain task takes a `run_ended` item it reads the run's `turn-N.jsonl`, `.prompt.md` and `.stderr.log` once (in a thread, capped by `agent.turnlog`) and the store inserts them into a new `run_turns` table in the same transaction as the run's row. Five new queries, two bounded fixes from the Phase 6 parked list, `issuebot web [--port] [--bind]` migrating at start like the worker, the compose `web` service on the host's loopback.

**Tech Stack:** Python 3.14, asyncio, FastAPI 0.141 / Starlette 1.6 / uvicorn 0.52 (pure Python; the phase's two runtime dependencies), `httpx2` (dev; the test client's transport), Jinja2 (already present), htmx 2.0.10 and Chart.js 4.5.1 vendored as files, `psycopg[binary]` and PostgreSQL 18 from Phase 6, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff 0.16.5.

**Spec:** `docs/superpowers/specs/2026-09-04-phase-7-dashboard-design.md` (parent: `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md`; Phase 6: `docs/superpowers/specs/2026-09-04-phase-6-persistence-design.md`).

**Pre-verified:** every code task below was built and run in a throwaway worktree before this plan was written, then replayed from these very edit blocks and file contents onto the previous task's commit: each replay reproduced the task's tree byte for byte, and the RED lines and test counts recorded here are the ones the replay produced (baseline on `main` c94bc59 plus the spec commit: 658 passed, 30 skipped without a database; 688 passed with one). Treat a different count or RED line as a finding, not as noise. The spec's prose beats this plan's code when they disagree; report the disagreement rather than resolving it silently.

## Global Constraints

Every task's requirements include this section. Every implementer and reviewer dispatch must carry it verbatim.

- Python >=3.14, `uv run` for everything. Three new dependencies, added in Task 1 and nowhere else: `uv add 'fastapi>=0.141' 'uvicorn>=0.52'` and `uv add --dev 'httpx2>=2.12'` (on 2026-09-04 they resolved to fastapi 0.141.1, starlette 1.6.0, uvicorn 0.52.4 and httpx2 2.12.0, all pure Python with 3.14 wheels; a newer patch release at execution time is fine and `uv.lock` will differ accordingly). `pyproject.toml` changes in Task 1 (the dependencies) and Task 5 (one `filterwarnings` entry) only. No `psycopg_pool`, no ORM, no Node, no CDN.
- Work on branch `phase-7-dashboard`; the spec and this plan are its first two commits. Never push to `main`, never merge or close PRs, never `rm -rf`, `git reset --hard` or `git clean -fd`; the SDD workspaces under `.superpowers/sdd/` are left for the operator to delete. Linux host: Bash, `&&` chaining.
- A Bash-level hook on this host blocks any shell command whose text contains the dot-env filename (the literal `.` + `env`, including `.example` and heredoc bodies); such files are written with Write/Edit, staged with `git add --all` after `git status --short`; say "dot-env" in commit messages and reports.
- The ruff-format pre-commit hook (v0.16.5) reflows Python fences inside docs/**/*.md; the ```python fences in this plan are whole files copied from ruff-formatted sources and the Python fragments in edit blocks sit in untagged fences, so neither is rewritten; re-`git add` after `pre-commit run --all-files` if anything moves. Two directories are excluded from every pre-commit hook because their files are kept byte-for-byte as their authors wrote them: `tests/fixtures/runs/` (Task 1) and `src/issuebot/web/static/vendor/` (Task 6); `end-of-file-fixer` would otherwise append a newline to the recorded prompt and to `htmx.min.js` and break their checksums.
- ruff rules E F I UP B N SIM RUF, target py314, line length 100. SIM300 ranks literal > ALL_CAPS name > other expression and flags a comparison whose left side ranks higher; apply ruff's fix, never suppress, never a per-file ignore. SIM105 wants `contextlib.suppress` over `try/except/pass`. N818: exception classes end in `Error`. RUF022 sorts `__all__`; RUF005 wants `[*a, *b]` over list concatenation; RUF100 flags an unused `noqa`. UP037: no quoted annotations. The formatter writes `except A, B:` without parentheses only when there is no `as` clause (PEP 758); `except (OSError, ValueError) as exc:` keeps its parentheses. Syntax-check Python fences with `uv run python`, never the system `python3`. isort puts `from fakes...` imports in the first-party block before `from issuebot...` (`src = ["src", "tests"]`).
- Commit messages: conventional prefix plus the attribution trailer the harness requires as the last lines (blank line before them). Before every commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`; before every push also `uv run pre-commit run --all-files`.
- Tests hermetic by default: `FakeGitHub`, `tests/fakes/claude`, `tests/fakes/gh`, `tmp_path`, `tests/fakes/database.py` (`FakeDatabase`, `FakeQueries`, `FakeStore`, `FakeListener`; shared by the CLI and web tests from Task 4 on), `tests/fakes/web.py` (row builders, a clock, the `TestClient` harness; Task 5 on), `fastapi.testclient.TestClient` over httpx2 (no socket). `tests/` is on `sys.path` through `tests/conftest.py`, so shared doubles are imported as `from fakes.database import ...`. Tests marked by the `db_url` fixture need a real PostgreSQL: they read `DATABASE_URL` (captured at conftest import, before the `clean_env` fixture clears it), create one schema per test and drop it afterwards, and are **skipped, and reported as skipped, when the variable is unset**. Run the suite both ways before every commit: `uv run pytest -q` (expect the stated `passed, skipped`) and `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot uv run pytest -q` (expect the stated `passed`, no skips), with the compose database up: `ISSUEBOT_DB_PORT=5440 docker compose up -d db` from the repository root (port 5432 is taken on this host; 5440 is free; the container is `issuebot-db-1`). Run long test commands under `timeout`. `tests/fixtures/runs/20260904T202535Z-0964cd/` holds a real recorded turn (scratch issue #7) whose SHA-256 sums are given in Task 1; never edit it.
- Frozen inputs, used as they are: `issuebot.orchestrator`, `issuebot.github`, `issuebot.agent` except the new `turnlog` module, `EVENT_KINDS` (no kind is added), `ServerSettings` (no field is added), `issuebot.notifications`, the Phase 6 migration file `0001_initial.sql` (a new file is added, nothing is edited).
- Package rules: `issuebot.web` imports `config`, `db`, `github` (`StateLabel`) and `log` only; `issuebot.db` imports `config`, `events`, `github`, `log` and now `agent.turnlog` (a pure file-reading module); `orchestrator` and `agent` never import `db` or `web`; `cli` wires everything. The sink still does no I/O in `handle`, `record_issues` or `record_snapshot`; the capture happens on the drain task, in a thread, once per `run_ended` item, before its first write attempt. The web process writes nothing to any table (its one write is `NOTIFY`), so the worker stays the single writer and `seen_at` needs no change.
- Everything the pages render is text through Jinja2 autoescape (`autoescape=True`, `StrictUndefined`); no template uses `|safe` on data; every `href` built from data passes the `href` filter (`safe_href`: `https://` URLs only); all script and style live in files (the CSP has neither `unsafe-inline` nor `unsafe-eval`); raw turn files are served as `text/plain; charset=utf-8` with `X-Content-Type-Options: nosniff`; path parameters are typed (`int`) or pattern-checked (`run_id` matches `RUN_ID_PATTERN`) and an invalid one is a 404, never a 422.
- Secrets: the database URL carries the password and is never logged or printed; log lines carry `describe(url)`; every error message the web returns comes from the facade and has passed `redact`; tests assert `"s3cret" not in` the output. The web container gets `DATABASE_URL` and the workflow file only: no `env_file`, no GitHub, Claude or Slack credential, no workspace volume.
- A live process must not run under the Bash tool's `run_in_background` (the harness kills that shell after a few minutes); start it detached inside a subshell, `( export ...; setsid nohup ... >> log 2>&1 < /dev/null & )`, export again for the foreground commands (`&` binds looser than `&&`), and record the python pid with `pgrep -f '^/home/jleavers/_dev/issuebot/\.venv/bin/python.*\.venv/bin/issuebot ...'` (the pattern must anchor on the interpreter or it also matches the `bash -c` wrapper and the `uv` parent). A `Monitor` on a log does not wake an idle session; wait with `run_in_background` and a bounded `timeout N bash -c 'until grep -q ...; do sleep 10; done'`. In the worktree of another checkout `docker compose exec` resolves to a different project name; address the database container as `docker exec -i issuebot-db-1 psql -U issuebot -d issuebot`.
- The live-check task runs against jleavers/issuebot-scratch (issues #1, #3 and #5 closed `complete`; #7 "Add a power function" in `review` with PR #8 open and mergeable; host dirs `~/issuebot-scratch` and `~/issuebot-workspaces`) with the compose database on port 5440 holding the Phase 6 history (4 issues, 1 run, 11 events, a snapshot), with `GH_TOKEN`, `SLACK_WEBHOOK_URL` and `DATABASE_URL` exported in the same command (from `gh auth token`, the operator's file `~/issuebot-scratch/slack-webhook` (mode 600), and the compose database), spends real Claude budget under the operator's subscription login (about $0.90 per run; no `ANTHROPIC_API_KEY`), and never prints any of the three values. The operator, not the executor, merges the scratch PR the check needs merged and opens the dashboard in a browser.
- The fake `claude` reads `CLAUDE_FAKE_*` only; in orchestrator tests a worker exit needs several loop turns to become visible; the sink tests use a `settle()` that loops `sleep(0)` twenty times.

---

## File map

| Path | Responsibility | Task |
|---|---|---|
| `pyproject.toml`, `uv.lock` | `fastapi`, `uvicorn`, dev `httpx2`; a pytest `filterwarnings` entry | 1, 5 |
| `.pre-commit-config.yaml` | `exclude:` for the recorded fixture and the vendored files | 1, 6 |
| `src/issuebot/agent/turnlog.py` | `TurnCapture`, `capture_turns(log_dir)`, the caps, `OMITTED_TYPE` | 1 |
| `tests/fixtures/runs/20260904T202535Z-0964cd/turn-1.*` | the real recorded turn of scratch issue #7 (copied, never edited) | 1 |
| `tests/test_agent_turnlog.py` | the capture against the sample and synthetic files | 1 |
| `src/issuebot/db/migrations/0002_run_turns.sql` | the `run_turns` table | 2 |
| `src/issuebot/db/store.py` | `Store.apply_event(event, turns=())`, `INSERT_TURN`, `turn_row` | 2 |
| `src/issuebot/db/sink.py` | the capture step on the drain task (`capture=` seam) | 2 |
| `tests/test_db_migrate.py`, `tests/test_db_database.py` | schema version 2 | 2 |
| `tests/test_db_store.py`, `tests/test_db_sink.py`, `tests/test_cli.py` | the turns writes; the capture; `FakeStore.apply_event(event, turns=())` | 2 |
| `src/issuebot/db/queries.py`, `src/issuebot/db/__init__.py` | `TurnSummaryRow`, `TurnRow`, `MAX_WINDOW_DAYS`, the five queries, the `issues_by_state` fix; re-exports (also `OMITTED_TYPE`, `TurnCapture` in Task 4) | 3, 4 |
| `tests/test_db_queries.py` | the five queries and the unknown-role skip | 3 |
| `tests/fakes/__init__.py`, `tests/fakes/database.py` | the shared `FakeDatabase` and friends (moved out of `test_cli.py`, `FakeQueries` grown) | 4 |
| `src/issuebot/web/__init__.py`, `src/issuebot/web/transcript.py` | the package; `parse_transcript` | 4, 5, 6 |
| `tests/test_web_transcript.py` | blocks from the sample and synthetic lines | 4 |
| `src/issuebot/web/views.py` | constants, the API documents, `describe_event`, `safe_href`, `window_days`, `worker_status`; template filters and `dashboard_context` (Task 6); `captured_turns` (Task 7) | 5, 6, 7 |
| `src/issuebot/web/app.py` | `create_app`: the API, `/healthz`, error envelopes, headers; pages, partial, raw files, static (Task 6) | 5, 6 |
| `tests/fakes/web.py`, `tests/test_web_app.py` | row builders, `Harness`; the API and health check tests | 5 |
| `src/issuebot/web/templates/*.html`, `partials/dashboard.html` | base, index, issue, turn, error, the live region | 6 |
| `src/issuebot/web/static/app.css`, `app.js`, `vendor/*` | the stylesheet, the chart and poll-button script, htmx 2.0.10, Chart.js 4.5.1, licences, `README.md` | 6 |
| `tests/test_web_pages.py` | the pages, the partial, the raw files, static, the filters | 6 |
| `tests/test_web_app_db.py` | the API contract and pages against a seeded database | 7 |
| `src/issuebot/cli.py` | `web [--port] [--bind]`, `_uvicorn_serve`/`_serve`, `stats` on `state_counts`, the `--days` ceiling | 8 |
| `compose.yaml`, the dot-env example | the `web` service; `ISSUEBOT_WEB_PORT` | 8 |
| `CLAUDE.md`, `README.md`, the roadmap, the Phase 6 spec | documentation and amendment notes | 9 |
| (scratch repository, compose database, a browser) | live check | 10 |

Test counts along the way (`uv run pytest -q` without a database / with `DATABASE_URL`):

| After task | Without a database | With a database |
|---|---|---|
| (the spec commit) | 658 passed, 30 skipped | 688 passed |
| 1 | 674 passed, 30 skipped | 704 passed |
| 2 | 679 passed, 34 skipped | 713 passed |
| 3 | 679 passed, 40 skipped | 719 passed |
| 4 | 696 passed, 40 skipped | 736 passed |
| 5 | 751 passed, 40 skipped | 791 passed |
| 6 | 781 passed, 40 skipped | 821 passed |
| 7 | 781 passed, 46 skipped | 827 passed |
| 8, 9 | 786 passed, 46 skipped | 832 passed |

---

### Task 1: Dependencies, the turn-file capture and the sample fixture

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (through `uv add`), `.pre-commit-config.yaml`
- Create: `src/issuebot/agent/turnlog.py`, `tests/fixtures/runs/20260904T202535Z-0964cd/turn-1.jsonl`, `turn-1.prompt.md`, `turn-1.stderr.log`
- Test: `tests/test_agent_turnlog.py`

**Interfaces:**
- Consumes: nothing new (`pathlib`, `json`, `re`).
- Produces: `issuebot.agent.turnlog.TurnCapture` (frozen dataclass: `turn_number`, `model`, `subtype`, `is_error`, `num_turns`, `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`, `cost_usd`, `duration_ms`, `result_text`, `prompt`, `prompt_bytes`, `stream`, `stream_bytes`, `stream_lines`, `omitted_lines`, `stderr`, `stderr_bytes`, `truncated`); `capture_turns(log_dir: Path) -> list[TurnCapture]`; constants `PROMPT_LIMIT`, `LINE_LIMIT`, `STREAM_LIMIT`, `STDERR_LIMIT`, `RESULT_TEXT_LIMIT`, `OMITTED_TYPE = "issuebot_omitted"`, `TURN_FILE`.

Spec: §4.1.

- [ ] **Step 1: Add the dependencies**

Run: `uv add 'fastapi>=0.141' 'uvicorn>=0.52' && uv add --dev 'httpx2>=2.12' && uv run python -c "import fastapi, uvicorn, starlette, httpx2; print(fastapi.__version__, uvicorn.__version__, starlette.__version__, httpx2.__version__)"`
Expected: `pyproject.toml` gains exactly these lines (alphabetical within each list), and the print reads `0.141.1 0.52.4 1.6.0 2.12.0` or newer patches:

```toml
dependencies = [
  "fastapi>=0.141",
  "jinja2>=3.1",
  "psycopg[binary]>=3.3",
  "pydantic>=2.12",
  "pyyaml>=6.0",
  "structlog>=25.1",
  "uvicorn>=0.52",
]
```

```toml
dev = [
  "httpx2>=2.12",
  "pre-commit>=4.0",
  "pytest>=8.4",
  "pytest-asyncio>=1.0",
  "ruff==0.16.5",
]
```

Starlette 1.6 prefers `httpx2` for its test client and warns when only `httpx` is installed, which is why the dev dependency is `httpx2`.

- [ ] **Step 2: Copy the recorded turn and exclude it from the pre-commit hooks**

The sample is the real first (and only) turn of scratch issue #7, written by the Phase 6 live check. Copy the three files (never edit them; the prompt has no trailing newline and `end-of-file-fixer` would add one):

```bash
mkdir -p tests/fixtures/runs/20260904T202535Z-0964cd && SRC=~/issuebot-workspaces/issuebot-scratch-7/.issuebot/runs/20260904T202535Z-0964cd && [ -d "$SRC" ] || SRC=~/issuebot-scratch/sample-run-20260904T202535Z-0964cd && cp "$SRC"/turn-1.jsonl "$SRC"/turn-1.prompt.md "$SRC"/turn-1.stderr.log tests/fixtures/runs/20260904T202535Z-0964cd/ && sha256sum tests/fixtures/runs/20260904T202535Z-0964cd/* && wc -c tests/fixtures/runs/20260904T202535Z-0964cd/*
```

Expected (the backup directory under `~/issuebot-scratch` holds identical copies in case the workspace has been removed):

```
b952a907a27d2f16eda334a63ed3c20ae8266bd0f051234e27627199912245c3  turn-1.jsonl      (115429 bytes)
5bbab72d6f61e41581e96cacc8466a0b79edfa9ba5075c9dd3b2bc7955ee380e  turn-1.prompt.md  (10106 bytes)
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  turn-1.stderr.log (0 bytes)
```

In `.pre-commit-config.yaml` replace

```yaml
repos:
```

with

```yaml
# Recorded agent runs are kept byte-for-byte as the runner wrote them.
exclude: ^tests/fixtures/runs/
repos:
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_agent_turnlog.py`:

```python
"""Tests for the turn-file capture (hermetic: the real sample fixture and synthetic files)."""

import json
from pathlib import Path

import pytest

from issuebot.agent import turnlog
from issuebot.agent.turnlog import OMITTED_TYPE, TurnCapture, capture_turns

SAMPLE = Path(__file__).parent / "fixtures" / "runs" / "20260904T202535Z-0964cd"

INIT = json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5"})
RESULT = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "duration_ms": 1234,
        "total_cost_usd": 0.25,
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30,
            "output_tokens": 40,
        },
        "result": "done",
    }
)


def assistant(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def write_turn(log_dir: Path, number: int, lines: list[str], **files: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"turn-{number}.jsonl").write_text("".join(f"{line}\n" for line in lines))
    for suffix, text in files.items():
        (log_dir / f"turn-{number}.{suffix}").write_text(text)


def only(captures: list[TurnCapture]) -> TurnCapture:
    assert len(captures) == 1
    return captures[0]


# --- the real sample -----------------------------------------------------------------------


def test_the_sample_turn_is_captured_whole() -> None:
    capture = only(capture_turns(SAMPLE))
    assert capture.turn_number == 1
    assert (capture.stream_lines, capture.stream_bytes) == (95, 115429)
    assert (capture.omitted_lines, capture.truncated) == (0, False)
    assert capture.stream == (SAMPLE / "turn-1.jsonl").read_text(encoding="utf-8")
    assert all(json.loads(line) for line in capture.stream.splitlines())
    assert (capture.model, capture.subtype, capture.is_error) == ("claude-opus-5", "success", False)
    assert (capture.num_turns, capture.duration_ms) == (19, 201719)
    assert (capture.input_tokens, capture.cache_creation_input_tokens) == (38, 23100)
    assert (capture.cache_read_input_tokens, capture.output_tokens) == (490200, 8425)
    assert capture.cost_usd == pytest.approx(0.89759225)
    assert capture.result_text is not None and capture.result_text.startswith("Done. Issue #7")
    assert capture.prompt_bytes == 10106
    assert capture.prompt.startswith("You are working on GitHub issue `issuebot-scratch-7`")
    assert (capture.stderr, capture.stderr_bytes) == ("", 0)


# --- caps ------------------------------------------------------------------------------------


def test_an_oversized_line_becomes_a_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turnlog, "LINE_LIMIT", len(RESULT))  # INIT and RESULT fit, big does not
    big = json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": "x" * len(RESULT)}]}}
    )
    write_turn(tmp_path, 1, [INIT, big, RESULT])
    capture = only(capture_turns(tmp_path))
    lines = capture.stream.splitlines()
    assert (capture.stream_lines, capture.omitted_lines, capture.truncated) == (3, 1, False)
    assert json.loads(lines[0])["subtype"] == "init"
    stub = json.loads(lines[1])
    assert stub == {"type": OMITTED_TYPE, "original_type": "user", "bytes": len(big)}
    assert json.loads(lines[2])["type"] == "result"
    assert capture.stream_bytes == len(INIT) + len(big) + len(RESULT) + 3


def test_an_oversized_unparseable_line_has_no_original_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turnlog, "LINE_LIMIT", 20)
    write_turn(tmp_path, 1, ["not json " * 10])
    capture = only(capture_turns(tmp_path))
    stub = json.loads(capture.stream)
    assert (stub["type"], stub["original_type"], capture.omitted_lines) == (OMITTED_TYPE, None, 1)


def test_the_head_cap_keeps_the_result_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turnlog, "STREAM_LIMIT", len(INIT) + 1 + 2 * (len(assistant("a" * 50)) + 1))
    chatter = [assistant("a" * 50) for _ in range(10)]
    write_turn(tmp_path, 1, [INIT, *chatter, RESULT])
    capture = only(capture_turns(tmp_path))
    lines = capture.stream.splitlines()
    assert capture.truncated is True
    assert (capture.stream_lines, len(lines)) == (12, 4)
    assert json.loads(lines[0])["subtype"] == "init"
    assert json.loads(lines[-1])["type"] == "result"
    assert capture.num_turns == 3  # the summary reads the file, not the capped stream


def test_a_kept_result_line_is_not_appended_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turnlog, "STREAM_LIMIT", len(INIT) + 1 + len(RESULT) + 1)
    write_turn(tmp_path, 1, [INIT, RESULT, assistant("trailing " * 20)])
    capture = only(capture_turns(tmp_path))
    lines = capture.stream.splitlines()
    assert capture.truncated is True
    assert [json.loads(line)["type"] for line in lines] == ["system", "result"]


def test_prompt_head_and_stderr_tail_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turnlog, "PROMPT_LIMIT", 16)
    monkeypatch.setattr(turnlog, "STDERR_LIMIT", 16)
    write_turn(tmp_path, 1, [INIT], **{"prompt.md": "p" * 40, "stderr.log": "x" * 20 + "TAIL"})
    capture = only(capture_turns(tmp_path))
    assert (capture.prompt, capture.prompt_bytes) == ("p" * 16, 40)
    assert (capture.stderr, capture.stderr_bytes) == ("x" * 12 + "TAIL", 24)


def test_result_text_is_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turnlog, "RESULT_TEXT_LIMIT", 5)
    write_turn(tmp_path, 1, [json.dumps({"type": "result", "result": "abcdefgh"})])
    assert only(capture_turns(tmp_path)).result_text == "abcde"


# --- files and directories -------------------------------------------------------------------


def test_missing_prompt_and_stderr_files_are_empty(tmp_path: Path) -> None:
    write_turn(tmp_path, 1, [INIT, RESULT])
    capture = only(capture_turns(tmp_path))
    assert (capture.prompt, capture.prompt_bytes, capture.stderr, capture.stderr_bytes) == (
        "",
        0,
        "",
        0,
    )


def test_a_missing_directory_gives_no_captures(tmp_path: Path) -> None:
    assert capture_turns(tmp_path / "nope") == []


def test_an_unreadable_stream_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "turn-1.jsonl").mkdir(parents=True)  # a directory: read_bytes raises OSError
    write_turn(tmp_path, 2, [INIT, RESULT])
    assert [capture.turn_number for capture in capture_turns(tmp_path)] == [2]


def test_turns_sort_numerically_and_other_files_are_ignored(tmp_path: Path) -> None:
    write_turn(tmp_path, 10, [INIT])
    write_turn(tmp_path, 2, [INIT])
    (tmp_path / "notes.txt").write_text("ignored")
    (tmp_path / "turn-x.jsonl").write_text("ignored")
    assert [capture.turn_number for capture in capture_turns(tmp_path)] == [2, 10]


def test_an_empty_stream_file(tmp_path: Path) -> None:
    write_turn(tmp_path, 1, [])
    capture = only(capture_turns(tmp_path))
    assert (capture.stream, capture.stream_lines, capture.stream_bytes) == ("", 0, 0)
    assert (capture.truncated, capture.model, capture.subtype) == (False, None, None)


# --- the summary -----------------------------------------------------------------------------


def test_unparseable_lines_are_kept_and_counted(tmp_path: Path) -> None:
    write_turn(tmp_path, 1, [INIT, "not json", RESULT])
    capture = only(capture_turns(tmp_path))
    assert capture.stream.splitlines()[1] == "not json"
    assert (capture.stream_lines, capture.omitted_lines, capture.subtype) == (3, 0, "success")


def test_no_result_line_gives_null_summary_columns(tmp_path: Path) -> None:
    write_turn(tmp_path, 1, [INIT, assistant("hello")])
    capture = only(capture_turns(tmp_path))
    assert capture.model == "claude-opus-5"
    assert (capture.subtype, capture.is_error, capture.num_turns) == (None, None, None)
    assert (capture.input_tokens, capture.output_tokens, capture.cost_usd) == (None, None, None)
    assert (capture.duration_ms, capture.result_text) == (None, None)


def test_values_of_the_wrong_type_are_ignored(tmp_path: Path) -> None:
    bad = json.dumps(
        {
            "type": "result",
            "subtype": 7,
            "is_error": "no",
            "num_turns": "19",
            "duration_ms": 1.5,
            "total_cost_usd": True,
            "usage": "lots",
            "result": ["a"],
        }
    )
    write_turn(tmp_path, 1, [bad])
    capture = only(capture_turns(tmp_path))
    assert (capture.subtype, capture.is_error, capture.num_turns) == (None, None, None)
    assert (capture.duration_ms, capture.cost_usd, capture.result_text) == (None, None, None)
    assert (capture.input_tokens, capture.output_tokens) == (None, None)


def test_the_last_result_and_init_lines_win(tmp_path: Path) -> None:
    second_init = json.dumps({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})
    second_result = json.dumps({"type": "result", "subtype": "error_during_execution"})
    write_turn(tmp_path, 1, [INIT, RESULT, second_init, second_result])
    capture = only(capture_turns(tmp_path))
    assert (capture.model, capture.subtype) == ("claude-sonnet-5", "error_during_execution")
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `timeout 120 uv run pytest tests/test_agent_turnlog.py -q`
Expected: collection fails with `ImportError: cannot import name 'turnlog' from 'issuebot.agent'` (the module does not exist yet).

- [ ] **Step 5: The capture module**

Create `src/issuebot/agent/turnlog.py`:

```python
"""Capture a run's turn files (stream-json, prompt, stderr) for the database, capped.

The runner writes ``turn-N.jsonl``, ``turn-N.prompt.md`` and ``turn-N.stderr.log`` under a run's
log directory. ``capture_turns`` reads them once, applies the size caps and parses the summary
the dashboard shows. It never raises: an unreadable directory yields nothing, an unreadable
stream file skips its turn, a missing prompt or stderr file is empty.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROMPT_LIMIT = 256 * 1024
LINE_LIMIT = 64 * 1024
STREAM_LIMIT = 2 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
RESULT_TEXT_LIMIT = 4 * 1024
OMITTED_TYPE = "issuebot_omitted"
TURN_FILE = re.compile(r"^turn-(\d+)\.jsonl$")


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnCapture:
    """One turn's files as stored in ``run_turns``: the capped texts, their sizes, the summary."""

    turn_number: int
    model: str | None
    subtype: str | None
    is_error: bool | None
    num_turns: int | None
    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    duration_ms: int | None
    result_text: str | None
    prompt: str
    prompt_bytes: int
    stream: str
    stream_bytes: int
    stream_lines: int
    omitted_lines: int
    stderr: str
    stderr_bytes: int
    truncated: bool


def capture_turns(log_dir: Path) -> list[TurnCapture]:
    """Every turn-N.jsonl under ``log_dir`` with its prompt and stderr, capped; never raises."""
    try:
        entries = list(log_dir.iterdir())
    except OSError:
        return []
    numbered: list[tuple[int, Path]] = []
    for entry in entries:
        match = TURN_FILE.match(entry.name)
        if match is not None:
            numbered.append((int(match.group(1)), entry))
    captures: list[TurnCapture] = []
    for number, path in sorted(numbered):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        prompt = _read(log_dir / f"turn-{number}.prompt.md")
        stderr = _read(log_dir / f"turn-{number}.stderr.log")
        captures.append(_capture(number, raw, prompt, stderr))
    return captures


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _capture(turn_number: int, raw: bytes, prompt: bytes, stderr: bytes) -> TurnCapture:
    lines = [line for line in raw.splitlines() if line.strip()]
    messages = [_message(line) for line in lines]
    stored: list[bytes] = []
    omitted = 0
    for line, message in zip(lines, messages, strict=True):
        if len(line) > LINE_LIMIT:
            omitted += 1
            line = _stub(message, len(line))
        stored.append(line)
    kept: list[bytes] = []
    total = 0
    truncated = False
    for line in stored:
        if total + len(line) + 1 > STREAM_LIMIT:
            truncated = True
            break
        kept.append(line)
        total += len(line) + 1
    result_index = _last_index(messages, "result")
    if truncated and result_index is not None and result_index >= len(kept):
        kept.append(stored[result_index])
    init_index = _last_index(messages, "system", subtype="init")
    init = messages[init_index] if init_index is not None else {}
    result = messages[result_index] if result_index is not None else {}
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    result_text = _string(result.get("result"))
    return TurnCapture(
        turn_number=turn_number,
        model=_string(init.get("model")),
        subtype=_string(result.get("subtype")),
        is_error=_bool(result.get("is_error")),
        num_turns=_int(result.get("num_turns")),
        input_tokens=_int(usage.get("input_tokens")),
        cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cost_usd=_float(result.get("total_cost_usd")),
        duration_ms=_int(result.get("duration_ms")),
        result_text=result_text[:RESULT_TEXT_LIMIT] if result_text is not None else None,
        prompt=prompt[:PROMPT_LIMIT].decode("utf-8", errors="replace"),
        prompt_bytes=len(prompt),
        stream=(b"\n".join(kept) + b"\n").decode("utf-8", errors="replace") if kept else "",
        stream_bytes=len(raw),
        stream_lines=len(lines),
        omitted_lines=omitted,
        stderr=stderr[-STDERR_LIMIT:].decode("utf-8", errors="replace"),
        stderr_bytes=len(stderr),
        truncated=truncated,
    )


def _message(line: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _stub(message: dict[str, Any] | None, size: int) -> bytes:
    original = _string(message.get("type")) if message is not None else None
    return json.dumps({"type": OMITTED_TYPE, "original_type": original, "bytes": size}).encode()


def _last_index(
    messages: list[dict[str, Any] | None], kind: str, *, subtype: str | None = None
) -> int | None:
    found = None
    for index, message in enumerate(messages):
        if message is None or message.get("type") != kind:
            continue
        if subtype is not None and message.get("subtype") != subtype:
            continue
        found = index
    return found


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
```

- [ ] **Step 6: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 120 uv run pytest tests/test_agent_turnlog.py -q && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `16 passed`; then `674 passed, 30 skipped`; then `704 passed`. `uv run pre-commit run --all-files` leaves the fixture directory untouched (the `exclude:` line) and passes.

- [ ] **Step 7: Commit**

```bash
git status --short && git add --all && git commit -m "feat: fastapi, uvicorn and httpx2; capture a run's turn files with caps (agent.turnlog)" -m "<trailer>"
```

(`--all`, not the fixture path, because the dot-env hook is not involved but the pattern keeps every commit in this plan the same; the `-m "<trailer>"` stands for the attribution trailer the harness requires.)

---

### Task 2: The `run_turns` migration, the store and the sink capture

**Files:**
- Create: `src/issuebot/db/migrations/0002_run_turns.sql`
- Modify: `src/issuebot/db/store.py`, `src/issuebot/db/sink.py`
- Test: `tests/test_db_migrate.py`, `tests/test_db_database.py`, `tests/test_db_store.py`, `tests/test_db_sink.py`, `tests/test_cli.py` (the `FakeStore` signature only)

**Interfaces:**
- Consumes: Task 1's `TurnCapture` and `capture_turns`; Phase 6's `PostgresStore`, `PostgresSink`, `RunEnded.log_dir`.
- Produces: `Store.apply_event(event: Event, turns: Sequence[TurnCapture] = ())` (the protocol and `PostgresStore`; `run_ended` inserts the turns in its transaction with `ON CONFLICT (run_id, turn_number) DO UPDATE`); `INSERT_TURN`; `turn_row(run_id, turn) -> dict`; `PostgresSink(store, *, sleep=, now=, description=, capture: Callable[[Path], list[TurnCapture]] = capture_turns)`; log events `db_turns_captured` (`run_id`, `turns`, `stream_bytes`) and `db_turns_capture_failed` (`run_id`, `log_dir`, `error`); schema version 2.

Spec: §3.1, §4.2, §4.3.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py` (edit 1 of 3) replace

```
from issuebot.agent import RunResult, SessionRecord, WorkspaceManager
from issuebot.cli import (
```

with

```
from issuebot.agent import RunResult, SessionRecord, WorkspaceManager
from issuebot.agent.turnlog import TurnCapture
from issuebot.cli import (
```

In `tests/test_cli.py` (edit 2 of 3) replace

```
        self.events: list[Event] = []
        self.issues: list[list[IssueSnapshot]] = []
```

with

```
        self.events: list[Event] = []
        self.turns: list[list[TurnCapture]] = []
        self.issues: list[list[IssueSnapshot]] = []
```

In `tests/test_cli.py` (edit 3 of 3) replace

```
    async def apply_event(self, event: Event) -> None:
        self.events.append(event)
```

with

```
    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None:
        self.events.append(event)
        self.turns.append(list(turns))
```

In `tests/test_db_database.py` replace

```
    assert (before.schema_version, before.latest_version, before.behind) == (0, 1, True)
    result = await database.migrate()
    assert result.applied == ("0001_initial",)
    after = await database.probe()
    assert (after.schema_version, after.behind, after.ahead) == (1, False, False)
```

with

```
    assert (before.schema_version, before.latest_version, before.behind) == (0, 2, True)
    result = await database.migrate()
    assert result.applied == ("0001_initial", "0002_run_turns")
    after = await database.probe()
    assert (after.schema_version, after.behind, after.ahead) == (2, False, False)
```

In `tests/test_db_migrate.py` (edit 1 of 4) replace

```
TABLES = {"issues", "runs", "events", "runtime_snapshot", "schema_migrations"}
```

with

```
TABLES = {"issues", "runs", "events", "runtime_snapshot", "run_turns", "schema_migrations"}
```

In `tests/test_db_migrate.py` (edit 2 of 4) replace

```
def test_the_package_ships_the_initial_migration() -> None:
    migrations = discover_migrations()
    assert [m.label for m in migrations] == ["0001_initial"]
    assert migrations[0].version == 1
    assert "CREATE TABLE issues" in migrations[0].sql
    assert "CREATE TABLE runtime_snapshot" in migrations[0].sql
```

with

```
def test_the_package_ships_the_two_migrations() -> None:
    migrations = discover_migrations()
    assert [m.label for m in migrations] == ["0001_initial", "0002_run_turns"]
    assert [m.version for m in migrations] == [1, 2]
    assert "CREATE TABLE issues" in migrations[0].sql
    assert "CREATE TABLE runtime_snapshot" in migrations[0].sql
    assert "CREATE TABLE run_turns" in migrations[1].sql
```

In `tests/test_db_migrate.py` (edit 3 of 4) replace

```
async def test_migrate_applies_the_initial_migration_once(db_url: str) -> None:
    first = await migrate(db_url)
    assert (first.applied, first.version) == (("0001_initial",), 1)
    second = await migrate(db_url)
    assert (second.applied, second.version) == ((), 1)
    conn = await connect(db_url)
    try:
        assert await _tables(conn) == TABLES
        assert await schema_version(conn) == 1
```

with

```
async def test_migrate_applies_every_migration_once(db_url: str) -> None:
    first = await migrate(db_url)
    assert (first.applied, first.version) == (("0001_initial", "0002_run_turns"), 2)
    second = await migrate(db_url)
    assert (second.applied, second.version) == ((), 2)
    conn = await connect(db_url)
    try:
        assert await _tables(conn) == TABLES
        assert await schema_version(conn) == 2
```

In `tests/test_db_migrate.py` (edit 4 of 4) replace

```
        with pytest.raises(MigrationError, match=r"schema version 7 is newer .* knows \(1\)"):
```

with

```
        with pytest.raises(MigrationError, match=r"schema version 7 is newer .* knows \(2\)"):
```

In `tests/test_db_sink.py` (edit 1 of 9) replace

```
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
```

with

```
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
```

In `tests/test_db_sink.py` (edit 2 of 9) replace

```

from issuebot.db import StoreError, StoreUnavailableError
```

with

```

from issuebot.agent.turnlog import TurnCapture, capture_turns
from issuebot.db import StoreError, StoreUnavailableError
```

In `tests/test_db_sink.py` (edit 3 of 9) replace

```
from issuebot.events import Blocked, Event, EventBus, LogSink, StateChanged
```

with

```
from issuebot.events import Blocked, Event, EventBus, LogSink, RunEnded, StateChanged
```

In `tests/test_db_sink.py` (edit 4 of 9) replace

```
DESCRIPTION = "postgresql://issuebot@db.example/issuebot"

```

with

```
DESCRIPTION = "postgresql://issuebot@db.example/issuebot"
SAMPLE = Path(__file__).parent / "fixtures" / "runs" / "20260904T202535Z-0964cd"

```

In `tests/test_db_sink.py` (edit 5 of 9) replace

```
    async def apply_event(self, event: Event) -> None:
        await self._gate()
        self.calls.append(("event", event))
```

with

```
    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None:
        await self._gate()
        self.calls.append(("event", event, tuple(turns)))
```

In `tests/test_db_sink.py` (edit 6 of 9) replace

```
class Harness:
    def __init__(self) -> None:
        self.store = FakeStore()
```

with

```
class Harness:
    def __init__(self, capture: Callable[[Path], list[TurnCapture]] = capture_turns) -> None:
        self.store = FakeStore()
```

In `tests/test_db_sink.py` (edit 7 of 9) replace

```
            self.store, sleep=self.sleep, now=self.clock, description=DESCRIPTION
```

with

```
            self.store, sleep=self.sleep, now=self.clock, description=DESCRIPTION, capture=capture
```

In `tests/test_db_sink.py` (edit 8 of 9) replace

```
    return Blocked(issue_number=number, issue_identifier=f"repo-{number}", reason=reason)

```

with

```
    return Blocked(issue_number=number, issue_identifier=f"repo-{number}", reason=reason)


def run_ended(log_dir: str | None) -> RunEnded:
    return RunEnded(
        issue_number=7,
        issue_identifier="repo-7",
        run_id="20260904T202535Z-0964cd",
        outcome="succeeded",
        error=None,
        turns=1,
        input_tokens=513338,
        output_tokens=8425,
        cost_usd=0.8976,
        duration_s=205.0,
        log_dir=log_dir,
    )

```

In `tests/test_db_sink.py` (edit 9 of 9) replace

```

async def test_a_bare_event_has_no_issue_number(h: Harness) -> None:
```

with

```

# --- turn capture -----------------------------------------------------------------------------


async def test_run_ended_captures_the_turn_files(h: Harness, tmp_path: Path) -> None:
    shutil.copytree(SAMPLE, tmp_path / "run")
    h.sink.handle(run_ended(str(tmp_path / "run")))
    h.sink.start()
    await h.sink.close()
    (call,) = h.store.calls
    assert call[0] == "event" and call[1].kind == "run_ended"
    (turn,) = call[2]
    assert (turn.turn_number, turn.model, turn.stream_lines) == (1, "claude-opus-5", 95)
    captured = h.logged("db_turns_captured")[0]
    assert (captured["run_id"], captured["turns"]) == ("20260904T202535Z-0964cd", 1)
    assert captured["stream_bytes"] == 115429


async def test_a_missing_log_dir_gives_no_captures(h: Harness, tmp_path: Path) -> None:
    h.sink.handle(run_ended(str(tmp_path / "gone")))
    h.sink.start()
    await h.sink.close()
    assert h.store.calls[0][2] == ()
    assert h.logged("db_turns_captured")[0]["turns"] == 0


async def test_run_ended_without_a_log_dir_never_captures() -> None:
    calls: list[Path] = []

    def capture(log_dir: Path) -> list[TurnCapture]:
        calls.append(log_dir)
        return []

    h = Harness(capture)
    h.sink.handle(run_ended(None))
    h.sink.handle(blocked(1))
    h.sink.start()
    await h.sink.close()
    assert calls == []
    assert [call[1].kind for call in h.store.calls] == ["run_ended", "blocked"]
    assert h.logged("db_turns_captured") == []


async def test_a_capture_failure_is_logged_and_the_event_still_written() -> None:
    def capture(log_dir: Path) -> list[TurnCapture]:
        raise RuntimeError("disk on fire")

    h = Harness(capture)
    h.sink.handle(run_ended("/workspaces/repo-7/.issuebot/runs/x"))
    h.sink.start()
    await h.sink.close()
    assert h.store.calls[0][2] == ()
    assert (h.sink.written, h.sink.failed) == (1, 0)
    failed = h.logged("db_turns_capture_failed")[0]
    assert failed["log_dir"] == "/workspaces/repo-7/.issuebot/runs/x"
    assert failed["error"] == "RuntimeError: disk on fire"


async def test_the_capture_runs_once_even_when_the_write_is_retried(tmp_path: Path) -> None:
    calls: list[Path] = []

    def capture(log_dir: Path) -> list[TurnCapture]:
        calls.append(log_dir)
        return capture_turns(log_dir)

    shutil.copytree(SAMPLE, tmp_path / "run")
    h = Harness(capture)
    h.store.fail_next = [StoreUnavailableError("server closed the connection")]
    h.sink.handle(run_ended(str(tmp_path / "run")))
    h.sink.start()
    await h.sink.close()
    assert calls == [tmp_path / "run"]
    (call,) = h.store.calls
    assert len(call[2]) == 1 and h.sink.reconnects == 1


async def test_a_bare_event_has_no_issue_number(h: Harness) -> None:
```

In `tests/test_db_store.py` (edit 1 of 3) replace

```

from issuebot.config import GitHubLabels
```

with

```

from issuebot.agent.turnlog import TurnCapture
from issuebot.config import GitHubLabels
```

In `tests/test_db_store.py` (edit 2 of 3) replace

```

def moved(**overrides: Any) -> StateChanged:
```

with

```

def capture(turn_number: int, **overrides: Any) -> TurnCapture:
    fields: dict[str, Any] = {
        "turn_number": turn_number,
        "model": "claude-opus-5",
        "subtype": "success",
        "is_error": False,
        "num_turns": 19,
        "input_tokens": 38,
        "cache_creation_input_tokens": 23100,
        "cache_read_input_tokens": 490200,
        "output_tokens": 8425,
        "cost_usd": 0.8976,
        "duration_ms": 201719,
        "result_text": "Done.",
        "prompt": "You are working on issue #42.",
        "prompt_bytes": 29,
        "stream": '{"type":"result"}\n',
        "stream_bytes": 18,
        "stream_lines": 1,
        "omitted_lines": 0,
        "stderr": "",
        "stderr_bytes": 0,
        "truncated": False,
    }
    fields.update(overrides)
    return TurnCapture(**fields)


def moved(**overrides: Any) -> StateChanged:
```

In `tests/test_db_store.py` (edit 3 of 3) replace

```
    assert (row["outcome"], row["error"]) == ("failed", "turn_failed: boom")

```

with

```
    assert (row["outcome"], row["error"]) == ("failed", "turn_failed: boom")


async def test_run_ended_with_captures_writes_run_turns(store: PostgresStore, db_url: str) -> None:
    turns = [capture(1), capture(2, subtype=None, num_turns=None, cost_usd=None, truncated=True)]
    await store.apply_event(ended(), turns=turns)
    first, second = await rows(db_url, "SELECT * FROM run_turns ORDER BY turn_number")
    assert (first["run_id"], first["turn_number"], first["model"]) == ("run-1", 1, "claude-opus-5")
    assert (first["subtype"], first["is_error"], first["num_turns"]) == ("success", False, 19)
    assert (first["input_tokens"], first["cache_creation_input_tokens"]) == (38, 23100)
    assert (first["cache_read_input_tokens"], first["output_tokens"]) == (490200, 8425)
    assert (first["cost_usd"], first["duration_ms"], first["result_text"]) == (
        0.8976,
        201719,
        "Done.",
    )
    assert (first["prompt"], first["prompt_bytes"]) == ("You are working on issue #42.", 29)
    assert (first["stream"], first["stream_bytes"], first["stream_lines"]) == (
        '{"type":"result"}\n',
        18,
        1,
    )
    assert (first["omitted_lines"], first["stderr"], first["stderr_bytes"]) == (0, "", 0)
    assert first["truncated"] is False
    assert first["captured_at"] is not None
    assert (second["turn_number"], second["subtype"], second["num_turns"]) == (2, None, None)
    assert (second["cost_usd"], second["truncated"]) == (None, True)


async def test_run_turns_are_idempotent_on_a_retried_event(
    store: PostgresStore, db_url: str
) -> None:
    await store.apply_event(ended(), turns=[capture(1)])
    await store.apply_event(ended(), turns=[capture(1, result_text="Done again.")])
    (row,) = await rows(db_url, "SELECT result_text FROM run_turns")
    assert row["result_text"] == "Done again."
    assert len(await rows(db_url, "SELECT id FROM events")) == 2


async def test_run_ended_without_captures_writes_no_turns(
    store: PostgresStore, db_url: str
) -> None:
    await store.apply_event(ended())
    assert await rows(db_url, "SELECT * FROM run_turns") == []


async def test_captures_are_ignored_for_other_kinds(store: PostgresStore, db_url: str) -> None:
    await store.apply_event(started(), turns=[capture(1)])
    assert await rows(db_url, "SELECT * FROM run_turns") == []
    assert len(await rows(db_url, "SELECT * FROM runs")) == 1

```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `timeout 300 uv run pytest tests/test_db_migrate.py tests/test_db_sink.py tests/test_cli.py -q`
Expected: `4 failed, 108 passed, 5 skipped, 20 errors`; the failures read `AssertionError: assert ['0001_initial'] == ['0001_initia...02_run_turns']` (discovery) and the errors `TypeError: PostgresSink.__init__() got an unexpected keyword argument 'capture'` (every sink test builds the harness).

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest tests/test_db_migrate.py tests/test_db_database.py tests/test_db_store.py -q`
Expected: `8 failed, 30 passed`; the failures read `TypeError: PostgresStore.apply_event() got an unexpected keyword argument 'turns'`, `psycopg.errors.UndefinedTable: relation "run_turns" does not exist`, and the version assertions (`assert (('0001_initial',), 1) == (('0001_initi...un_turns'), 2)`, `(0, 1, True) == (0, 2, True)`, `Regex pattern did not match` for `knows \(2\)`).

- [ ] **Step 3: The migration, the store and the sink**

Create `src/issuebot/db/migrations/0002_run_turns.sql`:

```sql
-- Phase 7: one row per captured turn of a run (Phase 7 spec §3.1). The PostgreSQL sink fills it
-- from the run's turn files when it drains run_ended; the dashboard's turn page reads it.

CREATE TABLE run_turns (
    run_id                      text NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    turn_number                 integer NOT NULL,
    captured_at                 timestamptz NOT NULL,
    model                       text,               -- system/init .model
    subtype                     text,               -- result .subtype
    is_error                    boolean,            -- result .is_error
    num_turns                   integer,            -- result .num_turns (agent iterations)
    input_tokens                bigint,             -- result .usage.*
    cache_creation_input_tokens bigint,
    cache_read_input_tokens     bigint,
    output_tokens               bigint,
    cost_usd                    double precision,   -- result .total_cost_usd
    duration_ms                 bigint,             -- result .duration_ms
    result_text                 text,               -- result .result, first RESULT_TEXT_LIMIT chars
    prompt                      text NOT NULL,      -- turn-N.prompt.md, first PROMPT_LIMIT bytes
    prompt_bytes                integer NOT NULL,   -- the file's size
    stream                      text NOT NULL,      -- turn-N.jsonl, capped (spec §4.1)
    stream_bytes                integer NOT NULL,
    stream_lines                integer NOT NULL,   -- lines in the file
    omitted_lines               integer NOT NULL,   -- lines replaced by a stub
    stderr                      text NOT NULL,      -- turn-N.stderr.log, last STDERR_LIMIT bytes
    stderr_bytes                integer NOT NULL,
    truncated                   boolean NOT NULL,   -- the head cap dropped at least one line
    PRIMARY KEY (run_id, turn_number)
);
```

In `src/issuebot/db/sink.py` (edit 1 of 7) replace

```
from typing import Any, Literal, Protocol

from issuebot.db.connection import reconnect_delay
from issuebot.db.errors import StoreError, StoreUnavailableError
from issuebot.db.store import IssueSnapshot, Store
from issuebot.events import Event, IssueEvent
```

with

```
from pathlib import Path
from typing import Any, Literal, Protocol

from issuebot.agent.turnlog import TurnCapture, capture_turns
from issuebot.db.connection import reconnect_delay
from issuebot.db.errors import StoreError, StoreUnavailableError
from issuebot.db.store import IssueSnapshot, Store
from issuebot.events import Event, IssueEvent, RunEnded
```

In `src/issuebot/db/sink.py` (edit 2 of 7) replace

```
    connection is retried with backoff and the item in flight is retried, not dropped.
    """
```

with

```
    connection is retried with backoff and the item in flight is retried, not dropped.
    A ``run_ended`` item has its turn files captured (in a thread, once) before its first
    write attempt, so the ``run_turns`` rows land in the same transaction as the run's.
    """
```

In `src/issuebot/db/sink.py` (edit 3 of 7) replace

```
        description: str | None = None,
    ) -> None:
```

with

```
        description: str | None = None,
        capture: Callable[[Path], list[TurnCapture]] = capture_turns,
    ) -> None:
```

In `src/issuebot/db/sink.py` (edit 4 of 7) replace

```
        self._description = description
        self._queue: asyncio.Queue[_Item | None] = asyncio.Queue()
```

with

```
        self._description = description
        self._capture_turns = capture
        self._queue: asyncio.Queue[_Item | None] = asyncio.Queue()
```

In `src/issuebot/db/sink.py` (edit 5 of 7) replace

```
    async def _write(self, work: _Work) -> None:
        retries = 0
```

with

```
    async def _write(self, work: _Work) -> None:
        turns = await self._capture(work)
        retries = 0
```

In `src/issuebot/db/sink.py` (edit 6 of 7) replace

```
                await self._apply(work)
```

with

```
                await self._apply(work, turns)
```

In `src/issuebot/db/sink.py` (edit 7 of 7) replace

```
    async def _apply(self, work: _Work) -> None:
        if isinstance(work, _EventItem):
            await self._store.apply_event(work.event)
```

with

```
    async def _capture(self, work: _Work) -> tuple[TurnCapture, ...]:
        """The turn files of a run_ended item, read once in a thread; () for anything else."""
        if not isinstance(work, _EventItem) or not isinstance(work.event, RunEnded):
            return ()
        event = work.event
        if not event.log_dir:
            return ()
        try:
            captures = await asyncio.to_thread(self._capture_turns, Path(event.log_dir))
        except Exception as exc:
            self._log.warning(
                "db_turns_capture_failed",
                run_id=event.run_id,
                log_dir=event.log_dir,
                error=f"{type(exc).__name__}: {exc}",
            )
            return ()
        self._log.info(
            "db_turns_captured",
            run_id=event.run_id,
            turns=len(captures),
            stream_bytes=sum(capture.stream_bytes for capture in captures),
        )
        return tuple(captures)

    async def _apply(self, work: _Work, turns: tuple[TurnCapture, ...]) -> None:
        if isinstance(work, _EventItem):
            await self._store.apply_event(work.event, turns=turns)
```

In `src/issuebot/db/store.py` (edit 1 of 7) replace

```
from dataclasses import dataclass
```

with

```
from dataclasses import dataclass, fields
```

In `src/issuebot/db/store.py` (edit 2 of 7) replace

```

from issuebot.config import GitHubLabels
```

with

```

from issuebot.agent.turnlog import TurnCapture
from issuebot.config import GitHubLabels
```

In `src/issuebot/db/store.py` (edit 3 of 7) replace

```
    async def apply_event(self, event: Event) -> None: ...
```

with

```
    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None: ...
```

In `src/issuebot/db/store.py` (edit 4 of 7) replace

```
    log_dir = EXCLUDED.log_dir
"""
```

with

```
    log_dir = EXCLUDED.log_dir
"""

INSERT_TURN = """
INSERT INTO run_turns (run_id, turn_number, captured_at, model, subtype, is_error, num_turns,
                       input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
                       output_tokens, cost_usd, duration_ms, result_text, prompt, prompt_bytes,
                       stream, stream_bytes, stream_lines, omitted_lines, stderr, stderr_bytes,
                       truncated)
VALUES (%(run_id)s, %(turn_number)s, now(), %(model)s, %(subtype)s, %(is_error)s, %(num_turns)s,
        %(input_tokens)s, %(cache_creation_input_tokens)s, %(cache_read_input_tokens)s,
        %(output_tokens)s, %(cost_usd)s, %(duration_ms)s, %(result_text)s, %(prompt)s,
        %(prompt_bytes)s, %(stream)s, %(stream_bytes)s, %(stream_lines)s, %(omitted_lines)s,
        %(stderr)s, %(stderr_bytes)s, %(truncated)s)
ON CONFLICT (run_id, turn_number) DO UPDATE SET
    captured_at = now(),
    model = EXCLUDED.model,
    subtype = EXCLUDED.subtype,
    is_error = EXCLUDED.is_error,
    num_turns = EXCLUDED.num_turns,
    input_tokens = EXCLUDED.input_tokens,
    cache_creation_input_tokens = EXCLUDED.cache_creation_input_tokens,
    cache_read_input_tokens = EXCLUDED.cache_read_input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    cost_usd = EXCLUDED.cost_usd,
    duration_ms = EXCLUDED.duration_ms,
    result_text = EXCLUDED.result_text,
    prompt = EXCLUDED.prompt,
    prompt_bytes = EXCLUDED.prompt_bytes,
    stream = EXCLUDED.stream,
    stream_bytes = EXCLUDED.stream_bytes,
    stream_lines = EXCLUDED.stream_lines,
    omitted_lines = EXCLUDED.omitted_lines,
    stderr = EXCLUDED.stderr,
    stderr_bytes = EXCLUDED.stderr_bytes,
    truncated = EXCLUDED.truncated
"""
```

In `src/issuebot/db/store.py` (edit 5 of 7) replace

```
    async def apply_event(self, event: Event) -> None:
        """Append the event; then upsert the run or update the issue it is about."""
```

with

```
    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None:
        """Append the event; then upsert the run (and its captured turns) or update the issue."""
```

In `src/issuebot/db/store.py` (edit 6 of 7) replace

```
                await conn.execute(RUN_ENDED, run_ended_row(event))
            elif isinstance(event, StateChanged):
```

with

```
                await conn.execute(RUN_ENDED, run_ended_row(event))
                if turns:
                    async with conn.cursor() as cursor:
                        await cursor.executemany(
                            INSERT_TURN, [turn_row(event.run_id, turn) for turn in turns]
                        )
            elif isinstance(event, StateChanged):
```

In `src/issuebot/db/store.py` (edit 7 of 7) replace

```

def run_ended_row(event: RunEnded) -> dict[str, Any]:
```

with

```

def turn_row(run_id: str, turn: TurnCapture) -> dict[str, Any]:
    """The bound parameters of INSERT_TURN: every TurnCapture field plus the run id."""
    return {"run_id": run_id, **{f.name: getattr(turn, f.name) for f in fields(turn)}}


def run_ended_row(event: RunEnded) -> dict[str, Any]:
```

- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `679 passed, 34 skipped`; then `713 passed`.

- [ ] **Step 5: Commit**

```bash
git status --short && git add --all && git commit -m "feat: run_turns migration; the store writes captured turns; the sink captures at run_ended" -m "<trailer>"
```

---

### Task 3: The queries

**Files:**
- Modify: `src/issuebot/db/queries.py`, `src/issuebot/db/__init__.py`
- Test: `tests/test_db_queries.py`

**Interfaces:**
- Consumes: Task 2's `run_turns` table; Task 1's `TurnCapture` (in the tests, to seed through the store).
- Produces: `MAX_WINDOW_DAYS = 365`; `TurnSummaryRow` (every `run_turns` column except `prompt`, `stream`, `stderr`) and `TurnRow(TurnSummaryRow)` (the whole row); `Queries.issue(number) -> IssueRow | None`; `events_for_issue(number, limit) -> list[EventRow]` (newest first); `turn_summaries_for_issue(number) -> list[TurnSummaryRow]` (newest run first, then turn order); `turn(run_id, turn_number) -> TurnRow | None`; `state_counts() -> dict[str, int]` (every role key, the Kanban's predicate); `issues_by_state` skips a row whose state is not a `StateLabel` value; all re-exported from `issuebot.db`.

Spec: §5.

- [ ] **Step 1: Write the failing tests**

In `tests/test_db_queries.py` (edit 1 of 3) replace

```
from issuebot.config import GitHubLabels
from issuebot.db import StoreError, connect, migrate
from issuebot.db.database import Database
from issuebot.db.queries import COMPLETE_LIMIT, DailyPoint, EventRow, IssueRow, RunRow
```

with

```
from issuebot.agent.turnlog import TurnCapture
from issuebot.config import GitHubLabels
from issuebot.db import StoreError, connect, migrate
from issuebot.db.database import Database
from issuebot.db.queries import (
    COMPLETE_LIMIT,
    DailyPoint,
    EventRow,
    IssueRow,
    RunRow,
    TurnRow,
    TurnSummaryRow,
)
```

In `tests/test_db_queries.py` (edit 2 of 3) replace

```
    yield Database(db_url)

```

with

```
    yield Database(db_url)


def capture(turn_number: int, **overrides: Any) -> TurnCapture:
    fields: dict[str, Any] = {
        "turn_number": turn_number,
        "model": "claude-opus-5",
        "subtype": "success",
        "is_error": False,
        "num_turns": 19,
        "input_tokens": 38,
        "cache_creation_input_tokens": 23100,
        "cache_read_input_tokens": 490200,
        "output_tokens": 8425,
        "cost_usd": 0.8976,
        "duration_ms": 201719,
        "result_text": "Done.",
        "prompt": "You are working on issue #2.",
        "prompt_bytes": 28,
        "stream": '{"type":"result"}\n',
        "stream_bytes": 18,
        "stream_lines": 1,
        "omitted_lines": 0,
        "stderr": "warning: slow\n",
        "stderr_bytes": 14,
        "truncated": False,
    }
    fields.update(overrides)
    return TurnCapture(**fields)


def run_ended(run_id: str, number: int, at: datetime, **overrides: Any) -> RunEnded:
    fields: dict[str, Any] = {
        "issue_number": number,
        "issue_identifier": f"repo-{number}",
        "run_id": run_id,
        "outcome": "succeeded",
        "error": None,
        "turns": 1,
        "input_tokens": 10,
        "output_tokens": 1,
        "cost_usd": 0.1,
        "duration_s": 30.0,
        "at": at,
    }
    fields.update(overrides)
    return RunEnded(**fields)


@pytest.fixture
async def with_turns(seeded: Database, db_url: str) -> Database:
    """Two captured turns on r2 and one on r0, an older finished run of the same issue."""
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()
    await store.apply_event(
        run_ended("r2", 2, NOW - 2 * DAY + timedelta(seconds=30), outcome="failed"),
        turns=[capture(1), capture(2, subtype=None, num_turns=None, truncated=True)],
    )
    await store.apply_event(
        RunStarted(
            issue_number=2,
            issue_identifier="repo-2",
            run_id="r0",
            attempt=1,
            session_id="s0",
            workspace_path="/w",
            at=NOW - 3 * DAY,
        )
    )
    await store.apply_event(
        run_ended("r0", 2, NOW - 3 * DAY + timedelta(seconds=30)),
        turns=[capture(1, model="claude-sonnet-5")],
    )
    await store.close()
    return seeded

```

In `tests/test_db_queries.py` (edit 3 of 3) replace

```

async def test_queries_on_an_empty_schema_report_a_database_error(db_url: str) -> None:
```

with

```

async def test_issue_by_number(seeded: Database) -> None:
    async with seeded.queries() as q:
        row = await q.issue(3)
        assert await q.issue(99) is None
    assert isinstance(row, IssueRow)
    assert (row.number, row.identifier, row.state) == (3, "repo-3", "review")


async def test_events_for_issue_newest_first_and_limited(seeded: Database) -> None:
    async with seeded.queries() as q:
        events = await q.events_for_issue(2, 10)
        two = await q.events_for_issue(2, 2)
        assert await q.events_for_issue(99, 10) == []
    assert [event.kind for event in events] == [
        "blocked",
        "run_ended",
        "run_started",
        "run_started",
    ]
    assert [event.run_id for event in events] == [None, "r2", "r2", "r1"]
    assert [event.kind for event in two] == ["blocked", "run_ended"]
    assert all(event.issue_number == 2 for event in events)


async def test_turn_summaries_for_issue_newest_run_first(with_turns: Database) -> None:
    async with with_turns.queries() as q:
        turns = await q.turn_summaries_for_issue(2)
        assert await q.turn_summaries_for_issue(99) == []
    assert [(turn.run_id, turn.turn_number) for turn in turns] == [("r2", 1), ("r2", 2), ("r0", 1)]
    assert all(isinstance(turn, TurnSummaryRow) for turn in turns)
    assert not any(isinstance(turn, TurnRow) for turn in turns)
    first = turns[0]
    assert (first.model, first.subtype, first.num_turns, first.truncated) == (
        "claude-opus-5",
        "success",
        19,
        False,
    )
    assert (first.cost_usd, first.prompt_bytes, first.stream_bytes, first.stderr_bytes) == (
        0.8976,
        28,
        18,
        14,
    )
    assert (turns[1].subtype, turns[1].num_turns, turns[1].truncated) == (None, None, True)
    assert turns[2].model == "claude-sonnet-5"
    assert not hasattr(first, "stream")


async def test_turn_returns_the_whole_row_or_none(with_turns: Database) -> None:
    async with with_turns.queries() as q:
        turn = await q.turn("r2", 2)
        assert await q.turn("r2", 9) is None
        assert await q.turn("nope", 1) is None
    assert isinstance(turn, TurnRow)
    assert (turn.run_id, turn.turn_number, turn.subtype) == ("r2", 2, None)
    assert (turn.prompt, turn.stream, turn.stderr) == (
        "You are working on issue #2.",
        '{"type":"result"}\n',
        "warning: slow\n",
    )
    assert turn.captured_at is not None


async def test_state_counts_follow_the_kanban_predicate(seeded: Database) -> None:
    async with seeded.queries() as q:
        counts = await q.state_counts()
    assert counts == {"todo": 2, "in_progress": 1, "review": 1, "rework": 0, "complete": 3}
    assert list(counts) == ["todo", "in_progress", "review", "rework", "complete"]


async def test_issues_by_state_skips_an_unknown_role(seeded: Database, db_url: str) -> None:
    conn = await connect(db_url)
    try:
        await conn.execute(
            """
            INSERT INTO issues (number, identifier, title, state, state_label, github_state, url,
                                created_at, updated_at, seen_at)
            VALUES (77, 'repo-77', 'Mystery', 'mystery', 'issuebot/mystery', 'open',
                    'https://github.com/example/repo/issues/77', now(), now(), now())
            """
        )
    finally:
        await conn.close()
    async with seeded.queries() as q:
        groups = await q.issues_by_state()
        counts = await q.state_counts()
    assert list(groups) == ["todo", "in_progress", "review", "rework", "complete"]
    assert 77 not in {row.number for rows in groups.values() for row in rows}
    assert list(counts) == ["todo", "in_progress", "review", "rework", "complete"]


async def test_queries_on_an_empty_schema_report_a_database_error(db_url: str) -> None:
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `timeout 120 uv run pytest tests/test_db_queries.py -q`
Expected: collection fails with `ImportError: cannot import name 'TurnRow' from 'issuebot.db.queries'` (with or without `DATABASE_URL`).

- [ ] **Step 3: The queries**

In `src/issuebot/db/__init__.py` (edit 1 of 4) replace

```
    COMPLETE_LIMIT,
    DailyPoint,
```

with

```
    COMPLETE_LIMIT,
    MAX_WINDOW_DAYS,
    DailyPoint,
```

In `src/issuebot/db/__init__.py` (edit 2 of 4) replace

```
    SnapshotRow,
)
```

with

```
    SnapshotRow,
    TurnRow,
    TurnSummaryRow,
)
```

In `src/issuebot/db/__init__.py` (edit 3 of 4) replace

```
    "DRAIN_TIMEOUT_S",
    "MIGRATIONS_ROOT",
```

with

```
    "DRAIN_TIMEOUT_S",
    "MAX_WINDOW_DAYS",
    "MIGRATIONS_ROOT",
```

In `src/issuebot/db/__init__.py` (edit 4 of 4) replace

```
    "StoreUnavailableError",
    "apply_migrations",
```

with

```
    "StoreUnavailableError",
    "TurnRow",
    "TurnSummaryRow",
    "apply_migrations",
```

In `src/issuebot/db/queries.py` (edit 1 of 5) replace

```
from dataclasses import dataclass
```

with

```
from dataclasses import dataclass, fields
```

In `src/issuebot/db/queries.py` (edit 2 of 5) replace

```
COMPLETE_LIMIT = 50

```

with

```
COMPLETE_LIMIT = 50
MAX_WINDOW_DAYS = 365

```

In `src/issuebot/db/queries.py` (edit 3 of 5) replace

```

CLOSED_COUNT = """
```

with

```

@dataclass(frozen=True, kw_only=True, slots=True)
class TurnSummaryRow:
    """Every ``run_turns`` column except the three texts (prompt, stream, stderr)."""

    run_id: str
    turn_number: int
    captured_at: datetime
    model: str | None
    subtype: str | None
    is_error: bool | None
    num_turns: int | None
    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    duration_ms: int | None
    result_text: str | None
    prompt_bytes: int
    stream_bytes: int
    stream_lines: int
    omitted_lines: int
    stderr_bytes: int
    truncated: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnRow(TurnSummaryRow):
    """The whole ``run_turns`` row."""

    prompt: str
    stream: str
    stderr: str


SUMMARY_COLUMNS = ", ".join(f"t.{f.name}" for f in fields(TurnSummaryRow))


CLOSED_COUNT = """
```

In `src/issuebot/db/queries.py` (edit 4 of 5) replace

```
SNAPSHOT = "SELECT at, written_at, data FROM runtime_snapshot WHERE id"


class Queries:
```

with

```
SNAPSHOT = "SELECT at, written_at, data FROM runtime_snapshot WHERE id"

ISSUE = "SELECT * FROM issues WHERE number = %(number)s"

EVENTS_FOR_ISSUE = """
SELECT * FROM events WHERE issue_number = %(number)s ORDER BY id DESC LIMIT %(limit)s
"""

TURN_SUMMARIES_FOR_ISSUE = f"""
SELECT {SUMMARY_COLUMNS} FROM run_turns t JOIN runs r ON r.run_id = t.run_id
WHERE r.issue_number = %(number)s
ORDER BY r.started_at DESC, r.run_id DESC, t.turn_number
"""

TURN = "SELECT * FROM run_turns WHERE run_id = %(run_id)s AND turn_number = %(turn_number)s"

STATE_COUNTS = """
SELECT state, count(*) AS n FROM issues
WHERE state IS NOT NULL AND (github_state = 'open' OR state = 'complete')
GROUP BY state
"""


class Queries:
```

In `src/issuebot/db/queries.py` (edit 5 of 5) replace

```
            groups.setdefault(row["state"], []).append(IssueRow(**row))
        for row in await self._rows(COMPLETE_ISSUES, {"limit": COMPLETE_LIMIT}):
            groups[StateLabel.COMPLETE.value].append(IssueRow(**row))
        return groups
```

with

```
            if row["state"] in groups:  # a role this issuebot does not know is on no column
                groups[row["state"]].append(IssueRow(**row))
        for row in await self._rows(COMPLETE_ISSUES, {"limit": COMPLETE_LIMIT}):
            groups[StateLabel.COMPLETE.value].append(IssueRow(**row))
        return groups

    async def state_counts(self) -> dict[str, int]:
        """Issues per StateLabel value over the Kanban's predicate; every role key present."""
        counts = {role.value: 0 for role in StateLabel}
        for row in await self._rows(STATE_COUNTS):
            if row["state"] in counts:
                counts[row["state"]] = int(row["n"])
        return counts

    async def issue(self, number: int) -> IssueRow | None:
        rows = await self._rows(ISSUE, {"number": number})
        return IssueRow(**rows[0]) if rows else None

    async def events_for_issue(self, number: int, limit: int) -> list[EventRow]:
        """Newest first."""
        rows = await self._rows(EVENTS_FOR_ISSUE, {"number": number, "limit": limit})
        return [EventRow(**row) for row in rows]

    async def turn_summaries_for_issue(self, number: int) -> list[TurnSummaryRow]:
        """Captured turns of the issue's runs: newest run first (runs.started_at), then turn."""
        rows = await self._rows(TURN_SUMMARIES_FOR_ISSUE, {"number": number})
        return [TurnSummaryRow(**row) for row in rows]

    async def turn(self, run_id: str, turn_number: int) -> TurnRow | None:
        rows = await self._rows(TURN, {"run_id": run_id, "turn_number": turn_number})
        return TurnRow(**rows[0]) if rows else None
```

- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest tests/test_db_queries.py -q && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `14 passed`; then `679 passed, 40 skipped`; then `719 passed`.

- [ ] **Step 5: Commit**

```bash
git status --short && git add --all && git commit -m "feat: issue, events_for_issue, turn_summaries_for_issue, turn and state_counts queries" -m "<trailer>"
```

---

### Task 4: The shared test fakes and the transcript parser

**Files:**
- Create: `tests/fakes/__init__.py`, `tests/fakes/database.py`, `src/issuebot/web/__init__.py`, `src/issuebot/web/transcript.py`
- Modify: `tests/test_cli.py` (the fakes move out), `src/issuebot/db/__init__.py` (re-export `OMITTED_TYPE` and `TurnCapture`)
- Test: `tests/test_web_transcript.py`

**Interfaces:**
- Consumes: Task 3's row types and the five queries (for `FakeQueries`); Task 1's `OMITTED_TYPE` (re-exported from `issuebot.db`, so `web` never imports `agent`).
- Produces: `fakes.database.FakeDatabase` (`factory(url)`, `description`, `migrate()`, `probe()`, `queries()` context manager counting `opened`, `store(labels)`, `listener(on_notify)`, `notify_refresh()`, `migrate_result` defaulting to version 2, `PROBE_OK` at 2 of 2), `FakeQueries` (every `Queries` method with canned rows: `snapshot_row`, `closed`, `runs`, `groups`, `counts`, `series`, `issue_rows`, `runs_by_issue`, `events_by_issue`, `turns_by_issue`, `turn_rows`; `error` makes them all raise; `calls` records names), `FakeStore` (`apply_event(event, turns=())` recording `turns`), `FakeListener`, `DB_URL`; `issuebot.web.transcript.parse_transcript(stream: str) -> Transcript(blocks: list[Block], hidden: int)`, `Block(kind, title, text, cut=0, collapsed=False)`, `BlockKind`, constants `TOOL_INPUT_COLLAPSE = 2048`, `TOOL_RESULT_LIMIT = 4096`, `UNPARSEABLE_LIMIT = 200`.

Spec: §6.5; §9 (the shared fakes).

- [ ] **Step 1: Move the CLI's database fakes into a shared module and write the failing transcript tests**

Create `tests/fakes/__init__.py`:

```python
"""Test doubles shared by more than one test module (the fake executables live here too)."""
```

Create `tests/fakes/database.py` (the classes that lived in `tests/test_cli.py`, with `FakeQueries` grown by the new queries, `FakeStore` recording turns and the probe and migration defaults at schema version 2):

```python
"""Stand-ins for issuebot.db.Database and what it hands out; shared by the CLI and web tests."""

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from issuebot.agent.turnlog import TurnCapture
from issuebot.config import GitHubLabels
from issuebot.db import DatabaseError, MigrationResult, Probe
from issuebot.db.queries import (
    DailyPoint,
    EventRow,
    IssueRow,
    RunRow,
    SnapshotRow,
    TurnRow,
    TurnSummaryRow,
)
from issuebot.db.store import IssueSnapshot
from issuebot.events import Event
from issuebot.github import StateLabel

DB_URL = "postgresql://issuebot:s3cret@db.example:5432/issuebot"
PROBE_OK = Probe(server_version="PostgreSQL 18.1", schema_version=2, latest_version=2)


class FakeStore:
    """The sink's store: records every write."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.turns: list[list[TurnCapture]] = []
        self.issues: list[list[IssueSnapshot]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None:
        self.events.append(event)
        self.turns.append(list(turns))

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None:
        self.issues.append(list(issues))

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None:
        self.snapshots.append(dict(data))


class FakeQueries:
    """Canned answers for every Queries method; ``error`` makes them all raise it."""

    def __init__(self) -> None:
        self.snapshot_row: SnapshotRow | None = None
        self.closed = {1: 0, 7: 0}
        self.runs = {1: 0, 7: 0}
        self.groups: dict[str, list[IssueRow]] = {role.value: [] for role in StateLabel}
        self.counts: dict[str, int] = {role.value: 0 for role in StateLabel}
        self.series: list[DailyPoint] = []
        self.issue_rows: dict[int, IssueRow] = {}
        self.runs_by_issue: dict[int, list[RunRow]] = {}
        self.events_by_issue: dict[int, list[EventRow]] = {}
        self.turns_by_issue: dict[int, list[TurnSummaryRow]] = {}
        self.turn_rows: dict[tuple[str, int], TurnRow] = {}
        self.error: DatabaseError | None = None
        self.days_asked: int | None = None
        self.calls: list[str] = []

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.error is not None:
            raise self.error

    async def snapshot(self) -> SnapshotRow | None:
        self._check("snapshot")
        return self.snapshot_row

    async def closed_count(self, window: timedelta) -> int:
        self._check("closed_count")
        return self.closed[window.days]

    async def runs_count(self, window: timedelta) -> int:
        self._check("runs_count")
        return self.runs[window.days]

    async def issues_by_state(self) -> dict[str, list[IssueRow]]:
        self._check("issues_by_state")
        return self.groups

    async def state_counts(self) -> dict[str, int]:
        self._check("state_counts")
        return self.counts

    async def daily_series(self, days: int) -> list[DailyPoint]:
        self._check("daily_series")
        self.days_asked = days
        return self.series

    async def issue(self, number: int) -> IssueRow | None:
        self._check("issue")
        return self.issue_rows.get(number)

    async def runs_for_issue(self, number: int) -> list[RunRow]:
        self._check("runs_for_issue")
        return self.runs_by_issue.get(number, [])

    async def events_for_issue(self, number: int, limit: int) -> list[EventRow]:
        self._check("events_for_issue")
        return self.events_by_issue.get(number, [])[:limit]

    async def recent_events(self, limit: int) -> list[EventRow]:
        self._check("recent_events")
        events = [event for rows in self.events_by_issue.values() for event in rows]
        return sorted(events, key=lambda event: event.id, reverse=True)[:limit]

    async def turn_summaries_for_issue(self, number: int) -> list[TurnSummaryRow]:
        self._check("turn_summaries_for_issue")
        return self.turns_by_issue.get(number, [])

    async def turn(self, run_id: str, turn_number: int) -> TurnRow | None:
        self._check("turn")
        return self.turn_rows.get((run_id, turn_number))


class FakeListener:
    def __init__(self, on_notify: Callable[[], None]) -> None:
        self.on_notify = on_notify
        self.started = False
        self.closed = False
        self.close_error: Exception | None = None

    def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeDatabase:
    """Stands in for issuebot.db.Database: one instance per test with canned results."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.migrations = 0
        self.migrate_result = MigrationResult(applied=(), version=2)
        self.migrate_error: DatabaseError | None = None
        self.probe_result = PROBE_OK
        self.probe_error: DatabaseError | None = None
        self.queries_obj = FakeQueries()
        self.store_obj = FakeStore()
        self.labels: GitHubLabels | None = None
        self.listeners: list[FakeListener] = []
        self.listener_close_error: Exception | None = None
        self.notified = 0
        self.notify_error: DatabaseError | None = None
        self.opened = 0

    def factory(self, url: str) -> FakeDatabase:
        self.urls.append(url)
        return self

    @property
    def description(self) -> str:
        return "postgresql://issuebot@db.example:5432/issuebot"

    async def migrate(self) -> MigrationResult:
        self.migrations += 1
        if self.migrate_error is not None:
            raise self.migrate_error
        return self.migrate_result

    async def probe(self) -> Probe:
        if self.probe_error is not None:
            raise self.probe_error
        return self.probe_result

    @asynccontextmanager
    async def queries(self) -> AsyncIterator[FakeQueries]:
        self.opened += 1
        yield self.queries_obj

    def store(self, labels: GitHubLabels) -> FakeStore:
        self.labels = labels
        return self.store_obj

    def listener(self, on_notify: Callable[[], None]) -> FakeListener:
        listener = FakeListener(on_notify)
        listener.close_error = self.listener_close_error
        self.listeners.append(listener)
        return listener

    async def notify_refresh(self) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self.notified += 1
```

In `tests/test_cli.py` (edit 1 of 6) replace

```
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
```

with

```
from collections.abc import Callable
```

In `tests/test_cli.py` (edit 2 of 6) replace

```
from issuebot import __version__
from issuebot.agent import RunResult, SessionRecord, WorkspaceManager
from issuebot.agent.turnlog import TurnCapture
```

with

```
from fakes.database import DB_URL, FakeDatabase
from issuebot import __version__
from issuebot.agent import RunResult, SessionRecord, WorkspaceManager
```

In `tests/test_cli.py` (edit 3 of 6) replace

```
from issuebot.db import DatabaseError, MigrationResult, Probe, StoreError, StoreUnavailableError
from issuebot.db.queries import DailyPoint, SnapshotRow
from issuebot.db.store import IssueSnapshot
```

with

```
from issuebot.db import MigrationResult, Probe, StoreError, StoreUnavailableError
from issuebot.db.queries import DailyPoint, SnapshotRow
```

In `tests/test_cli.py` (edit 4 of 6) replace

```


DB_URL = "postgresql://issuebot:s3cret@db.example:5432/issuebot"
PROBE_OK = Probe(server_version="PostgreSQL 18.1", schema_version=1, latest_version=1)


class FakeStore:
    """The sink's store: records every write."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.turns: list[list[TurnCapture]] = []
        self.issues: list[list[IssueSnapshot]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def apply_event(self, event: Event, turns: Sequence[TurnCapture] = ()) -> None:
        self.events.append(event)
        self.turns.append(list(turns))

    async def upsert_issues(self, issues: Sequence[IssueSnapshot]) -> None:
        self.issues.append(list(issues))

    async def write_snapshot(self, at: datetime, data: Mapping[str, Any]) -> None:
        self.snapshots.append(dict(data))


class FakeQueries:
    """Canned answers for status and stats."""

    def __init__(self) -> None:
        self.snapshot_row: SnapshotRow | None = None
        self.closed = {1: 0, 7: 0}
        self.runs = {1: 0, 7: 0}
        self.groups: dict[str, list[object]] = {role.value: [] for role in StateLabel}
        self.series: list[DailyPoint] = []
        self.error: DatabaseError | None = None
        self.days_asked: int | None = None

    def _check(self) -> None:
        if self.error is not None:
            raise self.error

    async def snapshot(self) -> SnapshotRow | None:
        self._check()
        return self.snapshot_row

    async def closed_count(self, window: timedelta) -> int:
        self._check()
        return self.closed[window.days]

    async def runs_count(self, window: timedelta) -> int:
        return self.runs[window.days]

    async def issues_by_state(self) -> dict[str, list[object]]:
        self._check()
        return self.groups

    async def daily_series(self, days: int) -> list[DailyPoint]:
        self.days_asked = days
        return self.series


class FakeListener:
    def __init__(self, on_notify: Callable[[], None]) -> None:
        self.on_notify = on_notify
        self.started = False
        self.closed = False
        self.close_error: Exception | None = None

    def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeDatabase:
    """Stands in for issuebot.db.Database: one instance per test with canned results."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.migrations = 0
        self.migrate_result = MigrationResult(applied=(), version=1)
        self.migrate_error: DatabaseError | None = None
        self.probe_result = PROBE_OK
        self.probe_error: DatabaseError | None = None
        self.queries_obj = FakeQueries()
        self.store_obj = FakeStore()
        self.labels: GitHubLabels | None = None
        self.listeners: list[FakeListener] = []
        self.listener_close_error: Exception | None = None
        self.notified = 0
        self.notify_error: DatabaseError | None = None

    def factory(self, url: str) -> FakeDatabase:
        self.urls.append(url)
        return self

    @property
    def description(self) -> str:
        return "postgresql://issuebot@db.example:5432/issuebot"

    async def migrate(self) -> MigrationResult:
        self.migrations += 1
        if self.migrate_error is not None:
            raise self.migrate_error
        return self.migrate_result

    async def probe(self) -> Probe:
        if self.probe_error is not None:
            raise self.probe_error
        return self.probe_result

    @asynccontextmanager
    async def queries(self) -> AsyncIterator[FakeQueries]:
        yield self.queries_obj

    def store(self, labels: GitHubLabels) -> FakeStore:
        self.labels = labels
        return self.store_obj

    def listener(self, on_notify: Callable[[], None]) -> FakeListener:
        listener = FakeListener(on_notify)
        listener.close_error = self.listener_close_error
        self.listeners.append(listener)
        return listener

    async def notify_refresh(self) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self.notified += 1
```

with

```

```

In `tests/test_cli.py` (edit 5 of 6) replace

```
    assert "[ OK ] database.url: connected (PostgreSQL 18.1); schema version 1" in out
```

with

```
    assert "[ OK ] database.url: connected (PostgreSQL 18.1); schema version 2" in out
```

In `tests/test_cli.py` (edit 6 of 6) replace

```
    assert capsys.readouterr().out == "[ OK ] database: unchanged at schema version 1\n"
```

with

```
    assert capsys.readouterr().out == "[ OK ] database: unchanged at schema version 2\n"
```

Create `tests/test_web_transcript.py`:

```python
"""Tests for the transcript parser (hermetic: the real sample fixture and synthetic lines)."""

import json
from collections import Counter
from pathlib import Path

import pytest

from issuebot.web import transcript as transcript_module
from issuebot.web.transcript import (
    TOOL_INPUT_COLLAPSE,
    TOOL_RESULT_LIMIT,
    UNPARSEABLE_LIMIT,
    Block,
    parse_transcript,
)

SAMPLE = Path(__file__).parent / "fixtures" / "runs" / "20260904T202535Z-0964cd" / "turn-1.jsonl"


def line(**message: object) -> str:
    return json.dumps(message)


def assistant(*blocks: dict[str, object]) -> str:
    return line(type="assistant", message={"role": "assistant", "content": list(blocks)})


def user(*blocks: dict[str, object]) -> str:
    return line(type="user", message={"role": "user", "content": list(blocks)})


def parse(*lines: str) -> list[Block]:
    return parse_transcript("".join(f"{text}\n" for text in lines)).blocks


# --- the real sample -----------------------------------------------------------------------


def test_the_sample_parses_into_the_expected_blocks() -> None:
    result = parse_transcript(SAMPLE.read_text(encoding="utf-8"))
    kinds = Counter(block.kind for block in result.blocks)
    assert kinds == {
        "init": 1,
        "text": 11,
        "thinking": 4,
        "tool_use": 24,
        "tool_result": 24,
        "result": 1,
    }
    assert result.hidden == 30
    first = result.blocks[0]
    assert first.kind == "init" and first.title == "session"
    assert "model: claude-opus-5" in first.text and "claude code: 2.1.261" in first.text
    assert {block.title for block in result.blocks if block.kind == "tool_use"} >= {"Bash", "Agent"}
    titles = [block.title for block in result.blocks if block.kind == "text"]
    assert titles.count("assistant") == 10 and titles.count("user") == 1
    last = result.blocks[-1]
    assert (last.kind, last.title) == ("result", "result: success")
    assert last.text.startswith("Done. Issue #7 is on `issuebot/review`.")
    assert all(block.collapsed for block in result.blocks if block.kind == "thinking")
    assert all(block.collapsed for block in result.blocks if block.kind == "tool_result")


# --- assistant and user blocks ---------------------------------------------------------------


def test_text_thinking_and_tool_use_blocks() -> None:
    blocks = parse(
        assistant(
            {"type": "text", "text": "Looking."},
            {"type": "thinking", "thinking": "hmm"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls", "b": 1}},
        )
    )
    assert [block.kind for block in blocks] == ["text", "thinking", "tool_use"]
    assert (blocks[0].title, blocks[0].text, blocks[0].collapsed) == (
        "assistant",
        "Looking.",
        False,
    )
    assert (blocks[1].title, blocks[1].text, blocks[1].collapsed) == ("thinking", "hmm", True)
    assert blocks[2].title == "Bash"
    assert blocks[2].text == '{\n  "b": 1,\n  "command": "ls"\n}'
    assert blocks[2].collapsed is False


def test_a_long_tool_input_is_collapsed_but_whole() -> None:
    text = "x" * (TOOL_INPUT_COLLAPSE + 10)
    (block,) = parse(assistant({"type": "tool_use", "name": "Write", "input": {"content": text}}))
    assert block.collapsed is True and block.cut == 0
    assert text in block.text


def test_tool_results_are_collapsed_and_cut() -> None:
    long = "y" * (TOOL_RESULT_LIMIT + 100)
    blocks = parse(
        user(
            {"type": "tool_result", "tool_use_id": "t1", "content": "short"},
            {"type": "tool_result", "tool_use_id": "t2", "content": long, "is_error": True},
        )
    )
    assert [block.kind for block in blocks] == ["tool_result", "tool_result"]
    assert (blocks[0].title, blocks[0].text, blocks[0].cut, blocks[0].collapsed) == (
        "tool result",
        "short",
        0,
        True,
    )
    assert (blocks[1].title, blocks[1].cut) == ("tool result (error)", 100)
    assert blocks[1].text == "y" * TOOL_RESULT_LIMIT


def test_a_non_text_tool_result_is_rendered_as_text_parts_and_json() -> None:
    content = [
        {"type": "text", "text": "first"},
        {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
        {"type": "text", "text": "second"},
    ]
    (block,) = parse(user({"type": "tool_result", "tool_use_id": "t1", "content": content}))
    image = (
        '{\n  "source": {\n    "data": "AAAA",\n    "type": "base64"\n  },\n  "type": "image"\n}'
    )
    assert block.text == f"first\n{image}\nsecond"


def test_a_user_text_block_is_titled_user() -> None:
    (block,) = parse(user({"type": "text", "text": "Review the diff."}))
    assert (block.kind, block.title, block.text) == ("text", "user", "Review the diff.")


def test_string_content_is_one_text_block() -> None:
    blocks = parse(
        line(type="assistant", message={"content": "plain"}),
        line(type="user", message={"content": "reply"}),
    )
    assert [(block.title, block.text) for block in blocks] == [
        ("assistant", "plain"),
        ("user", "reply"),
    ]


def test_unknown_content_blocks_are_ignored() -> None:
    assert parse(assistant({"type": "server_tool_use", "name": "web_search"})) == []


# --- init, result, omitted, unparseable ------------------------------------------------------


def test_the_init_block_lists_what_it_knows() -> None:
    (block,) = parse(
        line(
            type="system",
            subtype="init",
            model="claude-opus-5",
            claude_code_version="2.1.261",
            cwd="/w",
            permissionMode="auto",
            tools=["Bash", "Read"],
        )
    )
    assert block.kind == "init"
    assert block.text == (
        "model: claude-opus-5\nclaude code: 2.1.261\ncwd: /w\npermission mode: auto\ntools: 2"
    )
    assert parse(line(type="system", subtype="init"))[0].text == ""


def test_the_result_block_carries_subtype_and_text() -> None:
    (ok,) = parse(line(type="result", subtype="success", result="All done."))
    assert (ok.title, ok.text, ok.collapsed) == ("result: success", "All done.", False)
    (failed,) = parse(
        line(type="result", subtype="error_during_execution", is_error=True, errors=["a", "b"])
    )
    assert (failed.title, failed.text) == ("result: error_during_execution", "a\nb")
    (bare,) = parse(line(type="result"))
    assert (bare.title, bare.text) == ("result: unknown", "")


def test_an_omitted_stub_becomes_a_placeholder() -> None:
    (block,) = parse(line(type="issuebot_omitted", original_type="user", bytes=70000))
    assert (block.kind, block.title) == ("omitted", "omitted")
    assert block.text == "a user message of 70000 bytes was not stored"
    (unknown,) = parse(line(type="issuebot_omitted", original_type=None, bytes=5))
    assert unknown.text == "a message of 5 bytes was not stored"


def test_an_unparseable_line_is_shown_cut() -> None:
    junk = "not json " * 50
    (block, array) = parse(junk, "[1, 2]")
    assert (block.kind, block.title) == ("unparseable", "unparseable line")
    assert (block.text, block.cut) == (junk[:UNPARSEABLE_LIMIT], len(junk) - UNPARSEABLE_LIMIT)
    assert (array.kind, array.text, array.cut) == ("unparseable", "[1, 2]", 0)


def test_status_lines_are_counted_not_rendered() -> None:
    result = parse_transcript(
        "\n".join(
            [
                line(type="rate_limit_event"),
                line(type="system", subtype="task_started"),
                line(type="tool_progress"),
                "",
                line(type="result", subtype="success", result="x"),
            ]
        )
    )
    assert [block.kind for block in result.blocks] == ["result"]
    assert result.hidden == 3


def test_an_empty_stream_has_no_blocks() -> None:
    result = parse_transcript("")
    assert (result.blocks, result.hidden) == ([], 0)


@pytest.mark.parametrize("name", ["TOOL_INPUT_COLLAPSE", "TOOL_RESULT_LIMIT", "UNPARSEABLE_LIMIT"])
def test_the_limits_are_module_constants(name: str) -> None:
    assert isinstance(getattr(transcript_module, name), int)
```

- [ ] **Step 2: Run the tests to verify they fail (and that the CLI tests still pass after the move)**

Run: `timeout 300 uv run pytest tests/test_cli.py -q`
Expected: `102 passed` (the move is a refactor; two assertions moved to schema version 2 with the fakes).

Run: `timeout 120 uv run pytest tests/test_web_transcript.py -q`
Expected: collection fails with `ModuleNotFoundError: No module named 'issuebot.web'`.

- [ ] **Step 3: The package and the parser**

Create `src/issuebot/web/__init__.py`:

```python
"""The dashboard: a FastAPI app over the Phase 6 database, its view models and templates."""
```

Create `src/issuebot/web/transcript.py`:

```python
"""Turn a stored stream-json capture into the blocks the turn page renders. No HTML here.

Every ``title`` and ``text`` is plain text; the template escapes them. Status lines the page
does not show (rate limits, task progress, thinking-token counters, ...) are counted, not lost.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from issuebot.db import OMITTED_TYPE

TOOL_INPUT_COLLAPSE = 2 * 1024
TOOL_RESULT_LIMIT = 4 * 1024
UNPARSEABLE_LIMIT = 200

BlockKind = Literal[
    "init", "text", "thinking", "tool_use", "tool_result", "result", "omitted", "unparseable"
]

_INIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("model", "model"),
    ("claude_code_version", "claude code"),
    ("cwd", "cwd"),
    ("permissionMode", "permission mode"),
)


@dataclass(frozen=True, kw_only=True, slots=True)
class Block:
    kind: BlockKind
    title: str  # "assistant", "Bash", "tool result", "result: success", ...
    text: str  # the body, already cut
    cut: int = 0  # characters removed from the body; 0 when whole
    collapsed: bool = False  # rendered inside <details>


@dataclass(frozen=True, kw_only=True, slots=True)
class Transcript:
    blocks: list[Block]
    hidden: int  # status messages not rendered


def parse_transcript(stream: str) -> Transcript:
    """Blocks in stream order; unparseable lines and omitted stubs become placeholders."""
    blocks: list[Block] = []
    hidden = 0
    for line in stream.splitlines():
        if not line.strip():
            continue
        message = _message(line)
        if message is None:
            blocks.append(_cut("unparseable", "unparseable line", line, UNPARSEABLE_LIMIT))
            continue
        kind = message.get("type")
        if kind == "system" and message.get("subtype") == "init":
            blocks.append(_init(message))
        elif kind == "assistant":
            blocks.extend(_content(message, "assistant"))
        elif kind == "user":
            blocks.extend(_content(message, "user"))
        elif kind == "result":
            blocks.append(_result(message))
        elif kind == OMITTED_TYPE:
            blocks.append(_omitted(message))
        else:
            hidden += 1
    return Transcript(blocks=blocks, hidden=hidden)


def _message(line: str) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _init(message: dict[str, Any]) -> Block:
    lines = [f"{label}: {message[key]}" for key, label in _INIT_FIELDS if _text(message.get(key))]
    tools = message.get("tools")
    if isinstance(tools, list):
        lines.append(f"tools: {len(tools)}")
    return Block(kind="init", title="session", text="\n".join(lines))


def _content(message: dict[str, Any], role: str) -> list[Block]:
    inner = message.get("message")
    content = inner.get("content") if isinstance(inner, dict) else None
    if isinstance(content, str):
        return [Block(kind="text", title=role, text=content)]
    if not isinstance(content, list):
        return []
    blocks: list[Block] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            blocks.append(Block(kind="text", title=role, text=_text(part.get("text")) or ""))
        elif kind == "thinking":
            text = _text(part.get("thinking")) or ""
            blocks.append(Block(kind="thinking", title="thinking", text=text, collapsed=True))
        elif kind == "tool_use":
            text = _json(part.get("input"))
            name = _text(part.get("name")) or "tool"
            collapsed = len(text) > TOOL_INPUT_COLLAPSE
            blocks.append(Block(kind="tool_use", title=name, text=text, collapsed=collapsed))
        elif kind == "tool_result":
            title = "tool result (error)" if part.get("is_error") else "tool result"
            body = _result_content(part.get("content"))
            blocks.append(_cut("tool_result", title, body, TOOL_RESULT_LIMIT, collapsed=True))
    return blocks


def _result_content(content: object) -> str:
    """A tool result's content: the string itself, or text parts and the JSON of the rest."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and _text(part.get("text")):
                parts.append(part["text"])
            else:
                parts.append(_json(part))
        return "\n".join(parts)
    return _json(content) if content is not None else ""


def _result(message: dict[str, Any]) -> Block:
    subtype = _text(message.get("subtype")) or "unknown"
    text = _text(message.get("result"))
    if not text:
        errors = message.get("errors")
        text = "\n".join(str(item) for item in errors) if isinstance(errors, list) else ""
    return Block(kind="result", title=f"result: {subtype}", text=text)


def _omitted(message: dict[str, Any]) -> Block:
    original = _text(message.get("original_type"))
    what = f"a {original} message" if original else "a message"
    size = message.get("bytes")
    text = f"{what} of {size} bytes was not stored"
    return Block(kind="omitted", title="omitted", text=text)


def _cut(kind: BlockKind, title: str, text: str, limit: int, *, collapsed: bool = False) -> Block:
    cut = max(len(text) - limit, 0)
    return Block(kind=kind, title=title, text=text[:limit], cut=cut, collapsed=collapsed)


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None
```

In `src/issuebot/db/__init__.py` (edit 1 of 3) replace

```

from issuebot.db.connection import (
```

with

```

from issuebot.agent.turnlog import OMITTED_TYPE, TurnCapture
from issuebot.db.connection import (
```

In `src/issuebot/db/__init__.py` (edit 2 of 3) replace

```
    "MIGRATIONS_ROOT",
    "POSTGRES_SCHEMES",
```

with

```
    "MIGRATIONS_ROOT",
    "OMITTED_TYPE",
    "POSTGRES_SCHEMES",
```

In `src/issuebot/db/__init__.py` (edit 3 of 3) replace

```
    "StoreUnavailableError",
    "TurnRow",
```

with

```
    "StoreUnavailableError",
    "TurnCapture",
    "TurnRow",
```

- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 120 uv run pytest tests/test_web_transcript.py -q && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `17 passed`; then `696 passed, 40 skipped`; then `736 passed`.

- [ ] **Step 5: Commit**

```bash
git status --short && git add --all && git commit -m "feat: the transcript parser; shared FakeDatabase for the CLI and web tests" -m "<trailer>"
```

---

### Task 5: The view models and the JSON API

**Files:**
- Create: `src/issuebot/web/views.py`, `src/issuebot/web/app.py`, `tests/fakes/web.py`
- Modify: `src/issuebot/web/__init__.py`, `pyproject.toml` (one `filterwarnings` entry)
- Test: `tests/test_web_app.py`

**Interfaces:**
- Consumes: Task 3's queries and row types; Task 4's `FakeDatabase`; Phase 6's `Database`, `DatabaseError`; `RuntimeSnapshot.to_dict()` (in the tests, to build honest snapshot rows).
- Produces: `issuebot.web.create_app(database, settings, *, clock=time.monotonic, now=_utcnow) -> FastAPI` with `GET /api/v1/state`, `GET /api/v1/issues/{number}`, `GET /api/v1/stats?window=`, `POST /api/v1/refresh`, `GET /healthz`, JSON error envelopes under `/api/` and `/healthz`, a plain-text placeholder elsewhere (Task 6 replaces it with `error.html`), the four security headers on every response; `SECURITY_HEADERS`, `JSON_PREFIXES`, `envelope(status, code, message)`; `issuebot.web.views`: `LIVE_POLL_S = 10`, `CHART_POLL_S = 60`, `STALE_FACTOR = 3`, `REFRESH_MIN_INTERVAL_S = 5.0`, `RECENT_EVENTS_LIMIT = 50`, `RUN_ID_PATTERN`, `iso`, `snapshot_age_s`, `poll_interval_s`, `worker_status`, `window_days`, `safe_href`, `turn_url`, `turn_label`, `running_entry`, `retry_entry`, `state_document`, `stats_document`, `issue_document` (its `runs[]` carry a `"turns"` list here; Task 7 renames it), `row_dict`, `describe_event`; `fakes.web`: `NOW`, `RUN_ID`, `SETTINGS`, `Clock`, `running_row`, `retry_row`, `snapshot`, `issue_row`, `run_row`, `turn_summary`, `turn_row`, `event_row`, `Harness` (a `FakeDatabase`, a `Clock`, a `TestClient` over `create_app`, `seed_issue()`).

Spec: §6.1, §6.3, §6.4.

- [ ] **Step 1: Write the failing tests**

Create `tests/fakes/web.py`:

```python
"""Builders for the web tests: rows shaped like the query module's, a clock, a test client."""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from fakes.database import FakeDatabase
from issuebot.config import GitHubSettings, Settings
from issuebot.db.queries import (
    EventRow,
    IssueRow,
    RunRow,
    SnapshotRow,
    TurnRow,
    TurnSummaryRow,
)
from issuebot.orchestrator.state import (
    ClaudeTotals,
    Counters,
    RetryRow,
    RunningRow,
    RuntimeSnapshot,
)
from issuebot.web import create_app

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
RUN_ID = "20260904T202535Z-0964cd"
SETTINGS = Settings(github=GitHubSettings(repo="example/repo"))


class Clock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.now = NOW

    def __call__(self) -> float:
        return self.mono

    def utcnow(self) -> datetime:
        return self.now


def running_row(**overrides: Any) -> RunningRow:
    fields: dict[str, Any] = {
        "issue_number": 7,
        "identifier": "repo-7",
        "title": "Add a power function",
        "url": "https://github.com/example/repo/issues/7",
        "state": "in_progress",
        "attempt": 1,
        "rework": False,
        "resumed": False,
        "run_id": RUN_ID,
        "session_id": "sess-7",
        "started_at": NOW - timedelta(minutes=3),
        "last_activity_at": NOW - timedelta(seconds=20),
        "last_event": "turn_activity",
        "turns": 1,
        "stop_cause": None,
    }
    fields.update(overrides)
    return RunningRow(**fields)


def retry_row(**overrides: Any) -> RetryRow:
    fields: dict[str, Any] = {
        "issue_number": 9,
        "identifier": "repo-9",
        "url": "https://github.com/example/repo/issues/9",
        "attempt": 2,
        "kind": "failure",
        "due_at": NOW + timedelta(seconds=20),
        "error": "turn_failed: boom",
    }
    fields.update(overrides)
    return RetryRow(**fields)


def snapshot(
    *,
    running: tuple[RunningRow, ...] = (),
    retrying: tuple[RetryRow, ...] = (),
    age_s: float = 5.0,
    poll_interval_ms: int = 30_000,
) -> SnapshotRow:
    data = RuntimeSnapshot(
        at=NOW - timedelta(seconds=age_s + 1),
        workflow_path="/app/WORKFLOW.md",
        workflow_mtime_ns=1,
        config_valid=True,
        config_error=None,
        poll_interval_ms=poll_interval_ms,
        max_concurrent_agents=2,
        tick_count=41,
        last_tick_at=NOW - timedelta(seconds=age_s + 1),
        running=running,
        retrying=retrying,
        totals=ClaudeTotals(
            input_tokens=1000, output_tokens=50, cost_usd=1.25, seconds_running=90.0
        ),
        counters=Counters(runs_started=3, runs_ended=2, issues_completed=1),
    ).to_dict()
    at = NOW - timedelta(seconds=age_s + 1)
    return SnapshotRow(at=at, written_at=NOW - timedelta(seconds=age_s), data=data)


def issue_row(**overrides: Any) -> IssueRow:
    fields: dict[str, Any] = {
        "number": 7,
        "identifier": "repo-7",
        "title": "Add a power function",
        "state": "review",
        "state_label": "issuebot/review",
        "github_state": "open",
        "url": "https://github.com/example/repo/issues/7",
        "labels": ["issuebot/review"],
        "pr_number": 8,
        "pr_url": "https://github.com/example/repo/pull/8",
        "pr_state": "open",
        "pr_merged_at": None,
        "created_at": NOW - timedelta(days=1),
        "updated_at": NOW - timedelta(hours=1),
        "closed_at": None,
        "seen_at": NOW - timedelta(minutes=1),
    }
    fields.update(overrides)
    return IssueRow(**fields)


def run_row(**overrides: Any) -> RunRow:
    fields: dict[str, Any] = {
        "run_id": RUN_ID,
        "issue_number": 7,
        "issue_identifier": "repo-7",
        "attempt": 1,
        "session_id": "sess-7",
        "started_at": NOW - timedelta(hours=2),
        "ended_at": NOW - timedelta(hours=2) + timedelta(minutes=4),
        "outcome": "succeeded",
        "error": None,
        "turns": 1,
        "input_tokens": 513338,
        "output_tokens": 8425,
        "cost_usd": 0.8976,
        "duration_s": 205.4,
        "workspace_path": "/workspaces/repo-7",
        "log_dir": f"/workspaces/repo-7/.issuebot/runs/{RUN_ID}",
    }
    fields.update(overrides)
    return RunRow(**fields)


def turn_summary(**overrides: Any) -> TurnSummaryRow:
    fields: dict[str, Any] = {
        "run_id": RUN_ID,
        "turn_number": 1,
        "captured_at": NOW - timedelta(hours=2) + timedelta(minutes=4),
        "model": "claude-opus-5",
        "subtype": "success",
        "is_error": False,
        "num_turns": 19,
        "input_tokens": 38,
        "cache_creation_input_tokens": 23100,
        "cache_read_input_tokens": 490200,
        "output_tokens": 8425,
        "cost_usd": 0.8976,
        "duration_ms": 201719,
        "result_text": "Done.",
        "prompt_bytes": 10106,
        "stream_bytes": 115429,
        "stream_lines": 95,
        "omitted_lines": 0,
        "stderr_bytes": 0,
        "truncated": False,
    }
    fields.update(overrides)
    return TurnSummaryRow(**fields)


def turn_row(**overrides: Any) -> TurnRow:
    summary = turn_summary()
    fields: dict[str, Any] = {name: getattr(summary, name) for name in summary.__slots__}
    fields.update(
        {
            "prompt": "You are working on GitHub issue `repo-7` (#7).\n\n<b>bold</b>",
            "stream": "\n".join(
                [
                    '{"type":"system","subtype":"init","model":"claude-opus-5"}',
                    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",'
                    '"input":{"command":"ls <dir>"}}]}}',
                    '{"type":"user","message":{"content":[{"type":"tool_result",'
                    '"tool_use_id":"t","content":"README.md"}]}}',
                    '{"type":"rate_limit_event"}',
                    '{"type":"result","subtype":"success","result":"Done <script>x</script>"}',
                ]
            )
            + "\n",
            "stderr": "warning: something\n",
        }
    )
    fields.update(overrides)
    return TurnRow(**fields)


def event_row(**overrides: Any) -> EventRow:
    fields: dict[str, Any] = {
        "id": 11,
        "at": NOW - timedelta(hours=1),
        "kind": "run_ended",
        "issue_number": 7,
        "run_id": RUN_ID,
        "payload": {
            "kind": "run_ended",
            "run_id": RUN_ID,
            "outcome": "succeeded",
            "turns": 1,
            "cost_usd": 0.8976,
            "input_tokens": 513338,
            "output_tokens": 8425,
            "error": None,
        },
    }
    fields.update(overrides)
    return EventRow(**fields)


class Harness:
    def __init__(self) -> None:
        self.database = FakeDatabase()
        self.queries = self.database.queries_obj
        self.clock = Clock()
        self.client = TestClient(
            create_app(self.database, SETTINGS, clock=self.clock, now=self.clock.utcnow),
            raise_server_exceptions=False,
        )

    def seed_issue(self) -> None:
        self.queries.issue_rows[7] = issue_row()
        self.queries.runs_by_issue[7] = [run_row()]
        self.queries.turns_by_issue[7] = [turn_summary()]
        self.queries.turn_rows[(RUN_ID, 1)] = turn_row()
        self.queries.events_by_issue[7] = [
            event_row(),
            event_row(
                id=10,
                kind="state_changed",
                run_id=None,
                payload={
                    "kind": "state_changed",
                    "from_label": "issuebot/in-progress",
                    "to_label": "issuebot/review",
                    "actor": "agent",
                    "pr_url": "https://github.com/example/repo/pull/8",
                },
            ),
        ]
```

Create `tests/test_web_app.py`:

```python
"""Tests for the web app's JSON API and health check against a FakeDatabase (hermetic)."""

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest

from fakes.web import (
    NOW,
    RUN_ID,
    Harness,
    event_row,
    issue_row,
    retry_row,
    running_row,
    snapshot,
)
from issuebot.db import MAX_WINDOW_DAYS, StoreUnavailableError
from issuebot.db.queries import DailyPoint
from issuebot.web import REFRESH_MIN_INTERVAL_S, SECURITY_HEADERS, STALE_FACTOR
from issuebot.web.views import describe_event, safe_href, window_days, worker_status


@pytest.fixture
def h() -> Iterator[Harness]:
    harness = Harness()
    with harness.client:
        yield harness


# --- /api/v1/state ---------------------------------------------------------------------------


def test_state_reshapes_the_snapshot(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(running=(running_row(),), retrying=(retry_row(),))
    response = h.client.get("/api/v1/state")
    assert response.status_code == 200
    body = response.json()
    assert body["generated_at"] == (NOW - timedelta(seconds=6)).isoformat()
    assert body["written_at"] == (NOW - timedelta(seconds=5)).isoformat()
    assert body["snapshot_age_s"] == 5.0
    assert body["worker"] == {
        "tick_count": 41,
        "last_tick_at": (NOW - timedelta(seconds=6)).isoformat(),
        "poll_interval_ms": 30000,
        "max_concurrent_agents": 2,
        "workflow_path": "/app/WORKFLOW.md",
        "config_valid": True,
        "config_error": None,
        "stale": False,
    }
    assert body["counts"] == {"running": 1, "retrying": 1}
    (running,) = body["running"]
    assert running == {
        "issue_id": "7",
        "issue_identifier": "repo-7",
        "issue_number": 7,
        "issue_url": "https://github.com/example/repo/issues/7",
        "title": "Add a power function",
        "state": "in_progress",
        "run_id": RUN_ID,
        "session_id": "sess-7",
        "attempt": 1,
        "rework": False,
        "resumed": False,
        "turn_count": 1,
        "last_event": "turn_activity",
        "started_at": (NOW - timedelta(minutes=3)).isoformat(),
        "last_event_at": (NOW - timedelta(seconds=20)).isoformat(),
        "stop_cause": None,
    }
    (retrying,) = body["retrying"]
    assert retrying == {
        "issue_id": "9",
        "issue_identifier": "repo-9",
        "issue_number": 9,
        "issue_url": "https://github.com/example/repo/issues/9",
        "attempt": 2,
        "kind": "failure",
        "due_at": (NOW + timedelta(seconds=20)).isoformat(),
        "error": "turn_failed: boom",
    }
    assert body["claude_totals"] == {
        "input_tokens": 1000,
        "output_tokens": 50,
        "total_tokens": 1050,
        "cost_usd": 1.25,
        "seconds_running": 90.0,
    }
    assert body["counters"] == {
        "runs_started": 3,
        "runs_ended": 2,
        "issues_completed": 1,
        "issues_cancelled": 0,
        "blocked": 0,
    }


def test_state_without_a_snapshot_is_empty_not_missing(h: Harness) -> None:
    body = h.client.get("/api/v1/state").json()
    assert (body["generated_at"], body["written_at"], body["snapshot_age_s"]) == (None, None, None)
    assert body["worker"] is None
    assert (body["counts"], body["running"], body["retrying"]) == (
        {"running": 0, "retrying": 0},
        [],
        [],
    )
    assert body["claude_totals"]["total_tokens"] == 0 and body["counters"]["blocked"] == 0


def test_state_marks_a_stale_worker(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(age_s=STALE_FACTOR * 30 + 1)
    assert h.client.get("/api/v1/state").json()["worker"]["stale"] is True


# --- /api/v1/issues/{number} -----------------------------------------------------------------


def test_issue_document(h: Harness) -> None:
    h.seed_issue()
    h.queries.snapshot_row = snapshot(running=(running_row(),))
    response = h.client.get("/api/v1/issues/7")
    assert response.status_code == 200
    body = response.json()
    assert body["issue"]["number"] == 7 and body["issue"]["state"] == "review"
    assert body["issue"]["updated_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert body["issue"]["labels"] == ["issuebot/review"]
    assert body["running"]["run_id"] == RUN_ID and body["retry"] is None
    (run,) = body["runs"]
    assert (run["run_id"], run["outcome"], run["cost_usd"]) == (RUN_ID, "succeeded", 0.8976)
    (turn,) = run["turns"]
    assert (turn["turn_number"], turn["model"], turn["num_turns"]) == (1, "claude-opus-5", 19)
    assert turn["url"] == f"/issues/7/runs/{RUN_ID}/turns/1"
    assert "stream" not in turn and "prompt" not in turn
    assert body["logs"] == [
        {
            "run_id": RUN_ID,
            "turn_number": 1,
            "label": f"run {RUN_ID} turn 1",
            "url": f"/issues/7/runs/{RUN_ID}/turns/1",
        }
    ]
    assert [event["kind"] for event in body["recent_events"]] == ["run_ended", "state_changed"]
    assert body["recent_events"][0]["payload"]["outcome"] == "succeeded"
    assert body["recent_events"][0]["at"] == (NOW - timedelta(hours=1)).isoformat()


def test_issue_with_a_retry_entry(h: Harness) -> None:
    h.queries.issue_rows[9] = issue_row(number=9, identifier="repo-9", state="todo")
    h.queries.snapshot_row = snapshot(retrying=(retry_row(),))
    body = h.client.get("/api/v1/issues/9").json()
    assert body["running"] is None and body["retry"]["kind"] == "failure"
    assert body["runs"] == [] and body["logs"] == []


def test_unknown_issue_is_a_404_envelope(h: Harness) -> None:
    response = h.client.get("/api/v1/issues/99")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "unknown_issue", "message": "issue #99 is not known"}
    }


def test_a_non_numeric_issue_is_a_404_envelope(h: Harness) -> None:
    response = h.client.get("/api/v1/issues/abc")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- /api/v1/stats -----------------------------------------------------------------------------


def test_stats_default_window(h: Harness) -> None:
    h.queries.closed = {7: 2, 1: 1}
    h.queries.runs = {7: 3, 1: 1}
    h.queries.counts = {"todo": 1, "in_progress": 0, "review": 1, "rework": 0, "complete": 3}
    h.queries.series = [DailyPoint(day=date(2026, 9, 4), closed=1, runs=1)]
    body = h.client.get("/api/v1/stats").json()
    assert body == {
        "window": "7d",
        "days": 7,
        "closed": 2,
        "runs": 3,
        "by_state": {"todo": 1, "in_progress": 0, "review": 1, "rework": 0, "complete": 3},
        "series": [{"day": "2026-09-04", "closed": 1, "runs": 1}],
    }
    assert h.queries.days_asked == 7


def test_stats_thirty_day_window(h: Harness) -> None:
    h.queries.closed[30] = 5
    h.queries.runs[30] = 9
    body = h.client.get("/api/v1/stats?window=30d").json()
    assert (body["window"], body["days"], body["closed"], body["runs"]) == ("30d", 30, 5, 9)
    assert h.queries.days_asked == 30


@pytest.mark.parametrize("window", ["0d", f"{MAX_WINDOW_DAYS + 1}d", "7", "x", "7D", "-3d"])
def test_stats_rejects_a_bad_window(h: Harness, window: str) -> None:
    response = h.client.get("/api/v1/stats", params={"window": window})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_window"
    assert f"1 <= N <= {MAX_WINDOW_DAYS}" in response.json()["error"]["message"]


# --- POST /api/v1/refresh ----------------------------------------------------------------------


def test_refresh_notifies_then_coalesces_then_notifies_again(h: Harness) -> None:
    first = h.client.post("/api/v1/refresh")
    assert first.status_code == 202
    assert first.json() == {
        "queued": True,
        "coalesced": False,
        "requested_at": NOW.isoformat(),
        "operations": ["poll", "reconcile"],
    }
    h.clock.mono += REFRESH_MIN_INTERVAL_S - 0.5
    second = h.client.post("/api/v1/refresh")
    assert second.status_code == 202
    assert (second.json()["queued"], second.json()["coalesced"]) == (False, True)
    assert h.database.notified == 1
    h.clock.mono += 0.5
    third = h.client.post("/api/v1/refresh")
    assert (third.json()["queued"], third.json()["coalesced"]) == (True, False)
    assert h.database.notified == 2


def test_refresh_reports_a_database_failure(h: Harness) -> None:
    h.database.notify_error = StoreUnavailableError("cannot connect: refused")
    response = h.client.post("/api/v1/refresh")
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "database_unavailable", "message": "cannot connect: refused"}
    }
    assert "s3cret" not in response.text


def test_refresh_only_accepts_post(h: Harness) -> None:
    response = h.client.get("/api/v1/refresh")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


# --- /healthz ------------------------------------------------------------------------------------


def test_healthz_ok_stale_and_none(h: Harness) -> None:
    assert h.client.get("/healthz").json() == {
        "status": "ok",
        "database": "ok",
        "snapshot_at": None,
        "snapshot_age_s": None,
        "worker": "none",
    }
    h.queries.snapshot_row = snapshot(age_s=5.0)
    body = h.client.get("/healthz").json()
    assert (body["worker"], body["snapshot_age_s"]) == ("ok", 5.0)
    assert body["snapshot_at"] == (NOW - timedelta(seconds=6)).isoformat()
    h.queries.snapshot_row = snapshot(age_s=91.0)
    assert h.client.get("/healthz").json()["worker"] == "stale"


def test_healthz_reports_an_unreachable_database(h: Harness) -> None:
    h.queries.error = StoreUnavailableError("cannot connect: refused")
    response = h.client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "unavailable",
        "error": "cannot connect: refused",
    }


# --- errors and headers -------------------------------------------------------------------------


def test_a_database_error_in_the_api_is_a_503_envelope(h: Harness) -> None:
    h.queries.error = StoreUnavailableError("cannot connect: refused")
    response = h.client.get("/api/v1/state")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "database_unavailable",
        "message": "cannot connect: refused",
    }


def test_unknown_api_paths_and_methods_get_envelopes(h: Harness) -> None:
    missing = h.client.get("/api/v1/nothing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    wrong = h.client.delete("/api/v1/state")
    assert wrong.status_code == 405
    assert wrong.json()["error"]["code"] == "method_not_allowed"


def test_every_response_carries_the_security_headers(h: Harness) -> None:
    h.queries.snapshot_row = snapshot()
    for response in (
        h.client.get("/api/v1/state"),
        h.client.get("/healthz"),
        h.client.get("/api/v1/nothing"),
        h.client.post("/api/v1/refresh"),
    ):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (response.url, name)
    assert "'unsafe-inline'" not in SECURITY_HEADERS["Content-Security-Policy"]
    assert "'unsafe-eval'" not in SECURITY_HEADERS["Content-Security-Policy"]


def test_each_request_uses_one_connection(h: Harness) -> None:
    h.seed_issue()
    h.client.get("/api/v1/issues/7")
    assert h.database.opened == 1
    h.client.get("/api/v1/stats")
    assert h.database.opened == 2


# --- the pure builders --------------------------------------------------------------------------


def test_safe_href() -> None:
    assert safe_href("https://github.com/example/repo/issues/7") == (
        "https://github.com/example/repo/issues/7"
    )
    assert safe_href("http://github.com/x") is None
    assert safe_href("javascript:alert(1)") is None
    assert safe_href(None) is None
    assert safe_href(7) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "days"),
    [
        (None, 7),
        ("", 7),
        ("1d", 1),
        ("7d", 7),
        ("30d", 30),
        (f"{MAX_WINDOW_DAYS}d", MAX_WINDOW_DAYS),
    ],
)
def test_window_days_accepts(text: str | None, days: int) -> None:
    assert window_days(text) == days


@pytest.mark.parametrize("text", ["0d", "366d", "7", "d", "7D", "1.5d", " 7d", "-1d"])
def test_window_days_rejects(text: str) -> None:
    assert window_days(text) is None


def test_worker_status() -> None:
    assert worker_status(None, NOW) == "none"
    assert worker_status(snapshot(age_s=89.9), NOW) == "ok"
    assert worker_status(snapshot(age_s=90.1), NOW) == "stale"
    assert worker_status(snapshot(age_s=20.0, poll_interval_ms=5_000), NOW) == "stale"
    row = snapshot(age_s=100.0)
    row.data.pop("poll_interval_ms")  # an older worker's snapshot: assume 30 s
    assert worker_status(row, NOW) == "stale"


@pytest.mark.parametrize(
    ("kind", "payload", "text"),
    [
        (
            "state_changed",
            {
                "from_label": "issuebot/todo",
                "to_label": "issuebot/in-progress",
                "actor": "issuebot",
            },
            "issuebot changed issuebot/todo to issuebot/in-progress",
        ),
        (
            "state_changed",
            {"from_label": None, "to_label": "issuebot/todo", "actor": "human"},
            "human changed unlabelled to issuebot/todo",
        ),
        (
            "state_changed",
            {
                "from_label": "issuebot/in-progress",
                "to_label": "issuebot/review",
                "actor": "agent",
                "pr_url": "https://github.com/example/repo/pull/8",
            },
            "agent changed issuebot/in-progress to issuebot/review "
            "(https://github.com/example/repo/pull/8)",
        ),
        (
            "state_changed",
            {"from_label": "issuebot/review", "to_label": None, "actor": "issuebot"},
            "issuebot changed issuebot/review to unlabelled",
        ),
        ("run_started", {"run_id": RUN_ID, "attempt": 2}, f"run {RUN_ID} started (attempt 2)"),
        (
            "run_ended",
            {
                "run_id": RUN_ID,
                "outcome": "succeeded",
                "turns": 1,
                "cost_usd": 0.8976,
                "input_tokens": 513338,
                "output_tokens": 8425,
                "error": None,
            },
            f"run {RUN_ID} succeeded after 1 turn, $0.90, 513338 in / 8425 out",
        ),
        (
            "run_ended",
            {
                "run_id": RUN_ID,
                "outcome": "failed",
                "turns": 2,
                "cost_usd": 0.5,
                "input_tokens": 10,
                "output_tokens": 1,
                "error": "turn_failed: boom",
            },
            f"run {RUN_ID} failed after 2 turns, $0.50, 10 in / 1 out: turn_failed: boom",
        ),
        (
            "pr_opened",
            {"pr_number": 8, "pr_url": "https://github.com/example/repo/pull/8"},
            "pull request #8 opened (https://github.com/example/repo/pull/8)",
        ),
        ("blocked", {"reason": "turn budget exhausted"}, "blocked: turn budget exhausted"),
        (
            "issue_completed",
            {"pr_url": "https://github.com/example/repo/pull/8"},
            "completed (https://github.com/example/repo/pull/8)",
        ),
        ("issue_completed", {"pr_url": None}, "completed"),
        (
            "issue_cancelled",
            {"reason": "closed without a merged pull request"},
            "cancelled: closed without a merged pull request",
        ),
        (
            "notification_sent",
            {"channel": "slack", "about_kind": "blocked"},
            "slack notified about blocked",
        ),
        ("something_new", {"x": 1}, "something_new"),
        ("run_ended", {}, "run ? ? after 0 turns, $0.00, 0 in / 0 out"),
    ],
)
def test_describe_event(kind: str, payload: dict[str, Any], text: str) -> None:
    assert describe_event(event_row(kind=kind, payload=payload)) == text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `timeout 120 uv run pytest tests/test_web_app.py -q`
Expected: collection fails with `ImportError: cannot import name 'create_app' from 'issuebot.web'` (raised while importing `fakes.web`).

- [ ] **Step 3: The views, the app, the package exports and the warning filter**

Create `src/issuebot/web/views.py`:

```python
"""Pure builders for what the pages and the API show: no I/O, no HTML, JSON-safe values only."""

import re
from dataclasses import fields
from datetime import date, datetime
from typing import Any, Literal

from issuebot.db.queries import (
    MAX_WINDOW_DAYS,
    DailyPoint,
    EventRow,
    IssueRow,
    RunRow,
    SnapshotRow,
    TurnSummaryRow,
)

LIVE_POLL_S = 10
CHART_POLL_S = 60
STALE_FACTOR = 3
REFRESH_MIN_INTERVAL_S = 5.0
RECENT_EVENTS_LIMIT = 50
RUN_ID_PATTERN = r"^\d{8}T\d{6}Z-[0-9a-f]{6}$"
DEFAULT_WINDOW_DAYS = 7
DEFAULT_POLL_INTERVAL_MS = 30_000

WorkerStatus = Literal["ok", "stale", "none"]

_WINDOW = re.compile(r"^(\d+)d$")
_WORKER_KEYS = (
    "tick_count",
    "last_tick_at",
    "poll_interval_ms",
    "max_concurrent_agents",
    "workflow_path",
    "config_valid",
    "config_error",
)
_TOTAL_KEYS = ("input_tokens", "output_tokens", "total_tokens")
_COUNTER_KEYS = ("runs_started", "runs_ended", "issues_completed", "issues_cancelled", "blocked")


def iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def snapshot_age_s(row: SnapshotRow, now: datetime) -> float:
    return round(max((now - row.written_at).total_seconds(), 0.0), 3)


def poll_interval_s(row: SnapshotRow) -> float:
    """The worker's poll interval from its snapshot; 30 s when the snapshot lacks it."""
    value = row.data.get("poll_interval_ms")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value) / 1000
    return DEFAULT_POLL_INTERVAL_MS / 1000


def worker_status(row: SnapshotRow | None, now: datetime) -> WorkerStatus:
    """``none`` without a snapshot, ``stale`` past STALE_FACTOR poll intervals, else ``ok``."""
    if row is None:
        return "none"
    return "stale" if snapshot_age_s(row, now) > STALE_FACTOR * poll_interval_s(row) else "ok"


def window_days(text: str | None) -> int | None:
    """``<N>d`` with 1 <= N <= MAX_WINDOW_DAYS as N; the default when absent; None when bad."""
    if not text:
        return DEFAULT_WINDOW_DAYS
    match = _WINDOW.match(text)
    if match is None:
        return None
    days = int(match.group(1))
    return days if 1 <= days <= MAX_WINDOW_DAYS else None


def safe_href(url: object) -> str | None:
    """The URL when it is an https URL, else None (so a template renders no link at all)."""
    return url if isinstance(url, str) and url.startswith("https://") else None


def turn_url(number: int, run_id: str, turn_number: int) -> str:
    return f"/issues/{number}/runs/{run_id}/turns/{turn_number}"


def turn_label(run_id: str, turn_number: int) -> str:
    return f"run {run_id} turn {turn_number}"


# --- the API documents ----------------------------------------------------------------------


def running_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """One RunningRow (as the worker serialised it) in the Symphony shape."""
    number = entry.get("issue_number")
    return {
        "issue_id": str(number) if number is not None else None,
        "issue_identifier": entry.get("identifier"),
        "issue_number": number,
        "issue_url": entry.get("url"),
        "title": entry.get("title"),
        "state": entry.get("state"),
        "run_id": entry.get("run_id"),
        "session_id": entry.get("session_id"),
        "attempt": entry.get("attempt"),
        "rework": entry.get("rework"),
        "resumed": entry.get("resumed"),
        "turn_count": entry.get("turns"),
        "last_event": entry.get("last_event"),
        "started_at": entry.get("started_at"),
        "last_event_at": entry.get("last_activity_at"),
        "stop_cause": entry.get("stop_cause"),
    }


def retry_entry(entry: dict[str, Any]) -> dict[str, Any]:
    number = entry.get("issue_number")
    return {
        "issue_id": str(number) if number is not None else None,
        "issue_identifier": entry.get("identifier"),
        "issue_number": number,
        "issue_url": entry.get("url"),
        "attempt": entry.get("attempt"),
        "kind": entry.get("kind"),
        "due_at": entry.get("due_at"),
        "error": entry.get("error"),
    }


def _entries(row: SnapshotRow | None, key: str) -> list[dict[str, Any]]:
    if row is None:
        return []
    value = row.data.get(key)
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def state_document(row: SnapshotRow | None, now: datetime) -> dict[str, Any]:
    """GET /api/v1/state: the worker's last snapshot reshaped; empty, not missing, without one."""
    running = [running_entry(entry) for entry in _entries(row, "running")]
    retrying = [retry_entry(entry) for entry in _entries(row, "retrying")]
    data = row.data if row is not None else {}
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    counters = data.get("counters") if isinstance(data.get("counters"), dict) else {}
    worker = None
    if row is not None:
        worker = {key: data.get(key) for key in _WORKER_KEYS}
        worker["stale"] = worker_status(row, now) == "stale"
    return {
        "generated_at": iso(row.at) if row is not None else None,
        "written_at": iso(row.written_at) if row is not None else None,
        "snapshot_age_s": snapshot_age_s(row, now) if row is not None else None,
        "worker": worker,
        "counts": {"running": len(running), "retrying": len(retrying)},
        "running": running,
        "retrying": retrying,
        "claude_totals": {
            **{key: _int(totals.get(key)) for key in _TOTAL_KEYS},
            "cost_usd": _float(totals.get("cost_usd")),
            "seconds_running": _float(totals.get("seconds_running")),
        },
        "counters": {key: _int(counters.get(key)) for key in _COUNTER_KEYS},
    }


def stats_document(
    days: int, closed: int, runs: int, counts: dict[str, int], series: list[DailyPoint]
) -> dict[str, Any]:
    """GET /api/v1/stats."""
    return {
        "window": f"{days}d",
        "days": days,
        "closed": closed,
        "runs": runs,
        "by_state": dict(counts),
        "series": [
            {"day": iso(point.day), "closed": point.closed, "runs": point.runs} for point in series
        ],
    }


def issue_document(
    issue: IssueRow,
    runs: list[RunRow],
    turns: list[TurnSummaryRow],
    events: list[EventRow],
    snapshot: SnapshotRow | None,
) -> dict[str, Any]:
    """GET /api/v1/issues/<n>: the row, the snapshot's entries for it, runs with turns, events."""
    running = next(
        (
            entry
            for entry in _entries(snapshot, "running")
            if entry.get("issue_number") == issue.number
        ),
        None,
    )
    retry = next(
        (
            entry
            for entry in _entries(snapshot, "retrying")
            if entry.get("issue_number") == issue.number
        ),
        None,
    )
    run_documents = []
    logs = []
    for run in runs:
        run_turns = []
        for turn in turns:
            if turn.run_id != run.run_id:
                continue
            url = turn_url(issue.number, run.run_id, turn.turn_number)
            run_turns.append({**row_dict(turn), "url": url})
            logs.append(
                {
                    "run_id": run.run_id,
                    "turn_number": turn.turn_number,
                    "label": turn_label(run.run_id, turn.turn_number),
                    "url": url,
                }
            )
        run_documents.append({**row_dict(run), "turns": run_turns})
    return {
        "issue": row_dict(issue),
        "running": running_entry(running) if running is not None else None,
        "retry": retry_entry(retry) if retry is not None else None,
        "runs": run_documents,
        "logs": logs,
        "recent_events": [row_dict(event) for event in events],
    }


def row_dict(row: object) -> dict[str, Any]:
    """A frozen row as a JSON-safe dict: datetimes and dates as ISO 8601 strings."""
    return {f.name: _json_value(getattr(row, f.name)) for f in fields(row)}  # type: ignore[arg-type]


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


# --- one line per event -----------------------------------------------------------------------


def describe_event(event: EventRow) -> str:
    """A one-line description of an event for the issue page; plain text, kind-specific."""
    payload = event.payload
    kind = event.kind
    if kind == "state_changed":
        before = payload.get("from_label") or "unlabelled"
        after = payload.get("to_label") or "unlabelled"
        text = f"{payload.get('actor', '?')} changed {before} to {after}"
        return _with_pr(text, payload.get("pr_url"))
    if kind == "run_started":
        return f"run {payload.get('run_id', '?')} started (attempt {payload.get('attempt', '?')})"
    if kind == "run_ended":
        turns = _int(payload.get("turns"))
        word = "turn" if turns == 1 else "turns"
        text = (
            f"run {payload.get('run_id', '?')} {payload.get('outcome', '?')} after {turns} {word}, "
            f"${_float(payload.get('cost_usd')):.2f}, {_int(payload.get('input_tokens'))} in / "
            f"{_int(payload.get('output_tokens'))} out"
        )
        error = payload.get("error")
        return f"{text}: {error}" if error else text
    if kind == "pr_opened":
        return _with_pr(
            f"pull request #{payload.get('pr_number', '?')} opened", payload.get("pr_url")
        )
    if kind == "blocked":
        return f"blocked: {payload.get('reason', '?')}"
    if kind == "issue_completed":
        return _with_pr("completed", payload.get("pr_url"))
    if kind == "issue_cancelled":
        return f"cancelled: {payload.get('reason', '?')}"
    if kind == "notification_sent":
        return f"{payload.get('channel', '?')} notified about {payload.get('about_kind', '?')}"
    return kind


def _with_pr(text: str, pr_url: object) -> str:
    return f"{text} ({pr_url})" if pr_url else text
```

Create `src/issuebot/web/app.py`:

```python
"""The FastAPI app: the JSON API, the health check, error envelopes and response headers.

Every request that reads opens one connection through ``Database.queries()`` and closes it when
the response is built. The app never writes to a table; its one write is ``NOTIFY``.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from issuebot.config import Settings
from issuebot.db import MAX_WINDOW_DAYS, Database, DatabaseError
from issuebot.log import get_logger
from issuebot.web.views import (
    RECENT_EVENTS_LIMIT,
    REFRESH_MIN_INTERVAL_S,
    iso,
    issue_document,
    snapshot_age_s,
    state_document,
    stats_document,
    window_days,
    worker_status,
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
JSON_PREFIXES = ("/api/", "/healthz")
_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith(JSON_PREFIXES)


def envelope(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class _Refresh:
    """The refresh throttle: at most one NOTIFY per REFRESH_MIN_INTERVAL_S from this process."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.last: float | None = None

    def coalesced(self) -> bool:
        """True when a NOTIFY went out less than the interval ago (so this one is skipped)."""
        last = self.last
        return last is not None and self._clock() - last < REFRESH_MIN_INTERVAL_S

    def sent(self) -> None:
        self.last = self._clock()


def create_app(
    database: Database,
    settings: Settings,
    *,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    """The dashboard app over ``database``; ``clock`` and ``now`` are seams for tests."""
    app = FastAPI(title="issuebot", docs_url=None, redoc_url=None, openapi_url=None)
    log = get_logger(__name__)
    refresh = _Refresh(clock)
    app.state.settings = settings

    def error_response(request: Request, status: int, code: str, message: str) -> Response:
        if _wants_json(request):
            return envelope(status, code, message)
        return PlainTextResponse(f"{status} {message}", status_code=status)

    @app.middleware("http")
    async def add_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    @app.exception_handler(DatabaseError)
    async def database_error(request: Request, exc: DatabaseError) -> Response:
        log.warning("web_database_error", path=request.url.path, error=exc.message)
        return error_response(request, 503, "database_unavailable", exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        code = _HTTP_CODES.get(exc.status_code, f"http_{exc.status_code}")
        return error_response(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        return error_response(request, 404, "not_found", "not found")

    # --- the JSON API -----------------------------------------------------------------------

    @app.get("/api/v1/state")
    async def api_state() -> JSONResponse:
        async with database.queries() as queries:
            row = await queries.snapshot()
        return JSONResponse(state_document(row, now()))

    @app.get("/api/v1/issues/{number}")
    async def api_issue(number: int) -> JSONResponse:
        async with database.queries() as queries:
            issue = await queries.issue(number)
            if issue is None:
                return envelope(404, "unknown_issue", f"issue #{number} is not known")
            runs = await queries.runs_for_issue(number)
            turns = await queries.turn_summaries_for_issue(number)
            events = await queries.events_for_issue(number, RECENT_EVENTS_LIMIT)
            snapshot = await queries.snapshot()
        return JSONResponse(issue_document(issue, runs, turns, events, snapshot))

    @app.get("/api/v1/stats")
    async def api_stats(window: str | None = None) -> JSONResponse:
        days = window_days(window)
        if days is None:
            message = f"window must be <N>d with 1 <= N <= {MAX_WINDOW_DAYS}"
            return envelope(400, "invalid_window", message)
        async with database.queries() as queries:
            closed = await queries.closed_count(timedelta(days=days))
            runs = await queries.runs_count(timedelta(days=days))
            counts = await queries.state_counts()
            series = await queries.daily_series(days)
        return JSONResponse(stats_document(days, closed, runs, counts, series))

    @app.post("/api/v1/refresh")
    async def api_refresh() -> JSONResponse:
        coalesced = refresh.coalesced()
        if not coalesced:
            await database.notify_refresh()
            refresh.sent()
            log.info("web_refresh_requested")
        body: dict[str, Any] = {
            "queued": not coalesced,
            "coalesced": coalesced,
            "requested_at": iso(now()),
            "operations": ["poll", "reconcile"],
        }
        return JSONResponse(body, status_code=202)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        try:
            async with database.queries() as queries:
                row = await queries.snapshot()
        except DatabaseError as exc:
            body = {"status": "unavailable", "database": "unavailable", "error": exc.message}
            return JSONResponse(body, status_code=503)
        current = now()
        return JSONResponse(
            {
                "status": "ok",
                "database": "ok",
                "snapshot_at": iso(row.at) if row is not None else None,
                "snapshot_age_s": snapshot_age_s(row, current) if row is not None else None,
                "worker": worker_status(row, current),
            }
        )

    return app
```

In `src/issuebot/web/__init__.py` replace

```
"""The dashboard: a FastAPI app over the Phase 6 database, its view models and templates."""
```

with

```
"""The dashboard: a FastAPI app over the Phase 6 database, its view models and templates."""

from issuebot.web.app import JSON_PREFIXES, SECURITY_HEADERS, create_app
from issuebot.web.views import (
    CHART_POLL_S,
    LIVE_POLL_S,
    RECENT_EVENTS_LIMIT,
    REFRESH_MIN_INTERVAL_S,
    RUN_ID_PATTERN,
    STALE_FACTOR,
)

__all__ = [
    "CHART_POLL_S",
    "JSON_PREFIXES",
    "LIVE_POLL_S",
    "RECENT_EVENTS_LIMIT",
    "REFRESH_MIN_INTERVAL_S",
    "RUN_ID_PATTERN",
    "SECURITY_HEADERS",
    "STALE_FACTOR",
    "create_app",
]
```

In `pyproject.toml` replace

```toml
asyncio_mode = "auto"
```

with

```toml
asyncio_mode = "auto"
filterwarnings = [
  # Starlette 1.6's test client still names anyio's deprecated alias at import time.
  "ignore:The anyio.abc.BlockingPortal alias is deprecated:DeprecationWarning:starlette.testclient",
]
```

(Without the filter every run that imports the test client ends `... passed, 1 warning`, and the warning is Starlette's, not ours.)

- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 120 uv run pytest tests/test_web_app.py -q && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `55 passed` (no warning line); then `751 passed, 40 skipped`; then `791 passed`.

- [ ] **Step 5: Commit**

```bash
git status --short && git add --all && git commit -m "feat: the web app's JSON API, health check, view models" -m "<trailer>"
```

---

### Task 6: Pages, templates and static assets

**Files:**
- Create: `src/issuebot/web/templates/base.html`, `index.html`, `issue.html`, `turn.html`, `error.html`, `partials/dashboard.html`, `src/issuebot/web/static/app.css`, `app.js`, `vendor/htmx.min.js`, `vendor/htmx.LICENSE`, `vendor/chart.umd.js`, `vendor/chart.LICENSE`, `vendor/README.md`
- Modify: `src/issuebot/web/app.py`, `src/issuebot/web/views.py`, `src/issuebot/web/__init__.py`, `.pre-commit-config.yaml`
- Test: `tests/test_web_pages.py`

**Interfaces:**
- Consumes: Task 5's app, views and `fakes.web` builders; Task 4's `parse_transcript`; Phase 6's `COMPLETE_LIMIT`, `GitHubLabels`.
- Produces: pages `GET /`, `GET /partials/dashboard`, `GET /issues/{number}`, `GET /issues/{number}/runs/{run_id}/turns/{turn_number}` and `.../{part}` (`prompt|stream|stderr`, `text/plain`), `GET /static/...`; `CHART_DAYS = 30`, `RAW_PART_PATTERN`, `STATIC_ROOT`, `template_environment()` (filters `href`, `age`, `stamp`, `duration`, `money`, `thousands`); views: `age_text(value, now)`, `stamp_text(value)`, `duration_text(ms)`, `money(value)`, `thousands(value)`, `dashboard_context(row, groups, *, closed_1d, closed_7d, runs_1d, runs_7d, now, labels) -> dict` (`unavailable`, `worker`, `hero`, `running`, `retrying`, `columns`); `error.html` replaces the plain-text error placeholder; the live partial renders a 503 banner on a `DatabaseError`.

Spec: §6.2, §6.4, §6.6, §8.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_pages.py`:

```python
"""Tests for the HTML pages, the live partial, the raw text routes and the static files."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from fakes.web import (
    NOW,
    RUN_ID,
    Harness,
    issue_row,
    retry_row,
    run_row,
    running_row,
    snapshot,
)
from issuebot.config import GitHubLabels
from issuebot.db import COMPLETE_LIMIT, StoreUnavailableError
from issuebot.web import LIVE_POLL_S, SECURITY_HEADERS
from issuebot.web.views import (
    age_text,
    dashboard_context,
    duration_text,
    money,
    stamp_text,
    thousands,
)

RUN_ID_2 = "20260904T210000Z-abcdef"
HOSTILE = "<script>alert(1)</script>"
ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


@pytest.fixture
def h() -> Iterator[Harness]:
    harness = Harness()
    with harness.client:
        yield harness


def html(response: Any) -> str:
    assert response.headers["content-type"].startswith("text/html")
    return response.text


# --- the dashboard page -----------------------------------------------------------------------


def test_the_dashboard_renders_and_escapes(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(running=(running_row(title=HOSTILE),))
    h.queries.groups["review"] = [issue_row(title=HOSTILE)]
    h.queries.groups["todo"] = [
        issue_row(number=3, title="Safe", pr_number=4, pr_url="javascript:alert(1)")
    ]
    response = h.client.get("/")
    assert response.status_code == 200
    text = html(response)
    assert HOSTILE not in text and text.count(ESCAPED) == 2
    assert 'hx-get="/partials/dashboard"' in text
    assert f'hx-trigger="every {LIVE_POLL_S}s"' in text
    assert 'hx-post="/api/v1/refresh"' in text and 'id="refresh-status"' in text
    assert (
        'src="/static/vendor/htmx.min.js"' in text and 'src="/static/vendor/chart.umd.js"' in text
    )
    assert 'id="closed-chart"' in text and 'id="runs-chart"' in text
    assert 'data-chart-window="30d"' in text and 'data-chart-poll-s="60"' in text
    assert '<meta name="htmx-config"' in text and '"allowEval": false' in text
    for label in GitHubLabels().as_tuple():
        assert label in text
    assert 'href="/issues/7"' in text and 'href="/issues/3"' in text
    assert "javascript:" not in text and "PR #4" in text
    assert "<script>" not in text and ' style="' not in text  # the CSP forbids inline code
    assert "example/repo" in text


def test_the_dashboard_shows_a_running_agent_and_a_retry(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(running=(running_row(),), retrying=(retry_row(),))
    text = html(h.client.get("/"))
    assert 'class="panel worker ok"' in text and "tick 41" in text
    assert "turn_activity" in text and "20 s ago" in text
    assert "Retrying" in text and "turn_failed: boom" in text


def test_the_live_partial_without_a_snapshot(h: Harness) -> None:
    text = html(h.client.get("/partials/dashboard"))
    assert text.startswith('<div id="live"')
    assert 'class="panel worker none"' in text and "no report yet" in text
    assert "no agent is running" in text
    assert "Retrying" not in text


def test_the_live_partial_marks_a_stale_worker(h: Harness) -> None:
    h.queries.snapshot_row = snapshot(age_s=200.0)
    text = html(h.client.get("/partials/dashboard"))
    assert 'class="panel worker stale"' in text and "3 min ago" in text


def test_the_live_partial_shows_a_config_error(h: Harness) -> None:
    row = snapshot()
    row.data["config_valid"] = False
    row.data["config_error"] = "polling.interval_ms must be >= 1000"
    h.queries.snapshot_row = row
    text = html(h.client.get("/partials/dashboard"))
    assert "config error: polling.interval_ms must be &gt;= 1000" in text


def test_the_live_partial_notes_the_complete_cap(h: Harness) -> None:
    h.queries.groups["complete"] = [
        issue_row(number=n, state="complete", github_state="closed")
        for n in range(1, COMPLETE_LIMIT + 1)
    ]
    text = html(h.client.get("/partials/dashboard"))
    assert f"{COMPLETE_LIMIT} most recent" in text
    h.queries.groups["complete"].pop()
    assert "most recent" not in html(h.client.get("/partials/dashboard"))


def test_the_live_partial_survives_a_database_error(h: Harness) -> None:
    h.queries.error = StoreUnavailableError("cannot connect: refused")
    response = h.client.get("/partials/dashboard")
    assert response.status_code == 503
    text = html(response)
    assert text.startswith('<div id="live"') and 'hx-get="/partials/dashboard"' in text
    assert "database unavailable: cannot connect: refused" in text


def test_a_database_error_on_a_page_is_an_html_503(h: Harness) -> None:
    h.queries.error = StoreUnavailableError("cannot connect: refused")
    response = h.client.get("/")
    assert response.status_code == 503
    text = html(response)
    assert "<h1>503</h1>" in text and "database_unavailable" in text
    assert "cannot connect: refused" in text


# --- the issue page -----------------------------------------------------------------------------


def test_the_issue_page(h: Harness) -> None:
    h.seed_issue()
    h.queries.runs_by_issue[7].append(
        run_row(run_id=RUN_ID_2, attempt=2, outcome="failed", error="turn_failed: boom")
    )
    h.queries.snapshot_row = snapshot(running=(running_row(),))
    response = h.client.get("/issues/7")
    assert response.status_code == 200
    text = html(response)
    assert "Add a power function" in text and "issuebot/review" in text
    assert 'href="https://github.com/example/repo/issues/7"' in text
    assert 'href="https://github.com/example/repo/pull/8"' in text
    assert "Running now" in text and "turn_activity" in text
    assert RUN_ID in text and RUN_ID_2 in text
    assert "claude-opus-5" in text and "19 agent iterations" in text and "3m21s" in text
    assert f'href="/issues/7/runs/{RUN_ID}/turns/1"' in text
    assert text.count("turn logs were not captured") == 1  # the failed run has none
    assert "agent changed issuebot/in-progress to issuebot/review" in text
    assert "run 20260904T202535Z-0964cd succeeded after 1 turn, $0.90" in text
    assert "513,338" in text


def test_the_issue_page_escapes_the_title(h: Harness) -> None:
    h.queries.issue_rows[7] = issue_row(title=HOSTILE)
    text = html(h.client.get("/issues/7"))
    assert HOSTILE not in text and ESCAPED in text
    assert "no runs recorded" in text and "no events recorded" in text


def test_an_unknown_issue_is_a_404_page(h: Harness) -> None:
    response = h.client.get("/issues/99")
    assert response.status_code == 404
    text = html(response)
    assert "<h1>404</h1>" in text and "issue #99 is not known" in text


def test_a_non_numeric_issue_is_a_404_page(h: Harness) -> None:
    response = h.client.get("/issues/abc")
    assert response.status_code == 404
    assert "<h1>404</h1>" in html(response)


# --- the turn page and the raw files -------------------------------------------------------------


def test_the_turn_page_renders_the_transcript(h: Harness) -> None:
    h.seed_issue()
    response = h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1")
    assert response.status_code == 200
    text = html(response)
    assert "turn 1 of 1" in text and "claude-opus-5" in text
    assert "Bash" in text and "ls &lt;dir&gt;" in text
    assert "Done &lt;script&gt;x&lt;/script&gt;" in text and "<script>x" not in text
    assert "README.md" in text
    assert "1 status message hidden" in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text and "<b>bold</b>" not in text
    assert "warning: something" in text
    for part in ("prompt", "stream", "stderr"):
        assert f'href="/issues/7/runs/{RUN_ID}/turns/1/{part}"' in text
    assert "95 lines" in text and "115,429 bytes" in text


def test_the_turn_page_notes_caps(h: Harness) -> None:
    h.seed_issue()
    row = h.queries.turn_rows[(RUN_ID, 1)]
    h.queries.turn_rows[(RUN_ID, 1)] = replace(row, truncated=True, omitted_lines=2, stream="")
    text = html(h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1"))
    assert "<strong>truncated</strong>" in text and "2 oversized lines replaced" in text
    assert "the stored stream is empty" in text


@pytest.mark.parametrize(
    "path",
    [
        f"/issues/9/runs/{RUN_ID}/turns/1",  # another issue's run
        f"/issues/7/runs/{RUN_ID}/turns/2",  # no such turn
        f"/issues/7/runs/{RUN_ID_2}/turns/1",  # no such run
        "/issues/7/runs/bad/turns/1",  # malformed run id
        f"/issues/7/runs/{RUN_ID}/turns/x",  # malformed turn number
        f"/issues/99/runs/{RUN_ID}/turns/1",  # unknown issue
    ],
)
def test_turn_pages_that_do_not_exist_are_404_pages(h: Harness, path: str) -> None:
    h.seed_issue()
    h.queries.issue_rows[9] = issue_row(number=9, identifier="repo-9")
    response = h.client.get(path)
    assert response.status_code == 404
    assert "<h1>404</h1>" in html(response)


@pytest.mark.parametrize(
    ("part", "attribute", "extension"),
    [("prompt", "prompt", "md"), ("stream", "stream", "jsonl"), ("stderr", "stderr", "log")],
)
def test_raw_files_are_plain_text(h: Harness, part: str, attribute: str, extension: str) -> None:
    h.seed_issue()
    response = h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1/{part}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    expected = f'inline; filename="{RUN_ID}-turn-1.{extension}"'
    assert response.headers["content-disposition"] == expected
    assert response.text == getattr(h.queries.turn_rows[(RUN_ID, 1)], attribute)


def test_an_unknown_raw_part_is_a_404(h: Harness) -> None:
    h.seed_issue()
    assert h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1/other").status_code == 404
    assert h.client.get(f"/issues/7/runs/{RUN_ID}/turns/2/prompt").status_code == 404


# --- static files, unknown pages, headers -----------------------------------------------------


def test_static_files_are_served(h: Harness) -> None:
    css = h.client.get("/static/app.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "--accent" in css.text
    htmx = h.client.get("/static/vendor/htmx.min.js")
    assert htmx.status_code == 200 and htmx.text.startswith("var htmx=")
    chart = h.client.get("/static/vendor/chart.umd.js")
    assert chart.status_code == 200 and "Chart.js v4.5.1" in chart.text[:200]
    assert h.client.get("/static/app.js").status_code == 200
    assert h.client.get("/static/nope.css").status_code == 404


def test_an_unknown_page_is_a_404_page(h: Harness) -> None:
    response = h.client.get("/nothing")
    assert response.status_code == 404
    assert "<h1>404</h1>" in html(response)


def test_pages_and_static_files_carry_the_security_headers(h: Harness) -> None:
    h.seed_issue()
    for response in (
        h.client.get("/"),
        h.client.get("/partials/dashboard"),
        h.client.get("/issues/7"),
        h.client.get(f"/issues/7/runs/{RUN_ID}/turns/1"),
        h.client.get("/static/app.css"),
        h.client.get("/nothing"),
    ):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (response.url, name)


# --- the pure helpers -------------------------------------------------------------------------


def test_age_text() -> None:
    assert age_text(NOW - timedelta(seconds=12), NOW) == "12 s ago"
    assert age_text(NOW - timedelta(minutes=3, seconds=20), NOW) == "3 min ago"
    assert age_text(NOW - timedelta(hours=2, minutes=5), NOW) == "2 h ago"
    assert age_text(NOW - timedelta(days=4, hours=3), NOW) == "4 d ago"
    assert age_text((NOW - timedelta(seconds=45)).isoformat(), NOW) == "45 s ago"
    assert age_text(NOW + timedelta(seconds=30), NOW) == "0 s ago"
    assert age_text(None, NOW) == "-"
    assert age_text("not a date", NOW) == "not a date"


def test_stamp_duration_money_and_thousands() -> None:
    assert stamp_text(NOW) == "2026-09-04T12:00:00Z"
    assert stamp_text(NOW.isoformat()) == "2026-09-04T12:00:00Z"
    assert stamp_text(None) == "-"
    assert stamp_text("garbage") == "garbage"
    assert duration_text(201719) == "3m21s"
    assert duration_text(999) == "0m00s"
    assert duration_text(None) == "-"
    assert money(0.8976) == "$0.90"
    assert money(None) == "$0.00"
    assert thousands(513338) == "513,338"
    assert thousands(None) == "0"


def test_dashboard_context() -> None:
    groups = {role: [] for role in ("todo", "in_progress", "review", "rework", "complete")}
    groups["complete"] = [issue_row(number=n, state="complete") for n in range(COMPLETE_LIMIT)]
    live = dashboard_context(
        snapshot(running=(running_row(),)),
        groups,
        closed_1d=1,
        closed_7d=2,
        runs_1d=3,
        runs_7d=4,
        now=NOW,
        labels=GitHubLabels(),
    )
    assert live["unavailable"] is None
    assert (live["worker"]["status"], live["worker"]["tick_count"]) == ("ok", 41)
    assert live["hero"] == {
        "closed_1d": 1,
        "closed_7d": 2,
        "runs_1d": 3,
        "runs_7d": 4,
        "running": 1,
        "retrying": 0,
        "cost_usd": 1.25,
        "total_tokens": 1050,
    }
    assert [column["role"] for column in live["columns"]] == list(groups)
    assert [column["label"] for column in live["columns"]] == list(GitHubLabels().as_tuple())
    assert [column["capped"] for column in live["columns"]] == [False, False, False, False, True]
    assert live["running"][0]["issue_number"] == 7
    empty = dashboard_context(
        None, groups, closed_1d=0, closed_7d=0, runs_1d=0, runs_7d=0, now=NOW, labels=GitHubLabels()
    )
    assert empty["worker"] == {"status": "none"}
    assert empty["hero"]["cost_usd"] == 0.0 and empty["running"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `timeout 120 uv run pytest tests/test_web_pages.py -q`
Expected: collection fails with `ImportError: cannot import name 'age_text' from 'issuebot.web.views'`.

- [ ] **Step 3: The vendored libraries**

Download the two pinned files and their licences (the only network step in the code tasks), verify the checksums, and exclude the directory from the pre-commit hooks (`end-of-file-fixer` would append a newline to `htmx.min.js`):

```bash
mkdir -p src/issuebot/web/static/vendor && cd src/issuebot/web/static/vendor && curl -fsSL -o htmx.min.js https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js && curl -fsSL -o htmx.LICENSE https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.10/LICENSE && curl -fsSL -o chart.umd.js https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.js && curl -fsSL -o chart.LICENSE https://raw.githubusercontent.com/chartjs/Chart.js/v4.5.1/LICENSE.md && sha256sum htmx.min.js htmx.LICENSE chart.umd.js chart.LICENSE && cd -
```

Expected:

```
71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de  htmx.min.js     (51238 bytes)
d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38  htmx.LICENSE    (642 bytes)
ecc3cd1eeb8c34d2178e3f59fd63ec5a3d84358c11730af0b9958dc886d7652a  chart.umd.js    (208518 bytes)
41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce  chart.LICENSE   (1093 bytes)
```

(`unpkg.com/htmx.org@2.0.10/dist/htmx.min.js` serves the identical file if jsDelivr is unavailable. A different checksum is a finding: stop and report it.)

Create `src/issuebot/web/static/vendor/README.md`:

```markdown
# Vendored front-end libraries

Pinned files, served from `/static/vendor/`; no CDN, no build step (roadmap decision 7). Bump
them by hand: download the new file, verify its checksum against the release, update this table
and the licence file beside it. Dependabot does not see these files.

| File | Library | Version | Source | Licence |
|---|---|---|---|---|
| `htmx.min.js` | htmx | 2.0.10 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js | 0BSD (`htmx.LICENSE`) |
| `chart.umd.js` | Chart.js | 4.5.1 | https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.js | MIT (`chart.LICENSE`) |

SHA-256 as vendored on 2026-09-04:

```
71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de  htmx.min.js
ecc3cd1eeb8c34d2178e3f59fd63ec5a3d84358c11730af0b9958dc886d7652a  chart.umd.js
```
```

In `.pre-commit-config.yaml` replace

```yaml
# Recorded agent runs are kept byte-for-byte as the runner wrote them.
exclude: ^tests/fixtures/runs/
```

with

```yaml
# Recorded agent runs and vendored libraries are kept byte-for-byte as their authors wrote them.
exclude: ^(tests/fixtures/runs/|src/issuebot/web/static/vendor/)
```

- [ ] **Step 4: The stylesheet and the script**

Create `src/issuebot/web/static/app.css`:

```css
/* issuebot dashboard. One stylesheet, no inline styles anywhere (the CSP forbids them). */

:root {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --ink: #1f2328;
  --muted: #6b7280;
  --line: #d9dee5;
  --accent: #1d76db;
  --ok: #0e8a16;
  --warn: #b7791f;
  --bad: #c62828;
  --todo: #0e8a16;
  --in_progress: #b7791f;
  --review: #1d76db;
  --rework: #d93f0b;
  --complete: #5319e7;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px;
  line-height: 1.45;
  color: var(--ink);
}

* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
h1 { font-size: 20px; margin: 0 0 12px; }
h2 { font-size: 15px; margin: 0 0 8px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.muted { color: var(--muted); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }

header.top {
  display: flex; align-items: center; gap: 16px;
  padding: 10px 20px; background: var(--panel); border-bottom: 1px solid var(--line);
}
header.top .brand { font-weight: 700; font-size: 16px; color: var(--ink); }
header.top .repo { color: var(--muted); }
header.top nav { margin-left: auto; display: flex; gap: 14px; }

main { padding: 16px 20px 40px; max-width: 1500px; margin: 0 auto; }
section { margin-bottom: 20px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }

/* the worker line and the poll button */
.worker { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.worker .badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; color: #fff; background: var(--muted); }
.worker.ok .badge { background: var(--ok); }
.worker.stale .badge { background: var(--warn); }
.worker.none .badge { background: var(--muted); }
.worker .config-error { color: var(--bad); }
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
button.poll { padding: 5px 12px; border: 1px solid var(--accent); border-radius: 6px; background: var(--panel); color: var(--accent); cursor: pointer; }
button.poll:hover { background: var(--accent); color: #fff; }
#refresh-status { color: var(--muted); }

/* hero stats */
.hero { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.tile .label { color: var(--muted); font-size: 12px; }
.tile .value { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }

/* charts */
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 12px; }
.chart { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.chart canvas { width: 100%; max-height: 220px; }

/* kanban */
.kanban { display: grid; grid-template-columns: repeat(5, minmax(180px, 1fr)); gap: 10px; }
.column { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 8px; min-height: 120px; }
.column h3 { margin: 0 0 8px; font-size: 13px; display: flex; justify-content: space-between; align-items: center; }
.column h3 .count { color: var(--muted); font-weight: 500; }
.column.todo h3 { color: var(--todo); }
.column.in_progress h3 { color: var(--in_progress); }
.column.review h3 { color: var(--review); }
.column.rework h3 { color: var(--rework); }
.column.complete h3 { color: var(--complete); }
.card { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; margin-bottom: 6px; background: var(--bg); }
.card .number { color: var(--muted); }
.card .title { display: block; color: var(--ink); }
.card .meta { color: var(--muted); font-size: 12px; display: flex; justify-content: space-between; gap: 8px; }
.column .capped { color: var(--muted); font-size: 12px; margin-top: 4px; }

/* banners */
.banner { padding: 10px 12px; border-radius: 6px; border: 1px solid var(--line); background: var(--panel); }
.banner.error { border-color: var(--bad); color: var(--bad); }
.banner.warn { border-color: var(--warn); color: var(--warn); }

/* issue and turn pages */
.issue-head dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 0; }
.issue-head dt { color: var(--muted); }
.issue-head dd { margin: 0; }
.state { padding: 1px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; color: #fff; background: var(--muted); }
.state.todo { background: var(--todo); }
.state.in_progress { background: var(--in_progress); }
.state.review { background: var(--review); }
.state.rework { background: var(--rework); }
.state.complete { background: var(--complete); }
tr.turn td { background: var(--bg); }
.not-captured { color: var(--muted); font-style: italic; }
.outcome.succeeded { color: var(--ok); }
.outcome.failed, .outcome.timed_out, .outcome.stalled { color: var(--bad); }
.outcome.cancelled { color: var(--warn); }

.transcript .block { border-left: 3px solid var(--line); margin: 0 0 10px; padding: 6px 10px; background: var(--panel); border-radius: 0 6px 6px 0; }
.transcript .block-title, .transcript summary { font-weight: 600; font-size: 12px; color: var(--muted); margin-bottom: 4px; cursor: default; }
.transcript summary { cursor: pointer; }
.transcript .block.text { border-left-color: var(--accent); }
.transcript .block.thinking { border-left-color: var(--line); }
.transcript .block.tool_use { border-left-color: var(--in_progress); }
.transcript .block.tool_result { border-left-color: var(--muted); }
.transcript .block.result { border-left-color: var(--ok); }
.transcript .block.omitted, .transcript .block.unparseable { border-left-color: var(--bad); }
.transcript .block.init { border-left-color: var(--complete); }
details.file { margin-bottom: 10px; }
details.file summary { font-weight: 600; cursor: pointer; }
details.file pre { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 10px; margin-top: 6px; max-height: 60vh; overflow: auto; }
.raw-links a { margin-right: 12px; }

.error-page { text-align: center; padding: 60px 0; }
.error-page h1 { font-size: 48px; margin-bottom: 8px; }
```

Create `src/issuebot/web/static/app.js`:

```javascript
/* issuebot dashboard: the two charts and the "Poll now" status line. No inline scripts (CSP). */
(function () {
  "use strict";

  // --- the Poll now button: report what POST /api/v1/refresh answered --------------------
  document.body.addEventListener("htmx:afterRequest", function (event) {
    var status = document.getElementById("refresh-status");
    var info = event.detail && event.detail.pathInfo;
    if (!status || !info || info.requestPath !== "/api/v1/refresh") {
      return;
    }
    var xhr = event.detail.xhr;
    if (xhr && xhr.status === 202) {
      var body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch (error) {
        body = null;
      }
      status.textContent = body && body.coalesced ? "poll already requested" : "poll requested";
    } else {
      status.textContent = "refresh failed (" + (xhr ? xhr.status : "no response") + ")";
    }
  });

  // --- the charts: /api/v1/stats?window=<N>d on load and every chart_poll_s seconds ------
  var script = document.currentScript;
  var closedCanvas = document.getElementById("closed-chart");
  var runsCanvas = document.getElementById("runs-chart");
  if (!script || !closedCanvas || !runsCanvas || typeof Chart === "undefined") {
    return;
  }
  var windowText = script.dataset.chartWindow || "30d";
  var pollSeconds = Number(script.dataset.chartPollS) || 60;
  var charts = {};

  function draw(canvas, key, label, color, series) {
    var labels = series.map(function (point) { return point.day.slice(5); });
    var values = series.map(function (point) { return point[key]; });
    if (charts[key]) {
      charts[key].data.labels = labels;
      charts[key].data.datasets[0].data = values;
      charts[key].update();
      return;
    }
    charts[key] = new Chart(canvas, {
      type: "bar",
      data: { labels: labels, datasets: [{ label: label, data: values, backgroundColor: color }] },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
      }
    });
  }

  function refresh() {
    fetch("/api/v1/stats?window=" + encodeURIComponent(windowText), { headers: { Accept: "application/json" } })
      .then(function (response) { return response.ok ? response.json() : Promise.reject(response.status); })
      .then(function (body) {
        draw(closedCanvas, "closed", "issues closed", "#5319e7", body.series);
        draw(runsCanvas, "runs", "agent runs", "#1d76db", body.series);
      })
      .catch(function () { /* the next poll tries again */ });
  }

  refresh();
  window.setInterval(refresh, pollSeconds * 1000);
})();
```

- [ ] **Step 5: The templates**

Create `src/issuebot/web/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="htmx-config" content='{"allowEval": false, "selfRequestsOnly": true, "includeIndicatorStyles": false}'>
<title>{% block title %}issuebot{% endblock %}</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<header class="top">
  <a class="brand" href="/">issuebot</a>
  <span class="repo">{{ repo }}</span>
  <nav>
    <a href="/">dashboard</a>
    <a href="/api/v1/state">api</a>
    <a href="/healthz">health</a>
  </nav>
</header>
<main>
{% block content %}{% endblock %}
</main>
<script src="/static/vendor/htmx.min.js"></script>
{% block scripts %}{% endblock %}
</body>
</html>
```

Create `src/issuebot/web/templates/index.html`:

```html
{% extends "base.html" %}
{% block title %}issuebot: {{ repo }}{% endblock %}
{% block content %}
<div class="toolbar">
  <h1>{{ repo }}</h1>
  <button class="poll" type="button" hx-post="/api/v1/refresh" hx-swap="none">Poll now</button>
  <span id="refresh-status"></span>
</div>
{% include "partials/dashboard.html" %}
<section class="charts">
  <div class="chart">
    <h2>Issues closed per day ({{ chart_days }} days)</h2>
    <canvas id="closed-chart" height="200"></canvas>
  </div>
  <div class="chart">
    <h2>Agent runs per day ({{ chart_days }} days)</h2>
    <canvas id="runs-chart" height="200"></canvas>
  </div>
</section>
{% endblock %}
{% block scripts %}
<script src="/static/vendor/chart.umd.js"></script>
<script src="/static/app.js" data-chart-window="{{ chart_days }}d" data-chart-poll-s="{{ chart_poll_s }}"></script>
{% endblock %}
```

Create `src/issuebot/web/templates/partials/dashboard.html`:

```html
<div id="live" hx-get="/partials/dashboard" hx-trigger="every {{ live_poll_s }}s" hx-swap="outerHTML">
{% if live.unavailable %}
<div class="banner error">database unavailable: {{ live.unavailable }}</div>
{% else %}
{% set worker = live.worker %}
<section class="panel worker {{ worker.status }}">
  <span class="badge">worker {{ worker.status }}</span>
  {% if worker.status == "none" %}
  <span>no report yet (has the worker run against this database?)</span>
  {% else %}
  <span>reported {{ worker.written_at|age(now) }}</span>
  <span class="muted">tick {{ worker.tick_count }}, poll {{ worker.poll_interval_ms }} ms, {{ worker.max_concurrent_agents }} slots</span>
  {% if worker.config_valid %}
  <span class="muted">config valid</span>
  {% else %}
  <span class="config-error">config error: {{ worker.config_error }}</span>
  {% endif %}
  {% endif %}
</section>
<section class="hero">
  <div class="tile"><div class="label">closed, 1 day</div><div class="value">{{ live.hero.closed_1d }}</div></div>
  <div class="tile"><div class="label">closed, 7 days</div><div class="value">{{ live.hero.closed_7d }}</div></div>
  <div class="tile"><div class="label">agents run, 1 day</div><div class="value">{{ live.hero.runs_1d }}</div></div>
  <div class="tile"><div class="label">agents run, 7 days</div><div class="value">{{ live.hero.runs_7d }}</div></div>
  <div class="tile"><div class="label">running now</div><div class="value">{{ live.hero.running }}</div></div>
  <div class="tile"><div class="label">retrying</div><div class="value">{{ live.hero.retrying }}</div></div>
  <div class="tile"><div class="label">cost since start</div><div class="value">{{ live.hero.cost_usd|money }}</div></div>
  <div class="tile"><div class="label">tokens since start</div><div class="value">{{ live.hero.total_tokens|thousands }}</div></div>
</section>
<section class="panel running">
  <h2>Running</h2>
  {% if live.running %}
  <table>
    <tr><th>issue</th><th>attempt</th><th class="num">turns</th><th>last event</th><th>last activity</th><th>started</th><th>stop</th></tr>
    {% for entry in live.running %}
    <tr>
      <td><a href="/issues/{{ entry.issue_number }}">#{{ entry.issue_number }}</a> {{ entry.title }}{% if entry.rework %} <span class="muted">(rework)</span>{% endif %}{% if entry.resumed %} <span class="muted">(resumed)</span>{% endif %}</td>
      <td>{{ entry.attempt }}</td>
      <td class="num">{{ entry.turn_count }}</td>
      <td>{{ entry.last_event or "-" }}</td>
      <td>{{ entry.last_event_at|age(now) }}</td>
      <td>{{ entry.started_at|age(now) }}</td>
      <td>{{ entry.stop_cause or "-" }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="muted">no agent is running</p>
  {% endif %}
</section>
{% if live.retrying %}
<section class="panel retrying">
  <h2>Retrying</h2>
  <table>
    <tr><th>issue</th><th>kind</th><th>attempt</th><th>due</th><th>error</th></tr>
    {% for entry in live.retrying %}
    <tr>
      <td><a href="/issues/{{ entry.issue_number }}">#{{ entry.issue_number }}</a> {{ entry.issue_identifier }}</td>
      <td>{{ entry.kind }}</td>
      <td>{{ entry.attempt }}</td>
      <td>{{ entry.due_at|stamp }}</td>
      <td>{{ entry.error or "-" }}</td>
    </tr>
    {% endfor %}
  </table>
</section>
{% endif %}
<section class="kanban">
  {% for column in live.columns %}
  <div class="column {{ column.role }}">
    <h3><span>{{ column.label }}</span><span class="count">{{ column.rows|length }}</span></h3>
    {% for row in column.rows %}
    <div class="card">
      <a class="title" href="/issues/{{ row.number }}"><span class="number">#{{ row.number }}</span> {{ row.title }}</a>
      <div class="meta">
        <span>{% if row.pr_number %}{% set pr = row.pr_url|href %}{% if pr %}<a href="{{ pr }}">PR #{{ row.pr_number }}</a>{% else %}PR #{{ row.pr_number }}{% endif %} {{ row.pr_state }}{% endif %}</span>
        <span>{{ row.updated_at|age(now) }}</span>
      </div>
    </div>
    {% endfor %}
    {% if column.capped %}<div class="capped">{{ column.rows|length }} most recent</div>{% endif %}
  </div>
  {% endfor %}
</section>
{% endif %}
</div>
```

Create `src/issuebot/web/templates/issue.html`:

```html
{% extends "base.html" %}
{% block title %}#{{ issue.number }} {{ issue.title }}{% endblock %}
{% block content %}
<section class="panel issue-head">
  <h1><span class="muted">#{{ issue.number }}</span> {{ issue.title }} <span class="state {{ issue.state or 'unlabelled' }}">{{ issue.state_label or "unlabelled" }}</span></h1>
  <dl>
    <dt>identifier</dt><dd>{{ issue.identifier }}</dd>
    <dt>GitHub</dt><dd>{% set url = issue.url|href %}{% if url %}<a href="{{ url }}">{{ url }}</a>{% else %}{{ issue.github_state }}{% endif %} ({{ issue.github_state }})</dd>
    <dt>pull request</dt><dd>{% if issue.pr_number %}{% set pr = issue.pr_url|href %}{% if pr %}<a href="{{ pr }}">#{{ issue.pr_number }}</a>{% else %}#{{ issue.pr_number }}{% endif %} {{ issue.pr_state }}{% if issue.pr_merged_at %}, merged {{ issue.pr_merged_at|stamp }}{% endif %}{% else %}none{% endif %}</dd>
    <dt>labels</dt><dd>{{ issue.labels|join(", ") or "none" }}</dd>
    <dt>created</dt><dd>{{ issue.created_at|stamp }}</dd>
    <dt>updated</dt><dd>{{ issue.updated_at|stamp }} ({{ issue.updated_at|age(now) }})</dd>
    {% if issue.closed_at %}<dt>closed</dt><dd>{{ issue.closed_at|stamp }}</dd>{% endif %}
    <dt>last seen by the worker</dt><dd>{{ issue.seen_at|stamp }}</dd>
  </dl>
</section>
{% if running %}
<section class="panel">
  <h2>Running now</h2>
  <p>run <span class="mono">{{ running.run_id }}</span>, attempt {{ running.attempt }}, turn {{ running.turn_count }}, last event {{ running.last_event or "-" }} {{ running.last_event_at|age(now) }}, started {{ running.started_at|age(now) }}{% if running.stop_cause %}, stopping: {{ running.stop_cause }}{% endif %}</p>
</section>
{% endif %}
{% if retry %}
<section class="panel">
  <h2>Retry scheduled</h2>
  <p>{{ retry.kind }} retry, attempt {{ retry.attempt }}, due {{ retry.due_at|stamp }}{% if retry.error %}: {{ retry.error }}{% endif %}</p>
</section>
{% endif %}
<section class="panel">
  <h2>Runs</h2>
  {% if runs %}
  <table>
    <tr><th>run</th><th>attempt</th><th>started</th><th>ended</th><th>outcome</th><th class="num">turns</th><th class="num">tokens in</th><th class="num">tokens out</th><th class="num">cost</th><th>error</th></tr>
    {% for run in runs %}
    <tr>
      <td class="mono">{{ run.run_id }}</td>
      <td>{{ run.attempt }}</td>
      <td>{{ run.started_at|stamp }}</td>
      <td>{{ run.ended_at|stamp }}</td>
      <td class="outcome {{ run.outcome or 'running' }}">{{ run.outcome or "running" }}</td>
      <td class="num">{{ run.turns }}</td>
      <td class="num">{{ run.input_tokens|thousands }}</td>
      <td class="num">{{ run.output_tokens|thousands }}</td>
      <td class="num">{{ run.cost_usd|money }}</td>
      <td>{{ run.error or "-" }}</td>
    </tr>
    {% set turns = turns_by_run.get(run.run_id, []) %}
    {% for turn in turns %}
    <tr class="turn">
      <td class="muted">turn {{ turn.turn_number }}</td>
      <td colspan="3">{{ turn.model or "-" }}, {{ turn.num_turns if turn.num_turns is not none else "?" }} agent iterations, {{ turn.duration_ms|duration }}</td>
      <td>{{ turn.subtype or "no result" }}</td>
      <td class="num"></td>
      <td class="num">{{ ((turn.input_tokens or 0) + (turn.cache_creation_input_tokens or 0) + (turn.cache_read_input_tokens or 0))|thousands }}</td>
      <td class="num">{{ (turn.output_tokens or 0)|thousands }}</td>
      <td class="num">{{ (turn.cost_usd or 0)|money }}</td>
      <td><a href="/issues/{{ issue.number }}/runs/{{ run.run_id }}/turns/{{ turn.turn_number }}">transcript</a>{% if turn.truncated or turn.omitted_lines %} <span class="muted">(capped)</span>{% endif %}</td>
    </tr>
    {% endfor %}
    {% if not turns and run.ended_at %}
    <tr class="turn"><td colspan="10" class="not-captured">turn logs were not captured{% if run.log_dir %} (they lived under {{ run.log_dir }}){% endif %}</td></tr>
    {% endif %}
    {% endfor %}
  </table>
  {% else %}
  <p class="muted">no runs recorded</p>
  {% endif %}
</section>
<section class="panel">
  <h2>Events</h2>
  {% if events %}
  <table>
    <tr><th>at</th><th>kind</th><th>what</th></tr>
    {% for event, text in events %}
    <tr><td>{{ event.at|stamp }}</td><td>{{ event.kind }}</td><td>{{ text }}</td></tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="muted">no events recorded</p>
  {% endif %}
</section>
{% endblock %}
```

Create `src/issuebot/web/templates/turn.html`:

```html
{% extends "base.html" %}
{% block title %}#{{ issue.number }} run {{ run.run_id }} turn {{ turn.turn_number }}{% endblock %}
{% block content %}
<section class="panel issue-head">
  <h1><a href="/issues/{{ issue.number }}">#{{ issue.number }} {{ issue.title }}</a> <span class="muted">/ run <span class="mono">{{ run.run_id }}</span> / turn {{ turn.turn_number }} of {{ run.turns }}</span></h1>
  <dl>
    <dt>model</dt><dd>{{ turn.model or "-" }}</dd>
    <dt>result</dt><dd>{{ turn.subtype or "no result line (the turn ended without one)" }}{% if turn.is_error %} (error){% endif %}</dd>
    <dt>agent iterations</dt><dd>{{ turn.num_turns if turn.num_turns is not none else "?" }}</dd>
    <dt>tokens</dt><dd>{{ (turn.input_tokens or 0)|thousands }} in, {{ (turn.cache_creation_input_tokens or 0)|thousands }} cache write, {{ (turn.cache_read_input_tokens or 0)|thousands }} cache read, {{ (turn.output_tokens or 0)|thousands }} out</dd>
    <dt>cost</dt><dd>{{ (turn.cost_usd or 0)|money }}</dd>
    <dt>duration</dt><dd>{{ turn.duration_ms|duration }}</dd>
    <dt>captured</dt><dd>{{ turn.captured_at|stamp }}</dd>
    <dt>stream</dt><dd>{{ turn.stream_lines }} lines, {{ turn.stream_bytes|thousands }} bytes{% if turn.truncated %}; <strong>truncated</strong> to the stored head (plus the result line){% endif %}{% if turn.omitted_lines %}; {{ turn.omitted_lines }} oversized line{{ "s" if turn.omitted_lines != 1 }} replaced by a stub{% endif %}</dd>
  </dl>
  <p class="raw-links">raw: <a href="{{ raw_url }}/prompt">prompt</a> <a href="{{ raw_url }}/stream">stream</a> <a href="{{ raw_url }}/stderr">stderr</a></p>
</section>
<section class="transcript">
  <h2>Transcript</h2>
  {% for block in transcript.blocks %}
  {% if block.collapsed %}
  <details class="block {{ block.kind }}"><summary>{{ block.title }}{% if block.cut %} (showing {{ block.text|length|thousands }} of {{ (block.text|length + block.cut)|thousands }} characters){% endif %}</summary><pre>{{ block.text }}</pre></details>
  {% else %}
  <div class="block {{ block.kind }}"><div class="block-title">{{ block.title }}{% if block.cut %} (showing {{ block.text|length|thousands }} of {{ (block.text|length + block.cut)|thousands }} characters){% endif %}</div><pre>{{ block.text }}</pre></div>
  {% endif %}
  {% endfor %}
  {% if not transcript.blocks %}<p class="muted">the stored stream is empty</p>{% endif %}
  {% if transcript.hidden %}<p class="muted">{{ transcript.hidden }} status message{{ "s" if transcript.hidden != 1 }} hidden</p>{% endif %}
</section>
<section>
  <details class="file"><summary>prompt ({{ turn.prompt_bytes|thousands }} bytes{% if turn.prompt|length < turn.prompt_bytes %}, showing the first {{ turn.prompt|length|thousands }} characters{% endif %})</summary><pre>{{ turn.prompt }}</pre></details>
  <details class="file"><summary>stderr ({{ turn.stderr_bytes|thousands }} bytes{% if turn.stderr|length < turn.stderr_bytes %}, showing the tail{% endif %})</summary><pre>{{ turn.stderr or "(empty)" }}</pre></details>
</section>
{% endblock %}
```

Create `src/issuebot/web/templates/error.html`:

```html
{% extends "base.html" %}
{% block title %}{{ status }} {{ code }}{% endblock %}
{% block content %}
<div class="error-page">
  <h1>{{ status }}</h1>
  <p class="muted">{{ code }}</p>
  <p>{{ message }}</p>
  <p><a href="/">back to the dashboard</a></p>
</div>
{% endblock %}
```

- [ ] **Step 6: The view helpers and the page routes**

In `src/issuebot/web/__init__.py` (edit 1 of 2) replace

```
from issuebot.web.app import JSON_PREFIXES, SECURITY_HEADERS, create_app
```

with

```
from issuebot.web.app import CHART_DAYS, JSON_PREFIXES, SECURITY_HEADERS, create_app
```

In `src/issuebot/web/__init__.py` (edit 2 of 2) replace

```
__all__ = [
    "CHART_POLL_S",
```

with

```
__all__ = [
    "CHART_DAYS",
    "CHART_POLL_S",
```

In `src/issuebot/web/app.py` (edit 1 of 8) replace

```
"""The FastAPI app: the JSON API, the health check, error envelopes and response headers.

Every request that reads opens one connection through ``Database.queries()`` and closes it when
the response is built. The app never writes to a table; its one write is ``NOTIFY``.
```

with

```
"""The FastAPI app: pages, the live partial, the JSON API, the health check, static files.

Every request that reads opens one connection through ``Database.queries()`` and closes it when
the response is built. The app never writes to a table; its one write is ``NOTIFY``. Templates
render with autoescape on and ``StrictUndefined``; every response carries the security headers.
```

In `src/issuebot/web/app.py` (edit 2 of 8) replace

```
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
```

with

```
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, PackageLoader, StrictUndefined
```

In `src/issuebot/web/app.py` (edit 3 of 8) replace

```
from issuebot.log import get_logger
from issuebot.web.views import (
    RECENT_EVENTS_LIMIT,
    REFRESH_MIN_INTERVAL_S,
    iso,
    issue_document,
    snapshot_age_s,
    state_document,
    stats_document,
```

with

```
from issuebot.db.queries import IssueRow, RunRow, TurnRow, TurnSummaryRow
from issuebot.log import get_logger
from issuebot.web.transcript import parse_transcript
from issuebot.web.views import (
    CHART_POLL_S,
    LIVE_POLL_S,
    RECENT_EVENTS_LIMIT,
    REFRESH_MIN_INTERVAL_S,
    RUN_ID_PATTERN,
    age_text,
    dashboard_context,
    describe_event,
    duration_text,
    iso,
    issue_document,
    money,
    safe_href,
    snapshot_age_s,
    stamp_text,
    state_document,
    stats_document,
    thousands,
    turn_url,
```

In `src/issuebot/web/app.py` (edit 4 of 8) replace

```
_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}
```

with

```
CHART_DAYS = 30
RAW_PART_PATTERN = r"^(prompt|stream|stderr)$"
STATIC_ROOT = files("issuebot.web") / "static"
_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}
_RAW_EXTENSIONS = {"prompt": "md", "stream": "jsonl", "stderr": "log"}
```

In `src/issuebot/web/app.py` (edit 5 of 8) replace

```
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

```

with

```
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def template_environment() -> Environment:
    """The package's templates with autoescape, StrictUndefined and the display filters."""
    env = Environment(
        loader=PackageLoader("issuebot.web", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["href"] = safe_href
    env.filters["age"] = age_text
    env.filters["stamp"] = stamp_text
    env.filters["duration"] = duration_text
    env.filters["money"] = money
    env.filters["thousands"] = thousands
    return env

```

In `src/issuebot/web/app.py` (edit 6 of 8) replace

```
    app.state.settings = settings
```

with

```
    env = template_environment()
    labels = settings.github.labels
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

    def render(name: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
        text = env.get_template(name).render(
            repo=settings.github.repo,
            live_poll_s=LIVE_POLL_S,
            chart_poll_s=CHART_POLL_S,
            chart_days=CHART_DAYS,
            now=now(),
            **context,
        )
        return HTMLResponse(text, status_code=status_code)
```

In `src/issuebot/web/app.py` (edit 7 of 8) replace

```
        return PlainTextResponse(f"{status} {message}", status_code=status)
```

with

```
        return render("error.html", status=status, code=code, message=message, status_code=status)
```

In `src/issuebot/web/app.py` (edit 8 of 8) replace

```
        return error_response(request, 404, "not_found", "not found")

```

with

```
        return error_response(request, 404, "not_found", "not found")

    # --- pages ------------------------------------------------------------------------------

    async def live_context() -> dict[str, Any]:
        async with database.queries() as queries:
            row = await queries.snapshot()
            groups = await queries.issues_by_state()
            closed_1d = await queries.closed_count(timedelta(days=1))
            closed_7d = await queries.closed_count(timedelta(days=7))
            runs_1d = await queries.runs_count(timedelta(days=1))
            runs_7d = await queries.runs_count(timedelta(days=7))
        return dashboard_context(
            row,
            groups,
            closed_1d=closed_1d,
            closed_7d=closed_7d,
            runs_1d=runs_1d,
            runs_7d=runs_7d,
            now=now(),
            labels=labels,
        )

    async def load_turn(
        number: int, run_id: str, turn_number: int
    ) -> tuple[IssueRow, RunRow, TurnRow]:
        async with database.queries() as queries:
            issue = await queries.issue(number)
            runs = await queries.runs_for_issue(number) if issue is not None else []
            run = next((candidate for candidate in runs if candidate.run_id == run_id), None)
            turn = await queries.turn(run_id, turn_number) if run is not None else None
        if issue is None or run is None or turn is None:
            raise HTTPException(404, f"issue #{number} has no run {run_id} turn {turn_number}")
        return issue, run, turn

    @app.get("/")
    async def index() -> HTMLResponse:
        return render("index.html", live=await live_context())

    @app.get("/partials/dashboard")
    async def partial_dashboard() -> HTMLResponse:
        try:
            live = await live_context()
        except DatabaseError as exc:
            log.warning("web_database_error", path="/partials/dashboard", error=exc.message)
            live = {"unavailable": exc.message}
            return render("partials/dashboard.html", live=live, status_code=503)
        return render("partials/dashboard.html", live=live)

    @app.get("/issues/{number}")
    async def issue_page(number: int) -> HTMLResponse:
        async with database.queries() as queries:
            issue = await queries.issue(number)
            if issue is None:
                raise HTTPException(404, f"issue #{number} is not known")
            runs = await queries.runs_for_issue(number)
            turns = await queries.turn_summaries_for_issue(number)
            events = await queries.events_for_issue(number, RECENT_EVENTS_LIMIT)
            snapshot = await queries.snapshot()
        turns_by_run: dict[str, list[TurnSummaryRow]] = {}
        for turn in turns:
            turns_by_run.setdefault(turn.run_id, []).append(turn)
        document = issue_document(issue, runs, turns, events, snapshot)
        return render(
            "issue.html",
            issue=issue,
            runs=runs,
            turns_by_run=turns_by_run,
            events=[(event, describe_event(event)) for event in events],
            running=document["running"],
            retry=document["retry"],
        )

    @app.get("/issues/{number}/runs/{run_id}/turns/{turn_number}")
    async def turn_page(
        number: int, turn_number: int, run_id: str = PathParam(pattern=RUN_ID_PATTERN)
    ) -> HTMLResponse:
        issue, run, turn = await load_turn(number, run_id, turn_number)
        return render(
            "turn.html",
            issue=issue,
            run=run,
            turn=turn,
            transcript=parse_transcript(turn.stream),
            raw_url=turn_url(number, run_id, turn_number),
        )

    @app.get("/issues/{number}/runs/{run_id}/turns/{turn_number}/{part}")
    async def turn_raw(
        number: int,
        turn_number: int,
        run_id: str = PathParam(pattern=RUN_ID_PATTERN),
        part: str = PathParam(pattern=RAW_PART_PATTERN),
    ) -> PlainTextResponse:
        _issue, _run, turn = await load_turn(number, run_id, turn_number)
        filename = f"{run_id}-turn-{turn_number}.{_RAW_EXTENSIONS[part]}"
        headers = {"Content-Disposition": f'inline; filename="{filename}"'}
        return PlainTextResponse(getattr(turn, part), headers=headers)

```

In `src/issuebot/web/views.py` (edit 1 of 3) replace

```
from datetime import date, datetime
from typing import Any, Literal

from issuebot.db.queries import (
```

with

```
from datetime import UTC, date, datetime
from typing import Any, Literal

from issuebot.config import GitHubLabels
from issuebot.db.queries import (
    COMPLETE_LIMIT,
```

In `src/issuebot/web/views.py` (edit 2 of 3) replace

```
    TurnSummaryRow,
)

LIVE_POLL_S = 10
```

with

```
    TurnSummaryRow,
)
from issuebot.github import StateLabel

LIVE_POLL_S = 10
```

In `src/issuebot/web/views.py` (edit 3 of 3) replace

```
    return f"run {run_id} turn {turn_number}"

```

with

```
    return f"run {run_id} turn {turn_number}"


# --- template filters -------------------------------------------------------------------------


def age_text(value: object, now: datetime) -> str:
    """``12 s ago``, ``3 min ago``, ``2 h ago``, ``4 d ago``; ``-`` for None; other text as is."""
    moment = _datetime(value)
    if moment is None:
        return "-" if value is None else str(value)
    seconds = max(int((now - moment).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def stamp_text(value: object) -> str:
    """A second-precision UTC stamp for a datetime or an ISO 8601 string; ``-`` for None."""
    moment = _datetime(value)
    if moment is None:
        return "-" if value is None else str(value)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def duration_text(value: object) -> str:
    """Milliseconds as ``3m21s``; ``-`` for None."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "-"
    total = int(value) // 1000
    return f"{total // 60}m{total % 60:02d}s"


def money(value: object) -> str:
    return f"${_float(value):.2f}"


def thousands(value: object) -> str:
    return f"{_int(value):,}"


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# --- the live region --------------------------------------------------------------------------


def dashboard_context(
    row: SnapshotRow | None,
    groups: dict[str, list[IssueRow]],
    *,
    closed_1d: int,
    closed_7d: int,
    runs_1d: int,
    runs_7d: int,
    now: datetime,
    labels: GitHubLabels,
) -> dict[str, Any]:
    """What partials/dashboard.html renders: the worker line, hero stats, panels, columns."""
    running = [running_entry(entry) for entry in _entries(row, "running")]
    retrying = [retry_entry(entry) for entry in _entries(row, "retrying")]
    data = row.data if row is not None else {}
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    worker: dict[str, Any] = {"status": worker_status(row, now)}
    if row is not None:
        worker.update(
            written_at=iso(row.written_at),
            tick_count=data.get("tick_count"),
            poll_interval_ms=data.get("poll_interval_ms"),
            max_concurrent_agents=data.get("max_concurrent_agents"),
            config_valid=data.get("config_valid"),
            config_error=data.get("config_error"),
        )
    names = labels.model_dump()
    columns = []
    for role in StateLabel:
        rows = groups.get(role.value, [])
        capped = role is StateLabel.COMPLETE and len(rows) >= COMPLETE_LIMIT
        columns.append(
            {"role": role.value, "label": names[role.value], "rows": rows, "capped": capped}
        )
    return {
        "unavailable": None,
        "worker": worker,
        "hero": {
            "closed_1d": closed_1d,
            "closed_7d": closed_7d,
            "runs_1d": runs_1d,
            "runs_7d": runs_7d,
            "running": len(running),
            "retrying": len(retrying),
            "cost_usd": _float(totals.get("cost_usd")),
            "total_tokens": _int(totals.get("total_tokens")),
        },
        "running": running,
        "retrying": retrying,
        "columns": columns,
    }

```

- [ ] **Step 7: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 120 uv run pytest tests/test_web_pages.py tests/test_web_app.py -q && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q && uv run pre-commit run --all-files`
Expected: `85 passed`; then `781 passed, 40 skipped`; then `821 passed`; pre-commit passes and leaves `htmx.min.js` untouched (`sha256sum src/issuebot/web/static/vendor/htmx.min.js` still reads `71ea6718...`).

- [ ] **Step 8: Commit**

```bash
git status --short && git add --all && git commit -m "feat: dashboard pages, templates, stylesheet, vendored htmx and Chart.js" -m "<trailer>"
```

---

### Task 7: The database contract tests (and the `captured_turns` fix they force)

**Files:**
- Create: `tests/test_web_app_db.py`
- Modify: `src/issuebot/web/views.py`, `tests/test_web_app.py`

**Interfaces:**
- Consumes: everything so far; Phase 6's `PostgresStore`, `migrate`, `Database`; the `db_url` fixture.
- Produces: `issue_document`'s `runs[]` carry the captured turn rows as `"captured_turns"` (the run's turn count keeps the `runs` column name `"turns"`); the seeded contract tests.

Spec: §6.3 (`runs[].captured_turns`), §9 (`test_web_app_db.py`).

- [ ] **Step 1: Write the tests**

Create `tests/test_web_app_db.py`:

```python
"""The API contract and the pages against a seeded database (skipped without DATABASE_URL)."""

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from issuebot.agent.turnlog import capture_turns
from issuebot.config import GitHubLabels, GitHubSettings, Settings
from issuebot.db import Database, migrate
from issuebot.db.store import IssueSnapshot, PostgresStore
from issuebot.events import RunEnded, RunStarted, StateChanged
from issuebot.github import Issue, LinkedPr, StateLabel
from issuebot.orchestrator.state import ClaudeTotals, Counters, RuntimeSnapshot
from issuebot.web import create_app

SAMPLE = Path(__file__).parent / "fixtures" / "runs" / "20260904T202535Z-0964cd"
RUN_ID = "20260904T202535Z-0964cd"
NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
SETTINGS = Settings(github=GitHubSettings(repo="example/repo"))


@pytest.fixture
async def seeded(db_url: str, make_issue: Callable[..., Issue]) -> AsyncIterator[Database]:
    """Three issues, one finished run on #7 with the sample turn captured twice, a snapshot."""
    await migrate(db_url)
    store = PostgresStore(db_url, labels=GitHubLabels())
    await store.connect()

    def issue(number: int, state: StateLabel, **overrides: Any) -> Issue:
        fields: dict[str, Any] = {
            "number": number,
            "identifier": f"repo-{number}",
            "title": f"Issue {number} <b>title</b>",
            "state": state,
            "state_labels": (f"issuebot/{state.value.replace('_', '-')}",),
            "labels": (f"issuebot/{state.value.replace('_', '-')}",),
            "updated_at": NOW - number * HOUR,
        }
        fields.update(overrides)
        return make_issue(**fields)

    pr = LinkedPr(
        number=8, url="https://github.com/example/repo/pull/8", state="open", merged_at=None
    )
    issues = [
        issue(1, StateLabel.TODO),
        issue(7, StateLabel.REVIEW, linked_pr=pr),
        issue(10, StateLabel.COMPLETE, github_state="closed", closed_at=NOW - HOUR),
    ]
    await store.upsert_issues([IssueSnapshot(issue=i, seen_at=NOW - 2 * HOUR) for i in issues])
    await store.apply_event(
        RunStarted(
            issue_number=7,
            issue_identifier="repo-7",
            run_id=RUN_ID,
            attempt=1,
            session_id="sess-7",
            workspace_path="/workspaces/repo-7",
            at=NOW - 30 * timedelta(minutes=1),
        )
    )
    (capture,) = capture_turns(SAMPLE)
    second = replace(capture, turn_number=2)
    await store.apply_event(
        RunEnded(
            issue_number=7,
            issue_identifier="repo-7",
            run_id=RUN_ID,
            outcome="succeeded",
            error=None,
            turns=2,
            input_tokens=1026676,
            output_tokens=16850,
            cost_usd=1.7952,
            duration_s=410.0,
            log_dir=str(SAMPLE),
            at=NOW - 23 * timedelta(minutes=1),
        ),
        turns=[capture, second],
    )
    await store.apply_event(
        StateChanged(
            issue_number=7,
            issue_identifier="repo-7",
            from_label="issuebot/in-progress",
            to_label="issuebot/review",
            actor="agent",
            pr_url=pr.url,
            at=NOW - 22 * timedelta(minutes=1),
        )
    )
    snapshot = RuntimeSnapshot(
        at=NOW,
        workflow_path="/app/WORKFLOW.md",
        workflow_mtime_ns=1,
        config_valid=True,
        config_error=None,
        poll_interval_ms=30_000,
        max_concurrent_agents=2,
        tick_count=12,
        last_tick_at=NOW,
        running=(),
        retrying=(),
        totals=ClaudeTotals(input_tokens=1026676, output_tokens=16850, cost_usd=1.7952),
        counters=Counters(runs_started=1, runs_ended=1),
    )
    await store.write_snapshot(snapshot.at, snapshot.to_dict())
    await store.close()
    yield Database(db_url)


@pytest.fixture
def client(seeded: Database) -> TestClient:
    return TestClient(create_app(seeded, SETTINGS))


async def test_the_issue_api_lists_the_run_and_its_turns(client: TestClient) -> None:
    response = client.get("/api/v1/issues/7")
    assert response.status_code == 200
    body = response.json()
    assert (body["issue"]["state"], body["issue"]["pr_number"]) == ("review", 8)
    (run,) = body["runs"]
    assert (run["run_id"], run["outcome"], run["turns"]) == (RUN_ID, "succeeded", 2)
    captured = run["captured_turns"]
    assert [turn["turn_number"] for turn in captured] == [1, 2]
    assert captured[0]["model"] == "claude-opus-5" and captured[0]["num_turns"] == 19
    assert captured[0]["url"] == f"/issues/7/runs/{RUN_ID}/turns/1"
    assert [log["turn_number"] for log in body["logs"]] == [1, 2]
    assert [event["kind"] for event in body["recent_events"]] == [
        "state_changed",
        "run_ended",
        "run_started",
    ]
    assert body["running"] is None and body["retry"] is None


async def test_the_turn_page_and_the_raw_stream_come_from_run_turns(client: TestClient) -> None:
    page = client.get(f"/issues/7/runs/{RUN_ID}/turns/2")
    assert page.status_code == 200
    assert "turn 2 of 2" in page.text and "Bash" in page.text and "claude-opus-5" in page.text
    raw = client.get(f"/issues/7/runs/{RUN_ID}/turns/1/stream")
    assert raw.status_code == 200
    assert raw.text == (SAMPLE / "turn-1.jsonl").read_text(encoding="utf-8")
    prompt = client.get(f"/issues/7/runs/{RUN_ID}/turns/1/prompt")
    assert prompt.text == (SAMPLE / "turn-1.prompt.md").read_text(encoding="utf-8")


async def test_stats_match_the_query_module(client: TestClient, seeded: Database) -> None:
    body = client.get("/api/v1/stats?window=7d").json()
    async with seeded.queries() as q:
        closed = await q.closed_count(timedelta(days=7))
        runs = await q.runs_count(timedelta(days=7))
        counts = await q.state_counts()
        series = await q.daily_series(7)
    assert (body["closed"], body["runs"]) == (closed, runs) == (1, 1)
    assert body["by_state"] == counts
    assert counts == {"todo": 1, "in_progress": 0, "review": 1, "rework": 0, "complete": 1}
    assert len(body["series"]) == len(series) == 7
    assert body["series"][-1]["runs"] == series[-1].runs


async def test_the_dashboard_renders_the_seeded_rows(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    for label in GitHubLabels().as_tuple():
        assert label in response.text
    assert (
        "Issue 7 &lt;b&gt;title&lt;/b&gt;" in response.text and "<b>title</b>" not in response.text
    )
    assert 'href="/issues/7"' in response.text and 'href="/issues/10"' in response.text
    assert "PR #8" in response.text
    assert 'class="panel worker ok"' in response.text and "tick 12" in response.text


async def test_state_and_healthz_read_the_snapshot(client: TestClient) -> None:
    state = client.get("/api/v1/state").json()
    assert state["worker"]["tick_count"] == 12 and state["worker"]["stale"] is False
    assert state["claude_totals"]["total_tokens"] == 1026676 + 16850
    assert state["counters"]["runs_ended"] == 1
    health = client.get("/healthz").json()
    assert (health["status"], health["database"], health["worker"]) == ("ok", "ok", "ok")
    assert client.post("/api/v1/refresh").status_code == 202


async def test_unknown_issue_is_a_404_against_the_real_database(client: TestClient) -> None:
    assert client.get("/api/v1/issues/99").json()["error"]["code"] == "unknown_issue"
    assert client.get("/issues/99").status_code == 404
```

- [ ] **Step 2: Run them to see the one that fails**

Run: `DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest tests/test_web_app_db.py -q`
Expected: `1 failed, 5 passed`; the failure is `test_the_issue_api_lists_the_run_and_its_turns` with `assert (run["run_id"], run["outcome"], run["turns"]) == (RUN_ID, "succeeded", 2)` reading a list where the run's turn count should be: `RunRow` already has a `turns` column and Task 5's `issue_document` overwrote it with the captured turns (the hermetic test never noticed because its fake rows were consistent either way). Without `DATABASE_URL` the file reports `6 skipped`.

- [ ] **Step 3: Rename the list to `captured_turns`**

In `src/issuebot/web/views.py` (edit 1 of 2) replace

```
    """GET /api/v1/issues/<n>: the row, the snapshot's entries for it, runs with turns, events."""
```

with

```
    """GET /api/v1/issues/<n>: the row, the snapshot's entries, runs with captured turns, events.

    ``runs[].turns`` stays the run's turn count (a ``runs`` column); the captured turn rows
    are ``runs[].captured_turns``.
    """
```

In `src/issuebot/web/views.py` (edit 2 of 2) replace

```
        run_documents.append({**row_dict(run), "turns": run_turns})
```

with

```
        run_documents.append({**row_dict(run), "captured_turns": run_turns})
```

In `tests/test_web_app.py` replace

```
    (turn,) = run["turns"]
```

with

```
    assert run["turns"] == 1  # the run's turn count, a runs column
    (turn,) = run["captured_turns"]
```

- [ ] **Step 4: Run the tests to verify they pass, both ways**

Run: `uv run ruff check . && uv run ruff format --check . && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest tests/test_web_app_db.py tests/test_web_app.py -q && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `61 passed`; then `781 passed, 46 skipped`; then `827 passed`.

- [ ] **Step 5: Commit**

```bash
git status --short && git add --all && git commit -m "test: API contract against a seeded database; runs[].captured_turns" -m "<trailer>"
```

---

### Task 8: `issuebot web`, the compose `web` service, `stats` on `state_counts`

**Files:**
- Modify: `src/issuebot/cli.py`, `compose.yaml`, the dot-env example (`ISSUEBOT_WEB_PORT`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 5's `create_app`; Task 3's `MAX_WINDOW_DAYS`, `state_counts`; Phase 6's `_open_database`, `_database_or_report`.
- Produces: `issuebot web [--workflow PATH] [--port N] [--bind HOST]`; `cli._uvicorn_serve(app, *, host, port)` and the seam `cli._serve = _uvicorn_serve`; `cmd_web`, `_run_web(workflow, *, port, bind) -> int`; `_NOT_CONFIGURED`; log event `web_started` (`bind`, `port`, `database`); `stats --days` bounded by `MAX_WINDOW_DAYS` and `by_state` from `state_counts()`; the compose `web` service.

Spec: §7.1, §7.2.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py` (edit 1 of 5) replace

```
from issuebot.db import MigrationResult, Probe, StoreError, StoreUnavailableError
```

with

```
from issuebot.db import (
    MAX_WINDOW_DAYS,
    MigrationResult,
    Probe,
    StoreError,
    StoreUnavailableError,
)
```

In `tests/test_cli.py` (edit 2 of 5) replace

```

@pytest.mark.parametrize("command", [["migrate"], ["status"], ["stats"], ["refresh"]])
def test_database_commands_need_a_configured_url(
```

with

```

@pytest.mark.parametrize("command", [["migrate"], ["status"], ["stats"], ["refresh"], ["web"]])
def test_database_commands_need_a_configured_url(
```

In `tests/test_cli.py` (edit 3 of 5) replace

```

@pytest.mark.parametrize("command", [["migrate"], ["status"], ["stats"], ["refresh"]])
def test_database_commands_exit_two_on_an_unloadable_workflow(
```

with

```

@pytest.mark.parametrize("command", [["migrate"], ["status"], ["stats"], ["refresh"], ["web"]])
def test_database_commands_exit_two_on_an_unloadable_workflow(
```

In `tests/test_cli.py` (edit 4 of 5) replace

```
    queries.groups["review"] = [object()]
```

with

```
    queries.counts["review"] = 1
    queries.counts["complete"] = 73  # from state_counts, so no COMPLETE_LIMIT cap
```

In `tests/test_cli.py` (edit 5 of 5) replace

```
    assert "issues: todo 0, in_progress 0, review 1, rework 0, complete 0" in out
    assert out.endswith("DAY         CLOSED  RUNS\n2026-09-04  1       3\n")
    assert queries.days_asked == 3
    assert main(["stats", "--workflow", str(path), "--days", "0"]) == 1
    assert capsys.readouterr().out == "[FAIL] stats: --days must be at least 1\n"
    queries.error = StoreUnavailableError("cannot connect: refused")
    assert main(["stats", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"
```

with

```
    assert "issues: todo 0, in_progress 0, review 1, rework 0, complete 73" in out
    assert out.endswith("DAY         CLOSED  RUNS\n2026-09-04  1       3\n")
    assert queries.days_asked == 3
    assert "issues_by_state" not in queries.calls
    for days in ("0", str(MAX_WINDOW_DAYS + 1)):
        assert main(["stats", "--workflow", str(path), "--days", days]) == 1
        assert capsys.readouterr().out == (
            f"[FAIL] stats: --days must be between 1 and {MAX_WINDOW_DAYS}\n"
        )
    assert main(["stats", "--workflow", str(path), "--days", str(MAX_WINDOW_DAYS)]) == 0
    assert queries.days_asked == MAX_WINDOW_DAYS
    capsys.readouterr()
    queries.error = StoreUnavailableError("cannot connect: refused")
    assert main(["stats", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"


class FakeServe:
    """Stands in for cli._serve: records the app and the bind instead of running uvicorn."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, int]] = []

    async def __call__(self, app: object, *, host: str, port: int) -> None:
        self.calls.append((app, host, port))


@pytest.fixture
def fake_serve(monkeypatch: pytest.MonkeyPatch) -> FakeServe:
    fake = FakeServe()
    monkeypatch.setattr("issuebot.cli._serve", fake)
    return fake


def test_web_migrates_then_serves_on_the_configured_bind(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    path = _write(
        tmp_path,
        "---\ngithub:\n  repo: example/repo\nserver:\n  bind: 127.0.0.1\n  port: 9000\n---\nBody",
    )
    assert main(["web", "--workflow", str(path)]) == 0
    assert fake_database.migrations == 1 and fake_database.urls == [DB_URL]
    ((app, host, port),) = fake_serve.calls
    assert (host, port) == ("127.0.0.1", 9000)
    assert getattr(app, "title", None) == "issuebot"
    err = capsys.readouterr().err
    assert "web_started" in err and "s3cret" not in err


def test_web_overrides_the_bind_and_port_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
) -> None:
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["web", "--workflow", str(path), "--bind", "0.0.0.0", "--port", "0"]) == 0
    ((_app, host, port),) = fake_serve.calls
    assert (host, port) == ("0.0.0.0", 0)


def test_web_fails_fast_when_the_migration_fails(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_database: FakeDatabase,
    fake_serve: FakeServe,
) -> None:
    fake_database.migrate_error = StoreUnavailableError("cannot connect: refused")
    path = _db_workflow(tmp_path, monkeypatch)
    assert main(["web", "--workflow", str(path)]) == 1
    assert capsys.readouterr().out == "[FAIL] database: cannot connect: refused\n"
    assert fake_serve.calls == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `timeout 300 uv run pytest tests/test_cli.py -q`
Expected: `3 failed, 101 passed, 3 errors`; the errors read `AttributeError: module 'issuebot.cli' has no attribute '_serve'` (the `fake_serve` fixture), the `web` parametrisations fail with `argparse.ArgumentError: argument <command>: invalid choice: 'web'` (shown as `SystemExit: 2`), and the stats test reads `assert 'issues: todo 0, in_progress 0, review 1, rework 0, complete 73' in ...` against `complete 0`.

- [ ] **Step 3: The command, the seam, `stats`, compose and the dot-env example**

In `.env.example` replace

```

# Optional: where the worker records events, runs, issue snapshots and its runtime snapshot
```

with

```

# Host port for the compose web service, the dashboard, published on loopback only
# (container side stays 8080, WORKFLOW.md's server.port).
ISSUEBOT_WEB_PORT=8080

# Optional: where the worker records events, runs, issue snapshots and its runtime snapshot
```

In `compose.yaml` replace

```yaml

volumes:
```

with

```yaml

  web:
    build: .
    # The dashboard and the JSON API. Reads WORKFLOW.md for database.url, server.* and the
    # repository name; needs no GitHub, Claude or Slack credential, so no env_file and no
    # workspace volume. Migrates at start like the worker (the advisory lock makes that safe).
    command: ["web"]
    init: true
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://issuebot:issuebot@db:5432/issuebot
    ports:
      # Loopback only: the dashboard has no authentication. Override the host port with
      # ISSUEBOT_WEB_PORT in the project dot-env file; the container side is server.port.
      - "127.0.0.1:${ISSUEBOT_WEB_PORT:-8080}:8080"
    volumes:
      - ./WORKFLOW.md:/app/WORKFLOW.md:ro
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
    depends_on:
      db:
        condition: service_healthy

volumes:
```

In `src/issuebot/cli.py` (edit 1 of 10) replace

```
from typing import Literal
from urllib.parse import urlsplit

```

with

```
from typing import Any, Literal
from urllib.parse import urlsplit

import uvicorn
```

In `src/issuebot/cli.py` (edit 2 of 10) replace

```
from issuebot.db import Database, DatabaseError, PostgresSink, RefreshListener, is_postgres_url
```

with

```
from issuebot.db import (
    MAX_WINDOW_DAYS,
    Database,
    DatabaseError,
    PostgresSink,
    RefreshListener,
    is_postgres_url,
)
```

In `src/issuebot/cli.py` (edit 3 of 10) replace

```
from issuebot.orchestrator import Orchestrator, OrchestratorStartupError

```

with

```
from issuebot.orchestrator import Orchestrator, OrchestratorStartupError
from issuebot.web import create_app

```

In `src/issuebot/cli.py` (edit 4 of 10) replace

```
_database_factory: Callable[[str], Database] = Database

```

with

```
_database_factory: Callable[[str], Database] = Database


async def _uvicorn_serve(app: Any, *, host: str, port: int) -> None:
    """Serve ``app`` with uvicorn until SIGTERM or SIGINT, then return so the command exits 0.

    uvicorn installs its own handlers for both signals and, once its server has shut down,
    re-raises the signal that stopped it with the previous handler restored. The no-op
    handlers installed here make that re-raise harmless; uvicorn's own log lines go through
    the root logger (``log_config=None``), so they come out as structlog lines.
    """
    config = uvicorn.Config(app, host=host, port=port, log_config=None)
    server = uvicorn.Server(config)
    previous = {
        signum: signal.signal(signum, lambda signum, frame: None)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        await server.serve()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


_serve = _uvicorn_serve

```

In `src/issuebot/cli.py` (edit 5 of 10) replace

```
    refresh.set_defaults(func=cmd_refresh)
    return parser
```

with

```
    refresh.set_defaults(func=cmd_refresh)

    web = subparsers.add_parser(
        "web", help="serve the dashboard and the JSON API until SIGTERM or SIGINT"
    )
    _add_workflow_option(web)
    web.add_argument("--port", type=int, default=None, help="listen port (default: server.port)")
    web.add_argument("--bind", default=None, help="listen address (default: server.bind)")
    web.set_defaults(func=cmd_web)
    return parser
```

In `src/issuebot/cli.py` (edit 6 of 10) replace

```
def _database_or_report(settings: Settings) -> Database | None:
    if settings.database.url is None:
        print("[FAIL] database: not configured; export DATABASE_URL or set database.url: $VAR")
```

with

```
_NOT_CONFIGURED = "[FAIL] database: not configured; export DATABASE_URL or set database.url: $VAR"


def _database_or_report(settings: Settings) -> Database | None:
    if settings.database.url is None:
        print(_NOT_CONFIGURED)
```

In `src/issuebot/cli.py` (edit 7 of 10) replace

```
    if args.days < 1:
        print("[FAIL] stats: --days must be at least 1")
```

with

```
    if not 1 <= args.days <= MAX_WINDOW_DAYS:
        print(f"[FAIL] stats: --days must be between 1 and {MAX_WINDOW_DAYS}")
```

In `src/issuebot/cli.py` (edit 8 of 10) replace

```
            groups = await queries.issues_by_state()
```

with

```

```

In `src/issuebot/cli.py` (edit 9 of 10) replace

```
                by_state={state: len(rows) for state, rows in groups.items()},
```

with

```
                by_state=await queries.state_counts(),
```

In `src/issuebot/cli.py` (edit 10 of 10) replace

```
    print("[ OK ] refresh: notified issuebot_refresh")
    return 0
```

with

```
    print("[ OK ] refresh: notified issuebot_refresh")
    return 0


# --- web -------------------------------------------------------------------------------------


def cmd_web(args: argparse.Namespace) -> int:
    workflow = _load_or_report(args)
    if workflow is None:
        return 2
    return asyncio.run(_run_web(workflow, port=args.port, bind=args.bind))


async def _run_web(workflow: Workflow, *, port: int | None, bind: str | None) -> int:
    """Migrate, build the app and serve it until a stop signal; the database is required."""
    settings = workflow.config
    try:
        database = await _open_database(settings)
    except DatabaseError as exc:
        print(f"[FAIL] database: {exc.message}")
        return 1
    if database is None:
        print(_NOT_CONFIGURED)
        return 1
    host = bind or settings.server.bind
    listen_port = settings.server.port if port is None else port
    get_logger(__name__).info(
        "web_started", bind=host, port=listen_port, database=database.description
    )
    await _serve(create_app(database, settings), host=host, port=listen_port)
    return 0
```

Then, with the Edit tool (the dot-env hook blocks naming the file in a shell command), in the dot-env example replace

```
# Host port for the compose db service (container side stays 5432).
ISSUEBOT_DB_PORT=5432
```

with

```
# Host port for the compose db service (container side stays 5432).
ISSUEBOT_DB_PORT=5432

# Host port for the compose web service, the dashboard, published on loopback only
# (container side stays 8080, WORKFLOW.md's server.port).
ISSUEBOT_WEB_PORT=8080
```

- [ ] **Step 4: Run the tests to verify they pass, both ways, and smoke-test the real server**

Run: `uv run ruff check . && uv run ruff format --check . && timeout 300 uv run pytest tests/test_cli.py -q && docker compose config --quiet && echo compose ok && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q`
Expected: `107 passed`; `compose ok`; then `786 passed, 46 skipped`; then `832 passed`.

The seam hides uvicorn from the tests, so prove the real path once, against a throwaway schema so the operator's Phase 6 history is not migrated yet (the `&` in the second command backgrounds only the parenthesised killer; `PORT` is set in its own statement first):

```bash
docker exec -i issuebot-db-1 psql -q -U issuebot -d issuebot -c "DROP SCHEMA IF EXISTS p7smoke CASCADE; CREATE SCHEMA p7smoke;" && PORT=$(uv run python -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()") && echo "port $PORT"
```

```bash
( sleep 6; kill -TERM "$(pgrep -f "\.venv/bin/python.*\.venv/bin/issuebot web --bind 127.0.0.1 --port $PORT" | head -1)" ) & export DATABASE_URL="postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot?options=-c%20search_path%3Dp7smoke"; timeout 30 uv run issuebot web --bind 127.0.0.1 --port "$PORT" > /tmp/p7smoke.log 2>&1; echo "web exit code $?"; unset DATABASE_URL; wait; grep -c '"event": "Uvicorn running' /tmp/p7smoke.log; docker exec -i issuebot-db-1 psql -q -U issuebot -d issuebot -c "SELECT version, name FROM p7smoke.schema_migrations ORDER BY version" -c "DROP SCHEMA p7smoke CASCADE;"
```

Expected: `web exit code 0` (SIGTERM ends the command cleanly), `1`, the schema table listing versions 1 `initial` and 2 `run_turns` before the drop. The log's lines are JSON with `"logger": "issuebot.cli"` (`db_migrated`, `web_started`), `"logger": "uvicorn.error"` (`Started server process`, `Uvicorn running on http://127.0.0.1:<port>`, `Shutting down`, `Finished server process`), all through structlog. While the server is up, `curl -sS http://127.0.0.1:$PORT/healthz` answers `{"status":"ok","database":"ok","snapshot_at":null,"snapshot_age_s":null,"worker":"none"}` if you probe it inside the six seconds; the live check (Task 10) exercises every route at length.

- [ ] **Step 5: Commit**

```bash
git status --short && git add --all && git commit -m "feat: issuebot web, the compose web service, stats on state_counts" -m "<trailer>"
```

---

### Task 9: Documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md` (the `run_turns` row, the §2.8 route, the Phase 7 decisions, the Later list), `docs/superpowers/specs/2026-09-04-phase-6-persistence-design.md` (three amendment notes)

- [ ] **Step 1: The edits**

In `CLAUDE.md` (edit 1 of 4) replace

```markdown
docker compose build                 # image: git, gh, claude, app venv
docker compose up                    # db (postgres:18) + worker (issuebot worker)
```

with

```markdown
uv run issuebot web [--port N] [--bind HOST]   # the dashboard and the JSON API (needs DATABASE_URL)
docker compose build                 # image: git, gh, claude, app venv
docker compose up                    # db (postgres:18) + worker (issuebot worker) + web (issuebot web,
                                     #   http://127.0.0.1:${ISSUEBOT_WEB_PORT:-8080})
```

In `CLAUDE.md` (edit 2 of 4) replace

```markdown
  Tests use `tests/fakes/claude` (replays `tests/fixtures/claude/*.jsonl`).
```

with

```markdown
  Tests use `tests/fakes/claude` (replays `tests/fixtures/claude/*.jsonl`). `turnlog` (Phase 7):
  `capture_turns(log_dir)` reads a run's `turn-N.jsonl`, `.prompt.md` and `.stderr.log` into
  `TurnCapture`s, capped (prompt 256 KiB head; a stream line over 64 KiB becomes an
  `issuebot_omitted` stub; 2 MiB of head lines plus the last `result` line; stderr 64 KiB tail;
  result text 4 KiB), with the summary parsed from the init and result lines; it never raises.
  `tests/fixtures/runs/<run_id>/` holds a real turn (scratch issue #7), kept byte-for-byte
  (pre-commit excludes it).
```

In `CLAUDE.md` (edit 3 of 4) replace

```markdown
- `issuebot.db`: the observability store, imported by `cli` only; imports `config`, `events`,
  `github` and `log`. `migrations/NNNN_name.sql` applied by `migrate.py` in one transaction
  under an advisory lock (`schema_migrations` bookkeeping; a recorded version newer than the
  files is an error). `connection.py`: `connect` (autocommit, 5 s connect timeout, UTC session),
  `describe`/`redact` (the URL's password never reaches a log or a line), `reconnect_delay`
  (1, 2, 4, 8, 16, then 30 s). `store.py`: `PostgresStore` (`apply_event` appends to `events`
  and upserts `runs` on `run_started`/`run_ended` or updates `issues` on `state_changed`,
  `issue_completed`, `issue_cancelled`; `upsert_issues`; `write_snapshot`); every `issues`
  write is guarded by `seen_at`, so write order never matters. `sink.py`: `PostgresSink`
  (`handle` enqueues events, cap 1000; `record_issues` merges polled snapshots into one
  pending batch; `record_snapshot` keeps the latest; one drain task writes, reconnects with
  backoff and retries the item in flight; statement failures are dropped and counted;
  `close()` drains for up to 10 s). `listen.py`: `RefreshListener` (`LISTEN issuebot_refresh`
  on its own connection, callback per NOTIFY, reconnects). `queries.py`: `Queries` over one
  connection (`closed_count`, `runs_count`, `daily_series`, `issues_by_state`, `runs_for_issue`,
  `recent_events`, `snapshot`) returning the frozen row types Phase 7 renders. `database.py`:
  the `Database` facade the CLI goes through (`migrate`, `probe`, `queries`, `store`,
  `listener`, `notify_refresh`). Constants, not settings; a `database.url` change needs a
  restart. Tests: `db_url` (conftest) creates a schema per test and skips without
  `DATABASE_URL`; the sink and listener tests use fakes.
```

with

```markdown
- `issuebot.db`: the observability store, imported by `cli` and `web`; imports `config`,
  `events`, `github`, `log` and `agent.turnlog`. `migrations/NNNN_name.sql` (`0001_initial`,
  `0002_run_turns`) applied by `migrate.py` in one transaction under an advisory lock
  (`schema_migrations` bookkeeping; a recorded version newer than the files is an error).
  `connection.py`: `connect` (autocommit, 5 s connect timeout, UTC session), `describe`/`redact`
  (the URL's password never reaches a log or a line), `reconnect_delay` (1, 2, 4, 8, 16, then
  30 s). `store.py`: `PostgresStore` (`apply_event(event, turns=())` appends to `events`, upserts
  `runs` on `run_started`/`run_ended` and inserts the captured turns into `run_turns` in the
  `run_ended` transaction (idempotent per `(run_id, turn_number)`), or updates `issues` on
  `state_changed`, `issue_completed`, `issue_cancelled`; `upsert_issues`; `write_snapshot`);
  every `issues` write is guarded by `seen_at`, so write order never matters. `sink.py`:
  `PostgresSink` (`handle` enqueues events, cap 1000; `record_issues` merges polled snapshots
  into one pending batch; `record_snapshot` keeps the latest; one drain task writes, reconnects
  with backoff and retries the item in flight; a `run_ended` item's turn files are captured
  once, in a thread, before its first write attempt (`db_turns_captured`,
  `db_turns_capture_failed`); statement failures are dropped and counted; `close()` drains for
  up to 10 s). `listen.py`: `RefreshListener` (`LISTEN issuebot_refresh` on its own connection,
  callback per NOTIFY, reconnects). `queries.py`: `Queries` over one connection (`closed_count`,
  `runs_count`, `daily_series`, `issues_by_state` (unknown roles skipped), `state_counts`,
  `issue`, `runs_for_issue`, `events_for_issue`, `turn_summaries_for_issue`, `turn`,
  `recent_events`, `snapshot`) returning the frozen row types the dashboard renders;
  `MAX_WINDOW_DAYS = 365` bounds `--days` and the API window. `database.py`: the `Database`
  facade the CLI and the web app go through (`migrate`, `probe`, `queries`, `store`, `listener`,
  `notify_refresh`); one connection per call, no pool. Constants, not settings; a `database.url`
  change needs a restart. Tests: `db_url` (conftest) creates a schema per test and skips without
  `DATABASE_URL`; the sink and listener tests use fakes; `tests/fakes/database.py` is the
  `FakeDatabase` the CLI and web tests share.
- `issuebot.web`: the dashboard, imported by `cli` only; imports `config`, `db`, `github` and
  `log`. `app.py`: `create_app(database, settings, *, clock=, now=)` (FastAPI; pages `/`,
  `/issues/<n>`, `/issues/<n>/runs/<run_id>/turns/<t>` plus `/prompt|stream|stderr` as
  `text/plain`; `/partials/dashboard` (the htmx live region, every 10 s); `/api/v1/state`,
  `/api/v1/issues/<n>`, `/api/v1/stats?window=<N>d`, `POST /api/v1/refresh` (NOTIFY, throttled to
  one per 5 s, Symphony's `coalesced`), `/healthz` (503 only when the database does not answer;
  `worker` is `ok`, `stale` past three poll intervals, or `none`); `/static` (vendored htmx
  2.0.10 and Chart.js 4.5.1 under `static/vendor/`, kept byte-for-byte); JSON error envelopes
  under `/api/` and `/healthz`, `error.html` elsewhere; `DatabaseError` is 503; the four
  security headers on every response, a CSP without `unsafe-inline`). `views.py`: pure builders
  and template filters (`state_document`, `stats_document`, `issue_document` with
  `runs[].captured_turns`, `dashboard_context`, `describe_event`, `safe_href`, `window_days`,
  `worker_status`, `age_text`, `stamp_text`, ...). `transcript.py`: `parse_transcript(stream)`
  turns the stored stream-json into `Block`s (text, thinking, tool_use, tool_result, result,
  omitted, unparseable; status lines counted as `hidden`). Templates render with autoescape and
  `StrictUndefined`; nothing is inlined into HTML (`app.js` fetches the charts' data). One
  connection per request through `Database.queries()`. Constants, not settings; the web reads
  `WORKFLOW.md` once at start.
```

In `CLAUDE.md` (edit 4 of 4) replace

```markdown
  `status`, `stats [--days N]` and `refresh` (each `[FAIL] database:` and exit 1 without
  `DATABASE_URL`); `run-once` and `worker` migrate first when `database.url` is set (a failure
  is `[FAIL] database:` and exit 1), start the Slack and PostgreSQL sinks before and close them
  after (Slack never for a non-`https` webhook); `worker` also passes `on_snapshot`/`on_issues`
  to the orchestrator and runs the refresh listener; exit codes 0/1/2 (ok / failed / workflow
  unloadable).
  Tests substitute `_which`, `_claude_version`, `_adapter_factory`, `_run_session`,
  `_orchestrator_factory`, `_slack_post` and `_database_factory`.
```

with

```markdown
  `status`, `stats [--days N]` (`by_state` from `state_counts`; `--days` 1 to 365), `refresh` and
  `web [--port N] [--bind HOST]` (each `[FAIL] database:` and exit 1 without `DATABASE_URL`);
  `run-once`, `worker` and `web` migrate first when `database.url` is set (a failure is
  `[FAIL] database:` and exit 1); `run-once` and `worker` start the Slack and PostgreSQL sinks
  before and close them after (Slack never for a non-`https` webhook); `worker` also passes
  `on_snapshot`/`on_issues` to the orchestrator and runs the refresh listener; `web` builds
  `create_app` and serves it with uvicorn (`--port`/`--bind` override `server.*`; uvicorn's
  lines go through structlog; SIGTERM/SIGINT exit 0; a port in use is uvicorn's error and exit
  1); exit codes 0/1/2 (ok / failed / workflow unloadable).
  Tests substitute `_which`, `_claude_version`, `_adapter_factory`, `_run_session`,
  `_orchestrator_factory`, `_slack_post`, `_database_factory` and `_serve`.
```

In `README.md` (edit 1 of 2) replace

```markdown
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker (issuebot worker)
```

with

```markdown
uv run issuebot web               # the dashboard and its JSON API (needs DATABASE_URL)
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker + web (http://127.0.0.1:8080)
```

In `README.md` (edit 2 of 2) replace

```markdown

Slack notifications are optional: export `SLACK_WEBHOOK_URL` (an incoming webhook,
```

with

```markdown

The dashboard (`issuebot web`; the compose `web` service publishes it on the host's loopback
at `ISSUEBOT_WEB_PORT`, default 8080) shows the Kanban of the five label columns, the hero
stats, two 30-day charts, the running agents and, per issue, its runs with the transcript of
every captured turn; `/api/v1/state`, `/api/v1/issues/<n>`, `/api/v1/stats?window=7d` and
`POST /api/v1/refresh` serve the same as JSON and `/healthz` reports the database and the age
of the worker's last report. It needs `DATABASE_URL` and nothing else, reads `WORKFLOW.md`
once at start, and has no authentication: keep it on loopback (`server.bind: 127.0.0.1` outside
Docker) or behind a reverse proxy. Turn logs are captured into the database when a run ends,
so they outlive the workspace.

Slack notifications are optional: export `SLACK_WEBHOOK_URL` (an incoming webhook,
```

In `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md` (edit 1 of 4) replace

```markdown
| `runtime_snapshot` | Single row: the worker's in-memory snapshot, rewritten every tick |

```

with

```markdown
| `runtime_snapshot` | Single row: the worker's in-memory snapshot, rewritten every tick |
| `run_turns` | One row per captured turn of a run (Phase 7): the capped prompt, stream-json and stderr with a parsed summary; the dashboard's turn page reads it |

```

In `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md` (edit 2 of 4) replace

```markdown
`/issues/<n>` (run history, recent events, log links). API: `GET /api/v1/state`,
```

with

```markdown
`/issues/<n>` (run history, recent events, captured turns), `/issues/<n>/runs/<run_id>/turns/<t>`
(one turn's transcript, prompt and stderr; Phase 7). API: `GET /api/v1/state`,
```

In `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md` (edit 3 of 4) replace

```markdown

### Later (not scheduled)
```

with

```markdown

Decided 2026-09-04 (Phase 7 spec): the per-issue log viewer moves in from Later; turn logs
live in the database (`run_turns`, captured by the PostgreSQL sink when it drains
`run_ended`: the raw files capped, with a parsed summary), so the web process reads
PostgreSQL only and the logs outlive the workspace; the `Database` facade per request, no
pool; `issuebot web` migrates at start under the same advisory lock as the worker; the live
region polls every 10 s, the charts every 60 s, the worker is `stale` after three of its
poll intervals; `state_counts()` serves `by_state` for the CLI and the API; `POST
/api/v1/refresh` is throttled to one NOTIFY per 5 s and reports Symphony's `coalesced`;
`/healthz` is 503 only when the database does not answer; dependencies `fastapi`, `uvicorn`
and (dev) `httpx2`, vendored htmx 2.0.10 and Chart.js 4.5.1; no `validate` check for
`server.*`; the compose `web` service holds `DATABASE_URL` only; a CSP without
`unsafe-inline` or `unsafe-eval`; the database now holds untrusted text, escaped on render.
Deferred: a connection pool, retention for `run_turns`, a live tail of a running turn,
Markdown rendering of agent output, a reload of `WORKFLOW.md` in the web process.

### Later (not scheduled)
```

In `docs/superpowers/specs/2026-09-02-issuebot-phased-design.md` (edit 4 of 4) replace

```markdown
polling; per-issue log viewer in the dashboard; cost budgets per issue and per day;
```

with

```markdown
polling; cost budgets per issue and per day;
```

In `docs/superpowers/specs/2026-09-04-phase-6-persistence-design.md` (edit 1 of 3) replace

```markdown
Phase 4 amendments below are this phase's own needs.

```

with

```markdown
Phase 4 amendments below are this phase's own needs.

Amended 2026-09-04 (Phase 7): the per-issue log viewer is Phase 7's, backed by a `run_turns`
table (migration `0002_run_turns`) that the sink fills from a run's turn files when it drains
`run_ended`; the web process uses this facade one connection per request and no pool was
added.

```

In `docs/superpowers/specs/2026-09-04-phase-6-persistence-design.md` (edit 2 of 3) replace

````markdown
    def __init__(self, url: str, *, labels: GitHubLabels, connect: Connector = connect) -> None: ...
```

`PostgresStore` holds one `psycopg.AsyncConnection` (autocommit; §7). Every method maps
````

with

````markdown
    def __init__(self, url: str, *, labels: GitHubLabels, connect: Connector = connect) -> None: ...
```

Amended (Phase 7): `apply_event(event, turns: Sequence[TurnCapture] = ())` also inserts the
captured turns into `run_turns` in the `run_ended` transaction (`ON CONFLICT (run_id,
turn_number) DO UPDATE`, so a retried item is idempotent); the sink reads the files once, in a
thread, before the item's first write attempt.

`PostgresStore` holds one `psycopg.AsyncConnection` (autocommit; §7). Every method maps
````

In `docs/superpowers/specs/2026-09-04-phase-6-persistence-design.md` (edit 3 of 3) replace

```markdown
  (Phase 7) escapes what it renders.
```

with

```markdown
  (Phase 7) escapes what it renders. Amended (Phase 7): from Phase 7 the database holds
  untrusted text in `run_turns` (the rendered prompt embeds the issue body; tool results embed
  repository content and command output), stored as bound parameters and escaped on render.
```

- [ ] **Step 2: Check and commit**

Run: `uv run pre-commit run --all-files && timeout 300 uv run pytest -q`
Expected: every hook passes (the ruff markdown hook rewrites nothing); `786 passed, 46 skipped`.

```bash
git status --short && git add --all && git commit -m "docs: describe issuebot.web, agent.turnlog, the run_turns table and the web command" -m "<trailer>"
```

---

### Task 10: Live check against `jleavers/issuebot-scratch`, the compose database and a browser

**Files:** none in this repository. Everything here happens against GitHub, the compose database on port 5440, and directories outside every checkout. This task spends real Claude budget under the operator's subscription login (about $0.90; no `ANTHROPIC_API_KEY` exported) and creates a real issue, branch and pull request; that is intended. Never print `GH_TOKEN`, `SLACK_WEBHOOK_URL` or `DATABASE_URL`. The executor never merges a pull request; Step 6 asks the operator to. The executor cannot see a browser; Step 2 asks the operator to open the dashboard.

Starting state (from the Phase 6 live check): issues #1, #3 and #5 closed `issuebot/complete`; issue #7 (`Add a power function`) in `issuebot/review` with PR #8 open and mergeable; `~/issuebot-workspaces/issuebot-scratch-7` holds #7's workspace and its `.issuebot/runs/20260904T202535Z-0964cd/turn-1.*` files; `~/issuebot-scratch/WORKFLOW.md` has the repo, the workspace root, `stall_timeout_ms: 1800000` and `run_ended` in the Slack allow-list; `~/issuebot-scratch/slack-webhook` (mode 600) exists; `~/issuebot-scratch/worker.log` is the Phase 6 log; the compose database's `public` schema holds the Phase 6 history at schema version 1 (4 issues, 1 run, 11 events, a snapshot written at the Phase 6 worker's shutdown); no worker or web process is running.

- [ ] **Step 1: Rename the old log, bring the database up, migrate, validate**

```bash
mv ~/issuebot-scratch/worker.log ~/issuebot-scratch/worker-phase6.log && cd /home/jleavers/_dev/issuebot && ISSUEBOT_DB_PORT=5440 docker compose up -d db && sleep 6 && docker compose ps db && export GH_TOKEN=$(gh auth token) && export SLACK_WEBHOOK_URL=$(cat ~/issuebot-scratch/slack-webhook) && export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && unset ANTHROPIC_API_KEY && uv run issuebot validate --workflow ~/issuebot-scratch/WORKFLOW.md | grep -E "database.url|checks:" && uv run issuebot migrate --workflow ~/issuebot-scratch/WORKFLOW.md && uv run issuebot validate --workflow ~/issuebot-scratch/WORKFLOW.md | grep -E "database.url|checks:"
```

Expected: the container is `healthy`; `[WARN] database.url: connected (PostgreSQL 18.x); schema version 1 of 2; run issuebot migrate` and `12 checks: 0 failed, 1 warnings` (the Slack line is `[ OK ]` because the webhook is exported); `[ OK ] migration 0002_run_turns: applied` and `[ OK ] database: schema version 2`; then `[ OK ] database.url: connected (PostgreSQL 18.x); schema version 2` and `12 checks: 0 failed, 0 warnings`.

- [ ] **Step 2: Start the web process detached, before any worker, and read the Phase 6 history through it**

```bash
cd /home/jleavers/_dev/issuebot && ( export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot; setsid nohup uv run issuebot --log-format console web --workflow ~/issuebot-scratch/WORKFLOW.md --bind 127.0.0.1 --port 8090 >> ~/issuebot-scratch/web.log 2>&1 < /dev/null & ) && sleep 6 && pgrep -f '^/home/jleavers/_dev/issuebot/\.venv/bin/python.*\.venv/bin/issuebot --log-format console web' > ~/issuebot-scratch/web.pid && cat ~/issuebot-scratch/web.pid && grep -E "db_migrated|web_started|Uvicorn running" ~/issuebot-scratch/web.log | tail -3 && curl -sS http://127.0.0.1:8090/healthz && echo && curl -sS http://127.0.0.1:8090/api/v1/state | uv run python -c "import json,sys; d=json.load(sys.stdin); print({k: d[k] for k in ('generated_at','snapshot_age_s','counts')}, d['worker']['tick_count'], d['worker']['stale'])" && curl -sS "http://127.0.0.1:8090/api/v1/stats?window=30d" | uv run python -c "import json,sys; d=json.load(sys.stdin); print({k: d[k] for k in ('window','closed','runs','by_state')}, len(d['series']))" && curl -sS -o /dev/null -w "index %{http_code}\n" http://127.0.0.1:8090/ && curl -sS http://127.0.0.1:8090/ | grep -o -E 'class="column (todo|in_progress|review|rework|complete)"|href="/issues/[0-9]+"' | sort | uniq -c && curl -sS http://127.0.0.1:8090/issues/7 | grep -o -E "turn logs were not captured|Add a power function|20260904T202535Z-0964cd" | sort -u && curl -sS -o /dev/null -w "api issue 7 %{http_code}\n" http://127.0.0.1:8090/api/v1/issues/7
```

Expected: one pid; the log shows `db_migrated applied=[] version=2`, `web_started bind=127.0.0.1 port=8090 database=postgresql://issuebot@127.0.0.1:5440/issuebot` (no password) and `Uvicorn running on http://127.0.0.1:8090`; `/healthz` reads `{"status":"ok","database":"ok","snapshot_at":"2026-09-04T...","snapshot_age_s":<hours in seconds>,"worker":"stale"}` (the Phase 6 snapshot is old); the state document has the Phase 6 tick count and `stale` `True` with `counts` `{'running': 0, 'retrying': 0}`; the 30-day stats show `closed` 3 (#1, #3, #5), `runs` 1, `by_state` `{'todo': 0, 'in_progress': 0, 'review': 1, 'rework': 0, 'complete': 3}` and a 30-point series; the index is 200 with the five columns and links to `/issues/1`, `/issues/3`, `/issues/5`, `/issues/7`; the issue page for #7 shows its title, the Phase 6 run id and "turn logs were not captured" (Phase 6 had no `run_turns`); the API issue document is 200. Paste the outputs.

Then ask the operator to open `http://127.0.0.1:8090/` in a browser and confirm: the two charts render with bars on 2026-09-03 and 2026-09-04, the Kanban shows #7 under `issuebot/review` and #1, #3, #5 under `issuebot/complete`, the worker line says `worker stale`, and the browser console shows no Content-Security-Policy violation. Record the operator's answer.

- [ ] **Step 3: Start the worker detached; the dashboard sees it within one tick**

```bash
cd /home/jleavers/_dev/issuebot && ( export GH_TOKEN=$(gh auth token); export SLACK_WEBHOOK_URL=$(cat ~/issuebot-scratch/slack-webhook); export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot; unset ANTHROPIC_API_KEY; setsid nohup uv run issuebot --log-format console worker --workflow ~/issuebot-scratch/WORKFLOW.md >> ~/issuebot-scratch/worker.log 2>&1 < /dev/null & ) && sleep 10 && pgrep -f '^/home/jleavers/_dev/issuebot/\.venv/bin/python.*\.venv/bin/issuebot --log-format console worker' > ~/issuebot-scratch/worker.pid && cat ~/issuebot-scratch/worker.pid && grep -E "db_migrated|db_connected|db_listen_started|orchestrator_started" ~/issuebot-scratch/worker.log | tail -4 && curl -sS http://127.0.0.1:8090/healthz && echo && curl -sS http://127.0.0.1:8090/partials/dashboard | grep -o -E 'worker (ok|stale|none)|tick [0-9]+' | head -2
```

Expected: one pid; `db_migrated applied=[] version=2`, `orchestrator_started`, `db_listen_started`, `db_connected`; `/healthz` now reads `"worker":"ok"` with a small `snapshot_age_s`; the partial shows `worker ok` and `tick 1` (or 2).

- [ ] **Step 4: File a trivial issue and watch it reach `review`, the database and the turn page**

Write `~/issuebot-scratch/issue-modulo.md` with the Write tool:

```markdown
Add a `modulo(a: int, b: int) -> int` function to `src/scratch/__init__.py` next to the existing arithmetic functions, returning `a % b`.

## Acceptance criteria

- `modulo(7, 3) == 1` and `modulo(9, 3) == 0`.
- A test in `tests/test_scratch.py` covers both cases.
- `uv run pytest -q` passes.
```

```bash
gh issue create -R jleavers/issuebot-scratch --title "Add a modulo function" --body-file ~/issuebot-scratch/issue-modulo.md --label issuebot/todo
```

Note the number (`N` below). Then wait with a bounded loop under `run_in_background` (up to fifteen minutes):

```bash
timeout 900 bash -c 'until grep -q "to_label=issuebot/review" ~/issuebot-scratch/worker.log; do sleep 10; done'; sleep 15; grep -E "state_changed|dispatched|run_started|run_ended|db_turns_captured|db_turns_capture_failed|db_write_failed|db_write_crashed" ~/issuebot-scratch/worker.log | tail -12
```

Expected for issue N: `state_changed actor=issuebot to_label=issuebot/in-progress`, `dispatched`, `run_started`, ..., `state_changed actor=agent to_label=issuebot/review pr_url=...`, `run_ended outcome=succeeded`, `db_turns_captured run_id=<run> turns=1 stream_bytes=<about 100000>`; no `db_turns_capture_failed`, `db_write_failed` or `db_write_crashed`. Then read it through the dashboard (replace `N`; `RUN` is the run id from `db_turns_captured`):

```bash
curl -sS http://127.0.0.1:8090/api/v1/issues/N | uv run python -c "import json,sys; d=json.load(sys.stdin); r=d['runs'][0]; print(d['issue']['state'], r['run_id'], r['outcome'], r['turns'], [(t['turn_number'], t['model'], t['num_turns'], t['stream_lines'], t['truncated']) for t in r['captured_turns']], d['logs'][0]['url'])" && curl -sS -o /dev/null -w "turn page %{http_code}\n" http://127.0.0.1:8090/issues/N/runs/RUN/turns/1 && curl -sS http://127.0.0.1:8090/issues/N/runs/RUN/turns/1 | grep -o -E 'class="block (init|text|thinking|tool_use|tool_result|result)"|status messages? hidden' | sort | uniq -c && curl -sS -D - -o /dev/null http://127.0.0.1:8090/issues/N/runs/RUN/turns/1/stream | grep -i -E "content-type|content-disposition|x-content-type-options" && curl -sS http://127.0.0.1:8090/issues/N/runs/RUN/turns/1/prompt | head -3 && curl -sS http://127.0.0.1:8090/partials/dashboard | grep -o -E 'class="column review"|href="/issues/N"' | sort | uniq -c
```

Expected: `review <run> succeeded 1 [(1, 'claude-opus-5' or the configured model, <iterations>, <lines>, False)] /issues/N/runs/<run>/turns/1`; the turn page is 200 with init, text, tool_use, tool_result and result blocks and a "status messages hidden" line; the raw stream is `text/plain; charset=utf-8` with `content-disposition: inline; filename="<run>-turn-1.jsonl"` and `x-content-type-options: nosniff`; the prompt starts `You are working on GitHub issue`; the partial has a `review` column and a link to `/issues/N`. Ask the operator to reload the browser: N sits in the `review` column and its turn page renders the transcript. If the run ends `max_turns` instead, the worker applies the blocked escape; record it and note that the run's turns are still captured (`db_turns_captured` fires on every `run_ended`).

- [ ] **Step 5: `POST /api/v1/refresh` is throttled and reaches the worker**

```bash
grep -c db_refresh_received ~/issuebot-scratch/worker.log; curl -sS -X POST http://127.0.0.1:8090/api/v1/refresh && echo && curl -sS -X POST http://127.0.0.1:8090/api/v1/refresh && echo && sleep 3 && grep -c db_refresh_received ~/issuebot-scratch/worker.log && grep -c web_refresh_requested ~/issuebot-scratch/web.log
```

Expected: the first count `C`; `{"queued":true,"coalesced":false,...}` then `{"queued":false,"coalesced":true,...}`; the worker's count is `C + 1` (one NOTIFY for two requests); the web log has one `web_refresh_requested`.

- [ ] **Step 6: Merged pull request → `complete` on the Kanban (operator action)**

Ask the operator to merge PR #8 on GitHub (the executor never merges). Then wait for the next terminal sweep (every tenth tick, five minutes at the 30 s interval):

```bash
timeout 420 bash -c 'until grep -q "issue_finished.*outcome=complete" ~/issuebot-scratch/worker.log; do sleep 10; done'; sleep 15; grep -E "issue_finished|to_label=issuebot/complete|workspace_removed" ~/issuebot-scratch/worker.log | tail -3 && curl -sS http://127.0.0.1:8090/partials/dashboard | grep -o -E 'href="/issues/7"' | wc -l && curl -sS "http://127.0.0.1:8090/api/v1/stats?window=7d" | uv run python -c "import json,sys; d=json.load(sys.stdin); print(d['closed'], d['runs'], d['by_state'])" && export DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot && cd /home/jleavers/_dev/issuebot && uv run issuebot stats --workflow ~/issuebot-scratch/WORKFLOW.md | grep "^issues:" && curl -sS http://127.0.0.1:8090/issues/7 | grep -o -E "20260904T202535Z-0964cd|turn logs were not captured|issuebot/complete" | sort -u
```

Expected: `issue_finished outcome=complete` for issue 7, `state_changed actor=issuebot to_label=issuebot/complete`, `workspace_removed` for `issuebot-scratch-7`; the partial still links to `/issues/7` (now in the `complete` column; ask the operator to confirm in the browser); the 7-day stats show `closed` one higher than in Step 2 and `by_state` with `complete 4`, `review 1` (N); `issuebot stats` prints the same `issues:` numbers; the issue page for #7 still shows the Phase 6 run and "turn logs were not captured" (the workspace is gone, the row stays).

- [ ] **Step 7: Stop both processes and report**

```bash
kill -TERM $(cat ~/issuebot-scratch/worker.pid) && sleep 3 && grep -E "orchestrator_stopped|slack_sink_closed|db_sink_closed|db_listen_closed" ~/issuebot-scratch/worker.log | tail -4 && kill -TERM $(cat ~/issuebot-scratch/web.pid) && sleep 3 && tail -2 ~/issuebot-scratch/web.log && (pgrep -f '\.venv/bin/issuebot --log-format console' || echo "nothing left running")
```

Expected: `orchestrator_stopped`, `slack_sink_closed`, `db_sink_closed written=W failed=0 dropped=0`, `db_listen_closed notified=1 ...`; the web log ends with `Application shutdown complete` and `Finished server process`; `nothing left running`.

Paste the relevant log lines, CLI and curl outputs and the operator's browser confirmations from Steps 1 to 6 into the report for the PR body. Do not merge N's pull request. Leave `~/issuebot-scratch/slack-webhook` and the compose database to the operator.

---

### Task 11: Push the branch, whole-branch review, fix wave, and the pull request

**Files:** none (plus whatever the review's fix wave touches).

- [ ] **Step 1: Final full check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files && timeout 300 uv run pytest -q && DATABASE_URL=postgresql://issuebot:issuebot@127.0.0.1:5440/issuebot timeout 300 uv run pytest -q && docker compose build`
Expected: everything passes; `786 passed, 46 skipped` then `832 passed`; the image builds and `docker run --rm --entrypoint python issuebot-worker -c "from importlib.resources import files; r = files('issuebot.web'); print(sorted(p.name for p in (r / 'static' / 'vendor').iterdir()), sorted(p.name for p in files('issuebot.db').joinpath('migrations').iterdir()))"` lists the five vendored files and both migrations (the image tag is whatever `docker compose build` names the worker image; `docker compose images` shows it); `docker run --rm issuebot-worker web --help` prints the `--port`/`--bind` options; `git status --short` is empty; `git diff main -- pyproject.toml` shows only the three dependency lines and the `filterwarnings` entry.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin phase-7-dashboard`

- [ ] **Step 3: Whole-branch review (fable) and the fix wave**

Dispatch one reviewer subagent on model `fable` with the spec, this plan's Global Constraints, and `git diff main...phase-7-dashboard`, asking for a whole-branch review against the spec: the capture caps and their tests, the `run_turns` write in the `run_ended` transaction and its idempotence, the sink's one-capture-per-item rule, the five queries, escaping in every template (no `|safe`, every data `href` through the filter), the CSP and headers, the raw-text routes, the refresh throttle, the `healthz` semantics, the CLI seam and the uvicorn signal handling, secret hygiene (no URL in any log line, page or envelope), the test counts, and the documentation. Classify findings Critical / Important / Minor. Fix every Critical and Important finding in one wave (TDD: a failing test first where the finding is testable), re-run Step 1, commit as `fix: <what the review found>` with the trailer, push. Record the Minors as parked follow-ups for the report. Repeat once if the fix wave was non-trivial.

- [ ] **Step 4: Write the PR body to a file under the session scratchpad directory (a separate call from Step 5; use the Write tool)**

`<scratchpad>/issuebot-phase-7-pr.md`:

```markdown
## Phase 7: Web dashboard and the per-issue log viewer

Implements `docs/superpowers/specs/2026-09-04-phase-7-dashboard-design.md`.

- `issuebot.web`: a FastAPI app over the Phase 6 database (one connection per request through the `Database` facade, no pool): the dashboard (`/`: worker status, hero stats, two 30-day Chart.js charts, running and retry panels, the five-column Kanban; the live region refreshed by htmx every 10 s through `/partials/dashboard`), a page per issue (runs with their captured turns, events) and a page per turn (the transcript parsed from the stored stream-json, the prompt, stderr, raw `text/plain` files); the JSON API in Symphony §13.7.2 shapes (`/api/v1/state`, `/api/v1/issues/<n>`, `/api/v1/stats?window=<N>d`, `POST /api/v1/refresh` as a `NOTIFY` throttled to one per 5 s) and `/healthz` (503 only when the database does not answer; the worker reported `ok`, `stale` or `none`)
- Turn logs outlive the workspace: `agent.turnlog` reads a run's `turn-N.jsonl`, `.prompt.md` and `.stderr.log` with caps (prompt 256 KiB, lines over 64 KiB stubbed, 2 MiB of head lines plus the result line, stderr 64 KiB tail) and a parsed summary; the PostgreSQL sink captures them once per `run_ended` item, in a thread, and the store inserts `run_turns` rows (migration `0002_run_turns`) in the run's transaction
- Queries: `issue`, `events_for_issue`, `turn_summaries_for_issue`, `turn`, `state_counts` (the CLI's `stats` and the API agree past the Kanban cap); `issues_by_state` skips unknown roles; `MAX_WINDOW_DAYS = 365` bounds `--days` and the API window
- Security: Jinja2 autoescape with `StrictUndefined`, `safe_href` on every data URL, a CSP without `unsafe-inline` or `unsafe-eval` (all script and style in files; htmx with `allowEval` off), `nosniff` and `text/plain` for raw files, typed and pattern-checked path parameters, the database URL never shown; no authentication (compose publishes the web service on loopback)
- CLI `issuebot web [--port] [--bind]` (migrates at start, uvicorn through structlog, exits 0 on SIGTERM); a real compose `web` service with `DATABASE_URL` only and a `/healthz` check; dependencies `fastapi`, `uvicorn`, dev `httpx2`; vendored htmx 2.0.10 (0BSD) and Chart.js 4.5.1 (MIT) with licences and checksums
- Live check against `jleavers/issuebot-scratch` with the compose database: the dashboard rendered the Phase 6 history before the worker started, a `todo` issue ran to `review` with its turn captured and its transcript rendered, `POST /api/v1/refresh` coalesced and reached the worker, merging PR #8 moved #7 to the `complete` column, and both processes stopped cleanly (output below)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Append the live-check output from Task 10 under a `## Live check` heading before the generated-with line, and the session link the executing harness requires after it.

- [ ] **Step 5: Open the PR via the REST API (the CLI's `pr create` is blocked in this repo)**

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='Phase 7: Web dashboard and the per-issue log viewer' \
  -f head='phase-7-dashboard' -f base='main' \
  -F body=@<scratchpad>/issuebot-phase-7-pr.md
```

Then confirm with `gh pr view --json title,body --jq '.title'` and watch CI with `gh pr checks --watch`. CI must be green (lint, tests with the service container and no skips, the Docker build) before handing over for human review. Do not merge.

---

## Acceptance criteria

Spec §12, restated for the executor:

- `uv run pytest -q` passes with no network and no database (`786 passed, 46 skipped` after Task 8) and `DATABASE_URL=... uv run pytest -q` passes with the compose database (`832 passed`, no skips); ruff and pre-commit clean; CI green; `docker compose build` succeeds and the image ships the templates, the vendored files and both migrations; `pyproject.toml` and `uv.lock` carry `fastapi`, `uvicorn` and dev `httpx2` and nothing else new.
- `uv run issuebot validate` on the committed `WORKFLOW.md` still prints twelve checks; with `DATABASE_URL` pointing at the compose database the line warns `schema version 1 of 2` before `issuebot migrate` and reads `schema version 2` after it.
- The live check (Task 10) shows: `migrate` applied `0002_run_turns`; the web process rendered the Phase 6 history before any worker ran (`worker: stale`, the Kanban, the charts in a browser with no CSP violation, "turn logs were not captured" for the Phase 6 run); the worker's first tick turned `/healthz` to `worker: ok`; a `todo` issue reached `review` with `db_turns_captured turns=1`, its turn page rendered the transcript and its raw files were served as text; two POSTs to `/api/v1/refresh` produced one `db_refresh_received`; merging PR #8 moved #7 to `complete` on the Kanban with `stats` and the API agreeing; both processes exited cleanly on SIGTERM.
- `CLAUDE.md`, `README.md`, `compose.yaml`, the dot-env example, the roadmap and the Phase 6 spec carry the Task 8 and Task 9 edits.
