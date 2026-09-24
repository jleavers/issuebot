"""`tools/upgrade/upgrade.py`: the pure half, and the three claims its docstring makes.

The claims are about *when it stops*, because that is what the tool is for. Upgrading several
checkouts against one database is not a loop over one checkout: they are clones of the same
repository, and `migrate.py` refuses to start against a schema newer than its own code, so a run
that half-finishes leaves a worker that cannot start. Two of the three tests below are therefore
about a run that refuses to proceed, and the third is about one that proceeds without the
checkout that failed.

The third claim is the environment report's: it names keys and never reads a value. That file
holds the database password and the Claude credential, so a report that quoted a line would be a
new exit for both. A docstring saying so is not a guard; this is.

The tool is not a package -- `tools/` holds scripts an operator runs, like `tools/screenshots` --
so it is loaded by path rather than imported.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

UPGRADE_PATH = Path(__file__).resolve().parent.parent / "tools" / "upgrade" / "upgrade.py"
# A value of the shape an operator's file actually holds, so a report that leaked one would leak
# this. Long enough that no key name could contain it by accident.
DB_PASSWORD = "a-database-password-nobody-should-see"


def _load() -> ModuleType:
    name = "issuebot_tools_upgrade"
    spec = importlib.util.spec_from_file_location(name, UPGRADE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed, for the reason `tests/test_tools_watch.py` gives:
    # `@dataclass` resolves `cls.__module__` through `sys.modules`.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


upgrade = _load()


# ---------------------------------------------------------------------------
# Fixtures for the pure half
# ---------------------------------------------------------------------------
def checkout(
    name: str = "issuebot",
    *,
    branch: str = "main",
    upstream: str | None = "origin/main",
    dirty: bool = False,
    ahead: int = 0,
    behind: int = 3,
    services: tuple[str, ...] = ("egress", "worker"),
) -> object:
    return upgrade.Checkout(
        path=Path("/deployments") / name,
        branch=branch,
        upstream=upstream,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        services=services,
    )


def hub(name: str = "issuebot", **kwargs: object) -> object:
    kwargs.setdefault("services", ("db", "egress", "web", "worker"))
    return checkout(name, **kwargs)  # type: ignore[arg-type]


class FakeRun:
    """Records every subprocess the tool would run, and answers each one.

    `fail` maps a verb -- the word the tool's own phase uses, `stop`, `build`, `validate`,
    `up` -- to the checkout name it should fail for.
    """

    def __init__(self, fail: dict[str, str] | None = None, example: str = "") -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail = fail or {}
        self.example = example

    def __call__(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: Path,
        log: Path | None = None,
        quiet: bool = False,
    ) -> object:
        name = Path(cwd).name
        self.calls.append((name, tuple(argv)))
        verb = self._verb(tuple(argv))
        if self.fail.get(verb) == name:
            return upgrade.RunResult(returncode=1, stdout="")
        if verb == "example":
            return upgrade.RunResult(returncode=0, stdout=self.example)
        if verb == "ps":
            return upgrade.RunResult(returncode=0, stdout="")
        return upgrade.RunResult(returncode=0, stdout="")

    @staticmethod
    def _verb(argv: tuple[str, ...]) -> str:
        if argv[:2] == ("git", "show"):
            return "example"
        if argv[:2] == ("git", "merge"):
            return "merge"
        if "stop" in argv:
            return "stop"
        if "build" in argv:
            return "build"
        if "validate" in argv:
            return "validate"
        if "up" in argv:
            return "up"
        if "ps" in argv:
            return "ps"
        return " ".join(argv)

    def verbs_for(self, name: str) -> list[str]:
        return [self._verb(argv) for called, argv in self.calls if called == name]


def context(run: FakeRun, **kwargs: object) -> object:
    kwargs.setdefault("out", lambda _line: None)
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("read_text", lambda _path: "")
    return upgrade.Context(run=run, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Claim 1 -- a checkout that fails inspection stops the run before anything is stopped
# ---------------------------------------------------------------------------
def test_a_dirty_checkout_stops_the_run_before_any_worker_is_stopped() -> None:
    """The one that matters: the hub checkout is often the development checkout too.

    Pulling over a feature branch or uncommitted work would be destructive, and stopping the
    workers first would leave the deployment down while it happened.
    """
    run = FakeRun()
    code = upgrade.upgrade([hub("issuebot"), checkout("issuebot-two", dirty=True)], context(run))

    assert code == 1
    assert run.calls == []


def test_a_checkout_with_no_upstream_stops_the_run() -> None:
    """Without an upstream there is nothing to pull to, and no branch to report."""
    run = FakeRun()
    code = upgrade.upgrade([hub(), checkout("issuebot-two", upstream=None)], context(run))

    assert code == 1
    assert run.calls == []


def test_local_commits_stop_the_run() -> None:
    """`git merge --ff-only` would refuse anyway; saying so first is cheaper than a half-run."""
    run = FakeRun()
    code = upgrade.upgrade([hub(), checkout("issuebot-two", ahead=2)], context(run))

    assert code == 1
    assert run.calls == []


def test_everything_current_does_nothing_at_all() -> None:
    """Re-running after a successful upgrade must not restart three healthy deployments."""
    run = FakeRun()
    code = upgrade.upgrade([hub(behind=0), checkout("issuebot-two", behind=0)], context(run))

    assert code == 0
    assert run.calls == []


def test_force_runs_even_when_everything_is_current() -> None:
    run = FakeRun()
    code = upgrade.upgrade(
        [hub(behind=0)], context(run, force=True, skip_validate=True, health_wait=0)
    )

    assert code == 0
    assert "build" in run.verbs_for("issuebot")


# ---------------------------------------------------------------------------
# Claim 2 -- a checkout that fails later is skipped, and the others still come up
# ---------------------------------------------------------------------------
def test_a_build_failure_skips_only_that_checkout() -> None:
    run = FakeRun(fail={"build": "issuebot-two"})
    code = upgrade.upgrade(
        [hub("issuebot"), checkout("issuebot-two")],
        context(run, skip_validate=True, health_wait=0),
    )

    assert code == 1
    assert "up" in run.verbs_for("issuebot")
    assert "up" not in run.verbs_for("issuebot-two")


def test_a_failed_checkout_is_left_stopped_rather_than_started() -> None:
    """Its worker was stopped in phase 2 and nothing starts it again: a visible, safe state.

    The alternative -- starting it on the old image beside the others on the new one -- is the
    mixed-schema state the whole one-phase-at-a-time shape exists to prevent.
    """
    run = FakeRun(fail={"validate": "issuebot-two"})
    upgrade.upgrade(
        [hub("issuebot"), checkout("issuebot-two")],
        context(run, health_wait=0),
    )

    verbs = run.verbs_for("issuebot-two")
    assert "stop" in verbs
    assert "up" not in verbs


def test_the_hub_is_built_and_started_first() -> None:
    """The hub carries the database, so the newest schema must be migrated before the rest."""
    run = FakeRun()
    upgrade.upgrade(
        [checkout("issuebot-worker"), hub("issuebot-hub")],
        context(run, skip_validate=True, health_wait=0),
    )

    started = [name for name, argv in run.calls if FakeRun._verb(argv) == "up"]
    assert started == ["issuebot-hub", "issuebot-worker"]


def test_a_stop_failure_keeps_the_checkout_out_of_the_pull() -> None:
    """A worker still running would be handed the new configs against its old image."""
    run = FakeRun(fail={"stop": "issuebot-two"})
    code = upgrade.upgrade(
        [hub("issuebot"), checkout("issuebot-two")],
        context(run, skip_validate=True, health_wait=0),
    )

    assert code == 1
    assert "merge" not in run.verbs_for("issuebot-two")


def test_skip_validate_does_not_validate() -> None:
    run = FakeRun()
    upgrade.upgrade([hub()], context(run, skip_validate=True, health_wait=0))

    assert "validate" not in run.verbs_for("issuebot")


# ---------------------------------------------------------------------------
# Claim 3 -- the environment report names keys and never reads a value
# ---------------------------------------------------------------------------
def test_env_keys_are_names_only() -> None:
    text = f"ISSUEBOT_DB_PASSWORD={DB_PASSWORD}\nexport GH_TOKEN=ghp_x\n# COMMENTED=no\n\n"

    assert upgrade.env_keys(text) == {"ISSUEBOT_DB_PASSWORD", "GH_TOKEN"}


def test_env_drift_names_what_is_missing_and_what_is_extra() -> None:
    example = "A=\nB=\nC=\n"
    actual = "A=1\nC=3\nD=4\n"

    missing, extra = upgrade.env_drift(example, actual)

    assert missing == ["B"]
    assert extra == ["D"]


def test_no_value_from_either_file_can_reach_the_report() -> None:
    """The claim, driven through the phase rather than the helper.

    Both sides carry a secret: the checkout's own file, and the example the new release ships.
    Neither may appear in a line the tool prints.
    """
    printed: list[str] = []
    run = FakeRun(example=f"ISSUEBOT_DB_PASSWORD={DB_PASSWORD}\nISSUEBOT_NEW_KEY=example-value\n")
    ctx = context(
        run,
        out=printed.append,
        skip_validate=True,
        health_wait=0,
        read_text=lambda _path: f"ISSUEBOT_DB_PASSWORD={DB_PASSWORD}\nISSUEBOT_OLD_KEY=old-value\n",
    )

    upgrade.upgrade([hub()], ctx)

    report = "\n".join(printed)
    assert DB_PASSWORD not in report
    assert "example-value" not in report
    assert "old-value" not in report
    # It still has to be useful: the key that is missing here is named.
    assert "ISSUEBOT_NEW_KEY" in report
    assert "ISSUEBOT_OLD_KEY" in report


# ---------------------------------------------------------------------------
# The rest of the pure half
# ---------------------------------------------------------------------------
def test_hub_first_is_stable_for_the_rest() -> None:
    one, two, three = checkout("a"), hub("b"), checkout("c")

    assert upgrade.hub_first([one, two, three]) == [two, one, three]


def _fake_checkout(root: Path, *, git_is_a_directory: bool) -> Path:
    """A directory with the two things discovery looks at, and nothing else."""
    (root / "compose.yaml").write_text("services: {}\n")
    if git_is_a_directory:
        (root / ".git").mkdir()
    else:
        # What `git worktree add` writes: a file naming the real git directory.
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wip\n")
    return root


def test_discovery_skips_a_sibling_git_worktree(tmp_path: Path) -> None:
    """A worktree's `.git` is a file, and a worktree beside a checkout is not a deployment.

    It would otherwise be discovered and abort the whole run at phase 1 -- a worktree is
    normally on a feature branch that tracks no upstream -- so an ordinary parallel-session
    layout would block upgrading the deployments beside it.
    """
    assert not upgrade.looks_like_a_checkout(_fake_checkout(tmp_path, git_is_a_directory=False))


def test_discovery_accepts_an_ordinary_clone(tmp_path: Path) -> None:
    assert upgrade.looks_like_a_checkout(_fake_checkout(tmp_path, git_is_a_directory=True))


def test_a_worktree_named_explicitly_is_still_inspected(tmp_path: Path) -> None:
    """The asymmetry is deliberate: skipping one is discovery's guess, not a refusal.

    A deployment genuinely run from a worktree is reached by passing its path, so
    `inspect_checkout` must not reject it for the shape of its `.git`.
    """
    path = _fake_checkout(tmp_path, git_is_a_directory=False)

    _checkout, problem = upgrade.inspect_checkout(path)

    # It fails later, on git itself -- but never for being a worktree.
    assert problem != "not a git checkout"


def _clone_of(root: Path, origin: str) -> Path:
    """A real git repository with `origin` set, which is what `origin_of` asks git for."""
    root.mkdir()
    (root / "compose.yaml").write_text("services: {}\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", origin], check=True)
    return root


def test_discovery_finds_a_sibling_cloned_over_https_beside_one_over_ssh(tmp_path: Path) -> None:
    """The hub was cloned over SSH, the next checkout by the HTTPS line in `docs/operations.md`.

    Both are clones of one repository, and discovery compared the two URLs as strings, so the
    second was silently left out of every run -- the one failure an upgrade across checkouts
    exists to prevent, since that checkout then trails the schema the others migrate to.
    """
    hub_path = _clone_of(tmp_path / "issuebot", "git@github.com:jleavers/issuebot.git")
    sibling = _clone_of(tmp_path / "issuebot-myrepo", "https://github.com/jleavers/issuebot.git")
    stranger = _clone_of(tmp_path / "other", "https://github.com/jleavers/issuebot-fork.git")

    found = upgrade.discover(hub_path)

    assert sibling in found
    assert stranger not in found


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:jleavers/issuebot.git",
        "git@github.com:jleavers/issuebot",
        "ssh://git@github.com/jleavers/issuebot.git",
        "ssh://git@github.com:22/jleavers/issuebot.git",
        "https://github.com/jleavers/issuebot.git",
        "https://github.com/jleavers/issuebot",
        "https://github.com/jleavers/issuebot/",
        "https://x-access-token@github.com/jleavers/issuebot.git",
        "https://github.com/JLeavers/IssueBot.git",
        "git@GitHub.com:jleavers/issuebot.git",
    ],
)
def test_every_spelling_of_one_repository_is_the_same_repository(url: str) -> None:
    assert upgrade.repository_of(url) == upgrade.repository_of(
        "git@github.com:jleavers/issuebot.git"
    )


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:jleavers/issuebot-fork.git",
        "https://github.com/someone-else/issuebot.git",
        "https://gitlab.com/jleavers/issuebot.git",
    ],
)
def test_a_different_repository_is_not_the_same_repository(url: str) -> None:
    assert upgrade.repository_of(url) != upgrade.repository_of(
        "git@github.com:jleavers/issuebot.git"
    )


def test_the_table_sizes_its_columns_to_their_content() -> None:
    """A branch name is as long as whoever named it, and this is most often run from a long one."""
    branch = "issuebot/a-long-feature-branch"
    header, _rule, row = upgrade.format_table([hub("issuebot", branch=branch)])

    assert header.index("branch") == row.index(branch)
    assert header.index("services") == row.index("db egress web worker")


def test_a_checkout_without_a_db_service_is_not_the_hub() -> None:
    assert not checkout().is_hub
    assert hub().is_hub


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"Service":"worker","State":"running","Status":"Up 2 minutes"}', 1),
        ("", 0),
        ("not json at all", 0),
    ],
)
def test_parse_ps_is_total(text: str, expected: int) -> None:
    """The tool reports health; a container listing it cannot parse must not end the run."""
    assert len(upgrade.parse_ps(text)) == expected


def test_parse_ps_reads_the_fields_it_prints() -> None:
    text = (
        '{"Service":"db","State":"running","Status":"Up 6 days (healthy)"}\n'
        '{"Service":"worker","State":"restarting","Status":"Restarting (1)"}\n'
    )

    assert upgrade.parse_ps(text) == [
        ("db", "running", "Up 6 days (healthy)"),
        ("worker", "restarting", "Restarting (1)"),
    ]


def test_mixed_branches_are_reported_but_do_not_stop_the_run() -> None:
    """A deployment may pin one worker deliberately; the schema hazard is named, not enforced."""
    printed: list[str] = []
    run = FakeRun()
    code = upgrade.upgrade(
        [hub(), checkout("issuebot-two", branch="release", upstream="origin/release")],
        context(run, out=printed.append, skip_validate=True, health_wait=0),
    )

    assert code == 0
    assert any("branch" in line for line in printed)
