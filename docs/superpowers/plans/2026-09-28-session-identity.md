# Session Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `validate` warns when the account a session acts as could merge its own work — the token belongs to a repository admin, or the default branch requires no approving review — and the documentation says how a solo operator and an organisation each set up an identity that cannot.

**Architecture:** Two adapter reads: `RepoInfo.admin` (from `repos/{repo}`'s `permissions.admin`, one more key in the existing `--jq`) and a new `branch_rules(branch) -> BranchRules` from `repos/{repo}/rules/branches/{branch}`. `cli._probe_github` turns them into two `warn` checks, `github.token account` and `github.branch rules`, never `fail`. `docs/security-model.md` gets `## The account a session acts as` — solo operator first (a second personal account with write, a ruleset requiring one review, the operator as a `Repository admin` bypass actor in `pull_request` mode), organisation second (a machine user, an organisation ruleset, human teams as bypass actors) — and the README's Prerequisites point to it.

**Tech Stack:** Python 3.14, `uv`, pytest, `gh api` REST through the runner seam, the fake adapter.

**Spec:** `docs/superpowers/specs/2026-09-28-tracker-text-admission-design.md`, section 3.

## Global Constraints

- `uv run ruff check . && uv run ruff format --check .` clean; `uv run pytest` green (hermetic).
- Both new reads exist on `GhCliAdapter`, `FakeGitHub` and the `GitHubAdapter` protocol.
- The two checks are `warn`, never `fail`: `run-once` against a scratch repository is legitimate.
- `_NETWORK_SUBJECTS` in `cli.py` lists every check `validate` skips when `gh` is absent; the two new subjects join it, so the count of "skipped (gh not found)" lines stays honest.
- `tests/test_readme_bounds.py` pins four README passages by phrase; the Prerequisites edit sits *before* the Workflows-write incentive in the same paragraph and must not move that incentive's consequence out of its 700-character window.
- `tests/test_doc_pointers.py` resolves every `[text](file.md#anchor)` and every `(`file`, "Heading")` pointer; new headings must match their pointers exactly.
- `tests/test_compose_credentials.py` checks the operator-facing files for a working DSN; the new prose contains none.
- Never name the advisory's private sibling repository; refer to GHSA-jm8h-q3j6-p8xp.
- Commit messages end with the attribution lines the session was given; PRs are opened with `gh api repos/{owner}/{repo}/pulls -X POST` and a body file written in a separate Bash call.

## Review Focus

1. **`permissions` absent from the repository response** — a token that can read the repository but whose response carries no `permissions` block (some installation tokens) must read as `admin=False`, not raise. Pinned in Task 1.
2. **No rules on the branch** — `rules/branches/main` answers `[]`; `required_approving_reviews` is `None` and the check warns. Pinned in Task 2 and Task 3.
3. **A `pull_request` rule with `required_approving_review_count: 0`** — present but toothless; warns the same as absent, and the message says `0`. Pinned in Task 3.
4. **The branch rules read fails** (404 on an old GHES, a network error) — one `warn` line naming the error, and the other checks still run. Pinned in Task 3.
5. **The bypass actor** — the doc must say `pull_request` bypass mode, not `always`: `always` would let the admin push to the branch directly, which the deletion and non-fast-forward rules are there to stop. Pinned by the exact phrase in Task 4's doc test.

---

### Task 1: `RepoInfo.admin`

**Files:**
- Modify: `src/issuebot/github/models.py` (`class RepoInfo`)
- Modify: `src/issuebot/github/ghcli.py` (`repo_info`)
- Modify: `src/issuebot/github/fake.py` (`__init__`, `repo_info`)
- Modify: `tests/fixtures/gh/repo.json`
- Test: `tests/test_github_ghcli.py`

**Interfaces:**
- Produces: `RepoInfo.admin: bool = False`; `FakeGitHub(..., admin: bool = False)` stored as `self.admin`, so a test can flip it after construction.

- [ ] **Step 1: Write the failing tests**

Replace `test_repo_info_parses_fields` in `tests/test_github_ghcli.py` with:

```python
async def test_repo_info_parses_fields_and_the_callers_admin_right() -> None:
    runner = StubRunner()
    runner.on(has("repos/example/repo"), stdout=fixture("repo.json"))
    info = await make_adapter(runner).repo_info()
    assert runner.argv(0) == [
        "api",
        "repos/example/repo",
        "--jq",
        "{full_name,default_branch,private,admin: (.permissions.admin // false)}",
    ]
    assert (info.full_name, info.default_branch, info.private, info.admin) == (
        "example/repo",
        "main",
        False,
        True,
    )


async def test_repo_info_reads_no_permissions_as_not_admin() -> None:
    """An installation token's repository response can carry no ``permissions`` at all."""
    runner = StubRunner()
    runner.on(
        has("repos/example/repo"),
        stdout='{"full_name": "example/repo", "default_branch": "main", "private": false, "admin": false}',
    )
    assert (await make_adapter(runner).repo_info()).admin is False
```

and make `tests/fixtures/gh/repo.json`:

```json
{"full_name": "example/repo", "default_branch": "main", "private": false, "admin": true}
```

(The fixture is what `--jq` would have produced, as the existing one is.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_github_ghcli.py -k repo_info -v`
Expected: FAIL on the argv assertion.

- [ ] **Step 3: Implement**

`models.py`:

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class RepoInfo:
    full_name: str
    default_branch: str
    private: bool
    # Whether the account the token belongs to administers the repository (GHSA-jm8h-q3j6-p8xp):
    # an admin can bypass or rewrite the branch ruleset that stops a session merging its own
    # work, so `validate` warns when a session's token is one.
    admin: bool = False
```

`ghcli.py`, `repo_info`: the `--jq` becomes `"{full_name,default_branch,private,admin: (.permissions.admin // false)}"` and the constructor gets `admin=bool(payload.get("admin", False))`.

`fake.py`: `__init__` gains `admin: bool = False`, stored as `self.admin`; `repo_info` returns `RepoInfo(full_name=self.repo, default_branch="main", private=False, admin=self.admin)`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_github_ghcli.py -k repo_info tests/test_cli.py -k "validate" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/github tests/test_github_ghcli.py tests/fixtures/gh/repo.json
git commit -m "github: repo_info says whether the token's account is an admin"
```

---

### Task 2: `branch_rules`

**Files:**
- Modify: `src/issuebot/github/models.py` (after `RepoInfo`)
- Modify: `src/issuebot/github/adapter.py`, `src/issuebot/github/__init__.py`
- Modify: `src/issuebot/github/ghcli.py` (after `repo_info`)
- Modify: `src/issuebot/github/fake.py`
- Test: `tests/test_github_ghcli.py`, `tests/test_github_fake.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True, kw_only=True, slots=True)
  class BranchRules:
      branch: str
      required_approving_reviews: int | None  # None: no pull_request rule applies

  async def branch_rules(self, branch: str) -> BranchRules  # on the protocol, GhCliAdapter and FakeGitHub
  ```
  `FakeGitHub.branch_rules_result: BranchRules | None = None` — when `None`, the fake answers `BranchRules(branch=branch, required_approving_reviews=1)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_github_ghcli.py`:

```python
@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ("[]", None),
        ('[{"type": "deletion"}, {"type": "non_fast_forward"}]', None),
        ('[{"type": "pull_request", "parameters": {"required_approving_review_count": 0}}]', 0),
        ('[{"type": "pull_request", "parameters": {"required_approving_review_count": 2}}]', 2),
        ('[{"type": "pull_request"}]', 0),
    ],
)
async def test_branch_rules_reads_the_review_count_in_force(
    rules: str, expected: int | None
) -> None:
    """The rules endpoint answers what applies to the caller on that branch (rulesets only)."""
    runner = StubRunner()
    runner.on(has("rules/branches/main"), stdout=rules)
    result = await make_adapter(runner).branch_rules("main")
    assert runner.argv(0) == ["api", "repos/example/repo/rules/branches/main"]
    assert (result.branch, result.required_approving_reviews) == ("main", expected)


@pytest.mark.parametrize("stdout", ["", "null", "{}", "[1]"])
async def test_branch_rules_rejects_a_malformed_response(stdout: str) -> None:
    runner = StubRunner()
    runner.on(has("rules/branches/main"), stdout=stdout)
    with pytest.raises(GitHubError) as excinfo:
        await make_adapter(runner).branch_rules("main")
    assert excinfo.value.category == "response"
```

`tests/test_github_fake.py`:

```python
async def test_branch_rules_defaults_to_one_review_and_can_be_set() -> None:
    fake = FakeGitHub(GitHubSettings(repo="example/repo"))
    assert (await fake.branch_rules("main")).required_approving_reviews == 1
    fake.branch_rules_result = BranchRules(branch="main", required_approving_reviews=None)
    assert (await fake.branch_rules("main")).required_approving_reviews is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_github_ghcli.py tests/test_github_fake.py -k branch_rules -v`
Expected: FAIL with `AttributeError: ... has no attribute 'branch_rules'`

- [ ] **Step 3: Implement**

`models.py`, after `RepoInfo`:

```python
@dataclass(frozen=True, kw_only=True, slots=True)
class BranchRules:
    """What the repository's rulesets require of a pull request into ``branch``.

    ``required_approving_reviews`` is ``None`` when no ``pull_request`` rule applies. Rulesets
    only: classic branch protection is readable by admins alone, and the check that reads
    this must work for the account it recommends, which is not one.
    """

    branch: str
    required_approving_reviews: int | None
```

Export it from `issuebot.github`; declare on the protocol after `repo_info`:

```python
    async def branch_rules(self, branch: str) -> BranchRules:
        """The ruleset requirements in force for pull requests into ``branch``."""
        ...
```

`ghcli.py`, after `repo_info`:

```python
async def branch_rules(self, branch: str) -> BranchRules:
    self._log.debug("branch_rules", branch=branch)
    result = await self._gh(["api", f"repos/{self.repo}/rules/branches/{branch}"])
    payload = _parse_json(result.stdout)
    if not isinstance(payload, list):
        raise GitHubError("response", "branch rules response is not a list")
    required: int | None = None
    for rule in payload:
        if not isinstance(rule, Mapping):
            raise GitHubError("response", "branch rule is not an object")
        if rule.get("type") != "pull_request":
            continue
        parameters = rule.get("parameters")
        count = (
            parameters.get("required_approving_review_count")
            if isinstance(parameters, Mapping)
            else None
        )
        required = count if isinstance(count, int) and not isinstance(count, bool) else 0
    return BranchRules(branch=branch, required_approving_reviews=required)
```

`fake.py`: `self.branch_rules_result: BranchRules | None = None` in `__init__`, and

```python
    async def branch_rules(self, branch: str) -> BranchRules:
        self._enter("branch_rules", branch)
        if self.branch_rules_result is not None:
            return self.branch_rules_result
        return BranchRules(branch=branch, required_approving_reviews=1)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_github_ghcli.py tests/test_github_fake.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/github tests/test_github_ghcli.py tests/test_github_fake.py
git commit -m "github: read the review count a branch's rulesets require"
```

---

### Task 3: The two `validate` checks

**Files:**
- Modify: `src/issuebot/cli.py` (`_NETWORK_SUBJECTS`; `_probe_github`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `RepoInfo.admin` (Task 1), `adapter.branch_rules` (Task 2).
- Produces: check subjects `github.token account` and `github.branch rules`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`, after `test_validate_warns_about_missing_labels` (use its fixtures: `capsys`, `monkeypatch`, `executables`, `fake_github`, and `GOOD`):

```python
def test_validate_says_the_token_account_is_not_an_admin_and_the_branch_requires_review(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] github.token account: issuebot has write on example/repo, not admin\n" in out
    assert "[ OK ] github.branch rules: main requires 1 approving review\n" in out


def test_validate_warns_when_the_tokens_account_administers_the_repository(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    """GHSA-jm8h-q3j6-p8xp: an admin can bypass or rewrite the ruleset that stops a session
    merging its own work, and the session holds the token."""
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.admin = True
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert (
        "[WARN] github.token account: issuebot administers example/repo, and the session holds "
        "the token: a session can bypass or rewrite the branch ruleset. Run as a dedicated "
        'account with write access (docs/security-model.md, "The account a session acts as")'
    ) in out


@pytest.mark.parametrize(
    ("count", "detail"),
    [
        (None, "no pull_request rule applies to main"),
        (0, "main requires 0 approving reviews"),
    ],
)
def test_validate_warns_when_the_default_branch_needs_no_review(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
    count: int | None,
    detail: str,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    fake_github.branch_rules_result = BranchRules(branch="main", required_approving_reviews=count)
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert (
        f"[WARN] github.branch rules: {detail}: the account a session runs as can merge its own "
        "pull requests. Require at least one approving review (rulesets only; classic branch "
        "protection is not read here)"
    ) in out


def test_validate_warns_when_the_branch_rules_will_not_read(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: object,
    fake_github: FakeGitHub,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")

    async def failing(branch: str) -> object:
        raise GitHubError("not_found", "HTTP 404: Not Found")

    monkeypatch.setattr(fake_github, "branch_rules", failing)
    assert main(["validate", "--workflow", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] github.branch rules: could not read: HTTP 404: Not Found\n" in out
    assert "[ OK ] github.labels:" in out  # the checks after it still run


def test_validate_skips_the_identity_checks_without_gh(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    executables: Callable[[set[str]], None],
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    executables({"claude"})
    main(["validate", "--workflow", str(GOOD)])
    out = capsys.readouterr().out
    assert "[WARN] github.token account: skipped (gh not found)" in out
    assert "[WARN] github.branch rules: skipped (gh not found)" in out
```

Look at how the existing tests in this file call `executables` (some call `executables({"gh", "claude"})` explicitly; match that), and adjust the `"0 failed, N warnings"` totals in tests that count warnings if the new `OK` lines change nothing but a test asserts the total line.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "token_account or branch_rules or default_branch_needs or identity_checks" -v`
Expected: FAIL on the missing lines.

- [ ] **Step 3: Implement**

`cli.py`:

```python
_NETWORK_SUBJECTS = (
    "gh auth",
    "github.repo access",
    "github.token account",
    "github.branch rules",
    "github.labels",
)
```

In `_probe_github`, the `repo_info` block becomes:

```python
    try:
        info = await adapter.repo_info()
    except GitHubError as exc:
        checks.append(Check("github.repo access", "fail", str(exc)))
        info = None
    else:
        detail = f"{info.full_name} (default branch {info.default_branch})"
        checks.append(Check("github.repo access", "ok", detail))
    if info is not None:
        checks.append(_token_account_check(auth_login, info))
        checks.append(await _branch_rules_check(adapter, info.default_branch))
```

where `auth_login` is the login from the `auth_status` block above (`None` when it failed; then say `the token's account` in place of the login). Add:

```python
IDENTITY_DOC = 'docs/security-model.md, "The account a session acts as"'


def _token_account_check(login: str | None, info: RepoInfo) -> Check:
    """Whether the token's account could undo the rule that stops a session merging its own
    work (GHSA-jm8h-q3j6-p8xp): an admin can bypass or rewrite the ruleset, and the session
    holds the token."""
    who = login or "the token's account"
    if info.admin:
        return Check(
            "github.token account",
            "warn",
            f"{who} administers {info.full_name}, and the session holds the token: a session "
            "can bypass or rewrite the branch ruleset. Run as a dedicated account with write "
            f"access ({IDENTITY_DOC})",
        )
    return Check("github.token account", "ok", f"{who} has write on {info.full_name}, not admin")


async def _branch_rules_check(adapter: GitHubAdapter, branch: str) -> Check:
    try:
        rules = await adapter.branch_rules(branch)
    except GitHubError as exc:
        return Check("github.branch rules", "warn", f"could not read: {exc.message}")
    count = rules.required_approving_reviews
    if count is None:
        detail = f"no pull_request rule applies to {branch}"
    elif count == 0:
        detail = f"{branch} requires 0 approving reviews"
    else:
        noun = "approving review" if count == 1 else "approving reviews"
        return Check("github.branch rules", "ok", f"{branch} requires {count} {noun}")
    return Check(
        "github.branch rules",
        "warn",
        f"{detail}: the account a session runs as can merge its own pull requests. Require at "
        "least one approving review (rulesets only; classic branch protection is not read here)",
    )
```

(`GitHubError.message` is what `_github_hold_reason` and the auth check use; keep to it.)

- [ ] **Step 4: Run the CLI suite**

Run: `uv run pytest tests/test_cli.py -q`
Expected: PASS. Any test asserting a `N checks: 0 failed, M warnings` total moves by two checks; update those totals and say so in the commit.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/issuebot/cli.py tests/test_cli.py && uv run ruff format src/issuebot/cli.py tests/test_cli.py
git add src/issuebot/cli.py tests/test_cli.py
git commit -m "cli: validate warns when the session's identity could merge its own work"
```

---

### Task 4: The documentation

**Files:**
- Modify: `docs/security-model.md` (new `## The account a session acts as` before `## Checking that the credential took`; one clause in the opening paragraph)
- Modify: `README.md` (`### Prerequisites` item 1, first sentence; the `validate` sample under `### Step 2`)
- Modify: `docs/operations.md` (`### Safety`: one sentence pointing at the new section)
- Test: `tests/test_doc_pointers.py`, `tests/test_readme_bounds.py` (existing), plus one new test in `tests/test_readme_bounds.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_readme_bounds.py`:

```python
def test_the_bypass_is_pull_request_mode_only() -> None:
    """The admin bypass exists so a solo operator's own pull requests can merge; `always` would
    also let that account push to the branch directly, which the deletion and non-fast-forward
    rules are there to stop."""
    text = (ROOT / "docs" / "security-model.md").read_text(encoding="utf-8")
    assert "bypass actor in `pull_request` mode" in text
    assert "not `always`" in text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_readme_bounds.py -k bypass -v`
Expected: FAIL

- [ ] **Step 3: Write the prose**

`docs/security-model.md`, opening paragraph: `... and the credential it authenticates with — and, for the GitHub credential, whose it is.`

New section, before `## Checking that the credential took`:

```markdown
## The account a session acts as

Everything above bounds where a session's bytes can go. It does not bound what a session does
with `GH_TOKEN` at `api.github.com`, which the workflow needs and the egress allow-list
therefore admits: a session holds whatever that token's account may do to the repository. If
that account is yours, a persuaded session can approve and merge its own pull request, or
push to the default branch, with your authority (GHSA-jm8h-q3j6-p8xp). So the identity a
session acts as must be one that cannot merge what it wrote -- and `validate` says whether
yours is, on two lines: `github.token account` warns when the token's account administers the
repository, since an admin can bypass or rewrite the rule below; `github.branch rules` warns
when no ruleset requires an approving review on the default branch, since then the session's
account can merge its own pull requests. Both are warnings, because `run-once` against a
scratch repository is a legitimate use.

**A solo operator**, which is who this repository expects. Create a second personal account
for issuebot and add it as a collaborator with **write** -- never admin. Put a ruleset on the
default branch requiring one approving review, with no bypass for that account; you approve
the bot's pull requests from your own. Your own pull requests need an approver too, and GitHub
does not let an author approve their own, so add yourself as a `Repository admin` bypass actor
in `pull_request` mode, not `always`: your pull requests merge without a second account, and a
direct push to the branch is still refused, which is what the ruleset's deletion and
non-fast-forward rules are there for. The session's identity cannot merge what it wrote and
cannot change the rule that says so.

**An organisation** has the same shape with its own tools. The dedicated account is a machine
user -- GitHub's name for a personal account an organisation creates for automation -- made a
member of the organisation, or an outside collaborator, with **write** on the repository and no
seat on any team that carries admin or maintain. The ruleset is an *organisation* ruleset
targeting the repository's default branch rather than a repository one: a repository admin
cannot remove it, so the guarantee holds against the repository's own admins too, and it
covers every repository the organisation points a deployment at. Its bypass actors are teams
of humans, never the machine user, and its approvers are whoever reviews there already, so no
admin bypass is needed. `CODEOWNERS` with "require review from Code Owners" narrows who can
approve a session's change to a path; that is the organisation's choice. A GitHub App
installation token is not the recommended credential, though `gh` accepts one: it expires
after an hour, and a session can run longer than that.
```

`README.md`, Prerequisites item 1, replace the first two sentences with: `**A GitHub token** for the account the agent will act as. Every commit, PR and comment appears under that account, and the session holds the token, so run issuebot as a dedicated account that cannot merge its own work -- [The account a session acts as](docs/security-model.md#the-account-a-session-acts-as) is the recipe for one person and for an organisation. Create a fine-grained personal access token restricted to the target repository with:`

`README.md`, the `validate` sample under Step 2: after the `github.repo access` line add
`[ OK ] github.token account: your-bot has write on your-org/your-repo, not admin` and
`[ OK ] github.branch rules: main requires 1 approving review`, and change the last line to `20 checks: 0 failed, 2 warnings`.

`docs/operations.md`, `### Safety`, one sentence at the end of the paragraph that names the token's scoping: `Whose the token is matters as much as its scope: [The account a session acts as](security-model.md#the-account-a-session-acts-as).`

- [ ] **Step 4: Run the doc tests and the suite**

Run: `uv run pytest tests/test_doc_pointers.py tests/test_readme_bounds.py tests/test_instruction_bounds.py tests/test_compose_credentials.py -v && uv run pytest -q && uv run pre-commit run --all-files`
Expected: PASS. If `test_readme_bounds.py::test_workflows_write_names_the_review_gate_it_removes` fails, the Prerequisites edit pushed the Workflows consequence past 700 characters: shorten the new sentence rather than the pinned passage.

- [ ] **Step 5: Commit**

```bash
git add docs/security-model.md README.md docs/operations.md tests/test_readme_bounds.py
git commit -m "docs: the account a session acts as cannot merge its own work"
```

---

### Task 5: Pull request

- [ ] **Step 1: Push `security/session-identity` and open the PR through the REST API**

Body file in a separate Bash call, then `gh api repos/jleavers/issuebot/pulls -X POST -f title='validate: warn when the session identity could merge its own work' -f head='security/session-identity' -f base='main' -F body=@/path/to/body.md`. The body: finding 3 of GHSA-jm8h-q3j6-p8xp, the two checks and why they warn rather than fail, the solo-operator and organisation recipes in one line each, and the tests. Attribution lines at the end.
