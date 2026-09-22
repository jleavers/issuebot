"""Show what a running session is doing, from the turn log it is still writing.

The store answers "is it alive and roughly where" -- ``issuebot status`` and the dashboard both
read the worker's snapshot, which carries ``turns``, ``last_event`` and ``last_activity_at`` per
running issue. Neither can answer "doing what", because ``run_turns`` is written in the
``run_ended`` transaction: until the run finishes there is no transcript in the database, and a
turn can last an hour.

What does exist meanwhile is ``.issuebot/runs/<run_id>/turn-N.jsonl``, which is ``claude``'s
stdout tee'd byte for byte as the turn goes. This reads its tail and renders the tool calls and
the agent's own text, so a long turn is legible while it is still running.

**Everything printed goes through the scrubber**, and that is the point rather than a courtesy.
``capture_turns`` is the one scrubbing step for those files (#79) precisely because issuebot put
``GH_TOKEN`` into that process's environment and the file is not scrubbed on disk -- so a reader
that went straight to the bytes would be a second exit for the credential, past the step that
exists to stop exactly that. This is that reader, so it scrubs: the deployment's own secrets when
the workflow loads (``Scrubber.for_deployment``, which is what ``cli`` builds), and the
credential shapes alone otherwise. The header says which, because the difference is what a
deployment's own ``github.token`` and DSN password get masked by.

Read-only throughout. It runs ``ls``, ``tail`` and ``cat`` in the worker's container and writes
nothing anywhere, so it is safe against a live session -- which is the only kind worth watching.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from issuebot.agent.scrub import DEFAULT_SCRUBBER, Scrubber
from issuebot.config import ConfigError, load_workflow

# The tail read by default. These files reach tens of megabytes on a long turn, and what is
# wanted is the end of one; `--bytes` is the dial when more history is.
DEFAULT_TAIL_BYTES = 400_000
# How long each rendered line may be. A `Bash` command or a `Write` body is unbounded, and this
# is a terminal.
DETAIL_WIDTH = 110
DEFAULT_LINES = 20
DEFAULT_INTERVAL_S = 10.0
# Where compose mounts the workspaces volume inside the worker, and the image's own default.
DEFAULT_WORKSPACES = "/workspaces"
DEFAULT_SERVICE = "worker"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class WatchError(RuntimeError):
    """Something the operator can act on: no workspace, no run, no container."""


@dataclass(frozen=True, slots=True)
class Source:
    """Where the workspaces live, and how to run a read-only command against them.

    Two deployments and one interface. Under compose the tree is inside the worker's container
    and on a volume, so every read goes through ``docker compose exec``; on the host route
    (``agent.run_as`` unset, a developer running ``issuebot worker`` directly) it is an ordinary
    directory and ``--local`` reads it in place.
    """

    workspaces: str
    service: str | None
    project_directory: Path

    def shell(self, command: str) -> str:
        argv: list[str]
        if self.service is None:
            argv = ["sh", "-c", command]
        else:
            argv = [
                "docker",
                "compose",
                "--project-directory",
                str(self.project_directory),
                "exec",
                "-T",
                self.service,
                "sh",
                "-c",
                command,
            ]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        except FileNotFoundError as exc:
            raise WatchError(f"cannot run {argv[0]}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise WatchError(f"{argv[0]} did not answer within 30 s") from exc
        if done.returncode != 0:
            detail = (done.stderr or "").strip().splitlines()
            raise WatchError(detail[-1] if detail else f"{argv[0]} exited {done.returncode}")
        return done.stdout


def find_workspace(source: Source, issue: int) -> str:
    """The workspace directory for this issue number, by the suffix every key carries.

    A workspace key is ``<repository name>-<number>`` (``Issue.identifier``), so the repository
    half differs per deployment and only the number is known here. Two matches is a question for
    the operator rather than a guess: ``--workspace`` answers it.
    """
    listing = source.shell(f"ls -1d {shlex.quote(source.workspaces)}/*-{issue} 2>/dev/null || true")
    matches = [line.strip() for line in listing.splitlines() if line.strip()]
    if not matches:
        raise WatchError(
            f"no workspace for issue #{issue} under {source.workspaces}; "
            "the session may not have claimed it yet, or the workspace has been removed"
        )
    if len(matches) > 1:
        joined = ", ".join(matches)
        raise WatchError(f"several workspaces match issue #{issue}: {joined}; pass --workspace")
    return matches[0]


def latest_run(source: Source, workspace: str) -> str:
    """The newest run directory in this workspace, which is the running one if any is.

    Newest by name rather than by mtime: a run id starts with a UTC stamp precisely so that it
    sorts, and an mtime is whatever the filesystem last recorded.
    """
    runs = f"{workspace}/.issuebot/runs"
    listing = source.shell(f"ls -1 {shlex.quote(runs)} 2>/dev/null || true")
    ids = sorted(line.strip() for line in listing.splitlines() if line.strip())
    if not ids:
        raise WatchError(f"no runs under {runs}; the session has not started a turn yet")
    return ids[-1]


def latest_turn(source: Source, log_dir: str) -> tuple[str, int]:
    """The highest-numbered ``turn-N.jsonl`` in a run directory, and its size."""
    listing = source.shell(
        f"ls -1 {shlex.quote(log_dir)}/turn-*.jsonl 2>/dev/null | sort -V | tail -1"
    )
    path = listing.strip()
    if not path:
        raise WatchError(f"no turn log under {log_dir} yet")
    size = source.shell(f"wc -c < {shlex.quote(path)}").strip()
    return path, int(size) if size.isdigit() else 0


def read_tail(source: Source, path: str, limit: int) -> str:
    return source.shell(f"tail -c {limit} {shlex.quote(path)}")


@dataclass(frozen=True, slots=True)
class Line:
    kind: str
    detail: str


def _clip(text: str, width: int = DETAIL_WIDTH) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _tool_detail(name: str, payload: object) -> str:
    """The one field of a tool call worth a line: what it is about to do.

    A tool's input is its own shape and free to change, so this reads the names claude's own
    tools use and falls back to the whole document rather than asserting any of them exist.
    """
    if not isinstance(payload, dict):
        return _clip(str(payload))
    for key in ("description", "command", "file_path", "pattern", "path", "query", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value)
    return _clip(json.dumps(payload))


def decode(text: str, scrubber: Scrubber) -> Iterator[Line]:
    """Render the interesting blocks of a stream-json tail, scrubbed.

    Total, like every other reader of these files: the tail starts mid-line and the shape is
    claude's, so anything that will not parse is counted rather than raised. The scrub happens
    here, once, on the way out -- no caller can reach the raw bytes through this function.
    """
    unparseable = 0
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except ValueError:
            # The first line of a `tail -c` read is normally a partial one; the rest would be
            # claude having written something this does not know about.
            unparseable += 1
            continue
        if not isinstance(message, dict):
            unparseable += 1
            continue
        yield from _render(message, scrubber)
    if unparseable:
        yield Line("~", f"{unparseable} line(s) not parsed (the tail starts mid-line)")


def _render(message: dict[str, object], scrubber: Scrubber) -> Iterator[Line]:
    kind = message.get("type")
    if kind == "system" and message.get("subtype") == "init":
        model = message.get("model")
        session = message.get("session_id")
        yield Line("init", scrubber.scrub(f"model={model} session={session}"))
        return
    if kind == "rate_limit_event":
        info = message.get("rate_limit_info")
        if isinstance(info, dict) and info.get("status") != "allowed":
            # A refusal only. `allowed` arrives every few tool calls and says nothing about
            # what the session is doing, where this line is the turn about to fail on the
            # account's window rather than on anything this issue did (`usage_limited`). How
            # full the windows are is the dashboard's limits tile, which is built for it.
            yield Line("limit", f"status={info.get('status')} resets_at={info.get('resetsAt')}")
        return
    if kind == "result":
        subtype = message.get("subtype")
        errored = bool(message.get("is_error"))
        text = message.get("result")
        detail = f"subtype={subtype} is_error={errored}"
        if isinstance(text, str) and text.strip():
            detail = f"{detail} {_clip(scrubber.scrub(text))}"
        yield Line("result", detail)
        return
    content = message.get("message")
    blocks = content.get("content") if isinstance(content, dict) else None
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_use":
            name = str(block.get("name") or "tool")
            yield Line(name, scrubber.scrub(_tool_detail(name, block.get("input"))))
        elif block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                yield Line("text", _clip(scrubber.scrub(text)))
        elif block_type == "thinking":
            yield Line("think", "(thinking)")


def build_scrubber(workflow: Path | None) -> tuple[Scrubber, str]:
    """The deployment's scrubber where a workflow loads, and the shapes alone otherwise.

    Never a failure: this is a viewer, and a workflow that will not load is a reason to mask
    less rather than to refuse to show anything. The caller prints which one was built, because
    the difference is whether this deployment's own `github.token` and DSN password are masked
    by value as well as by shape.
    """
    if workflow is None or not workflow.exists():
        return DEFAULT_SCRUBBER, "credential shapes only (no workflow loaded)"
    try:
        loaded = load_workflow(workflow)
    except (ConfigError, OSError) as exc:
        return DEFAULT_SCRUBBER, f"credential shapes only ({type(exc).__name__} loading workflow)"
    return Scrubber.for_deployment(loaded.config, os.environ), f"deployment ({workflow})"


def show(source: Source, args: argparse.Namespace, scrubber: Scrubber) -> str:
    workspace = args.workspace or find_workspace(source, args.issue)
    run_id = args.run_id or latest_run(source, workspace)
    log_dir = f"{workspace}/.issuebot/runs/{run_id}"
    path, size = latest_turn(source, log_dir)
    tail = read_tail(source, path, args.bytes)
    lines = list(decode(tail, scrubber))
    turn = Path(path).name
    print(f"  {workspace}  run {run_id}  {turn}  {size:,} bytes")
    if not lines:
        print("  (nothing rendered from the tail; try a larger --bytes)")
    for line in lines[-args.lines :]:
        print(f"  {line.kind:<14} {line.detail}")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="watch.py",
        description="Show what a running session is doing, from its live turn log.",
    )
    parser.add_argument("issue", type=int, help="the issue number the session is working on")
    parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE,
        help=f"compose service holding the workspaces (default: {DEFAULT_SERVICE})",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="read the workspaces directly rather than through docker compose",
    )
    parser.add_argument(
        "--workspaces",
        default=DEFAULT_WORKSPACES,
        help=f"workspace root as the reader sees it (default: {DEFAULT_WORKSPACES})",
    )
    parser.add_argument(
        "--project-directory",
        type=Path,
        default=REPO_ROOT,
        help=(
            "the checkout compose reads its environment file from (default: this one). A git "
            "worktree carries no such file of its own, so point this at the deployment's "
            "checkout when watching from one, or compose refuses to interpolate before it "
            "ever reaches the container."
        ),
    )
    parser.add_argument("--workspace", help="the workspace directory, when the guess is wrong")
    parser.add_argument("--run-id", help="a run other than the newest one")
    parser.add_argument(
        "--bytes",
        type=int,
        default=DEFAULT_TAIL_BYTES,
        help=f"how much of the turn log's tail to read (default: {DEFAULT_TAIL_BYTES})",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=DEFAULT_LINES,
        help=f"how many rendered lines to print (default: {DEFAULT_LINES})",
    )
    parser.add_argument("--follow", action="store_true", help="keep printing until interrupted")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help=f"seconds between reads with --follow (default: {DEFAULT_INTERVAL_S})",
    )
    parser.add_argument(
        "--workflow",
        type=Path,
        default=REPO_ROOT / "configs" / "WORKFLOW.md",
        help="the workflow whose secrets to mask by value (default: ./configs/WORKFLOW.md)",
    )
    args = parser.parse_args(argv)

    scrubber, described = build_scrubber(args.workflow)
    source = Source(
        workspaces=args.workspaces,
        service=None if args.local else args.service,
        project_directory=args.project_directory,
    )
    print(f"  scrubbing: {described}")
    try:
        show(source, args, scrubber)
        while args.follow:
            time.sleep(args.interval)
            print()
            show(source, args, scrubber)
    except WatchError as exc:
        print(f"  [FAIL] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
