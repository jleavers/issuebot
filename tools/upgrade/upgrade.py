"""Pull, rebuild and restart every issuebot checkout on this host, one phase at a time.

One database and one dashboard serve every repository, and each repository gets its own worker
in its own checkout (`docs/operations.md`, "More than one repository"). So an upgrade is not a
loop over one checkout repeated: the checkouts are clones of the same repository against one
store, and `migrate.py` refuses to start when the recorded schema version is newer than the code
it is running. Finishing one checkout before starting the next is therefore how a *stale* worker
ends up in a restart loop -- the newest code migrates, and every worker still on the old image
fails its next start. Every phase here runs across every checkout before the next phase begins,
and the hub goes first within each, so the database is up and the newest schema applied before
another worker tries.

Nothing here needs telling which checkout is the hub. Each declares its own role through
`COMPOSE_PROFILES` in its environment file, and `docker compose build` and `up -d` are
profile-aware, so the hub builds `web` and `worker` beside `db` and every other checkout builds
`egress` and `worker`, with no flag from this tool. What it derives from `docker compose config
--services` is only the *order*: a `db` service means the hub, and the hub goes first.

Three things it will not do.

It will not pull over a checkout that is dirty, carries local commits or has no upstream: that is
a **phase-1** refusal, which ends the run before a single worker has been stopped. The hub
checkout is frequently the development checkout as well, and stopping three deployments to
discover the fourth problem is worse than not starting.

It will not start a checkout whose build or `validate` failed. That one is left stopped and named
in the summary while the others come up. A stopped worker is a visible, safe state; the same
worker started on the old image beside the others on the new one is the mixed-schema state this
whole shape exists to prevent.

It will not read a value out of an environment file. The drift report compares *key names*
between the example the new release ships and the file this checkout holds, so an operator learns
that `ISSUEBOT_EGRESS_ALLOW` is now expected without the database password or the Claude
credential passing through a line anyone prints. `tests/test_tools_upgrade.py` is what enforces
that rather than this paragraph.

Standard library only, and no `issuebot` import. This tool upgrades the checkout that contains
its own virtual environment, so running it through that environment would resolve dependencies it
is in the middle of replacing:

    python3 tools/upgrade/upgrade.py --dry-run

It is also Python rather than a shell script for a reason beyond `tools/` already being Python:
`bash` reads a script lazily as it executes, and this one rewrites its own file when it pulls.
Python compiles the whole module before running a line of it. The corollary is that a release
which changes this tool was upgraded by the *previous* version of it -- run it again if the
`tools/upgrade` directory moved in the diff.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# The checkout this file belongs to: tools/upgrade/upgrade.py -> the repository root.
HERE = Path(__file__).resolve().parents[2]

ENV_FILE = ".env"
EXAMPLE_FILE = ".env.example"
COMPOSE_FILE = "compose.yaml"

# How long to wait before the health report. The worker writes its first snapshot one
# `polling.interval_ms` in, 30 s by default, so a shorter wait reports "no snapshot" for a
# worker that is perfectly well.
DEFAULT_HEALTH_WAIT = 40

_ENV_KEY = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=", re.MULTILINE)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunResult:
    """What one subprocess did. Deliberately not `CompletedProcess`, so tests need no shape."""

    returncode: int
    stdout: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class Checkout:
    """One deployment directory, as inspection found it."""

    path: Path
    branch: str
    upstream: str | None
    dirty: bool
    ahead: int
    behind: int
    services: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def is_hub(self) -> bool:
        """The hub is the checkout carrying the database, and it is upgraded first."""
        return "db" in self.services

    @property
    def role(self) -> str:
        return "hub" if self.is_hub else "worker"


@dataclass
class Context:
    """Every edge the tool has on the world, so the phases above can be driven in a test."""

    run: Callable[..., RunResult]
    out: Callable[[str], None] = print
    read_text: Callable[[Path], str] = _read_text
    sleep: Callable[[float], None] = time.sleep
    log_dir: Path | None = None
    force: bool = False
    skip_validate: bool = False
    health_wait: int = DEFAULT_HEALTH_WAIT

    def log_for(self, checkout: Checkout) -> Path | None:
        if self.log_dir is None:
            return None
        return self.log_dir / f"{checkout.name}.log"


# ---------------------------------------------------------------------------
# The pure half
# ---------------------------------------------------------------------------
def env_keys(text: str) -> set[str]:
    """The names assigned in an environment file, and nothing else it holds.

    A commented line is not an assignment, so it does not appear; neither does any value.
    """
    return set(_ENV_KEY.findall(text))


def env_drift(example: str, actual: str) -> tuple[list[str], list[str]]:
    """`(in the example and not here, here and not in the example)`, both sorted key names."""
    theirs, ours = env_keys(example), env_keys(actual)
    return sorted(theirs - ours), sorted(ours - theirs)


def hub_first(checkouts: Iterable[Checkout]) -> list[Checkout]:
    """The hub, then the rest in the order given."""
    ordered = list(checkouts)
    return [c for c in ordered if c.is_hub] + [c for c in ordered if not c.is_hub]


def blocking_problems(checkout: Checkout) -> list[str]:
    """Why this checkout cannot be upgraded at all. Any one of these ends the whole run."""
    problems = []
    if checkout.upstream is None:
        problems.append(f"{checkout.branch} tracks no upstream branch")
    if checkout.dirty:
        problems.append("working tree is not clean")
    if checkout.ahead:
        problems.append(f"{checkout.ahead} local commit(s) not on {checkout.upstream}")
    return problems


def parse_ps(text: str) -> list[tuple[str, str, str]]:
    """`docker compose ps --format json` as `(service, state, status)`, total over its input.

    The tool reports health at the end of a successful upgrade; a listing it cannot parse must
    cost the report, never the run.
    """
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        entries = parsed if isinstance(parsed, list) else [parsed]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rows.append(
                (
                    str(entry.get("Service", "?")),
                    str(entry.get("State", "?")),
                    str(entry.get("Status", "")),
                )
            )
    return rows


def format_table(checkouts: Sequence[Checkout]) -> list[str]:
    """One row per checkout. Both variable columns size to their content, since a branch name is
    as long as whoever named it -- a fixed width there breaks the alignment on the very branch a
    developer is most likely to run this from."""
    name_width = max((len(c.name) for c in checkouts), default=8)
    name_width = max(name_width, len("checkout"))
    branch_width = max((len(c.branch) for c in checkouts), default=6)
    branch_width = max(branch_width, len("branch"))
    header = (
        f"  {'checkout':<{name_width}}  {'role':<6}  "
        f"{'branch':<{branch_width}}  {'behind':>6}  services"
    )
    lines = [header, "  " + "-" * (len(header) - 2)]
    for c in checkouts:
        services = " ".join(c.services)
        lines.append(
            f"  {c.name:<{name_width}}  {c.role:<6}  "
            f"{c.branch:<{branch_width}}  {c.behind:>6}  {services}"
        )
    return lines


# ---------------------------------------------------------------------------
# The phases
# ---------------------------------------------------------------------------
@dataclass
class _State:
    """Which checkouts are still in the run, and why the others left it."""

    failed: dict[str, str] = field(default_factory=dict)

    def fail(self, checkout: Checkout, reason: str) -> None:
        self.failed[checkout.name] = reason

    def live(self, checkouts: Sequence[Checkout]) -> list[Checkout]:
        return [c for c in checkouts if c.name not in self.failed]


def upgrade(checkouts: Sequence[Checkout], ctx: Context) -> int:
    """Run every phase across every checkout. Returns the process exit code."""
    ordered = hub_first(checkouts)

    # Phase 1 is a refusal, not a step: nothing has been stopped yet, and that is the point.
    blocked = [(c, problem) for c in ordered for problem in blocking_problems(c)]
    if blocked:
        for checkout, problem in blocked:
            ctx.out(f"  [fail] {checkout.name}: {problem}")
        ctx.out("")
        ctx.out("Aborted before anything was stopped: fix the checkouts named above.")
        return 1

    branches = {c.branch for c in ordered}
    if len(branches) > 1:
        ctx.out(
            f"  [warn] these checkouts are on different branches ({', '.join(sorted(branches))});"
        )
        ctx.out("         one database serves them all, so a schema applied by the newest code")
        ctx.out("         will stop an older worker starting.")

    if not ctx.force and all(c.behind == 0 for c in ordered):
        ctx.out("Everything is at its upstream; nothing to do (use --force to rebuild anyway).")
        return 0

    state = _State()

    _stop_workers(ordered, ctx, state)
    _pull(ordered, ctx, state)
    _report_env_drift(ordered, ctx, state)
    _build(ordered, ctx, state)
    if ctx.skip_validate:
        ctx.out("\n==> Validating (skipped)")
    else:
        _validate(ordered, ctx, state)
    started = _start(ordered, ctx, state)
    if started:
        _report_health(ordered, ctx, state)

    return _summarise(ordered, ctx, state)


def _compose(ctx: Context, checkout: Checkout, *args: str, quiet: bool = False) -> RunResult:
    return ctx.run(
        ["docker", "compose", *args], cwd=checkout.path, log=ctx.log_for(checkout), quiet=quiet
    )


def _git(ctx: Context, checkout: Checkout, *args: str, quiet: bool = True) -> RunResult:
    return ctx.run(["git", *args], cwd=checkout.path, log=ctx.log_for(checkout), quiet=quiet)


def _stop_workers(checkouts: Sequence[Checkout], ctx: Context, state: _State) -> None:
    """Only the worker. `db`, `web` and `egress` stay up, and `down` is never used.

    `./configs` is bind-mounted into the running worker and reloads live, so pulling underneath
    one hands it a `WORKFLOW.md` its image does not understand. `down` is worse than unnecessary:
    in the hub it would take the database away from every other checkout.
    """
    ctx.out("\n==> Stopping workers")
    for checkout in state.live(checkouts):
        if "worker" not in checkout.services:
            ctx.out(f"  {checkout.name}: no worker service, nothing to stop")
            continue
        if _compose(ctx, checkout, "stop", "worker").ok:
            ctx.out(f"  [ ok ] {checkout.name}: worker stopped")
        else:
            ctx.out(f"  [fail] {checkout.name}: could not stop the worker")
            state.fail(checkout, "stop failed")


def _pull(checkouts: Sequence[Checkout], ctx: Context, state: _State) -> None:
    ctx.out("\n==> Pulling")
    for checkout in state.live(checkouts):
        upstream = checkout.upstream or ""
        if _git(ctx, checkout, "merge", "--ff-only", upstream, quiet=False).ok:
            ctx.out(f"  [ ok ] {checkout.name}: at {upstream}")
        else:
            ctx.out(f"  [fail] {checkout.name}: git merge --ff-only {upstream} failed")
            state.fail(checkout, "pull failed")


def _report_env_drift(checkouts: Sequence[Checkout], ctx: Context, state: _State) -> None:
    """Key names only, from both sides. No value from either file reaches a printed line."""
    ctx.out("\n==> Environment keys")
    for checkout in state.live(checkouts):
        report_drift(checkout, ctx)


def report_drift(checkout: Checkout, ctx: Context) -> None:
    """Compare this checkout's environment file with the example its upstream ships.

    The example is read from the upstream ref rather than the working tree, so this says the same
    thing before the pull as after it -- which is the point of running it under `--dry-run`: the
    keys a release expects are worth knowing before committing to the upgrade.
    """
    if checkout.upstream is None:
        ctx.out(f"  [warn] {checkout.name}: no upstream to read {EXAMPLE_FILE} from")
        return

    shown = _git(ctx, checkout, "show", f"{checkout.upstream}:{EXAMPLE_FILE}")
    if not shown.ok:
        ctx.out(f"  [warn] {checkout.name}: no {EXAMPLE_FILE} on {checkout.upstream}")
        return

    actual = ctx.read_text(checkout.path / ENV_FILE)
    if not actual:
        ctx.out(f"  [warn] {checkout.name}: no {ENV_FILE} to compare")
        return

    missing, extra = env_drift(shown.stdout, actual)
    if missing:
        ctx.out(
            f"  [warn] {checkout.name}: key(s) in the new example, not set here: "
            f"{' '.join(missing)}"
        )
    if extra:
        ctx.out(f"  {checkout.name}: set here, not in the new example: {' '.join(extra)}")
    if not missing and not extra:
        ctx.out(f"  [ ok ] {checkout.name}: keys match the new example")


def _build(checkouts: Sequence[Checkout], ctx: Context, state: _State) -> None:
    ctx.out("\n==> Building")
    for checkout in state.live(checkouts):
        ctx.out(f"\n  {checkout.name}")
        if _compose(ctx, checkout, "build").ok:
            ctx.out(f"  [ ok ] {checkout.name}: built")
        else:
            ctx.out(f"  [fail] {checkout.name}: build failed")
            state.fail(checkout, "build failed")


def _validate(checkouts: Sequence[Checkout], ctx: Context, state: _State) -> None:
    """Catch a configuration the new image rejects before the live worker is replaced by one
    that will not start -- a setting a newer `WORKFLOW.md` introduces reads as
    `<key>: Extra inputs are not permitted` against a stale image.

    It also fails on a lapsed Claude credential, which is a real problem but not this upgrade's;
    `--skip-validate` restarts the worker anyway.
    """
    ctx.out("\n==> Validating")
    for checkout in state.live(checkouts):
        ctx.out(f"\n  {checkout.name}")
        if _compose(ctx, checkout, "run", "--rm", "worker", "validate").ok:
            ctx.out(f"  [ ok ] {checkout.name}: valid")
        else:
            ctx.out(f"  [fail] {checkout.name}: validate failed -- not restarting this worker")
            state.fail(checkout, "validate failed")


def _start(checkouts: Sequence[Checkout], ctx: Context, state: _State) -> int:
    ctx.out("\n==> Starting")
    started = 0
    for checkout in hub_first(checkouts):
        if checkout.name in state.failed:
            ctx.out(
                f"  [warn] {checkout.name}: skipped ({state.failed[checkout.name]})"
                " -- its worker is left stopped"
            )
            continue
        if _compose(ctx, checkout, "up", "-d").ok:
            ctx.out(f"  [ ok ] {checkout.name}: up")
            started += 1
        else:
            ctx.out(f"  [fail] {checkout.name}: docker compose up -d failed")
            state.fail(checkout, "up failed")
    return started


def _report_health(checkouts: Sequence[Checkout], ctx: Context, state: _State) -> None:
    ctx.out("\n==> Health")
    if ctx.health_wait > 0:
        ctx.out(f"  waiting {ctx.health_wait}s for the workers to settle and write a snapshot...")
        ctx.sleep(ctx.health_wait)

    for checkout in state.live(checkouts):
        ctx.out(f"\n  {checkout.name}")
        listing = _compose(ctx, checkout, "ps", "--all", "--format", "json", quiet=True)
        for service, service_state, status in parse_ps(listing.stdout):
            ctx.out(f"    {service:<10} {service_state:<12} {status}")
            if service_state == "restarting":
                ctx.out(f"    [fail] {service} is restarting -- docker compose logs {service}")

        # The worker's own snapshot: the config verdict and any dispatch hold, which is where a
        # lapsed credential or an unreadable account registry shows up.
        snapshot = _compose(ctx, checkout, "run", "--rm", "worker", "status", quiet=True)
        if snapshot.ok:
            for line in snapshot.stdout.splitlines():
                ctx.out(f"    {line}")
        else:
            ctx.out(f"    [warn] {checkout.name}: could not read the worker's status snapshot")


def _summarise(checkouts: Sequence[Checkout], ctx: Context, state: _State) -> int:
    ctx.out("\n==> Summary")
    code = 0
    for checkout in hub_first(checkouts):
        reason = state.failed.get(checkout.name)
        if reason:
            ctx.out(f"  [fail] {checkout.name:<24} {checkout.role:<6} {reason}")
            code = 1
        else:
            ctx.out(f"  [ ok ] {checkout.name:<24} {checkout.role:<6} upgraded")
    if ctx.log_dir is not None:
        ctx.out(f"  Logs: {ctx.log_dir}")
    return code


# ---------------------------------------------------------------------------
# The edges: subprocesses, discovery, inspection
# ---------------------------------------------------------------------------
def run_command(
    argv: Sequence[str], *, cwd: Path, log: Path | None = None, quiet: bool = False
) -> RunResult:
    """Run one command in a checkout, echoing it unless the caller means to parse the output.

    Everything is appended to that checkout's log whether it was echoed or not, so a build that
    scrolled past is still there afterwards.
    """
    process = subprocess.run(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        check=False,
    )
    if log is not None:
        try:
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"$ {' '.join(argv)}\n{process.stdout}\n")
        except OSError:
            pass
    if not quiet and process.stdout:
        for line in process.stdout.splitlines():
            print(f"    {line}")
    return RunResult(returncode=process.returncode, stdout=process.stdout)


def looks_like_a_checkout(path: Path) -> bool:
    return (path / COMPOSE_FILE).is_file() and (path / ".git").exists()


def origin_of(path: Path) -> str | None:
    result = run_command(["git", "remote", "get-url", "origin"], cwd=path, quiet=True)
    return result.stdout.strip() if result.ok else None


def discover(start: Path) -> list[Path]:
    """This checkout, plus every sibling directory that is a checkout of the same repository.

    `docs/operations.md` adds a repository by cloning issuebot again, so siblings are where the
    other deployments are. Pass paths explicitly for any other arrangement.
    """
    found = [start]
    origin = origin_of(start)
    if origin is None:
        return found
    for sibling in sorted(start.parent.iterdir()):
        if sibling == start or not sibling.is_dir():
            continue
        if looks_like_a_checkout(sibling) and origin_of(sibling) == origin:
            found.append(sibling)
    return found


def inspect_checkout(path: Path) -> tuple[Checkout | None, str | None]:
    """Fetch, then read everything the phases need. `(checkout, None)` or `(None, why not)`."""
    if not path.is_dir():
        return None, f"no such directory: {path}"
    if not (path / COMPOSE_FILE).is_file():
        return None, f"no {COMPOSE_FILE} in {path}"
    if not (path / ".git").exists():
        return None, "not a git checkout"

    if not run_command(["git", "fetch", "--quiet", "origin"], cwd=path, quiet=True).ok:
        return None, "git fetch origin failed"

    branch = run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path, quiet=True
    ).stdout.strip()

    tracked = run_command(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=path,
        quiet=True,
    )
    upstream = tracked.stdout.strip() if tracked.ok else None

    dirty = bool(run_command(["git", "status", "--porcelain"], cwd=path, quiet=True).stdout.strip())

    ahead = behind = 0
    if upstream:
        counts = run_command(
            ["git", "rev-list", "--left-right", "--count", f"{upstream}...HEAD"],
            cwd=path,
            quiet=True,
        )
        parts = counts.stdout.split()
        if counts.ok and len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])

    services = run_command(["docker", "compose", "config", "--services"], cwd=path, quiet=True)
    if not services.ok:
        return None, "docker compose config --services failed"

    return (
        Checkout(
            path=path,
            branch=branch or "?",
            upstream=upstream,
            dirty=dirty,
            ahead=ahead,
            behind=behind,
            services=tuple(sorted(services.stdout.split())),
        ),
        None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="upgrade.py",
        description=(
            "Pull, rebuild and restart every issuebot deployment on this host, "
            "one phase at a time across all of them."
        ),
    )
    parser.add_argument(
        "checkout",
        nargs="*",
        help="checkout directories (default: this one and every sibling clone of it)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what each checkout would do, and change nothing",
    )
    parser.add_argument(
        "--force", action="store_true", help="run even when every checkout is already current"
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="do not run `compose run --rm worker validate` before restarting",
    )
    parser.add_argument(
        "--health-wait",
        type=int,
        default=DEFAULT_HEALTH_WAIT,
        metavar="N",
        help=f"seconds to wait before the health report (default {DEFAULT_HEALTH_WAIT})",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="where to write one log per checkout (default: a timestamped directory under "
        "~/.cache/issuebot-upgrade)",
    )
    args = parser.parse_args(argv)

    paths = [Path(p).expanduser().resolve() for p in args.checkout] or discover(HERE)

    print(f"==> Inspecting {len(paths)} checkout(s)")
    checkouts: list[Checkout] = []
    refused = False
    for path in paths:
        checkout, problem = inspect_checkout(path)
        if checkout is None:
            print(f"  [fail] {path.name}: {problem}")
            refused = True
            continue
        checkouts.append(checkout)

    if not checkouts:
        print("  no checkouts to upgrade")
        return 1

    print("")
    for line in format_table(hub_first(checkouts)):
        print(line)

    if refused:
        print("\nAborted before anything was stopped: fix the checkouts named above.")
        return 1

    log_dir = args.log_dir
    if log_dir is None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        log_dir = Path.home() / ".cache" / "issuebot-upgrade" / stamp
    if not args.dry_run:
        log_dir.mkdir(parents=True, exist_ok=True)

    ctx = Context(
        run=run_command,
        log_dir=None if args.dry_run else log_dir,
        force=args.force,
        skip_validate=args.skip_validate,
        health_wait=args.health_wait,
    )

    if args.dry_run:
        # A dry run that showed the table and said nothing about a dirty checkout would report a
        # run that is ready when the real one would refuse before stopping anything.
        blocked = [(c, problem) for c in hub_first(checkouts) for problem in blocking_problems(c)]
        if blocked:
            print("")
            for checkout, problem in blocked:
                print(f"  [fail] {checkout.name}: {problem}")
            print("  a real run would stop here, before any worker was stopped")

        print("\n==> Environment keys (against the example on each upstream)")
        for checkout in hub_first(checkouts):
            report_drift(checkout, ctx)
        pending = sum(c.behind for c in checkouts)
        print("")
        if pending:
            print(f"Dry run: {pending} commit(s) to pull across {len(checkouts)} checkout(s).")
        else:
            print("Dry run: everything is at its upstream; nothing to deploy.")
        return 0

    print(f"  Logs: {log_dir}")
    return upgrade(checkouts, ctx)


if __name__ == "__main__":
    sys.exit(main())
