# One credential route for the container, and the pool as its default — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the account pool the worker image's default, expressed once as the account list
the build itself writes, and remove the interactive Claude login from the container route so a
session that runs as another account always takes its credential from the environment.

**Architecture:** Three seams change. `resolve.py` gains a third fallback for `agent.run_as` —
a file the Dockerfile's `useradd` loop writes — so the image's default is definitionally the
accounts it built. `accounts.credential_complaint` widens from "a pool" to "any
`agent.run_as`", making the environment credential the container's rule rather than a special
case. The `claude-home` volume and its login recipe go. The host route (`agent.run_as` unset)
is untouched in code and tests, and loses only its place in the README.

**Tech Stack:** Python 3.14, `uv`, pytest, pydantic, Docker Compose, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-15-session-credential-standard-design.md`

## Global Constraints

- Branch: `issuebot/142-session-credential-standard`, already created, with the spec committed
  on it. **Never push to `main`**; open a PR for human review; never merge or close it.
- Never run `rm -rf`, `git reset --hard` or `git clean -fd`.
- Linux host: bash, `&&`, `.sh`. Never emit PowerShell or `.ps1`.
- `uv run pytest` must stay hermetic: no network, no Docker, and it must pass **inside the
  worker image as well as on a host**. That is the whole of #140, and Tasks 1 and 2 are what
  keep it true.
- Every commit runs pre-commit (`trailing-whitespace`, `end-of-file-fixer`, `ruff`,
  `ruff format`). Before the PR: `uv run ruff check . && uv run ruff format --check .` and
  `uv run pytest`.
- A PreToolUse hook rejects any Bash command whose text contains the literal example
  environment filename (`.` + `env` + `.example`). Stage with `git add --all` after checking
  `git status --short`; never name that file in a shell command. Edit it with the Write/Edit
  tools.
- PR title and body go through the REST API, never `gh pr create`/`gh pr edit`: write the body
  to a temp `.md` file in a **separate** Bash call, then
  `gh api repos/jleavers/issuebot/pulls -X POST -f title='...' -f head='...' -f base='main' -F body=@file.md`.
  Capital `-F` for the body.
- Commit messages end with the two attribution lines this session is configured with
  (`Co-Authored-By:` and `Claude-Session:`), using the executing session's own URL.
- Exact values that must not drift: the accounts file is `/etc/issuebot/session-accounts`, one
  account per line; the build arg stays `ARG ISSUEBOT_AGENT_POOL_SIZE=3`; the credential names
  stay `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` in that order.

---

## File Structure

| File | Responsibility after this change |
|---|---|
| `src/issuebot/config/resolve.py` | Adds `SESSION_ACCOUNTS_FILE` and `built_session_accounts()`; `agent.run_as` resolves front matter → `ISSUEBOT_AGENT_USER` → the built list → host route |
| `src/issuebot/agent/accounts.py` | `credential_complaint` becomes the rule for any `agent.run_as`, not only a pool |
| `src/issuebot/agent/runner.py` | The logged-out detail names the container route first and the host route second |
| `src/issuebot/cli.py` | `validate` reports the built pool and warns when a runtime pool size disagrees with it |
| `Dockerfile` | The `useradd` loop writes the account list; no `ENV ISSUEBOT_AGENT_USER`; no `/home/agent/.claude` in `VOLUME` |
| `compose.yaml` | No `claude-home` volume and no mount of it |
| `tests/conftest.py` | Neutralises the built account list for the suite, as it already neutralises `ISSUEBOT_AGENT_USER` |
| `tests/test_cli.py` | Account names in `validate` output are stubbed, not read off the machine (#140) |
| `README.md`, `.env.example`, `CLAUDE.md` | One documented way to run the app; the host route lives under Development |

---

### Task 1: the suite stops reading account uids off the machine (#140)

`validate` prints `agent-1 (uid 1011)` inside the worker image and `agent-1` on a host, because
`_with_uid` looks each account up in `/etc/passwd`. Tests that assert the line therefore pass
or fail according to the machine. Fix it first: every later task adds assertions to this same
output.

**Files:**
- Modify: `tests/test_cli.py:3283-3304` (and the two neighbouring pool tests)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `issuebot.cli._with_uid(account: str) -> str` (exists, unchanged)
- Produces: pytest fixture `bare_account_names` in `tests/test_cli.py` — monkeypatches
  `issuebot.cli._with_uid` to the identity, so a test asserting `validate`'s `agent.run_as`
  line gets the same text on every machine. Tasks 5 and 7 use it.

- [ ] **Step 1: Write the failing tests**

Add near the other `agent.run_as` tests in `tests/test_cli.py`:

```python
@pytest.fixture
def bare_account_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """`validate` names an account with its uid where the account resolves, so the line it
    prints depends on the machine: `agent-1` is uid 1011 inside the worker image and resolves
    to nothing on a developer's host (#140). A test about the *line* pins the lookup instead
    of the host; `_with_uid` itself is tested directly below.
    """
    monkeypatch.setattr("issuebot.cli._with_uid", lambda account: account)


def test_with_uid_names_an_account_that_resolves() -> None:
    me = pwd.getpwuid(os.getuid()).pw_name
    assert _with_uid(me) == f"{me} (uid {os.getuid()})"


def test_with_uid_falls_back_to_the_bare_name() -> None:
    assert _with_uid("no-such-account-142") == "no-such-account-142"
```

Add `import pwd` and `from issuebot.cli import _with_uid` to the test module's imports if they
are not already there (`os` is imported).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "with_uid" -v`
Expected: FAIL — `ImportError` or `NameError` on `_with_uid` until the import is added; then
both pass. If they pass at once, the import was already present and only the fixture is new.

- [ ] **Step 3: Use the fixture in the three tests that assert the line**

Add `bare_account_names: None` to the parameters of
`test_validate_reports_a_pool_of_session_accounts`,
`test_validate_fails_a_pool_with_no_credential_in_the_environment` and the single-account test
at `tests/test_cli.py:3208`. Then delete the parenthetical in the pool test that explains the
host-dependence, since it is no longer true:

```python
    # The pool line carries #111's evidence too: the probe compared every member's uid with
    # this process's, so the line says so rather than leaving the reader to take it on trust.
    assert f"each at a uid other than this process's ({os.getuid()})" in out
```

- [ ] **Step 4: Run the whole CLI suite**

Run: `uv run pytest tests/test_cli.py -q`
Expected: PASS. Sanity-check the fix does what #140 asks by forcing the other spelling:
`uv run pytest tests/test_cli.py -k pool -q` must still pass with
`monkeypatch.setattr("issuebot.cli._with_uid", lambda a: f"{a} (uid 1011)")` substituted in the
fixture by hand — then put the identity back.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cli.py
git commit -m "test: pin validate's account names instead of the host's passwd (#140)"
```

---

### Task 2: `agent.run_as` falls back to the accounts the image built

**Files:**
- Modify: `src/issuebot/config/resolve.py:25-27` (constants), `:107-119` (the `run_as` block)
- Modify: `tests/conftest.py:20-40`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Produces: `issuebot.config.resolve.SESSION_ACCOUNTS_FILE: Path` and
  `built_session_accounts() -> list[str] | None`. Task 5 imports the second one into `cli.py`.
  `None` means "no such file", which is the host route; a file of only blank lines is also
  `None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_resolve.py`:

```python
def test_run_as_falls_back_to_the_accounts_the_image_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = tmp_path / "session-accounts"
    listing.write_text("agent-1\nagent-2\nagent-3\n", encoding="utf-8")
    monkeypatch.setattr("issuebot.config.resolve.SESSION_ACCOUNTS_FILE", listing)
    out = resolve_config({"github": {"repo": "o/r"}}, environ={}, base_dir=BASE)
    assert out["agent"]["run_as"] == ["agent-1", "agent-2", "agent-3"]


def test_the_environment_variable_wins_over_the_built_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = tmp_path / "session-accounts"
    listing.write_text("agent-1\nagent-2\n", encoding="utf-8")
    monkeypatch.setattr("issuebot.config.resolve.SESSION_ACCOUNTS_FILE", listing)
    out = resolve_config(
        {"github": {"repo": "o/r"}}, environ={"ISSUEBOT_AGENT_USER": "agent"}, base_dir=BASE
    )
    assert out["agent"]["run_as"] == "agent"


def test_an_explicit_run_as_wins_over_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    listing = tmp_path / "session-accounts"
    listing.write_text("agent-1\n", encoding="utf-8")
    monkeypatch.setattr("issuebot.config.resolve.SESSION_ACCOUNTS_FILE", listing)
    out = resolve_config(
        {"github": {"repo": "o/r"}, "agent": {"run_as": ["chosen"]}},
        environ={"ISSUEBOT_AGENT_USER": "agent"},
        base_dir=BASE,
    )
    assert out["agent"]["run_as"] == ["chosen"]


def test_no_built_accounts_is_the_host_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "issuebot.config.resolve.SESSION_ACCOUNTS_FILE", tmp_path / "does-not-exist"
    )
    out = resolve_config({"github": {"repo": "o/r"}}, environ={}, base_dir=BASE)
    assert "agent" not in out


def test_a_blank_built_account_list_is_the_host_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = tmp_path / "session-accounts"
    listing.write_text("\n  \n", encoding="utf-8")
    monkeypatch.setattr("issuebot.config.resolve.SESSION_ACCOUNTS_FILE", listing)
    out = resolve_config({"github": {"repo": "o/r"}}, environ={}, base_dir=BASE)
    assert "agent" not in out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_resolve.py -k built -v`
Expected: FAIL with `AttributeError: <module 'issuebot.config.resolve'> does not have the
attribute 'SESSION_ACCOUNTS_FILE'`.

- [ ] **Step 3: Implement**

In `src/issuebot/config/resolve.py`, beside the other `AGENT_RUN_AS_*` constants:

```python
AGENT_RUN_AS_FIELD: tuple[str, ...] = ("agent", "run_as")
AGENT_RUN_AS_FALLBACK = "ISSUEBOT_AGENT_USER"
# The accounts the worker image built, written by the same ``useradd`` loop that creates them
# (#142). Below the variable and above the host route, so the image's default is the accounts
# it actually has and a pool raised at build time cannot disagree with the names the worker
# resolves at run time.
SESSION_ACCOUNTS_FILE = Path("/etc/issuebot/session-accounts")
```

and a reader beside `resolve_env_value`:

```python
def built_session_accounts() -> list[str] | None:
    """The session accounts this image was built with, or ``None`` outside one.

    The built fact rather than a number to re-derive from: compose passes the operator's own
    ``ISSUEBOT_AGENT_POOL_SIZE`` into the container through ``env_file``, so a runtime copy of
    the size describes the file they just edited and not the image they are running (#142).
    Read at call time, never at import, so a test can point the constant at a file of its own.
    """
    try:
        text = SESSION_ACCOUNTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    accounts = [line.strip() for line in text.splitlines() if line.strip()]
    return accounts or None
```

Then replace the `run_as` block (currently one `_set(...)` call wrapping `resolve_env_value`):

```python
    # The image writes its account list; the variable and the front matter both win over it,
    # and a host has neither (#75, #142). An explicit ``agent.run_as`` in WORKFLOW.md wins over
    # everything, as for the root.
    run_as = resolve_env_value(
        _get(config, AGENT_RUN_AS_FIELD),
        field=".".join(AGENT_RUN_AS_FIELD),
        fallback=AGENT_RUN_AS_FALLBACK,
        environ=environ,
    )
    if run_as is None:
        run_as = built_session_accounts()
    _set(config, AGENT_RUN_AS_FIELD, run_as)
```

- [ ] **Step 4: Make the suite blind to the host's list**

`tests/conftest.py` already strips `ISSUEBOT_AGENT_USER` so a suite run inside the image does
not resolve a real account. The built list is the same hazard, one layer down. Extend the
comment at `tests/conftest.py:25-31` to name both, and add an autouse fixture beside
`clean_env`:

```python
@pytest.fixture(autouse=True)
def unbuilt_session_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker image writes ``/etc/issuebot/session-accounts`` (#142) and the suite runs
    inside that image as well as on a host, so a test that says nothing about accounts must
    not pick the image's up: `agent.run_as` falls back to the host route unless a test points
    the constant at a list of its own.
    """
    monkeypatch.setattr(
        "issuebot.config.resolve.SESSION_ACCOUNTS_FILE",
        Path("/nonexistent/issuebot/session-accounts"),
    )
```

Add `from pathlib import Path` to `tests/conftest.py` if it is not imported already.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, all of it. A failure in `tests/test_workflow*.py` or `tests/test_cli.py` here
means the autouse fixture is not taking effect before `load_workflow` runs.

- [ ] **Step 6: Commit**

```bash
git add src/issuebot/config/resolve.py tests/test_resolve.py tests/conftest.py
git commit -m "feat: agent.run_as falls back to the accounts the image built (#142)"
```

---

### Task 3: the image records the accounts it built, and names none in the environment

**Files:**
- Modify: `Dockerfile:191-214` (the account loop), `:251-255` (the `ENV` block)
- Test: `tests/test_image_layout.py:22-35`

**Interfaces:**
- Consumes: `built_session_accounts()` from Task 2 — this task writes the file it reads.
- Produces: `/etc/issuebot/session-accounts` in the image, mode `0444`, listing `agent-1` …
  `agent-N`, or `agent` alone when the pool size is below 1.

- [ ] **Step 1: Write the failing tests**

In `tests/test_image_layout.py`, replace the `ISSUEBOT_AGENT_USER` assertion in
`test_two_accounts_and_one_delegation` (line 28) with a new test beside it:

```python
def test_the_image_records_the_accounts_it_built_and_names_none_in_the_environment() -> None:
    """#142: the pool is the default, expressed once. The loop that creates the accounts
    writes them, so `agent.run_as` cannot resolve a name the build did not create; and no
    `ENV ISSUEBOT_AGENT_USER` is left to shadow that list with a single account.
    """
    assert "install -d -m 0755 /etc/issuebot" in DOCKERFILE
    assert "sed 's/^/agent-/' > /etc/issuebot/session-accounts" in DOCKERFILE
    assert "echo agent > /etc/issuebot/session-accounts" in DOCKERFILE
    assert "chmod 0444 /etc/issuebot/session-accounts" in DOCKERFILE
    assert "ISSUEBOT_AGENT_USER=" not in DOCKERFILE
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_image_layout.py -k records_the_accounts -v`
Expected: FAIL on the first assertion (`install -d -m 0755 /etc/issuebot` not in DOCKERFILE).

- [ ] **Step 3: Implement**

In the `RUN` at `Dockerfile:192`, after the `for account in ${accounts}; do ... done;` loop and
before `chmod 0750 /home/issuebot /home/web;`, add:

```dockerfile
    install -d -m 0755 /etc/issuebot; \
    if [ "${ISSUEBOT_AGENT_POOL_SIZE}" -ge 1 ]; then \
      seq 1 "${ISSUEBOT_AGENT_POOL_SIZE}" | sed 's/^/agent-/' > /etc/issuebot/session-accounts; \
    else \
      echo agent > /etc/issuebot/session-accounts; \
    fi; \
    chmod 0444 /etc/issuebot/session-accounts; \
```

Then remove `ISSUEBOT_AGENT_USER=agent \` from the `ENV` at `Dockerfile:253-255`, leaving
`LANG` and `PATH`, and replace the comment above it (`Dockerfile:251-252`) with:

```dockerfile
# No ISSUEBOT_AGENT_USER: `agent.run_as` falls back to /etc/issuebot/session-accounts, written
# by the account loop above, so the image's default is the pool it built rather than a name
# that could outlive the accounts (#142). The variable still overrides it for an operator who
# wants one account, and WORKFLOW.md overrides both.
```

Also update the comment at `Dockerfile:157-159`, which says the image "also builds" the pool
beside `agent`, to say the pool **is** the default and `agent` is the single-account route.

- [ ] **Step 4: Run the image-layout tests**

Run: `uv run pytest tests/test_image_layout.py -q`
Expected: PASS.

- [ ] **Step 5: Build the image and read the file back**

```bash
docker compose build worker
docker run --rm --entrypoint sh issuebot-worker:latest -c 'cat /etc/issuebot/session-accounts; stat -c "%a %U" /etc/issuebot/session-accounts'
```

Expected: the accounts one per line (`agent-1` … `agent-N` for the configured pool size) and
`444 root`. This is the one step in the plan that needs Docker; if it is unavailable, say so
in the PR rather than skipping it silently.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile tests/test_image_layout.py
git commit -m "feat: the image records the session accounts it built (#142)"
```

---

### Task 4: a session account's credential comes from the environment

**Files:**
- Modify: `src/issuebot/agent/accounts.py:86-104`
- Test: `tests/test_agent_accounts.py`

**Interfaces:**
- Consumes: `Settings.agent.run_as: tuple[str, ...]`, `ENV_CREDENTIAL_NAMES`
- Produces: `credential_complaint(settings, environ) -> str | None` — unchanged signature,
  widened rule. Task 5's `validate` check and the orchestrator's startup both already call it.

- [ ] **Step 1: Write the failing test**

In `tests/test_agent_accounts.py`, beside the existing pool credential tests:

```python
def test_a_single_session_account_also_needs_an_environment_credential() -> None:
    """#142: the container has no interactive login, so one account is the same rule as N."""
    complaint = credential_complaint(settings(run_as="agent"), environ={})
    assert complaint is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY" in complaint


def test_a_single_session_account_with_a_credential_is_silent() -> None:
    environ = {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
    assert credential_complaint(settings(run_as="agent"), environ) is None


def test_the_host_route_needs_no_environment_credential() -> None:
    """`run_as` unset is the operator's own account, which has its own login (#142)."""
    assert credential_complaint(settings(), environ={}) is None
```

`settings(**agent)` is the module's own helper at `tests/test_agent_accounts.py:37` -- it
validates `{"github": {"repo": "o/r"}, "agent": agent}`. The host route is `settings()` with
no `run_as` at all: `settings(run_as=[])` raises, because the field validator refuses an empty
list.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_agent_accounts.py -k single_session_account -v`
Expected: FAIL — `credential_complaint` returns `None` for one account, so
`assert complaint is not None` fails.

- [ ] **Step 3: Implement**

Replace the guard and the message in `credential_complaint`:

```python
def credential_complaint(settings: Settings, environ: Mapping[str, str]) -> str | None:
    """Why a session account could not authenticate under this environment, or ``None``.

    A session runs as an account nobody logs into: the container has had no interactive login
    since #142, and a pool never had one, since N accounts are N homes and sharing one OAuth
    login between them is a refresh race nobody has established is safe. So the credential is
    the one with no file to share -- one the deployment puts in the environment, which
    `agent_environment` already passes through to every account. The host route (`run_as`
    unset) is the operator's own account, with its own login, and is unaffected.
    """
    if not settings.agent.run_as:
        return None
    if any(environ.get(name) for name in ENV_CREDENTIAL_NAMES):
        return None
    return (
        "a session account has a home nobody logs into, so its credential comes from the "
        f"environment: set {' or '.join(ENV_CREDENTIAL_NAMES)}"
    )
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_agent_accounts.py tests/test_cli.py tests/test_orchestrator.py -q`
Expected: PASS. Any startup or `validate` test that set one account and no credential now gets
a refusal; those tests are asserting the old rule, so give them
`monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "...")` — the deployment shape the spec
standardises on — rather than weakening the rule.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/agent/accounts.py tests/test_agent_accounts.py tests/test_cli.py tests/test_orchestrator.py
git commit -m "feat: any session account takes its credential from the environment (#142)"
```

---

### Task 5: `validate` reports the built pool and catches a stale image

The failure this whole change exists to prevent: `ISSUEBOT_AGENT_POOL_SIZE=5` in the operator's
environment file, an image built with 3, and a `[FAIL]` that names neither the image nor the
rebuild.

**Files:**
- Modify: `src/issuebot/cli.py:62` (import), `:558-618` (`_run_as_check` and a new helper)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `built_session_accounts()` from Task 2; `bare_account_names` fixture from Task 1
- Produces: `_built_pool_complaint() -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
def test_validate_warns_when_the_pool_size_disagrees_with_the_image(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    bare_account_names: None,
    tmp_path: Path,
) -> None:
    """The failure #142 was filed for: the variable says five, the image built three."""
    listing = tmp_path / "session-accounts"
    listing.write_text("agent-1\nagent-2\nagent-3\n", encoding="utf-8")
    monkeypatch.setattr("issuebot.config.resolve.SESSION_ACCOUNTS_FILE", listing)
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    monkeypatch.setenv("ISSUEBOT_AGENT_USER", "agent-1,agent-2,agent-3")
    monkeypatch.setenv("ISSUEBOT_AGENT_POOL_SIZE", "5")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "pool-credential")
    monkeypatch.setattr("issuebot.cli._run_as_probe", lambda accounts, environ: [])
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] agent.run_as:" in out
    assert "ISSUEBOT_AGENT_POOL_SIZE=5 but this image was built with 3 session accounts" in out
    assert "docker compose build worker" in out


def test_validate_says_nothing_about_a_built_pool_on_a_host(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    bare_account_names: None,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    monkeypatch.setenv("ISSUEBOT_AGENT_USER", "agent-1,agent-2,agent-3")
    monkeypatch.setenv("ISSUEBOT_AGENT_POOL_SIZE", "5")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "pool-credential")
    monkeypatch.setattr("issuebot.cli._run_as_probe", lambda accounts, environ: [])
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "docker compose build worker" not in out
```

The second test relies on the autouse `unbuilt_session_accounts` fixture from Task 2: there is
no built list on a host, so a mismatched variable says nothing.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "disagrees_with_the_image or built_pool_on_a_host" -v`
Expected: the first FAILs (no warning text in the output); the second passes already.

- [ ] **Step 3: Implement**

Extend the import at `src/issuebot/cli.py:62`:

```python
from issuebot.config.resolve import ENV_REF, built_session_accounts
```

Add beside `_with_uid`:

```python
def _built_pool_complaint() -> str | None:
    """Why the accounts configured here and the ones this image built disagree, or ``None``.

    The mismatch #142 was filed for: `ISSUEBOT_AGENT_POOL_SIZE` is a build argument, compose
    passes the operator's copy of it into the container through `env_file`, and a value raised
    without a rebuild names accounts no image has. The list wins the comparison because it is
    the built fact; the variable is only ever the operator's intent.
    """
    built = built_session_accounts()
    if built is None:
        return None
    size = os.environ.get("ISSUEBOT_AGENT_POOL_SIZE", "").strip()
    if not size.isdigit() or int(size) == len(built):
        return None
    return (
        f"ISSUEBOT_AGENT_POOL_SIZE={size} but this image was built with "
        f"{_plural(len(built), 'session account')} ({', '.join(built)}): "
        "docker compose build worker"
    )
```

`_plural` already exists in `cli.py` (used by `_mcp_config_check`); confirm its signature reads
`_plural(count, noun)` and produces `3 session accounts`, and inline the f-string if it does
not.

Then rewrite the tail of `_run_as_check` so both branches flow through one exit:

```python
    named = ", ".join(_with_uid(account) for account in accounts)
    if len(accounts) == 1:
        shared = concurrent > 1
        detail = (
            f"{named}; the session runs as a separate account, at a uid other than this "
            f"process's ({os.getuid()})"
        )
        if shared:
            detail += f", but all {concurrent} concurrent sessions share it"
        check = Check(subject, "warn" if shared else "ok", detail)
    else:
        status: CheckStatus = "warn" if len(accounts) < concurrent else "ok"
        detail = f"{named}; a pool of {len(accounts)}, one account per concurrent session"
        if status == "warn":
            detail += (
                f", which is fewer than agent.max_concurrent_agents "
                f"({concurrent}): dispatch is capped by the pool"
            )
        # Last, and behind its own semicolon: the clause above qualifies the pool's *size*, and
        # a uid phrase between the two would read as qualifying that instead.
        detail += f"; each at a uid other than this process's ({os.getuid()})"
        check = Check(subject, status, detail)
    # A stale image is never a failure: the accounts named here all exist, so every session
    # will run; what is wrong is that the operator asked for more of them than the build made.
    stale = _built_pool_complaint()
    if stale is None:
        return check
    return Check(subject, "warn", f"{check.detail}; {stale}")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli.py -q`
Expected: PASS. The warning count in tests that assert `"17 checks: 0 failed, N warnings"` may
move by one for the new test only; do not change the counts in tests that set no pool size.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/cli.py tests/test_cli.py
git commit -m "feat: validate names the pool the image built and catches a stale one (#142)"
```

---

### Task 6: the logged-out line names the container route first

**Files:**
- Modify: `src/issuebot/agent/runner.py:266`
- Test: `tests/test_agent_runner.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `describe_claude_auth(None-or-logged-out) -> ClaudeAuth("logged_out", <new text>)`

- [ ] **Step 1: Write the failing test**

```python
def test_a_logged_out_probe_names_the_container_route_first() -> None:
    """The container has no interactive login since #142, so the line that tells an operator
    what to do names the variable; `claude auth login` is the host route and says so."""
    auth = describe_claude_auth('{"loggedIn": false}')
    assert auth.verdict == "logged_out"
    assert auth.detail == (
        "not logged in; set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), "
        "or run claude auth login on the host"
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_agent_runner.py -k logged_out_probe_names -v`
Expected: FAIL — the detail is still
`not logged in; run claude auth login or set ANTHROPIC_API_KEY`.

- [ ] **Step 3: Implement**

In `describe_claude_auth`:

```python
    if not status.get("loggedIn"):
        detail = (
            "not logged in; set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), "
            "or run claude auth login on the host"
        )
        return ClaudeAuth("logged_out", detail)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_agent_runner.py tests/test_cli.py tests/test_orchestrator.py -q`
Expected: PASS. Other tests assert this string — `tests/test_cli.py` around the `claude auth`
check and `tests/test_orchestrator.py` around the startup failure. Update them to the new text.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/agent/runner.py tests/test_agent_runner.py tests/test_cli.py tests/test_orchestrator.py
git commit -m "feat: the logged-out line names the container's credential first (#142)"
```

---

### Task 7: the login volume goes

**Files:**
- Modify: `compose.yaml:124-128` (the mount), `:174-178` (the volume declaration)
- Modify: `Dockerfile:285` (`VOLUME`), `:142`, `:170` (comments), `:249-250` (the `--user agent`
  comment, which names the login recipe)
- Test: `tests/test_image_layout.py:66-70`, `:160-165`

**Interfaces:**
- Consumes: nothing; this is the removal the previous tasks make safe
- Produces: an image and a compose file with no `claude-home`

- [ ] **Step 1: Write the failing test**

Replace `test_the_login_volume_is_the_sessions_home` in `tests/test_image_layout.py` with:

```python
def test_no_login_volume_is_mounted_anywhere() -> None:
    """#142: a session account's home holds no login, because nobody logs into it -- the
    credential is in the environment, where every account reads the same one. A volume at that
    path would be a second, stale credential route for the default deployment to disagree with.
    """
    assert "claude-home" not in COMPOSE
    assert "/home/agent/.claude" not in COMPOSE
    assert 'VOLUME ["/workspaces"]' in DOCKERFILE
    assert "/home/agent/.claude" not in DOCKERFILE
```

Note the last assertion is about the Dockerfile's `VOLUME`/mount paths; the CI sweep proof
lives in `ci.yml` and still uses `/home/agent/.claude`, which is the image's own directory and
must not be touched.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_image_layout.py -k no_login_volume -v`
Expected: FAIL on `assert "claude-home" not in COMPOSE`.

- [ ] **Step 3: Implement**

In `compose.yaml`, delete the mount and its three-line comment:

```yaml
      # The session's home, not the worker's (#75): claude authenticates itself, so the
      # login is the account the session runs as. Upgrading across this move needs the
      # volume's files re-owned once (README, "Upgrades").
      - claude-home:/home/agent/.claude
```

and the `claude-home:` entry under `volumes:`. In the `Dockerfile`, change line 285 to

```dockerfile
VOLUME ["/workspaces"]
```

and update the two comments that describe the mount (`:142`, `:170`) to say the session
account's home holds no login and the credential is in the environment. Update the `--user
agent` remark at `:249-250`, which cites "the login recipe in the README", to cite the sweep
proof in CI instead — that is what still runs a command as `agent`.

- [ ] **Step 4: Run the tests and a compose parse**

```bash
uv run pytest tests/test_image_layout.py tests/test_compose_credentials.py -q
COMPOSE_PROFILES=hub,worker docker compose config --quiet && echo "compose ok"
```

Expected: PASS, then `compose ok`. The second command is what CI's `docker` job runs under each
profile; a dangling `claude-home` reference fails it.

- [ ] **Step 5: Commit**

```bash
git add compose.yaml Dockerfile tests/test_image_layout.py
git commit -m "feat: remove the login volume from the container route (#142)"
```

---

### Task 8: the README becomes the setup guide

**Files:**
- Modify: `README.md` — `### Prerequisites` (the host entries), `### Step 2` (the two
  `# on the host:` parentheticals at `:241-242`), `### Step 3` (the whole "To run on the host
  instead" block and the `workspace.root` note, `:421-439`), the Claude credential section
  (`:308-340`), the `claude-home` volume paragraph (`:393-405`), `:449`, `:469`, and the
  "Upgrades" bullet (`:945-960`)
- Modify: `.env.example` — the `ISSUEBOT_AGENT_USER` and `ISSUEBOT_AGENT_POOL_SIZE` comments
- Modify: `CLAUDE.md:239`, `:409`, `:715` — the three `claude-home` sentences
- Modify: `.github/workflows/ci.yml:280`, `src/issuebot/agent/runas.py:51`,
  `src/issuebot/agent/runner.py:736` — comments naming the volume

**Interfaces:**
- Consumes: the behaviour of every task above
- Produces: documentation; no code

- [ ] **Step 1: Rewrite the credential section**

Replace the "log in once inside the container" recipe with the environment credential as the
only container route: `claude setup-token` on a machine with a browser, the value into this
checkout's environment file as `CLAUDE_CODE_OAUTH_TOKEN`. Give the credential table a column
saying which route each row belongs to:

| Line | Route | What it means |
|---|---|---|
| `logged in (CLAUDE_CODE_OAUTH_TOKEN)` | container | the token from `claude setup-token` |
| `logged in (API key from ANTHROPIC_API_KEY)` | container | an Anthropic API key |
| `logged in (claude.ai, max)` | host, development | the login in your own `~/.claude` |
| `not logged in` | either | nothing usable — a `[FAIL]`, because the agent cannot run |

Delete the `claude-home` volume paragraph at `:393-405` entirely: there is no volume to inspect.

- [ ] **Step 2: Move the host route out of the setup path**

Delete the "To run on the host instead" block and the `workspace.root` note that follows it,
the `# on the host: uv run issuebot ...` parentheticals at `:241-242`, `:449` and `:469`, and
the host entries under "Prerequisites". Anything they say that is not already under
`## Development` moves there — check the `set -a` sourcing line and the `workspace.root` advice
specifically. **A DSN moved into `## Development` keeps `${ISSUEBOT_DB_PASSWORD}` in its
password position**: `tests/test_compose_credentials.py` holds `README.md` to that rule.

- [ ] **Step 3: Prune "Upgrades" to its evergreen half**

Keep: overrides live in `configs/WORKFLOW.local.md`, a clean `git pull` updates the tracked
file, run `docker compose build` after pulling. Delete the three version-to-version notes (the
`configs/` mount move, the #75 `chown` of the login volume, the #102 `web` account rebuild).

- [ ] **Step 4: Update the environment example and the instruction files**

With the Write/Edit tools, never a shell heredoc (the hook rejects any Bash command naming that
file). In `.env.example`, rewrite the `ISSUEBOT_AGENT_USER` comment: the image's default is now
the pool it built, the variable is an override for naming accounts by hand, and
`ISSUEBOT_AGENT_POOL_SIZE` is what a deployment raises — with `docker compose build worker`, in
the same breath. In `CLAUDE.md`, fix the three sentences that describe the volume: `:239` (a
pool gives each account its own home), `:409` (`~/.claude.json` beside `.claude/`) and `:715`
(the restart-loop until the volume holds a login — now until the environment holds a
credential). Then the three code comments in `ci.yml`, `runas.py` and `runner.py`.

- [ ] **Step 5: Verify the docs did not break a parser**

```bash
uv run pytest tests/test_compose_credentials.py tests/test_image_layout.py -q
python3 -c "import re,pathlib; print(bool(re.search(r'```bash\n(#!/usr/bin/env bash.*?)```', pathlib.Path('README.md').read_text(), re.S)))"
```

Expected: PASS, then `True` — CI parses the PostgreSQL cluster recipe out of `README.md` by
regex (`.github/workflows/ci.yml:481-487`) and runs it inside the image. If that prints
`False`, the recipe's fences moved and the `docker` job will fail; put them back.

- [ ] **Step 6: Commit**

```bash
git status --short
git add --all
git commit -m "docs: one way to run the app, and the host route lives under Development (#142)"
```

---

### Task 9: verification and the pull request

**Files:**
- Create: a temporary body file under the scratchpad directory
- Modify: none

- [ ] **Step 1: Run everything**

```bash
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files
uv run pytest -q
```

Expected: all pass. Record the test count for the PR body.

- [ ] **Step 2: Prove it in the image**

```bash
docker compose build worker
docker run --rm --entrypoint sh issuebot-worker:latest -c 'cat /etc/issuebot/session-accounts'
docker run --rm --entrypoint sh issuebot-worker:latest -c 'cd /app && uv run pytest -q' || true
```

Expected: the account list, then the suite passing **inside the image** — the property #140
asks for. If `uv` is not on the image's path, run the suite on the host and say in the PR that
the in-image run was not possible.

- [ ] **Step 3: Write the PR body**

In a **separate** Bash call from the `gh api` one, with the Write tool, into the scratchpad
directory. The body carries what the README no longer does:

- what changed and why, in three sentences;
- **Migration for an existing deployment**, as a numbered list: mint a token with `claude
  setup-token` if the deployment has none; put it in that checkout's environment file as
  `CLAUDE_CODE_OAUTH_TOKEN`; `docker compose build worker`; `docker compose up -d worker`;
  then `docker volume rm <project>_claude-home` once the worker is up. Note that the
  environment credential already wins over a volume login, so a deployment that has set the
  variable is on this route already and the rebuild changes nothing it does at run time;
- the one thing given up: a volume login rotates its own refresh token, a `setup-token` lapses
  and is re-minted, with the auth hold as the reminder;
- that it closes #142 and #140.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin issuebot/142-session-credential-standard
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='One credential route for the container, and the pool as its default' \
  -f head='issuebot/142-session-credential-standard' -f base='main' \
  -F body=@<scratchpad>/pr-body.md
```

Then read it back: `gh pr view <N> --json body --jq '.body'`.

- [ ] **Step 5: Stop**

Do not merge or close the PR. Report the number and the verification output.

---

## Notes for the executor

- **The host route is not being removed.** `agent.run_as` unset must keep working in code and
  in tests: it is how `uv run pytest` runs at all. If a task seems to require deleting a
  `run_as is None` branch, it is the wrong task.
- **`agent` stays in the image.** It is the single-account container route and what CI's sweep
  proof runs as. Only its volume goes.
- The orchestrator needs no change: it already calls `credential_complaint` through
  `_settle_run_as`, so Task 4 widens the startup refusal and the dispatch hold for free. Assert
  that rather than assuming it — `tests/test_orchestrator.py` has the hold tests.
