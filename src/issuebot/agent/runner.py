"""The claude -p subprocess boundary: argv, environment, stream-json parsing and timeouts."""

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import SecretStr

from issuebot.agent.accounts import session_account
from issuebot.agent.boundary import ENV_FILE, TURN_STDERR, Boundary, BoundaryError, split_parts
from issuebot.agent.errors import AgentErrorCategory
from issuebot.agent.runas import CLAUDE_HOME_DIR, RunAs, Spawn
from issuebot.agent.scrub import DEFAULT_SCRUBBER, Scrubber
from issuebot.agent.uvcache import UV_CACHE_ENV, ensure_uv_cache_dir
from issuebot.config import Settings
from issuebot.egress import PROXY_ENV_NAMES
from issuebot.log import get_logger

# The oldest claude carrying `--permission-prompts none`, the flag that makes an
# unattended headless run possible. A compatibility floor `validate` enforces against
# whatever claude is on PATH, not the version the image ships: raise it only when the
# code starts depending on something newer.
MIN_CLAUDE_VERSION: tuple[int, int, int] = (2, 1, 259)
# `claude --version` and `claude auth status` are quick; past this they are treated as unanswered.
CLAUDE_PROBE_TIMEOUT_S = 10
STREAM_LINE_LIMIT = 10 * 1024 * 1024
TERMINATE_GRACE_S = 10.0
# `PROXY_ENV_NAMES` is the third property of the session's authority, after its tools and its
# token (#126): under compose the session's container has no route off the host except the
# allow-listing proxy these name, and `claude`, `gh`, `git`, `uv`, `pip`, `npm` and `curl` all
# read them. Passed through rather than fixed here, because the address is the deployment's
# (compose sets it) and the host route has none -- where the absence is what `validate` warns
# about rather than something this allow-list could supply.
PASSTHROUGH_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "TERM",
        *PROXY_ENV_NAMES,
    }
)
PASSTHROUGH_PREFIXES: tuple[str, ...] = ("ANTHROPIC_", "CLAUDE_", "GIT_AUTHOR_", "GIT_COMMITTER_")
FIXED_ENVIRONMENT: dict[str, str] = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "GH_PAGER": "cat",
    "DISABLE_AUTOUPDATER": "1",
    # Auto memory (`~/.claude/projects/<project>/memory/`) is read whatever `--setting-sources`
    # says and keyed by repository, so under the shared session home (#101) one issue's notes
    # would be the next session's system prompt on the same repository. Off at the source; the
    # sweep in `runas.py` clears what an older image or a session's own hand wrote there.
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}
# Hooks hand the agent variables by writing them here, inside the workspace: `agent_environment`
# is an allow-list, so a DSN a `before_run` shell exports dies with that shell.
WORKSPACE_ENV_PATH = (".issuebot", "env")
# Enough for any plausible set of variables, and a bound on a hook that redirects a log here
# by accident: the file is re-read for every turn and every hook. The bound is the boundary's,
# applied to the bytes read, not to a string built after the whole file was read (#104).
WORKSPACE_ENV_LIMIT = ENV_FILE.limit
# What the file may not take out from under a running turn. A hook writes it, but it lives in
# the agent's own workspace, so the session can write it too -- which is why the line is drawn
# at the tooling issuebot launches rather than at "a hook would not do that". `PATH`, `HOME`,
# `GH_TOKEN` and the fixed entries keep `gh` and `claude` running; the prefixes are the ones
# `agent_environment` passes through to configure `claude` itself, and the file's job is to add
# what the target repository's tests need, not to re-point or re-credential the agent for its
# next turn. Everything else the agent could already do from inside the workspace anyway.
# The proxy variables are here for the same reason `PATH` is, and for no stronger one: what
# bounds egress is the container's lack of a route, not a variable the session could rewrite,
# so a hook that emptied them would take `gh`, `git` and the next turn's `claude` off the
# network rather than let anything off the allow-list (#126).
# The tool-config entries below are the same rule one step in (#171): `PATH` decides *which*
# binary `git` and `gh` are, and these decide what that binary does and which further commands
# it runs; the shell entries beside them are the same rule again for `bash`, which is the
# process every hook and the post-clone setup *is* (#179); and the loader entries below them are
# the same rule one layer under all of those (#187), since `ld.so` reads its own names out of the
# same environment before any of those binaries reaches `main`. The file outlives the session -- a
# workspace belongs to one issue -- so what such a line re-points is the next session on that
# issue. It is also the environment spelling of what `TOOL_CONFIG_SWEEP` (`runas.py`, #151 and
# #173) and `SHELL_STARTUP_SWEEP` (#137) remove from the account's home: a sweep of
# `~/.gitconfig`, of `~/.config/gh/config.yml` or of `~/.profile` would leave a guarantee
# conditional on a variable nothing checked.
#   The names are the rungs of git's and gh's own documented precedence chains that fall
#   outside the two prefixes below. That is the rule, and it is checkable against
#   `git-var(1)`, `git-commit(1)` and `gh environment` rather than being a list of everything
#   that might name a command -- which matters, because protecting the head of a chain and
#   leaving its tail closes nothing. Each chain, with the protected head first:
#     editor    GIT_EDITOR / GH_EDITOR -> core.editor -> VISUAL -> EDITOR
#     pager     GIT_PAGER / GH_PAGER   -> core.pager  -> PAGER
#     browser   GH_BROWSER                            -> BROWSER
#     askpass   GIT_ASKPASS            -> core.askPass -> SSH_ASKPASS (SSH_ASKPASS_REQUIRE)
#     identity  GIT_AUTHOR_EMAIL       -> user.email  -> EMAIL
#     token     GH_TOKEN               -> GITHUB_TOKEN; GH_ENTERPRISE_TOKEN ->
#               GITHUB_ENTERPRISE_TOKEN
#   The config rung of each is swept out of the home by `TOOL_CONFIG_SWEEP` -- git's and
#   ssh's by #151, and gh's `config.yml`, which carries the `editor`, `pager` and `browser`
#   rungs of the gh chains above, by #173 -- so the environment rungs are the whole of what is
#   left. `EDITOR` was measured firing on a plain `git commit` with no `TERM` set at all, and
#   `EMAIL` setting the author of a commit; `PAGER` needs a terminal, which a hook may well
#   have.
#   `XDG_CONFIG_HOME` is not on any chain: it moves the config directory of everything
#   following the base-directory specification, `$XDG_CONFIG_HOME/gh/config.yml` among them,
#   whose aliases may be shell commands -- and which, at the path the sweep names, #173 now
#   removes, so this name is what keeps it from being read somewhere else instead.
#   Names and not prefixes (`SSH_`, `GITHUB_`, `EDITOR`...), because `SSH_AUTH_SOCK` is a
#   legitimate route for exactly the deploy-key case this bound has to leave a hook author, and
#   `GITHUB_`/generic namespaces hold plenty a hook may hand over. A chain has an end, so this
#   list has one too.
#   The two XDG roots are on no chain, and they are here under a rule of their own (#171,
#   #191): a base directory is protected when a tool issuebot launches resolves through it
#   something it will execute or read as configuration. Measured against the two tools this
#   list is drawn for -- `gh` 2.100.0 and git 2.47.3; `claude` reads XDG names too and is the
#   note's residual -- two of the specification's roots are:
#     XDG_CONFIG_HOME  the config directory of everything following the specification,
#                      `$XDG_CONFIG_HOME/gh/config.yml` among them, whose aliases may be shell
#                      commands, and `$XDG_CONFIG_HOME/git/config`, which names commands too.
#     XDG_DATA_HOME    `$XDG_DATA_HOME/gh/extensions`, the directory `gh` dispatches
#                      `gh <name>` from -- a program it *runs*, one step past a setting that
#                      names one, and the environment spelling of the same directory in the
#                      account's home, which is #186's question. It moves that lookup
#                      wholesale and hides the home's own extensions with it, so leaving it
#                      would make any sweep there conditional on a variable nothing checked,
#                      the way #171 stood to #151. No `GH_` name
#                      reaches it -- `GH_CONFIG_DIR` moves the config directory alone, and
#                      `gh` dispatches from no `PATH` -- so the prefix below does not cover it.
#   `XDG_` is not a prefix here either, and for a reason of its own: `GIT_` and `GH_` are
#   prefixes because they are those tools' own namespaces and the tools add to them, where
#   `XDG_` is a specification's -- its roots are a short fixed list, seven in the current
#   version, and what changes is which of them a tool reads: a measurement, rather than a
#   manual to keep up with. `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR`,
#   `XDG_CONFIG_DIRS` and `XDG_DATA_DIRS` were each measured unread by those two and stay
#   out: a hook pointing a cache or a state directory somewhere is what this file is for.
TOOL_CONFIG_ENV_NAMES: frozenset[str] = frozenset(
    {
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
        "EDITOR",
        "VISUAL",
        "PAGER",
        "BROWSER",
        "EMAIL",
        "GITHUB_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    }
)
# `GIT_` and `GH_` whole, rather than the handful of names the issue started from. An
# enumeration here is one somebody has to keep complete against those tools' own manuals, and
# two drafts of this list were not. `GIT_CONFIG_GLOBAL` and `GIT_SSH_COMMAND` name a command,
# but so do `GIT_EDITOR` (on a plain `git commit`, which a session runs constantly),
# `GIT_SEQUENCE_EDITOR`, `GIT_PAGER`, `GIT_ASKPASS`, `GIT_PROXY_COMMAND`, `GIT_SSH`,
# `GIT_EXEC_PATH` (the directory `git <subcommand>` is looked up in) and `GIT_TEMPLATE_DIR`
# (the hooks copied into the next repository `git init` creates); `GIT_CONFIG_COUNT` with
# `GIT_CONFIG_KEY_<n>` and `GIT_CONFIG_VALUE_<n>` sets `alias.x = !...` with no file at all;
# and `GIT_DIR` and `GIT_WORK_TREE` re-point which repository is being operated on. On the `gh`
# side, `GH_CONFIG_DIR` takes precedence over `$XDG_CONFIG_HOME/gh` for the same aliases, and
# `GH_EDITOR`/`GH_BROWSER` name commands, while `GH_PAGER` was already fixed and protected --
# an asymmetry with no reason behind it. The prefixes cover all of them and whatever either
# tool adds next, which is the only version of this that stays true.
# The cost, stated where the code is: `GIT_AUTHOR_`/`GIT_COMMITTER_` are caught too, but
# `.issuebot/env` was never the deployment's channel for them -- they are set in `.env`, reach
# the worker's own environment and are inherited through `PASSTHROUGH_PREFIXES` exactly as
# before, and refusing the *session-writable file* from re-pointing the identity a pull
# request's commits carry is worth having on its own account. Behaviour-only switches with no
# config equivalent (`GIT_TERMINAL_PROMPT`, `GIT_TRACE*`, `GIT_LFS_SKIP_SMUDGE`) are caught as
# well, and for those the hook has its own shell around the git it runs, or a derived image.
# Otherwise a hook that needs a setting has `git config --local` in the clone, `git -c`, and a
# root-owned `/etc/gitconfig` or `/etc/ssh/ssh_config` for a deployment-wide one; what it may
# not do is hand the variable to the session.
TOOL_CONFIG_ENV_PREFIXES: tuple[str, ...] = ("GIT_", "GH_")
# The same rule one tool further out again (#179), and the one tool every script issuebot runs
# for a session goes through: `WorkspaceManager.hook_shell` is `bash -lc`, which opens the
# post-clone setup and all four hooks. These are the variables `bash` itself reads out of the
# environment it is handed, before or around the commands the hook actually wrote -- the
# environment spelling of the shell start-up files `SHELL_STARTUP_SWEEP` (`runas.py`, #137)
# removes from the account's home, where a sweep of `~/.profile` would otherwise leave a
# guarantee conditional on a variable nothing checked. Measured, each of them, against the
# image's own `bash` 5.2:
#   BASH_ENV    the file a non-interactive `bash` sources before the command it was given:
#               `BASH_ENV=<script> bash -lc 'echo hook-ran'` runs the script first. #137's
#               channel exactly, in one line of a file the session can write.
#   SHELLOPTS   `set -o` options enabled from the environment before any start-up file is read,
#               `xtrace` among them (`BASHOPTS` is the `shopt` half of the same thing). No
#               command of its own, and here for what it turns on:
#   PS4         expanded before every traced command once `xtrace` is on, command substitution
#               and all -- the first of them inside `/etc/profile`, long before the hook's own
#               script. `SHELLOPTS=xtrace` with `PS4='$(...)'` was measured running the
#               substitution. It takes the pair to run anything -- `PS4` is inert without
#               `xtrace`, and `xtrace` with the default `PS4` only prints -- so both are here.
#   CDPATH      `PATH`'s rule for directories: `cd sub` in a hook resolves through it, so a
#               line here sends the hook into a tree of the last session's choosing and the
#               relative command after the `cd` is that tree's file. `PATH` is protected for
#               this reason and `cd` is the one lookup it does not cover.
# `ENV` is *not* here: it is POSIX's start-up file for an *interactive* shell, and nothing
# issuebot runs is interactive. Measured unread by `bash -lc`, by `bash --posix -c`, by `bash`
# invoked as `sh`, and by `sh -c` (dash) -- so it is a name that would close nothing, and the
# rule this list states is what was shown to work.
# The cost is as close to nothing as a protection gets: nothing in the tree sets any of these,
# and a hook that wants a file sourced before its own commands has `source` in the script it
# already owns, `set -x` for a trace and an absolute path for a `cd`. What it may not do is
# hand the *next* session's shell the variable.
SHELL_ENV_NAMES: frozenset[str] = frozenset(
    {
        "BASH_ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "CDPATH",
    }
)
# The same rule one layer *below* every tool rather than one tool further out (#187). `PATH`
# decides which binary `git` is, `CDPATH` which directory a hook's `cd` finds, and these decide
# which shared objects the dynamic loader maps into that binary before its `main` runs -- and
# into `bash`, `git` and the `claude` child alike, since the loader reads them out of whatever
# environment the process was handed. (`gh` is the exception and was checked rather than
# assumed: it is a static Go binary in this image, so no loader runs for it. `bash`, `git` and
# `claude` are all ELF-dynamic here, which is enough to make the reach argument carry.)
#   This is the decision #171 and #179 both deferred. Each of those notes filed `LD_PRELOAD`
#   under "variables of *other* tooling", beside `NODE_OPTIONS` and `PYTHONSTARTUP` -- and that
#   filing was wrong, because those are variables of a tool a hook *chooses* to run, while the
#   loader's names reach every dynamically linked program the merged environment is handed to.
#   Measured, each of them, against the image's own glibc 2.41 -- with `libmemusage.so`, which
#   ships with glibc and prints at exit, standing in for the prebuilt object the issue
#   describes, since the default image carries no compiler:
#   LD_PRELOAD  objects mapped ahead of all others into every dynamically linked program, their
#               ELF constructors run before `main`. `LD_PRELOAD=...libmemusage.so bash -lc 'echo
#               hook-ran'` printed the object's output before the hook's own.
#   LD_AUDIT    the rtld-audit interface, loaded earlier still. Measured running the named
#               object's constructors *even when it is not a valid audit module* -- so "it must
#               implement `la_version`" is no kind of bound on what it may run.
#   LD_LIBRARY_PATH
#               the directories a `DT_NEEDED` soname is resolved through, ahead of the system
#               ones. It names no object, which is the whole of the case for treating it
#               differently -- and that case does not survive the measurement: a file planted at
#               `libpcre2-8.so.0` in a directory of the line's choosing was what `git` loaded,
#               and its constructor ran inside `git`, with no `LD_PRELOAD` anywhere. It is
#               `PATH`'s rule one layer down, which is exactly why `PATH` and `CDPATH` are here.
#   LD_DEBUG    the same, by a second spelling, and the one that is easiest to miss: *any* value
#               containing `help` (`LD_DEBUG=help`, `LD_DEBUG=libs,help`) makes the loader print
#               its option list and exit 0 without entering `main`, to *stdout* -- so it
#               displaces whatever a hook's stdout was being read for as well. Measured voiding
#               `bash -lc 'echo hook-ran'`, `git rev-parse` and `claude --version`. Every other
#               value is inert (`libs`, `all` and `unused` were measured leaving `git --version`
#               working), which is exactly why this one is easy to certify as safe by measuring
#               the wrong value -- the first draft of this list did.
#   LD_TRACE_LOADED_OBJECTS
#               not a way to run code but a way to run *none*: the loader prints the object's
#               dependencies and exits 0 without entering `main`. Measured voiding
#               `git rev-parse` (exit 0, a library list instead of an answer), `claude
#               --version`, and `bash -lc 'echo hook-ran'`, whose `echo` never ran while the
#               shell still reported success. That is the half of this list's rule that `PATH`,
#               `HOME` and the fixed entries already serve -- a line here must not take the
#               tooling down in the middle of a run -- and it fails *silently*, which is one of
#               only two ways a protected name in `.issuebot/env` can: `LD_DEBUG` below is the
#               other, and nothing else in the file is either.
# The rule is *what makes the dynamic loader load an object of the value's choosing into every
# dynamically linked program, or not run one at all*, checkable against `ld.so(8)`'s ENVIRONMENT
# section and finite. Names and not an `LD_` prefix, and the counter-example is decisive:
# `LD_RUN_PATH` is binutils `ld`'s link-time default for `-rpath`, so it is the very route a
# hook is told to use instead of `LD_LIBRARY_PATH` below, and a prefix would refuse the
# recommended workaround. `LD_BIND_NOW`, `LD_DYNAMIC_WEAK`, `LD_PROFILE` and `GLIBC_TUNABLES`
# stay out too: each was measured leaving `git --version` working, and none of them names an
# object the loader would not otherwise have loaded. So does `LD_SHOW_AUXV`, which is the one
# exclusion that touches a stream issuebot reads: it prints the auxiliary vector to stdout and
# *then* runs the command, at exit 0, so it loads nothing and denies nothing -- which is the
# whole difference from the two above -- and the noise it leaves ahead of a hook's output is
# something that hook's own `echo` could add, while `StreamParser` counts a non-JSON line and
# carries on.
# `LD_DEBUG_OUTPUT` stays out as well, for a reason worth stating since `LD_DEBUG` is in: it
# only redirects what `LD_DEBUG` asks for and is inert on its own, measured leaving
# `git --version` working with no `LD_DEBUG` set.
# The cost is not zero, and it is `LD_LIBRARY_PATH`'s alone: a target repository's
# `after_create` may legitimately build against a library in a private prefix whose tests the
# *agent's* turn then runs, which is a hand-over and so exactly what this refuses. The routes
# that remain are in `docs/toolchains.md`: a `RUNPATH` baked at link time (`-Wl,-rpath`, or
# `LD_RUN_PATH`, which is unprotected), an `/etc/ld.so.conf.d` entry with `ldconfig` in an image
# built `FROM` this one -- root's, outside the session's reach, the route already given for
# `/etc/gitconfig` -- and the variable in the hook's own shell around the command the hook
# itself runs, which is unchanged. A built artefact that needs a library at run time has
# `RUNPATH` for exactly that; `LD_LIBRARY_PATH` is the override you reach for while testing one.
LOADER_ENV_NAMES: frozenset[str] = frozenset(
    {
        "LD_PRELOAD",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_TRACE_LOADED_OBJECTS",
        "LD_DEBUG",
    }
)
PROTECTED_ENV_NAMES: frozenset[str] = frozenset(
    {
        "GH_TOKEN",
        "PATH",
        "HOME",
        *FIXED_ENVIRONMENT,
        *PROXY_ENV_NAMES,
        *TOOL_CONFIG_ENV_NAMES,
        *SHELL_ENV_NAMES,
        *LOADER_ENV_NAMES,
    }
)
PROTECTED_ENV_PREFIXES: tuple[str, ...] = ("ANTHROPIC_", "CLAUDE_", *TOOL_CONFIG_ENV_PREFIXES)
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MESSAGE_LIMIT = 500
STDERR_TAIL_LIMIT = 64 * 1024
_LOGGED_ARG_LENGTH = 120
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

TurnEventKind = Literal[
    "session_started",
    "rate_limits",
    "turn_activity",
    "turn_completed",
    "turn_failed",
    "turn_timeout",
    "process_exit",
]


def agent_environment(
    environ: Mapping[str, str], *, token: SecretStr | None, uv_cache: Path | None = None
) -> dict[str, str]:
    """The minimal environment the agent child and every hook see.

    ``uv_cache`` is the one thing here the worker *computes* rather than passes through or
    fixes (#164): the cache directory belongs to the session account and sits under
    ``workspace.root``, so neither the allow-list above nor a constant could carry it. It joins
    the environment the way ``GH_TOKEN`` does, and ``None`` -- the host route, an image with no
    uv, a directory that could not be made -- leaves uv's own default alone.
    """
    env = {
        name: value
        for name, value in environ.items()
        if name in PASSTHROUGH_NAMES or name.startswith(PASSTHROUGH_PREFIXES)
    }
    env.update(FIXED_ENVIRONMENT)
    if token is not None:
        env["GH_TOKEN"] = token.get_secret_value()
    if uv_cache is not None:
        env[UV_CACHE_ENV] = str(uv_cache)
    return env


def parse_workspace_env(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse ``KEY=VALUE`` lines into a mapping and a list of complaints about the rest.

    One assignment per line, an optional ``export `` prefix stripped, blank lines and ``#``
    comments skipped. The line's own leading and trailing whitespace goes -- so a here-doc may
    indent and a CRLF file parses -- and whatever is left after the first ``=`` is the value:
    no quote stripping and no ``$VAR`` expansion, because a hook that wants either has a shell.
    A complaint names the line number and nothing else -- the text before a missing ``=`` can be
    most of a DSN, password included.
    """
    env: dict[str, str] = {}
    warnings: list[str] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            warnings.append(f"line {number}: not KEY=VALUE")
        elif not _ENV_KEY.fullmatch(key):
            warnings.append(f"line {number}: not a variable name")
        elif "\x00" in value:
            # An environment cannot hold one, and `create_subprocess_exec` raises `ValueError`
            # rather than `OSError` for it, which would escape the turn loop entirely.
            warnings.append(f"line {number}: the value has a null byte in it")
        else:
            env[key] = value
    return env, warnings


def merge_workspace_env(
    base: Mapping[str, str], extra: Mapping[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Layer `extra` over `base`, refusing the names a turn needs to stay alive."""
    merged = dict(base)
    refused: list[str] = []
    for key, value in extra.items():
        if key in PROTECTED_ENV_NAMES or key.startswith(PROTECTED_ENV_PREFIXES):
            refused.append(key)
        else:
            merged[key] = value
    return merged, refused


def read_workspace_env(
    workspace: Path, *, boundary: Boundary | None = None
) -> tuple[dict[str, str], list[str]]:
    """Read ``<workspace>/.issuebot/env``. No file is the normal case and costs nothing.

    The read goes through the boundary (#104): the file sits in a directory the session can
    write, so a FIFO, a device, a directory or a symbolic link at the name is refused before
    a byte is read -- a FIFO would otherwise block the event loop for every session, and a
    link would read whatever file the worker's uid can reach back into the session's
    environment -- and at most ``WORKSPACE_ENV_LIMIT`` bytes are taken however large it is.
    """
    boundary = boundary or Boundary.current()
    try:
        read = boundary.read(workspace, WORKSPACE_ENV_PATH, ENV_FILE)
    except FileNotFoundError:
        return {}, []
    except BoundaryError as exc:
        return {}, [f"refused {exc.filename}: {exc.reason}"]
    except OSError as exc:
        # A hook's problem, and the hook's own failure is what `before_run` already reports.
        return {}, [f"cannot read {workspace.joinpath(*WORKSPACE_ENV_PATH)}: {exc}"]
    text = read.data.decode("utf-8", errors="replace")
    if not read.truncated:
        return parse_workspace_env(text)
    # Cut at a line boundary, so the last variable kept is one a hook finished writing.
    head, _, _ = text.rpartition("\n")
    env, warnings = parse_workspace_env(head)
    return env, [f"longer than {WORKSPACE_ENV_LIMIT} bytes: the rest was ignored", *warnings]


def workspace_environment(
    base: Mapping[str, str], workspace: Path, *, boundary: Boundary | None = None
) -> tuple[dict[str, str], list[str]]:
    """`base` with the workspace's env file layered over it, plus the keys that took effect.

    Everything it could not use is a warning, never a failure: the file is read fresh for every
    turn and every hook, so a bad line must not be the thing that ends a run.
    """
    extra, warnings = read_workspace_env(workspace, boundary=boundary)
    merged, refused = merge_workspace_env(base, extra)
    log = get_logger(__name__)
    for reason in warnings:
        log.warning("workspace_env_ignored", workspace=str(workspace), reason=reason)
    for key in refused:
        log.warning("workspace_env_ignored", workspace=str(workspace), reason=f"{key} is protected")
    applied = [key for key in extra if key not in refused]
    if applied:
        # The keys, never the values: one of them is usually a DSN with a password in it.
        log.debug("workspace_env_applied", workspace=str(workspace), keys=applied)
    return merged, applied


def parse_claude_version(text: str | None) -> tuple[int, int, int] | None:
    """``2.1.259 (Claude Code)`` -> ``(2, 1, 259)``; ``None`` when nothing parses."""
    if not text:
        return None
    match = _VERSION.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def claude_auth_status(
    command: str, environ: Mapping[str, str], *, run_as: str | None = None
) -> str | None:
    """Run ``<command> auth status --json`` and return its stdout, or None when it cannot run.

    The probe runs under the same filtered environment ``ClaudeRunner`` gives the agent, and
    as the same account when ``agent.run_as`` is set (#75): the login lives in that account's
    home, so it answers "can the agent authenticate", not "can this shell".
    """
    argv = [command, "auth", "status", "--json"]
    env = agent_environment(environ, token=None)
    try:
        if run_as is not None:
            completed = RunAs(run_as).run(argv, env, timeout=CLAUDE_PROBE_TIMEOUT_S)
        else:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=CLAUDE_PROBE_TIMEOUT_S,
                check=False,
                env=env,
            )
    except OSError, subprocess.TimeoutExpired:
        return None
    return completed.stdout or None


ClaudeAuthVerdict = Literal["ok", "ambiguous", "unreadable", "logged_out"]
# Which kind of credential the agent spends: a Claude subscription (a claude.ai login or
# the OAuth token, both of which have usage windows and no per-token charge), an API key
# (billed per token, no windows), or an answer too unclear to label either way.
Credential = Literal["subscription", "api_key", "unknown"]


@dataclass(frozen=True, slots=True)
class ClaudeAuth:
    """What ``claude auth status --json`` said, reduced to a verdict, a line of detail and
    which kind of credential the agent will spend.

    ``logged_out`` is the definite answer; ``ambiguous`` is a login with an API key also set;
    ``unreadable`` means the probe gave no usable answer, which each caller decides how to treat.

    ``credential`` is what the dashboard labels cost by. Only a definite answer names one: an
    ``ambiguous`` probe is exactly the case where issuebot declines to guess which credential is
    used (``validate`` says as much), so it is ``unknown`` rather than a coin toss.
    """

    verdict: ClaudeAuthVerdict
    detail: str
    credential: Credential = "unknown"


def describe_claude_auth(output: str | None) -> ClaudeAuth:
    """The auth line ``validate`` prints and the worker's startup checks."""
    status = _parse_auth_status(output)
    if status is None:
        reason = "no output" if not output else f"unparseable output {output.strip()[:40]!r}"
        return ClaudeAuth("unreadable", f"could not read auth status ({reason})")
    if not status.get("loggedIn"):
        detail = (
            "not logged in; set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), "
            "or run claude auth login on the host"
        )
        return ClaudeAuth("logged_out", detail)
    method = _auth_method_text(status)
    source = status.get("apiKeySource")
    if source and status.get("authMethod") != "api_key":
        detail = (
            f"logged in ({method}) with {source} also set; "
            "unset one to be sure which credential is used"
        )
        return ClaudeAuth("ambiguous", detail)
    credential: Credential = "api_key" if status.get("authMethod") == "api_key" else "subscription"
    return ClaudeAuth("ok", f"logged in ({method})", credential)


def _parse_auth_status(output: str | None) -> dict[str, Any] | None:
    """``claude auth status --json`` stdout as a mapping; None when it is not one."""
    if not output:
        return None
    try:
        parsed = json.loads(output)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _auth_method_text(status: Mapping[str, Any]) -> str:
    """How the agent authenticates, as the check prints it.

    ``claude auth status`` names the Claude Code login after the site it came from, so
    ``claude.ai`` is passed through as-is; the two variable-borne credentials are named after
    the variable that carries them, which is what a reader has to go and change.
    """
    method = status.get("authMethod")
    if method == "api_key":
        source = status.get("apiKeySource")
        return f"API key from {source}" if source else "API key"
    if method == "oauth_token":
        return "CLAUDE_CODE_OAUTH_TOKEN"
    subscription = status.get("subscriptionType")
    return f"{method}, {subscription}" if subscription else str(method)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True, slots=True)
class RateLimitWindow:
    """One of the account's usage windows: how much of it is spent, and when it rolls over."""

    utilization: float  # 0.0 to 1.0, as claude reports it
    resets_at: datetime


@dataclass(frozen=True, kw_only=True, slots=True)
class RateLimits:
    """A `rate_limit_event` reading. About the account, not the issue whose turn saw it."""

    five_hour: RateLimitWindow | None
    seven_day: RateLimitWindow | None
    observed_at: datetime


def parse_rate_limits(message: dict[str, Any], *, at: datetime) -> RateLimits | None:
    """Read the windows out of a `rate_limit_event` line, or return None.

    The line's shape is claude's, undocumented and free to change, and a worker must not fall
    over because a field moved. So this is total: anything it cannot read is no reading at all,
    which the dashboard already has to handle for a worker that has run nothing yet.
    """
    info = message.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    windows = info.get("unifiedWindows")
    if not isinstance(windows, dict):
        return None
    five_hour = _rate_limit_window(windows.get("five_hour"))
    seven_day = _rate_limit_window(windows.get("seven_day"))
    if five_hour is None and seven_day is None:
        return None
    return RateLimits(five_hour=five_hour, seven_day=seven_day, observed_at=at)


def _rate_limit_window(value: object) -> RateLimitWindow | None:
    if not isinstance(value, dict):
        return None
    utilization = _number(value.get("utilization"))
    resets = _number(value.get("resetsAt"))
    if utilization is None or resets is None:
        return None
    try:
        resets_at = datetime.fromtimestamp(resets, tz=UTC)
    except OSError, OverflowError, ValueError:
        return None
    # A share of a window cannot be outside 0..1, and the tile renders it as a bar's width.
    return RateLimitWindow(utilization=min(max(utilization, 0.0), 1.0), resets_at=resets_at)


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnEvent:
    """A runtime event of one turn, reported to the observer and the log, never to the bus."""

    kind: TurnEventKind
    turn_number: int
    at: datetime = field(default_factory=_utcnow)
    session_id: str | None = None
    message_type: str | None = None
    tool_name: str | None = None
    detail: str | None = None
    rate_limits: RateLimits | None = None


class TurnObserver(Protocol):
    def on_turn_event(self, event: TurnEvent) -> None: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class TurnResult:
    turn_number: int
    session_id: str | None
    model: str | None
    api_key_source: str | None
    exit_code: int | None
    subtype: str | None
    is_error: bool
    num_turns: int
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

    @property
    def ok(self) -> bool:
        return self.error_category is None

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


class TurnRunner(Protocol):
    """What the session needs from a runner; ``ClaudeRunner`` satisfies it, tests stub it."""

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
        deadline: float | None = None,
    ) -> TurnResult: ...


class StreamParser:
    """Consumes stream-json lines, remembers init and result, and reports activity."""

    def __init__(self, *, turn_number: int, expected_session_id: str) -> None:
        self.turn_number = turn_number
        self.expected_session_id = expected_session_id
        self.session_id: str | None = None
        self.model: str | None = None
        self.api_key_source: str | None = None
        self.result: dict[str, Any] | None = None
        self.rate_limits: RateLimits | None = None
        self.unparseable = 0
        self._log = get_logger(__name__)

    def feed(self, line: str) -> list[TurnEvent]:
        text = line.strip()
        if not text:
            return []
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            message = None
        if not isinstance(message, dict):
            self.unparseable += 1
            self._log.warning(
                "claude_stream_unparseable", turn_number=self.turn_number, length=len(text)
            )
            return [self._activity("unparseable")]
        kind = message.get("type")
        if kind == "system" and message.get("subtype") == "init":
            return [self._init(message)]
        if kind == "assistant":
            return self._assistant(message)
        if kind == "result":
            self.result = message
            return []
        if kind == "rate_limit_event":
            return [self._rate_limits(message)]
        return [self._activity(str(kind) if kind is not None else "unknown")]

    def overrun(self) -> TurnEvent:
        """Account for a line the stream reader dropped because it exceeded the limit."""
        self.unparseable += 1
        return self._activity("unparseable")

    def _init(self, message: dict[str, Any]) -> TurnEvent:
        self.session_id = _string(message.get("session_id"))
        self.model = _string(message.get("model"))
        self.api_key_source = _string(message.get("apiKeySource"))
        if self.session_id != self.expected_session_id:
            self._log.warning(
                "claude_session_id_mismatch",
                expected=self.expected_session_id,
                actual=self.session_id,
            )
        return TurnEvent(
            kind="session_started",
            turn_number=self.turn_number,
            session_id=self.session_id,
            detail=self.model,
        )

    def _assistant(self, message: dict[str, Any]) -> list[TurnEvent]:
        inner = message.get("message")
        content = inner.get("content") if isinstance(inner, dict) else None
        blocks = content if isinstance(content, list) else []
        tools = [
            _string(block.get("name"))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if not tools:
            return [self._activity("assistant")]
        return [self._activity("assistant", tool_name=name) for name in tools]

    def _rate_limits(self, message: dict[str, Any]) -> TurnEvent:
        limits = parse_rate_limits(message, at=_utcnow())
        if limits is None:
            return self._activity("rate_limit_event")
        self.rate_limits = limits
        return TurnEvent(
            kind="rate_limits",
            turn_number=self.turn_number,
            session_id=self.session_id,
            message_type="rate_limit_event",
            rate_limits=limits,
        )

    def _activity(self, message_type: str, *, tool_name: str | None = None) -> TurnEvent:
        return TurnEvent(
            kind="turn_activity",
            turn_number=self.turn_number,
            session_id=self.session_id,
            message_type=message_type,
            tool_name=tool_name,
        )


# What a lapsed or revoked Claude credential says, lowercased. These are claude's own words,
# from its stderr or from a failing result, never the agent's prose (see classify_result).
AUTH_FAILURE_MARKERS: tuple[str, ...] = (
    "authentication_error",
    "authentication failed",
    "failed to authenticate",
    "invalid api key",
    "invalid x-api-key",
    "invalid_api_key",
    "invalid bearer token",
    "please run /login",
    "claude auth login",
)
# The OAuth family says the same thing too many ways to list ("has expired", "is invalid",
# "was revoked", ...), so a token word and a verdict word co-occurring is the marker. What
# lapses is not always spelled "token": a login whose refresh is refused reports the *session*
# gone ("OAuth session expired and could not be refreshed", the live wording on 2026-09-14).
AUTH_TOKEN_WORDS: tuple[str, ...] = (
    "oauth token",
    "oauth session",
    "bearer token",
    "access token",
)
AUTH_VERDICT_WORDS: tuple[str, ...] = ("expire", "invalid", "revoke", "unauthorized")


def is_auth_failure(*texts: str | None) -> bool:
    """True when any text carries a marker of a credential claude could not authenticate with."""
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if any(marker in lowered for marker in AUTH_FAILURE_MARKERS):
            return True
        if any(word in lowered for word in AUTH_TOKEN_WORDS) and any(
            word in lowered for word in AUTH_VERDICT_WORDS
        ):
            return True
    return False


def classify_result(
    result: dict[str, Any] | None,
    exit_code: int | None,
    stderr_tail: str,
    *,
    scrubber: Scrubber = DEFAULT_SCRUBBER,
) -> tuple[AgentErrorCategory | None, str | None]:
    """Map the final result (or its absence) and the exit code to a failure category.

    ``stderr_tail`` is the end of the turn's stderr. Every failure reads it for a credential
    problem (#20), and reports its last line; a result's own text is read for one only when
    claude itself failed, because a "success" result normally carries the agent's final
    message, which may discuss API keys without one having failed.

    "Claude itself failed" is the subtype saying so *or* a non-zero exit status, not the
    subtype alone: a login whose refresh is refused arrives as ``subtype: "success"`` with
    ``is_error`` and status 1, carrying claude's own sentence rather than the agent's (a live
    worker, 2026-09-14). Reading only the subtype made ``auth_failed`` unreachable for the
    commonest lapse there is, so every issue on the board burned ``max_attempts`` and the
    dispatch hold that should have parked it never engaged. The agent's own final message is
    still never mined for markers: that is the status-0 case, and it stays ``turn_failed``.

    The message is built from claude's own words, and it leaves the workspace without passing
    ``capture_turns`` (#91): it becomes the run's ``error``, which reaches the ``events`` and
    ``runs`` tables, Slack and the blocked-escape workpad block. So both parts go through
    ``scrubber`` here, and *before* the ``_MESSAGE_LIMIT`` cut, since a cut that lands inside
    a credential would leave a fragment the shapes no longer recognise.
    """
    stderr_line = _capped(scrubber.scrub(_last_line(stderr_tail)))
    auth = is_auth_failure(stderr_tail)
    if result is None:
        message = f"claude exited with status {exit_code} before reporting a result"
        category: AgentErrorCategory = "auth_failed" if auth else "process_exit"
        return category, _with_tail(message, stderr_line)
    subtype = _string(result.get("subtype")) or ""
    is_error = bool(result.get("is_error"))
    text = _capped(scrubber.scrub(_result_text(result)))
    if subtype == "error_max_budget_usd":
        return "budget_exceeded", text or "claude stopped at the --max-budget-usd cap"
    if is_error or subtype != "success":
        if (subtype != "success" or exit_code != 0) and is_auth_failure(text):
            auth = True
        return (
            "auth_failed" if auth else "turn_failed",
            _with_tail(subtype or "unknown subtype", text),
        )
    if exit_code != 0:
        message = f"claude reported success but exited with status {exit_code}"
        return "auth_failed" if auth else "process_exit", _with_tail(message, stderr_line)
    return None, None


def _with_tail(message: str, tail: str) -> str:
    return f"{message}: {tail}" if tail else message


def _result_text(result: dict[str, Any]) -> str:
    """The result's own text, uncapped: the cap runs after the scrub (see ``classify_result``)."""
    text = _string(result.get("result"))
    if not text:
        errors = result.get("errors")
        if isinstance(errors, list):
            text = "; ".join(str(item) for item in errors)
    return text or ""


def _capped(text: str) -> str:
    return text[:_MESSAGE_LIMIT]


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def settings_with_model(settings: Settings, model: str | None) -> Settings:
    """Settings running ``claude`` with ``model``; the same object when nothing changes."""
    if model == settings.claude.model:
        return settings
    claude = settings.claude.model_copy(update={"model": model})
    return settings.model_copy(update={"claude": claude})


def settings_for_labels(settings: Settings, labels: Sequence[str]) -> Settings:
    """Settings with ``claude.model`` replaced by the one an issue's model label names.

    Exactly one distinct model wins; no match or a disagreement keeps ``claude.model``.
    """
    mapping = settings.claude.model_labels
    if not mapping:
        return settings
    carried = {name.lower() for name in labels}
    models = {model for name, model in mapping.items() if name.lower() in carried}
    if len(models) != 1:
        if models:
            get_logger(__name__).warning(
                "model_labels_ambiguous", models=sorted(models), model=settings.claude.model
            )
        return settings
    return settings_with_model(settings, models.pop())


# The CLAUDE.md allow-list (#135, the same spec as `--strict-mcp-config` below).
#
# `claude` reads `projects.<git root of the cwd>.hasClaudeMdExternalIncludesApproved` out of
# the session account's `~/.claude.json` and, where it is true, lets a Project or Local
# `CLAUDE.md` -- and a `.claude/CLAUDE.md`, and a `.claude/rules` file, and anything any of
# them `@`-includes -- read a path from *outside* the clone. That file is the surface #119
# closed for `mcpServers`: it sits in `$HOME` beside `.claude/`, outside the directory the home
# sweep walks, and a `-p` session can write the key itself, so an approval is one the next
# session at that workspace path inherits.
#
# `claudeMdExcludes` is claude's own answer and it is reachable from the command line, which is
# what makes this the flag-shaped closure #119 preferred to clearing a key out of claude's own
# file: measured, a `.claude/settings.json` in the clone setting it back to `[]` does not win
# against the argv. It is matched against absolute paths, and only for the `User`, `Project`
# and `Local` memory types -- never `Managed` -- so an operator's root-owned policy CLAUDE.md
# is outside it, as it is outside the session's privilege domain.
CLAUDE_MD_EXCLUDES_KEY = "claudeMdExcludes"
# What a path cannot carry and still be a literal arm of the pattern below. `,` and `{}` would
# change the arms, the rest would make the arm a glob of its own.
_GLOB_METACHARACTERS = frozenset("*?[]{}(),!\\\n")


def claude_md_allowlist(*, trees: Sequence[Path] = (), files: Sequence[Path] = ()) -> str | None:
    """The ``--settings`` document keeping CLAUDE.md and its ``@`` includes to these paths.

    "Keeping", not "confining": claude matches the exclusion against the path as written and
    only then resolves it, so a symlink among these paths is still followed out of them. That
    is measured and recorded in the spec, and it is why the argument is a narrowing rather
    than a boundary.

    ``trees`` are allowed with everything under them, ``files`` exactly. The split is the
    point: the workspace is a tree, because a clone's instruction files may include anything
    in the clone, while the session account's config directory is *not* -- only
    ``CLAUDE.md`` and ``rules/`` in it are user memory (measured against the loader:
    ``dQ("User")`` and ``age()``), and the rest of that directory is the account's
    credential, its other sessions' transcripts and its caches. Allowing the tree would
    leave the fence around the most valuable target in the home.

    One *negated* pattern carrying every arm in one brace, and deliberately not one negation
    per arm: picomatch matches a list when **any** pattern matches, so ``!a/**`` and ``!b/**``
    would each match everything outside their own arm and OR together to "exclude everything"
    -- every instruction file, the clone's and the user's alike. Braced, the negation is
    evaluated once against the union, which makes this the shape ``--strict-mcp-config`` is: it
    names what survives rather than what is removed, so a path nobody thought of is excluded by
    default instead of being a hole until someone notices.

    ``None`` when there is nothing to allow, or when an arm cannot be spelled: a relative path
    (``claudeMdExcludes`` is matched against absolute paths, so a relative arm would match
    nothing and the negation would then match *everything*), or a brace, comma or glob
    metacharacter, which would silently change what the pattern means. The failure that matters
    is not "too little is excluded" but "everything is", which costs the session every
    instruction file it should have loaded.
    """
    arms = [f"{tree}/**" for tree in trees] + [str(file) for file in files]
    paths = [*trees, *files]
    if not arms or any(not path.is_absolute() for path in paths):
        return None
    if any(_GLOB_METACHARACTERS & set(str(path)) for path in paths):
        return None
    allowed = arms[0] if len(arms) == 1 else "{" + ",".join(arms) + "}"
    return json.dumps({CLAUDE_MD_EXCLUDES_KEY: [f"!{allowed}"]})


class ClaudeRunner:
    """Builds and runs one ``claude -p`` process per turn."""

    def __init__(self, settings: Settings, *, environ: Mapping[str, str] | None = None) -> None:
        self._claude = settings.claude
        self._token = settings.github.token
        self._root = settings.workspace.root.resolve()
        self._environ = dict(os.environ if environ is None else environ)
        self._timeout_s = settings.claude.turn_timeout_ms / 1000
        self._run_timeout_s = settings.agent.run_timeout_ms / 1000
        # The runner is built from the settings that hold the token and under the environment
        # the scrubber reads, so it owns the scrubber and masks at the source (#91): every
        # ``TurnResult.error`` and ``result_text``, and every ``TurnEvent.detail``, rather than
        # each sink they reach.
        self._scrubber = Scrubber.for_deployment(settings, self._environ)
        # The account every turn runs as (#75), or None for the worker's own uid. One
        # account: a pool has been narrowed to this workspace's bound member above (#121).
        account = session_account(settings)
        self._account = account
        self._runas = RunAs(account) if account else None
        # The worker's side of the line the session writes across (#104): every read of the
        # workspace's env file and of the turn files goes through it. Its session uid is that
        # one account's, so under a pool the boundary accepts the workspace's bound member and
        # no other session's uid (#121).
        self._boundary = Boundary.current(account)
        self._log = get_logger(__name__)

    def _prepared(
        self, argv: Sequence[str], env: Mapping[str, str]
    ) -> contextlib.AbstractContextManager[Spawn]:
        """The spawn for ``argv``: through the uid change when there is one, else as given."""
        if self._runas is not None:
            return self._runas.prepared(argv, env)
        return contextlib.nullcontext(Spawn(argv=list(argv), env=dict(env), pass_fds=()))

    def build_argv(self, *, session_id: str, resume: bool, workspace: Path) -> list[str]:
        cfg = self._claude
        argv = [
            cfg.command,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            cfg.permission_mode,
            "--permission-prompts",
            "none",
            # Unconditional, like `--permission-prompts none`, and for the same reason (#119,
            # #109): it is what makes the run safe, not what makes it convenient, so no
            # setting turns it off. `claude` otherwise loads `mcpServers` out of the session
            # account's `~/.claude.json` -- which lives in $HOME beside `.claude/`, outside
            # the directory the home sweep walks, is recreated per container and persists
            # across every
            # session in one -- and offers the planted server's tools to the next session. It
            # also drops a target repository's `.mcp.json`, and any MCP location a later
            # `claude` adds, since the flag names what is kept rather than what is removed:
            # only `--mcp-config` servers survive it, and `claude.mcp_config` below, the front
            # matter's and empty by default, is the one place they are named. The session's
            # tool set is therefore what this argv says. The rest of the account home's config
            # surfaces are a separate question, the per-turn sweep's (#101).
            "--strict-mcp-config",
            "--max-budget-usd",
            str(cfg.max_budget_usd),
            # Always passed (#107): claude's own default would load the clone's CLAUDE.md,
            # .claude/ and .mcp.json as configuration, and the setting is never empty.
            "--setting-sources",
            ",".join(cfg.setting_sources),
        ]
        # Unconditional, for the reason `--strict-mcp-config` above is (#135). Not narrowed to
        # `claude.setting_sources` naming the clone, although that is the only source the
        # approval key gates: under the shipped `[user]` the argument still bounds the *user*
        # CLAUDE.md's own `@` includes, which claude loads from outside the home whatever the
        # key says, and which nothing else bounds on the host route -- `sweep_agent_home`
        # returns early there, because the home is the operator's own. A bound and not a
        # boundary: a symlink among the allowed paths is still followed out of them, which the
        # spec measures and records.
        trees, files = self._claude_md_allowed(workspace)
        allowlist = claude_md_allowlist(trees=trees, files=files)
        if allowlist is not None:
            argv += ["--settings", allowlist]
        elif trees or files:
            # The other way `claude_md_allowlist` declines: a path it cannot spell as an arm.
            # `_claude_md_allowed` has already said its piece when it returned nothing at all.
            self._log.warning("claude_md_allowlist_unavailable", reason="path not expressible")
        argv += ["--resume", session_id] if resume else ["--session-id", session_id]
        if cfg.model:
            argv += ["--model", cfg.model]
        if cfg.append_system_prompt:
            argv += ["--append-system-prompt", cfg.append_system_prompt]
        if cfg.allowed_tools:
            argv += ["--allowedTools", *cfg.allowed_tools]
        if cfg.disallowed_tools:
            argv += ["--disallowedTools", *cfg.disallowed_tools]
        if cfg.mcp_config:
            argv += ["--mcp-config", *cfg.mcp_config]
        return argv

    def _claude_md_allowed(self, workspace: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        """The trees and the files a turn's instruction loading may read, or two empties (#135).

        The workspace is a tree, so an operator who has named `project` or `local` still gets
        the clone's `CLAUDE.md`, its `.claude/CLAUDE.md` and its `.claude/rules`, and an `@`
        include of theirs still resolves anywhere inside the clone; what it no longer names is
        the session account's home, which outlives the run. This workspace and not `self._root`,
        which is every workspace's parent: one issue's CLAUDE.md has no more business reading
        another issue's clone than reading the home.

        The config directory is not a tree. `claude` loads exactly two things from it as user
        memory, `CLAUDE.md` and `rules/`, and they have to be allowed or the argument would stop
        the user memory issuebot does leave in place. The rest of that directory is
        `.credentials.json`, the other sessions' transcripts under `projects/` that the sweep
        deliberately keeps, and the caches, and none of it is instructions.

        Those two are allowed, not trusted, and what clears them is the home sweep rather than
        this argument -- `CLAUDE.md` and `rules` are both `CLAUDE_HOME_SWEEP` names, removed
        before every turn and every hook on the `agent.run_as` route (#101, #137). Two gaps,
        both recorded in the spec rather than closed here: on the host route nothing sweeps
        them, because the home is the operator's own; and the sweep walks `~/.claude`
        literally, so a deployment that sets `$CLAUDE_CONFIG_DIR` has its user memory allowed
        here and swept nowhere.

        `$CLAUDE_CONFIG_DIR` and not `~/.claude` whenever the deployment sets one, because that
        is what `claude` itself resolves the pair against, and it reaches the child through
        `PASSTHROUGH_PREFIXES`. The session cannot re-point it: `CLAUDE_` is a
        `PROTECTED_ENV_PREFIXES` entry, so `.issuebot/env` is refused it.

        The arms are the paths as written, never what they resolve to, which is a choice and
        costs something measured: where `<config>/rules` is a symlink, claude matches the
        exclusion against the *resolved* path and the argument then stops those rules loading.
        Resolving here would fix that and would also feed a path the session may own into the
        fence -- a planted `rules -> /` would widen it to everything -- so an operator who
        keeps user memory elsewhere has `$CLAUDE_CONFIG_DIR`, which this does follow.

        Two empties -- so no argument -- when the directory does not resolve, rather than an
        allow-list of the workspace alone: that would stop the user memory loading, and the
        residual it would be buying against is bounded and recorded, where silently dropping
        instructions a deployment means to load is a fault that shows up nowhere.
        """
        config = self._session_config_dir()
        if config is None:
            self._log.warning("claude_md_allowlist_unavailable", reason="config dir unresolved")
            return ((), ())
        return ((workspace, config / "rules"), (config / "CLAUDE.md",))

    def _session_config_dir(self) -> Path | None:
        """Where the session's own ``claude`` keeps user memory: ``$CLAUDE_CONFIG_DIR``, else
        ``~/.claude`` under the home of the account the turn runs as."""
        configured = self._environ.get("CLAUDE_CONFIG_DIR")
        if configured:
            return Path(configured)
        home = self._session_home()
        return home / CLAUDE_HOME_DIR if home is not None else None

    def _session_home(self) -> Path | None:
        """The home of the account a turn runs as: the account's own, or this process's."""
        if self._runas is not None:
            # `RunAs.account()` reports a name that will not resolve as `RunAsError`, which is
            # an `OSError`; there is no separate `KeyError` to catch here.
            with contextlib.suppress(OSError):
                return Path(self._runas.account().pw_dir)
            return None
        home = self._environ.get("HOME")
        return Path(home) if home else None

    def child_environment(self) -> dict[str, str]:
        """What one turn's ``claude -p`` is run with.

        Not free of side effects, despite the name: it ensures this session account's uv cache
        directory exists first (#164), because the path it exports has to be one uv can write.
        """
        return agent_environment(self._environ, token=self._token, uv_cache=self._ensure_uv_cache())

    def _ensure_uv_cache(self) -> Path | None:
        """This session account's uv cache on the workspaces volume, or ``None`` (#164).

        Asked per turn rather than once in ``__init__``: the directory is the worker's to
        create, the call is idempotent, and a constructor that touched the filesystem would
        make every runner a test builds do it too.
        """
        return ensure_uv_cache_dir(self._root, self._account, self._environ)

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
        deadline: float | None = None,
    ) -> TurnResult:
        """Run one turn; every failure is reported in the result, only cancellation propagates.

        ``deadline`` is the run's, on the monotonic clock (#110): a turn still running then is
        terminated whatever it is printing, where ``claude.turn_timeout_ms`` only ever
        measures the silence since the last line.
        """
        stdout_path = log_dir / f"turn-{turn_number}.jsonl"
        stderr_path = log_dir / f"turn-{turn_number}.stderr.log"
        parser = StreamParser(turn_number=turn_number, expected_session_id=session_id)
        emit = _Emitter(observer, self._log, self._scrubber)
        started = time.monotonic()

        def finish(
            category: AgentErrorCategory | None, error: str | None, exit_code: int | None
        ) -> TurnResult:
            # `classify_result` has scrubbed its own message already; the runner's own
            # messages (a path, an OSError) have not. Scrubbing is idempotent.
            error = self._scrub(error)
            result = parser.result or {}
            usage = result.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            turn = TurnResult(
                turn_number=turn_number,
                session_id=parser.session_id,
                model=parser.model,
                api_key_source=parser.api_key_source,
                exit_code=exit_code,
                subtype=_string(result.get("subtype")),
                is_error=bool(result.get("is_error")),
                num_turns=_int(result.get("num_turns")),
                input_tokens=_int(usage.get("input_tokens")),
                cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
                cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
                output_tokens=_int(usage.get("output_tokens")),
                cost_usd=_float(result.get("total_cost_usd")),
                duration_ms=_int(result.get("duration_ms")),
                permission_denials=len(result.get("permission_denials") or []),
                result_text=self._scrub(_string(result.get("result"))),
                error_category=category,
                error=error,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            self._log.info(
                "claude_turn_finished",
                turn_number=turn_number,
                exit_code=exit_code,
                error_category=category,
                error=error,
                cost_usd=turn.cost_usd,
                input_tokens=turn.total_input_tokens,
                output_tokens=turn.output_tokens,
                num_turns=turn.num_turns,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return turn

        resolved = workspace.resolve()
        inside = resolved != self._root and resolved.is_relative_to(self._root)
        if not (resolved.is_dir() and inside):
            message = f"{workspace} is not a directory inside {self._root}"
            return finish("invalid_workspace_cwd", message, None)
        if cancel is not None and cancel.is_set():
            return finish("cancelled", "cancelled before the turn started", None)
        try:
            # The worker's own directory, verified before a name in it is opened (#104): the
            # turn files are what `capture_turns` reads back, and a directory the session had
            # placed there first would have the worker writing through the session's names.
            _own_log_dir(self._boundary, resolved, log_dir)
        except OSError as exc:
            return finish(
                "invalid_workspace_cwd", f"cannot use log directory {log_dir}: {exc}", None
            )
        (log_dir / f"turn-{turn_number}.prompt.md").write_text(prompt, encoding="utf-8")
        argv = self.build_argv(session_id=session_id, resume=resume, workspace=resolved)
        # Read every turn: a `before_run` that ran once still feeds a session resumed after a
        # retry, and a hook is free to rewrite the file between turns.
        env, workspace_env = workspace_environment(
            self.child_environment(), resolved, boundary=self._boundary
        )
        self._log.info(
            "claude_turn_started",
            turn_number=turn_number,
            argv=self._logged_argv(argv),
            workspace=str(resolved),
            workspace_env_count=len(workspace_env),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        category: AgentErrorCategory | None = None
        error: str | None = None
        with stderr_path.open("wb") as stderr_file:
            try:
                with self._prepared(argv, env) as spawn:
                    process = await asyncio.create_subprocess_exec(
                        *spawn.argv,
                        cwd=resolved,
                        env=spawn.env,
                        pass_fds=spawn.pass_fds,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=stderr_file,
                        start_new_session=True,
                        limit=STREAM_LINE_LIMIT,
                    )
            except OSError as exc:
                return finish("claude_not_found", f"cannot run {argv[0]!r}: {exc}", None)

            writer = asyncio.create_task(_feed_stdin(process, prompt))
            reader = asyncio.create_task(
                self._read_stream(process, parser, emit, stdout_path, deadline)
            )
            waiters: set[asyncio.Task[Any]] = {reader}
            cancel_waiter = asyncio.create_task(cancel.wait()) if cancel is not None else None
            if cancel_waiter is not None:
                waiters.add(cancel_waiter)
            try:
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if cancel_waiter is not None and cancel_waiter in done:
                    category, error = "cancelled", "cancelled while the turn was running"
                    await self._terminate(process)
                elif reader.result() == "timeout":
                    category = "turn_timeout"
                    error = f"no output for {self._timeout_s:.0f}s"
                    await self._terminate(process)
                elif reader.result() == "deadline":
                    category = "run_timeout"
                    error = (
                        f"run deadline reached: {self._run_timeout_s:.0f}s of wall clock "
                        "(agent.run_timeout_ms)"
                    )
                    await self._terminate(process)
            except Exception as exc:
                await self._terminate(process)
                category, error = "process_exit", f"turn supervision failed: {exc}"
            except BaseException:
                await self._terminate(process)
                emit(_event("process_exit", parser, detail=str(process.returncode)))
                raise
            finally:
                for task in (writer, reader, cancel_waiter):
                    if task is None:
                        continue
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    elif not task.cancelled() and task.exception() is not None:
                        self._log.warning(
                            "claude_turn_task_failed",
                            turn_number=turn_number,
                            error=str(task.exception()),
                        )
            exit_code = await process.wait()

        emit(_event("process_exit", parser, detail=str(exit_code)))
        if category is None:
            category, error = classify_result(
                parser.result,
                exit_code,
                _stderr_tail(self._boundary, stderr_path),
                scrubber=self._scrubber,
            )
        if category is None:
            emit(_event("turn_completed", parser, detail=parser.model))
        elif category in ("turn_timeout", "run_timeout"):
            emit(_event("turn_timeout", parser, detail=error))
        else:
            emit(_event("turn_failed", parser, detail=error))
        return finish(category, error, exit_code)

    def _logged_argv(self, argv: Sequence[str]) -> list[str]:
        """The argv for the turn's log line, scrubbed before it is cut (#109).

        Every other element is a flag, a model name, a tool name or a session id, but
        ``claude.mcp_config`` takes a JSON document as well as a path, and an MCP server
        definition carries its credentials in its own ``env`` block -- so an operator who
        inlines one puts it in this line, and in ``ps``, which is why the README's row says
        a file is the better spelling. Scrubbed first and cut after, like every other bounded
        message here: a cut through a credential leaves a fragment the shapes no longer match.
        """
        return [self._scrubber.scrub(arg)[:_LOGGED_ARG_LENGTH] for arg in argv]

    def _scrub(self, text: str | None) -> str | None:
        return None if text is None else self._scrubber.scrub(text)

    async def _read_stream(
        self,
        process: asyncio.subprocess.Process,
        parser: StreamParser,
        emit: _Emitter,
        stdout_path: Path,
        deadline: float | None,
    ) -> str:
        """Tee stdout to the log file and feed the parser.

        "eof" at the end of the stream, "timeout" after ``turn_timeout_ms`` of silence, and
        "deadline" once the run's deadline has passed, which every line read is checked
        against: the silence timer restarts with each line, the deadline never does. The
        check comes before the read on purpose, so a line already in the pipe at the deadline
        is not drained first: "still running at the deadline" is the rule, and a result that
        landed a moment before it is the timed-out run's, the work it pushed intact.
        """
        stdout = process.stdout
        if stdout is None:
            return "eof"
        with stdout_path.open("ab") as out:
            while True:
                wait = self._timeout_s
                by_deadline = False
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return "deadline"
                    by_deadline = remaining < wait
                    wait = min(wait, remaining)
                try:
                    raw = await asyncio.wait_for(stdout.readline(), timeout=wait)
                except TimeoutError:
                    # Whichever bound set the wait is the one that expired: re-reading the
                    # clock could call a wait the deadline cut short "silence".
                    return "deadline" if by_deadline else "timeout"
                except ValueError:
                    self._log.warning(
                        "claude_stream_line_too_long",
                        turn_number=parser.turn_number,
                        limit=STREAM_LINE_LIMIT,
                    )
                    emit(parser.overrun())
                    continue
                if not raw:
                    return "eof"
                out.write(raw)
                out.flush()
                for event in parser.feed(raw.decode("utf-8", errors="replace")):
                    emit(event)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        """SIGTERM the leader, wait for the grace period, then SIGKILL the whole group.

        The group is killed even when the leader has already exited: a grandchild that
        inherited stdout would otherwise outlive the turn and hold the pipe open. Under
        ``agent.run_as`` the leader is sudo, which relays the SIGTERM, but the group's other
        members are the account's and the worker's uid may not signal them: the SIGKILL goes
        through the same delegation first (#75), and the worker's own covers the leader.
        """
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_S)
        if self._runas is not None:
            await asyncio.to_thread(self._runas.kill_group, process.pid)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


class _Emitter:
    """Logs every turn event and hands it to the observer, isolating observer failures.

    ``detail`` is scrubbed on the way through: a failed turn's carries the error message,
    which is claude's words (#91), and the log line below is the first place it lands.
    """

    def __init__(
        self, observer: TurnObserver | None, log: Any, scrubber: Scrubber = DEFAULT_SCRUBBER
    ) -> None:
        self._observer = observer
        self._log = log
        self._scrubber = scrubber

    def __call__(self, event: TurnEvent) -> None:
        if event.detail is not None:
            event = replace(event, detail=self._scrubber.scrub(event.detail))
        self._log.debug(
            "claude_turn_event",
            kind=event.kind,
            turn_number=event.turn_number,
            message_type=event.message_type,
            tool_name=event.tool_name,
            detail=event.detail,
        )
        if self._observer is None:
            return
        try:
            self._observer.on_turn_event(event)
        except Exception:
            self._log.exception("turn_observer_failed", kind=event.kind)


def _event(kind: TurnEventKind, parser: StreamParser, *, detail: str | None) -> TurnEvent:
    return TurnEvent(
        kind=kind, turn_number=parser.turn_number, session_id=parser.session_id, detail=detail
    )


async def _feed_stdin(process: asyncio.subprocess.Process, prompt: str) -> None:
    stdin = process.stdin
    if stdin is None:
        return
    try:
        stdin.write(prompt.encode("utf-8"))
        await stdin.drain()
    except BrokenPipeError, ConnectionResetError:
        return
    finally:
        stdin.close()


def _stderr_tail(boundary: Boundary, path: Path) -> str:
    """The end of a turn's stderr, bounded; the whole of it when the file is small.

    The whole tail is read, not just the last line, because claude prints its reason for
    stopping and then whatever the runtime says on the way out, so a credential problem is
    rarely the last thing on the stream (see ``classify_result``). Read back through the
    boundary like every other turn file (#104).
    """
    try:
        read = boundary.read(
            path.parent, (path.name,), TURN_STDERR, keep="tail", limit=STDERR_TAIL_LIMIT
        )
    except OSError:
        return ""
    return read.data.decode("utf-8", errors="replace")


def _own_log_dir(boundary: Boundary, workspace: Path, log_dir: Path) -> None:
    """Create the run's log directory as the worker's own, inside the workspace as a rule."""
    try:
        parts = split_parts(workspace, log_dir)
    except ValueError:
        # Outside the workspace: the caller's choice, and still the worker's own directory.
        log_dir.parent.mkdir(parents=True, exist_ok=True)
        boundary.own_dir(log_dir.parent, (log_dir.name,))
        return
    boundary.own_dir(workspace, parts)


def _last_line(text: str) -> str:
    """The last non-blank line of a tail, uncapped: the part worth putting in a message."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""
