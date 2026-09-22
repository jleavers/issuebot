"""Running the session as another account: the one privilege the worker delegates (#75).

With ``agent.run_as`` set, ``claude -p``, every hook, the clone and the post-clone setup run
as that account, a different uid from the worker's, so the worker's code and interpreter,
its environment (``/proc/<pid>/environ``), its home and the state it keeps in a workspace
are out of the session's reach. The worker itself stays unprivileged: in the image ``sudo``
carries exactly one rule -- ``issuebot ALL=(%agents) NOPASSWD: ALL`` since #121, so the worker
may become any session account and nobody else -- and the binary is executable by the worker's
group alone, so the account the session runs as cannot invoke it at all. That the rule names a
*group* is what lets a caller pass an account it worked out at runtime: anything outside the
pool is refused by sudo itself rather than by the caller.

sudo's environment policy never shapes what the session sees. The worker serialises the
environment it built (``agent_environment`` plus the workspace's ``.issuebot/env``) into an
anonymous file (``anonymous_fd``), passes that one descriptor across the uid change, and the
``exec`` verb of this module -- run by the worker's own interpreter, root-owned in the image
-- installs it whole and execs the command. ``HOME``, ``USER`` and ``LOGNAME`` are the target
account's; everything else is exactly what the worker built. ``python -m
issuebot.agent.runas`` is the module's other face, and it has four verbs: ``exec``,
``kill`` (the agent's process group, since the worker's uid may not signal it), ``remove``
(the agent's files under a workspace, which the worker's uid may not unlink) and ``sweep``
(what a prior session left in the account's home for the next one to load: the config under
``~/.claude``, #101; the shell start-up files every ``bash -lc`` hook sources, #137; the tool
config that can name a command, #151; and the steering keys inside ``gh``'s ``hosts.yml``, the
one file that is edited rather than removed because it is also credential state, #190).
"""

import argparse
import contextlib
import errno
import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import yaml

MODULE = "issuebot.agent.runas"
# A uid change is quick; past this sudo is treated as wedged rather than waited for.
SUDO_TIMEOUT_S = 10
# Removing a workspace as the agent walks a tree the agent wrote, node_modules included.
REMOVE_TIMEOUT_S = 120

# The directory inside the home that the config sweep below walks. Named once, because the
# sweep is aimed at the *home* (#137) and reaches this from it.
CLAUDE_HOME_DIR = ".claude"

# The entries under the session account's ``~/.claude`` that a later ``claude -p`` loads as
# instructions or behaviour, and that a session must therefore not leave behind for the next
# one at the same uid (#101). Each account keeps the image's own ``0700`` home and nothing is
# mounted over it (#142); with a single session account that home is shared by every session in
# the container, across every repository, and with a pool (#121) the sharing is with the next
# session bound to that account rather than with the ones running beside it. Either way, a
# slash command, skill, rule, subagent, workflow, plugin, output style, memory file or
# settings a hostile issue plants would otherwise be read by an unrelated session next week.
# ``setting_sources`` does not stand in for this: since #107 it is always passed and defaults
# to ``[user]``, which is the very source most of these surfaces belong to
# -- ``settings.json``, ``CLAUDE.md``, ``rules``, ``skills``, ``commands`` and ``agents`` -- while
# ``plugins``, ``output-styles``, ``workflows`` and ``agent-memory`` are not in that flag's table
# at all. The whole list is swept regardless: the flag is a workflow's choice and the sweep is
# the worker's. Auto memory (``projects/<project>/memory/``) is read
# whatever the flag says and keyed by repository, so a session working one issue seeds every
# later session on the same repository; it is swept below by ``CLAUDE_HOME_MEMORY_DIR`` and
# never written in the first place, since ``FIXED_ENVIRONMENT`` (``runner.py``) sets
# ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1``. The credential (``.credentials.json``) and claude's own
# per-session runtime state
# (``projects/<project>/*.jsonl``/``sessions``/``shell-snapshots``/... -- transcripts, not
# instructions) are deliberately absent, and for the reason the list itself gives: neither is a
# surface a later ``claude -p`` loads as instructions or behaviour, so removing them would close
# no channel while costing something real. A credential file authenticates the next session
# rather than steering it, and ``claude`` rotates the refresh token inside it as it goes, so a
# sweep would break a login an account does hold; wiping live runtime would break a concurrent
# session's ``--resume``. A container session authenticates from the environment instead (#142),
# which is why its home usually holds no such file at all. A denylist, not an
# allowlist: everything it does not name is left alone, and a new claude config location has
# to be added here by hand, which is the residual accepted over a whole-home allowlist that
# would fail the other way, by wiping a runtime directory claude adds.
CLAUDE_HOME_SWEEP: tuple[str, ...] = (
    "CLAUDE.md",
    "rules",
    "skills",
    "commands",
    "agents",
    "workflows",
    "agent-memory",
    "plugins",
    "output-styles",
    "settings.json",
    "settings.local.json",
)
# Auto memory sits beside the transcripts it must not take with it: ``projects/<project>/`` holds
# the session ``.jsonl`` files (kept) and a ``memory/`` directory (swept). Named as the two path
# components rather than a glob, so the sweep walks ``projects`` itself and can refuse to follow
# a symlink at either level.
CLAUDE_HOME_MEMORY_DIR: tuple[str, str] = ("projects", "memory")

# The other half of the same class, and the reason the sweep is aimed at the home rather than
# at ``.claude`` inside it (#137): the account's own shell start-up files. Every hook issuebot
# runs is ``bash -lc`` (``WorkspaceManager.hook_shell``), a login shell, which sources
# ``/etc/profile`` and then the first of ``~/.bash_profile``, ``~/.bash_login`` and
# ``~/.profile`` that exists, runs ``~/.bash_logout`` on the way out, and reaches ``~/.bashrc``
# through whichever of those Debian's own copies source; ``claude`` takes a shell snapshot for
# the session's Bash tool the same way. The home is the account's and writable by it -- only
# ``.claude`` inside it is created by the image -- so a session that writes one of these leaves
# a script the next session's hooks run at the same uid, for the container's lifetime. Swept
# beside the ``.claude`` surfaces above and on the same schedule, which is what closes it
# whatever a later ``claude`` or a target repository's hooks read. Since #179 a workspace's
# ``.issuebot/env`` cannot name such a file through the environment either (``SHELL_ENV_NAMES``,
# ``runner.py``): ``BASH_ENV`` is this list's channel in variable form, and sweeping the files
# while leaving the variable would make the guarantee conditional on a name nothing checked.
# Removing them costs nothing an account nobody logs into needs: ``PATH`` for a login shell
# comes from ``/etc/profile`` and ``/etc/profile.d`` (root's, and where the image puts node and
# the PostgreSQL binaries), and the copies ``useradd`` took from ``/etc/skel`` set a prompt and
# some aliases for an interactive session that never happens here. That is also why the sweep
# is only ever pointed at a session account's home: on the host route (``agent.run_as`` unset)
# nothing is swept at all, because that home is the operator's own.
# ``.bash_aliases`` and the like are deliberately absent: nothing reads them but a ``.bashrc``
# that sources them, and ``.bashrc`` is on the list, so naming what the shells themselves read
# keeps this to something a reader can check against ``bash(1)``.
SHELL_STARTUP_SWEEP: tuple[str, ...] = (
    ".bash_profile",
    ".bash_login",
    ".profile",
    ".bashrc",
    ".bash_logout",
)

# The same class again, one tool further out (#151): the config files a *tool* the session runs
# reads out of the account's home, each of which can name a command to execute. Not shell
# start-up files, which is why they are a list of their own rather than entries in the one
# above, but the same residual -- a file the account may write, in a home the container keeps
# for its lifetime, read by the next session at that uid -- and so the same sweep.
#   ``.gitconfig`` and ``.config/git/config``: every session runs ``git`` as the account, and
#   user-level git config names commands (``core.pager``, ``core.editor``, ``core.fsmonitor``,
#   ``credential.helper``, ``[alias] x = !sh -c ...``, ``diff.<driver>.textconv``). Both
#   spellings, because git reads both: ``$XDG_CONFIG_HOME/git/config`` first -- which is
#   ``~/.config/git/config`` here, since ``XDG_CONFIG_HOME`` is not in ``PASSTHROUGH_NAMES``
#   and so is not inherited from the worker's environment, and since #171 a workspace's
#   ``.issuebot/env`` cannot set it either (``TOOL_CONFIG_ENV_NAMES``, ``runner.py``): the two
#   halves are what make this a guarantee rather than a default -- and then ``~/.gitconfig``.
#   Sweeping one and not the other would leave the channel open at the name git looks at first.
#   ``.ssh/config``: ``ProxyCommand``, ``LocalCommand`` and ``Match exec`` run a shell command
#   for a matching host. issuebot's own clone is HTTPS through ``gh`` and the default image
#   installs no ssh client, so nothing issuebot does reads it today; a target repository's hook
#   or a submodule URL in an image built ``FROM`` this one can, and a name on this list costs
#   nothing where the file does not exist.
# No deployment has a reason to leave any of them in a session account's home, which is what
# makes this a sweep rather than a residual: the session's commit identity comes from the
# ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` variables (``PASSTHROUGH_PREFIXES``), the workspace's
# ``safe.directory`` entry is the image's ``--system`` one, and the post-clone setup's
# credential helper is ``git config --local`` inside the clone. A deployment that does want
# global git config for its sessions has ``/etc/gitconfig``, which is root's and outside the
# session's privilege domain, in the image or in one built ``FROM`` it.
# A denylist like the two above: named paths, and everything else in the home is left alone.
# Each is the sequence of its path components, because every one of them is nested and the
# sweep walks rather than follows -- ``.ssh`` or ``.config`` replaced with a symlink is
# unlinked as the plant it is, not stepped through to whatever it points at.
TOOL_CONFIG_SWEEP: tuple[tuple[str, ...], ...] = (
    (".gitconfig",),
    (".config", "git", "config"),
    (".ssh", "config"),
)

# The one file the sweep *edits* instead of removing, and the one place it looks inside a file
# at all (#190). ``~/.config/gh/hosts.yml`` is credential state: it holds the ``oauth_token``
# that authenticates the next session's ``gh`` where that session has one, so #151 pinned it as
# a survivor and #173 kept it -- taking it would cost every deployment more than any channel in
# it does. But it is not *only* credential state. ``gh config set -h <host> <key> <value>``
# writes into this file rather than into the ``config.yml`` #173 sweeps, and one of the keys it
# can carry re-points ``gh`` on an ordinary core command.
#   Measured against ``gh version 2.100.0``, in the post-#173 state with no ``config.yml``
#   anywhere: ``api_host: 127.0.0.1`` sends ``gh api``, ``gh issue list``, ``gh pr list`` and
#   ``gh repo clone`` to the host the planting session named. That last one is issuebot's own
#   (``WorkspaceManager._run_argv``, through ``RunAs``), so the previous session's leavings
#   break the next session's workspace clone.
# What the channel cannot do bounds how much this matters, and all of it was measured rather
# than argued: ``gh`` sends no ``Authorization`` header to a substituted host -- neither the
# environment's ``GH_TOKEN`` nor the file's own ``oauth_token``, which ``gh help config`` states
# outright ("this is not a security boundary and requests to the canonical host will remain
# authenticated") -- a self-signed certificate is refused, so forging an answer needs a CA in
# the *system* trust store, which is root's; and #126's proxy refuses a name off its allow-list
# (``egress_denied``, ``Forbidden`` at the client) while an on-list name completes. So what
# survives is availability, not confidentiality: a plant needing no network at all that breaks
# every core ``gh`` command of the next session bound to that account.
# A key-level edit rather than a path-level entry, because the file has to keep working. What
# makes it bounded is that the keys this position carries are enumerable: ``gh config`` manages
# a fixed set, ``gh config --help`` advertises it, and ``gh config set -h <host> <key> <value>``
# writes *every one of them* into ``hosts.yml`` rather than into ``config.yml``. Measured by
# asking for each of the thirteen with a value that key accepts -- which is the whole of the
# measurement, and the easy thing to get wrong: ``gh config set`` validates the enum-valued keys,
# so a probe that passes a placeholder is refused for eight of the thirteen and reports only the
# five free-form ones. All thirteen land here.
# So the list is ``gh``'s own configuration surface, removed whole from a file that also holds
# credentials, and what survives is what ``gh config`` does not manage: ``oauth_token``, ``user``
# and the ``users:`` subtree. That is the shape of the decision -- the sweep does not judge which
# keys are dangerous, it declines to let a session leave *configuration* in a credential file --
# and it is why the list can be a denylist without being a guess.
#   ``api_host`` is what #190 was opened about, and the only one of the thirteen that is *only*
#   reachable from here: at top level it is inert. A planted value sends ``gh api``,
#   ``gh issue list``, ``gh pr list`` and ``gh repo clone`` -- issuebot's own, for the next
#   session's workspace -- to the host the planting session named.
#   ``git_protocol`` is the other one measured live from this position, and the one that shows
#   why the list is the whole surface rather than a hand-picked pair: set to ``ssh`` it reads
#   back ahead of the hostname-less lookup, ``gh auth status`` reports ``Git operations protocol:
#   ssh``, and ``gh repo clone`` then fails outright with ``cannot run ssh: No such file or
#   directory``, since the image installs no ssh client.
#   ``http_unix_socket``, ``pager``, ``editor`` and ``browser`` are measured *inert* in this
#   position -- the same values at top level do fire, so it is where the key sits and not the
#   test -- and the remaining seven are cosmetic or, like ``prompt``, documented as global. They
#   are removed all the same: naming a key that does nothing costs nothing, where leaving one out
#   costs the channel back if a later ``gh`` starts honouring it from here.
# What the live channel cannot do bounds how much of this is urgent, and all of it was measured:
# ``gh`` sends no ``Authorization`` header to a substituted host (from ``GH_TOKEN`` or from the
# file's own ``oauth_token``; ``gh help config`` says so outright), a self-signed certificate is
# refused so forging an answer needs a CA in root's trust store, and #126's proxy refuses a name
# off its allow-list while an on-list one completes. So what survives is availability, not
# confidentiality, and it needs no network at all.
# None of the thirteen is credential state, so removing them cannot touch what the file is kept
# for; dropping ``git_protocol`` leaves ``gh``'s own ``https`` default, which is what issuebot
# clones and pushes over (the post-clone setup's credential helper is a token, not a key).
# A denylist rather than a keep-list of the credential keys, unlike ``--strict-mcp-config``'s
# "name what survives", because the two failures are not symmetrical: a key a future ``gh`` adds
# and this list misses costs the bounded channel above, where a keep-list that stripped a
# credential key a future ``gh`` adds would break authentication for every session in the
# deployment. The asymmetry is covered by proving the set rather than asserting it -- the CI
# ``docker`` job reads the image's own ``gh config --help`` and fails when it advertises a key
# this list does not name, so a release that adds a fourteenth fails a pull request rather than a
# session.
GH_HOSTS_FILE: tuple[str, ...] = (".config", "gh", "hosts.yml")
GH_HOSTS_STEERING_KEYS: frozenset[str] = frozenset(
    {
        "api_host",
        "git_protocol",
        "editor",
        "prompt",
        "prefer_editor_prompt",
        "pager",
        "http_unix_socket",
        "browser",
        "color_labels",
        "accessible_colors",
        "accessible_prompter",
        "spinner",
        "telemetry",
    }
)
# What the sweep will read of that file before deciding it is not the small credential document
# it is editing. ``gh`` writes a handful of lines per host; this is four orders of magnitude of
# headroom, and the bound is the point (#110's rule at the one seam that parses a file the
# session can grow). Past it the file is left exactly as it is: a sweep that cannot understand
# a credential file must not rewrite it, and an unparsed ``hosts.yml`` this large is not one
# ``gh`` is authenticating from either.
GH_HOSTS_LIMIT = 256 * 1024


# The fallback descriptor's file, while it briefly has a name. A tmpfs, so the environment
# it carries -- GH_TOKEN, the Anthropic credential, any DSN a ``before_run`` hook wrote --
# stays in memory as the memfd's bytes do, rather than in a disk-backed filesystem's freed
# blocks. A preference only: where there is no writable ``/dev/shm`` the file goes wherever
# ``tempfile`` puts it, which may well be a disk. The fall-through covers opening the file,
# not filling it: a tmpfs that runs out mid-write fails the spawn with the ``OSError`` every
# site catches rather than starting again somewhere else.
SHM_DIR = "/dev/shm"


def anonymous_fd(name: str) -> int:
    """A read-write descriptor on a file no path names, positioned at its start.

    ``memfd_create`` where the interpreter has it and the kernel answers, and otherwise a
    temporary file unlinked before anything is written to it -- which is what made the memfd
    the right choice in the first place: the descriptor is inherited through ``pass_fds`` and
    survives sudo's ``-C``, and with no directory entry nothing else can open the environment
    it holds. A pipe would not do; the writer would block on the buffer if the environment
    ever outgrew it. What the fallback cannot promise is the memfd's other property, that the
    bytes never reach a filesystem: it prefers ``SHM_DIR`` for that and settles for whatever
    ``tempfile`` picks, which may be disk-backed.

    The fallback is not theoretical (#115): ``python-build-standalone``, which is what ``uv``
    installs, configures against a glibc older than the call, so the interpreter a developer
    runs the suite under is regularly one without it while the image's Debian Python has it.
    The attribute is what to ask for, not ``sysconfig``'s ``HAVE_MEMFD_CREATE``: that
    describes the build rather than the runtime, and reads ``0`` on a ``uv`` CPython 3.14.7
    that does define ``os.memfd_create``.

    ``name`` is a label, not a path: it reaches the fallback as a filename prefix, so pass a
    bare identifier. Raises ``OSError``, like ``memfd_create`` itself, so every spawn site
    reports it the way it reports a missing ``claude``.
    """
    create = getattr(os, "memfd_create", None)
    if create is not None:
        # A present call can still fail: an old kernel, a seccomp profile, a sandbox. The
        # fallback needs none of those, so try it rather than failing the spawn.
        with contextlib.suppress(OSError):
            return create(name)
    if os.path.isdir(SHM_DIR) and os.access(SHM_DIR, os.W_OK):
        with contextlib.suppress(OSError):
            return _unlinked_fd(name, SHM_DIR)
    return _unlinked_fd(name, None)


def _unlinked_fd(name: str, directory: str | None) -> int:
    """A descriptor on a temporary file in ``directory``, unlinked before it is written to.

    ``mkstemp`` opens it ``0600`` to this uid, and the unlink follows immediately, so the
    window in which the file has a name is one in which it is empty and unreadable to the
    account the session runs as.
    """
    fd, path = tempfile.mkstemp(prefix=f"{name}-", dir=directory)
    try:
        os.unlink(path)
    except OSError:
        os.close(fd)
        raise
    return fd


class RunAsError(OSError):
    """The delegation cannot be set up: no such account, or sudo cannot run.

    An ``OSError`` so every spawn site's existing ``except OSError`` reports it the way it
    reports a missing ``claude``: in the result, never as a crash.
    """


@dataclass(frozen=True, slots=True)
class Spawn:
    """What to pass to ``create_subprocess_exec``: the wrapped argv, sudo's own environment,
    and the descriptor the helper reads the session's environment from."""

    argv: list[str]
    env: dict[str, str]
    pass_fds: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RunAs:
    """The account the session runs as, and the sudo that gets there."""

    user: str
    sudo: str = "sudo"

    def account(self) -> pwd.struct_passwd:
        try:
            return pwd.getpwnam(self.user)
        except KeyError:
            raise RunAsError(f"agent.run_as: no account named {self.user!r}") from None

    def environment(self, env: Mapping[str, str]) -> dict[str, str]:
        """``env`` with the identity variables the account's, since the worker's came through."""
        account = self.account()
        merged = dict(env)
        merged["HOME"] = account.pw_dir
        merged["USER"] = self.user
        merged["LOGNAME"] = self.user
        return merged

    @contextlib.contextmanager
    def prepared(self, argv: Sequence[str], env: Mapping[str, str]) -> Iterator[Spawn]:
        """The spawn that runs ``argv`` as the account with exactly ``env``.

        The descriptor lives for the block: spawn inside it. ``-C`` closes everything above
        the descriptor in sudo, and ``pass_fds`` means the worker's other descriptors never
        reach sudo in the first place.
        """
        payload = json.dumps(self.environment(env)).encode("utf-8")
        fd = anonymous_fd("issuebot-agent-env")
        try:
            _write_all(fd, payload)
            os.lseek(fd, 0, os.SEEK_SET)
            yield Spawn(
                argv=[
                    *self._sudo(),
                    "-C",
                    str(fd + 1),
                    "--",
                    *self._helper("exec", "--env-fd", str(fd), "--", *argv),
                ],
                env=dict(env),
                pass_fds=(fd,),
            )
        finally:
            os.close(fd)

    def run(
        self, argv: Sequence[str], env: Mapping[str, str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` as the account synchronously and capture its output."""
        with self.prepared(argv, env) as spawn:
            return subprocess.run(
                spawn.argv,
                env=spawn.env,
                pass_fds=spawn.pass_fds,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

    def probe(self, env: Mapping[str, str]) -> str | None:
        """None when the delegation runs ``id -u`` as the account *and* that uid is not this
        process's; else why not.

        The referent is the invoking uid (#111). Comparing the delegated answer with the
        target's uid alone proves that the delegation works, which is not the same as
        proving that it separates: a delegation that ran the command at this process's own
        uid has set up no boundary, whatever account it was asked for, and an account whose
        uid *is* this process's would answer correctly with nothing separated. So the
        target must differ from the invoker before sudo is asked anything, and an answer
        that is the invoker's is reported as no separation, apart from a refusal.
        """
        me = os.getuid()
        try:
            account = self.account()
        except RunAsError as exc:
            return str(exc)
        if account.pw_uid == me:
            return (
                f"agent.run_as names {self.user!r}, this process's own account (uid {me}); "
                "the session would run with no separation"
            )
        try:
            completed = self.run(["id", "-u"], env, timeout=SUDO_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return f"{self.sudo} did not answer within {SUDO_TIMEOUT_S}s"
        except OSError as exc:
            return f"cannot run {self.sudo!r}: {exc}"
        answer = completed.stdout.strip()
        if completed.returncode == 0 and answer == str(account.pw_uid):
            return None
        if completed.returncode == 0 and answer == str(me):
            return (
                f"{self.sudo} ran the command as this process (uid {me}), not as "
                f"{self.user!r} (uid {account.pw_uid}): no separation"
            )
        if completed.returncode == 0 and answer.isdigit():
            # Separated, but not as asked: a rule that maps the account elsewhere.
            return (
                f"{self.sudo} ran the command as uid {answer}, not as {self.user!r} "
                f"(uid {account.pw_uid})"
            )
        detail = _last_line(completed.stderr) or _last_line(completed.stdout)
        return f"cannot run as {self.user!r}: {detail or f'exit status {completed.returncode}'}"

    def kill_group(self, pgid: int) -> None:
        """SIGKILL the process group as the account. Never raises: the caller's own
        ``killpg`` follows, and a kill that could not be delegated has nothing to add."""
        self._delegate("kill", str(pgid), timeout=SUDO_TIMEOUT_S)

    def remove_tree(self, path: Path) -> None:
        """Remove what the account owns under ``path``; the worker removes its own after."""
        self._delegate("remove", str(path), timeout=REMOVE_TIMEOUT_S)

    def sweep_home(self, home: Path | None = None) -> bool:
        """Clear what a prior session could steer the next one with from the account's home:
        the loadable config under ``~/.claude`` (#101), the shell start-up files a login
        shell reads (#137), the tool config files that can name a command (#151), and the
        steering keys inside the one file that has to survive rather than go, ``gh``'s
        ``hosts.yml`` (#190).

        Delegated, since the home is the account's and closed to the worker's uid; never raises,
        like ``kill_group`` and ``remove_tree``, but unlike them reports whether the helper ran
        and exited 0, because a sweep that silently never happens is a security control with
        no failure signal. ``home`` defaults to the account's own; a caller (the tests) passes
        an explicit path so the sweep can be proved without touching a real home.

        Never the invoking process's own account, whichever way the path was arrived at: this
        unlinks a home's dotfiles, and the home it exists to clear is one at *another* uid.
        ``probe_run_as`` refuses such an account at worker startup and in ``validate`` (#111's
        separation rule), but ``run-once`` runs no probe, so an operator who pointed
        ``agent.run_as`` at their own account would otherwise have their own ``~/.claude`` and
        ``.profile`` swept before the first hook. Refused here, where the removal is, and
        reported like any other sweep that did not run.
        """
        try:
            account = self.account()
        except RunAsError:
            return False
        if account.pw_uid == os.getuid():
            return False
        if home is None:
            home = Path(account.pw_dir)
        # SUDO_TIMEOUT_S, not REMOVE_TIMEOUT_S: this removes a handful of small config entries,
        # not an arbitrary workspace tree. The timeout bounds the worker's wait, not the helper:
        # `subprocess.run` kills `sudo`, while the helper, the account's own process, runs on to
        # completion. A tree deep enough to outlast it reads as a failed sweep here, and the
        # next turn sweeps again.
        return self._delegate("sweep", str(home), timeout=SUDO_TIMEOUT_S)

    def _delegate(self, *args: str, timeout: float) -> bool:
        """Run one helper verb as the account; ``True`` only when it ran and exited 0."""
        argv = [*self._sudo(), "--", *self._helper(*args)]
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
            return completed.returncode == 0
        return False

    def _sudo(self) -> list[str]:
        return [self.sudo, "-n", "-u", self.user]

    @staticmethod
    def _helper(*args: str) -> list[str]:
        # ``-P`` keeps the process's cwd off ``sys.path``: the exec verb runs with the agent's
        # workspace as cwd, and without it a planted ``issuebot/agent/runas.py`` there would
        # shadow the real module (#75). No escalation -- the helper is already the agent -- but
        # the interpreter should resolve to the root-owned package under /app regardless.
        return [sys.executable, "-P", "-m", MODULE, *args]


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte. A memfd took the lot in one call; a file on a filesystem that fills
    mid-write need not, and a truncated environment reaches the helper as unparseable JSON."""
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if not written:
            raise OSError(errno.EIO, "wrote no bytes of the session environment")
        view = view[written:]


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


# --- the helper: what runs on the far side of the uid change ----------------------------


def _exec(env_fd: int, argv: list[str]) -> NoReturn:
    with os.fdopen(env_fd, "rb") as handle:
        env = json.loads(handle.read())
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise SystemExit("runas exec: the environment is not a mapping of strings")
    os.execvpe(argv[0], argv, env)


def _kill(pgid: int) -> None:
    # EPERM covers the group leader, sudo, which is the worker's; every process that is the
    # account's has been signalled once any has, which is what the call is for.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _relax(path: Path) -> None:
    """Restore this uid's own access to the directory ``path``, if it is one and it owns it.

    A mode is the owner's to set and the owner's to put back, so a directory this account owns
    can never be a directory it cannot open. Anything else -- another account's, a symlink, a
    file -- is left exactly as it is, and a failure is skipped like every other step of a
    best-effort removal.
    """
    with contextlib.suppress(OSError):
        # `lstat`, so `S_ISDIR` is false for a symlink to a directory and the chmod below can
        # never travel down one.
        st = os.lstat(path)
        if st.st_uid == os.getuid() and stat.S_ISDIR(st.st_mode):
            os.chmod(path, st.st_mode | stat.S_IRWXU)


def _relax_file(path: Path) -> None:
    """``_relax`` for a regular file this account owns, which the sweep needs in one place.

    The directory form above is enough for every *removal*: unlinking a file needs write and
    search on the directory holding it and nothing at all of the file's own mode, which is why
    it declines anything that is not a directory. The one edit the sweep makes (#190,
    ``_sweep_gh_hosts``) has to *read* its target, so a session that plants ``api_host`` in
    ``~/.config/gh/hosts.yml`` and then ``chmod 0000``s it would otherwise keep the plant at no
    cost to itself -- ``gh`` reads the file as its owner, which the plant's author and this
    sweep both are. Owner read and write only, added to whatever is there; the group and other
    bits are left alone, since ``gh`` refuses a ``hosts.yml`` wider than its own ``0600``.
    """
    with contextlib.suppress(OSError):
        st = os.lstat(path)
        if st.st_uid == os.getuid() and stat.S_ISREG(st.st_mode):
            os.chmod(path, st.st_mode | stat.S_IRUSR | stat.S_IWUSR)


def _relax_tree(path: Path) -> None:
    """``_relax`` for ``path`` and every directory under it, top down.

    Top down because ``os.walk`` has to read a directory to reach what is inside it: each level
    is opened before the level below is listed. ``os.walk`` does not follow a link below its
    top, and ``_relax`` reads an ``lstat``, so no chmod travels down one; the *top* is the
    caller's to check, since ``os.walk`` does follow that one (``_sweep`` does).

    ``path`` itself is relaxed, where ``_remove``'s inline loop used to start one level down.
    That reaches one directory more than before on the workspace path, and only ever a
    directory the calling account owns -- a workspace root is the worker's (``SEALED_DIR_MODE``,
    ``WorkspaceManager``), so the uid check declines it there and the removal is unchanged.
    """
    _relax(path)
    for dirpath, dirnames, _filenames in os.walk(path):
        for name in dirnames:
            _relax(Path(dirpath) / name)


def _remove(path: Path) -> None:
    """Remove every entry the caller owns under ``path``, opening its own directories first.

    Anything else -- the worker's ``.issuebot`` state, a directory it cannot empty -- is left
    for the worker, and no failure is reported: the worker's own removal is what decides.
    """
    _relax_tree(path)
    shutil.rmtree(path, onexc=lambda *_: None)


def _walk(root: Path, parts: Sequence[str]) -> Path | None:
    """The path ``parts`` names under ``root``, or the first symlink on the way to it.

    The sweep removes what it is pointed at, so a nested target has to be resolved one
    component at a time: with ``.ssh`` replaced by a symlink, ``root / ".ssh" / "config"``
    names a file inside whatever it points at, and unlinking that would reach outside the home
    -- while the symlink itself is what a session planted and what ``ssh`` would read through.
    So an intermediate symlink is returned instead, to be unlinked like a symlinked surface,
    and ``None`` comes back when a component below one is missing, which is the ordinary case
    of a home that never held the file. Never resolves the final component: whether *that* is a
    symlink is ``_sweep``'s to decide, and it unlinks either way.
    """
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            return current
        if not current.is_dir():
            # `is_dir` answers False for a directory this process cannot stat as well as for
            # one that is not there, and the difference matters: a session that plants
            # `~/.config/git/config` and closes `~/.config` to search would otherwise have the
            # sweep yield no target at all, where `_sweep`'s retry cannot reach it. The modes
            # are this account's own (`_relax`), so put them back and ask again; a component
            # that is really absent, or really not a directory, still ends the walk.
            if not _exists(current):
                return None
            _relax(current.parent)
            _relax(current)
            if current.is_symlink():
                return current
            if not current.is_dir():
                return None
    return current / parts[-1]


def _sweep_targets(home: Path) -> Iterator[Path]:
    """Every path the sweep removes under ``home``: the shell start-up files, the tool config
    files, the named ``.claude`` surfaces and each project's auto memory directory.

    ``projects`` and each entry in it are walked, never followed: claude
    creates real directories there, so a symlink at either level is a session's, planted to
    point claude's memory read at a tree the sweep would not visit, and it is yielded as the
    target -- unlinked like a symlinked surface -- rather than stepped through. ``_walk``
    applies the same rule to the nested entries of ``TOOL_CONFIG_SWEEP``."""
    yield from (home / name for name in SHELL_STARTUP_SWEEP)
    for parts in TOOL_CONFIG_SWEEP:
        target = _walk(home, parts)
        if target is not None:
            yield target
    # Through ``_walk`` as well, rather than by joining: the image creates ``.claude`` as a real
    # directory owned by the account, so a link there is a session's, and following it would have
    # this sweep delete the named entries inside whatever tree it points at -- the rule
    # ``projects/<project>`` has always had, applied one level up. Going through ``_walk`` is
    # also what keeps that check from resting on the home being readable by luck: it relaxes a
    # component it cannot stat rather than answering ``False`` for it.
    for name in CLAUDE_HOME_SWEEP:
        target = _walk(home, (CLAUDE_HOME_DIR, name))
        if target is not None:
            yield target
    projects_name, memory_name = CLAUDE_HOME_MEMORY_DIR
    projects = _walk(home, (CLAUDE_HOME_DIR, projects_name))
    if projects is None:
        return
    # A link at either level -- ``.claude`` itself, which ``_walk`` returns in place of what is
    # under it, or ``projects`` -- is the target, and nothing below it is visited.
    if projects.is_symlink():
        yield projects
        return
    if not projects.is_dir():
        return
    # Relaxed before it is read rather than after the read failed, which is the one place the
    # sweep does that: reaching auto memory needs both *read* on ``projects``, to list the
    # project directories, and *search*, to tell a directory from a link -- and a session can
    # drop either one on its own, leaving a listing whose names cannot be classified and so a
    # target that is never yielded for ``_sweep``'s retry to repair. One chmod on a directory
    # this account owns, against a mode game with no other cost to the plant: ``claude`` opens
    # a path it already knows and lists nothing.
    _relax(projects)
    for project in _entries(projects):
        if project.is_symlink():
            yield project
        elif project.is_dir():
            yield project / memory_name


def _entries(path: Path) -> list[Path]:
    """What ``path`` holds, with the modes put back if it will not list.

    Listing a directory needs *read* on it, where opening a file inside one by name needs only
    search -- so a session that plants ``projects/<project>/memory/`` and drops read on
    ``projects`` would keep it: the listing this walk depends on fails, no target is yielded and
    ``_sweep``'s retry never sees one, while ``claude`` opens the planted path by name as
    before. The mode is the account's own, like every other in this home, so the repair is the
    same one (``_relax``) and the listing is asked again. Empty when it still will not answer,
    which is the best-effort rule the rest of the sweep keeps.
    """
    with contextlib.suppress(OSError):
        return list(path.iterdir())
    _relax(path)
    with contextlib.suppress(OSError):
        return list(path.iterdir())
    return []


class _HostsLoader(yaml.SafeLoader):
    """``SafeLoader`` with the implicit scalar resolvers removed, so every plain scalar loads as
    the text ``gh`` wrote (#190).

    The sweep parses ``hosts.yml`` only to drop keys from it, and then writes the rest back, so
    the round trip has to be value-faithful for a credential file. PyYAML resolves YAML 1.1
    scalars, where ``go-yaml`` -- which is what reads this file -- does not: a ``user: no`` would
    come back ``user: false``, and the same for ``on``, ``off``, ``y``, ``n`` and the sexagesimal
    forms. Loading every plain scalar as a string and letting the dumper quote what needs quoting
    keeps what was there, and costs nothing, since nothing here is compared as a number or a
    boolean -- only key names are looked at.
    """


_HostsLoader.yaml_implicit_resolvers = {}


def _sweep_gh_hosts(home: Path) -> None:
    """Remove the steering keys (``GH_HOSTS_STEERING_KEYS``) from ``~/.config/gh/hosts.yml``,
    keeping everything else in it (#190).

    The one place the sweep looks *inside* a file rather than removing it, because this file is
    credential state the next session may authenticate with and a sweep that took it would cost
    every deployment more than the channel in it does. The keys it removes are the closed set
    ``gh config set -h <host>`` can write, none of which is credential state, so ``oauth_token``,
    ``user`` and the ``users:`` subtree come through untouched.

    Only the keys directly under a host, never deeper: a steering key inside the ``users:``
    subtree is measured *not* honoured -- ``users.<name>.api_host`` left the request on the real
    ``api.github.com`` where the same key one level up re-pointed it -- so the subtree that holds
    the per-account tokens can be preserved whole without leaving the channel open under it.

    Best-effort and fail-safe, in that order: anything this cannot read, parse or understand as
    the small mapping-of-hosts ``gh`` writes keeps its contents exactly as they are, since
    rewriting a credential file on a guess is the one outcome worse than the plant. (Its *mode*
    may have been widened to the owner read and write ``gh`` needs anyway, which is
    ``_relax_file``'s repair and the only mark a declined file carries.) The contents are not
    written at all -- not even rewritten identically -- unless a key actually came out, so a
    home no session has planted in keeps its ``hosts.yml`` byte for byte, inode included.

    A symlink at that name, or on the way to it, is unlinked instead -- the rule every other
    surface in this sweep has, and ``_walk``'s, which yields the first link it meets rather than
    descending through it. ``gh`` writes a regular file in a real directory, so a link at either
    is a session's redirection, and editing through one would rewrite a file outside the home.
    """
    target = _walk(home, GH_HOSTS_FILE)
    if target is None:
        return
    if target.is_symlink():
        _remove_swept(target)
        return
    document = _read_gh_hosts(target)
    if document is None:
        return
    parsed, seen = document
    stripped = {
        host: {key: value for key, value in entry.items() if key not in GH_HOSTS_STEERING_KEYS}
        if isinstance(entry, dict)
        else entry
        for host, entry in parsed.items()
    }
    if stripped == parsed:
        return
    _replace_gh_hosts(target, stripped, seen)


def _read_gh_hosts(target: Path) -> tuple[dict, os.stat_result] | None:
    """The mapping ``hosts.yml`` parses to and the file it was read from, or ``None``.

    ``None`` covers every way this can decline: a file that will not open even with the modes
    put back, one over ``GH_HOSTS_LIMIT``, bytes that are not UTF-8 or not YAML, and a document
    that is not the mapping of host names ``gh`` writes. The modes are put back the way the rest
    of the sweep does, since a session that drops read on its own ``hosts.yml`` would otherwise
    keep a plant at no cost to itself -- ``gh`` opens the path it already knows.
    """
    seen = _read_capped(target)
    if seen is None:
        _relax(target.parent)
        _relax_file(target)
        seen = _read_capped(target)
    if seen is None:
        return None
    text, st = seen
    try:
        # `_HostsLoader` is a `SafeLoader`; only its scalar resolvers differ.
        parsed = yaml.load(text, Loader=_HostsLoader)
    except yaml.YAMLError, RecursionError:
        # `RecursionError` beside `YAMLError` because PyYAML's scanner and representer both
        # recurse per nesting level, and a document nested a few thousand deep -- well inside
        # `GH_HOSTS_LIMIT`, a few hundred bytes of brackets -- raises it rather than complaining.
        # It is not a `YAMLError`, so without this it would leave `_sweep` altogether: the sweep
        # would report failure on every turn and every hook for the container's lifetime, and
        # the plant in that same file would never be stripped. Declining is the fail-safe branch
        # the rest of this function already has.
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed, st


def _read_capped(target: Path) -> tuple[str, os.stat_result] | None:
    """At most ``GH_HOSTS_LIMIT`` bytes of ``target`` as text, with what it was read from.

    ``O_NOFOLLOW`` because the caller's symlink check and this open are two steps, and the
    session owns the directory between them. ``O_NONBLOCK`` and the ``fstat`` for the reason
    ``Boundary.read`` has both (#104): a session that replaces its own ``hosts.yml`` with a FIFO
    would otherwise have this ``open`` wait for a writer that never comes, and the sweep runs
    before every turn and every hook -- so the plant would cost a hung helper per turn and a
    ``claude_home_sweep_failed`` warning, where declining a file that is not a regular file
    costs the plant nothing it did not already have. One byte past the cap is read deliberately,
    so a file *at* the limit is told from one over it.
    """
    fd = None
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        # To the end rather than one `os.read`: a short read is allowed to happen, and a
        # truncated document that still parses would be written back over the whole file --
        # which the staleness guard could not catch, the file not having changed.
        raw = b""
        while len(raw) <= GH_HOSTS_LIMIT:
            chunk = os.read(fd, GH_HOSTS_LIMIT + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        return None
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
    if len(raw) > GH_HOSTS_LIMIT:
        return None
    try:
        return raw.decode(), st
    except UnicodeDecodeError:
        return None


def _replace_gh_hosts(target: Path, stripped: dict, seen: os.stat_result) -> None:
    """Write ``stripped`` over ``target`` atomically, or leave the file as it was.

    Through a temporary file in the same directory and ``os.replace``, so a session's ``gh``
    running beside this sweep reads either the old document or the new one and never a partial
    write of its own credentials. The mode is the original's rather than the umask's, which is
    about the file and not about ``gh``: ``gh`` writes ``hosts.yml`` ``0600`` but reads a wider
    one without complaint (measured), so nothing would *report* a sweep that quietly widened the
    permissions on a file holding a token.

    ``seen`` is the file the document was parsed from, and the write is declined unless the name
    still resolves to it. Reading and writing are two steps, the session owns the directory
    between them, and ``gh`` rewrites this file on ordinary commands of its own -- it normalises
    the document and it refreshes an OAuth token in place -- so a rename over a file that moved
    in that window would discard a credential ``gh`` had just written. Declining costs the plant
    one more turn, where the sweep runs again; the other order costs a login.

    The file also has to be this account's own, the rule ``_relax`` and ``_relax_file`` keep: a
    ``hosts.yml`` an operator seeded as root is not a session's plant, and replacing it with an
    account-owned copy would hand the next session a file it can rewrite freely.
    """
    # `RecursionError` beside the other two for the reason `_read_gh_hosts` catches it: the
    # representer recurses per nesting level, so a document this deep raises on the way out as
    # well as on the way in -- and a sweep that raises is one that never finishes, where a sweep
    # that declines leaves a plant the next turn tries again.
    with contextlib.suppress(OSError, yaml.YAMLError, RecursionError):
        # The temporary file is created in this directory and renamed over the target, so the
        # replace needs write and search on it -- the same bits `_sweep`'s retry puts back for
        # a removal, and the account's own to set either way.
        _relax(target.parent)
        # ``lstat``, so a name that has become a link is declined rather than measured through.
        # ``os.replace`` would unlink such a link rather than follow it, so nothing outside the
        # home could be written either way; what this refuses is rewriting a file whose identity
        # is no longer the one the document came from.
        st = os.lstat(target)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return
        if (st.st_ino, st.st_dev, st.st_mtime_ns, st.st_size) != (
            seen.st_ino,
            seen.st_dev,
            seen.st_mtime_ns,
            seen.st_size,
        ):
            return
        mode = stat.S_IMODE(st.st_mode)
        handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".hosts-", suffix=".yml")
        replaced = False
        try:
            # `os.fdopen` takes ownership of the descriptor, but only once it returns: if it
            # raises, the descriptor is still this process's to close, and the sweep runs before
            # every turn and every hook.
            try:
                stream = os.fdopen(handle, "w")
            except OSError:
                os.close(handle)
                raise
            with stream:
                yaml.safe_dump(stripped, stream, default_flow_style=False, sort_keys=False)
                # Flushed and synced before the rename: the rename is what makes the new file
                # the credential, and a rename that reached the disk ahead of the bytes would
                # leave a truncated `hosts.yml` -- the credential loss this whole function is
                # shaped to avoid -- across an unclean shutdown.
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, target)
            replaced = True
        finally:
            # A `finally` rather than an `except` clause, so the temporary file is cleaned up
            # whichever of the steps above raised and without naming their exception types.
            if not replaced:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
    # Suppressed rather than reported: the replace is atomic, so a failure leaves the original
    # file intact and still carrying the plant, and the next turn sweeps again. This helper's
    # exit status is what ``sweep_home`` reports, and a sweep that cleared every other surface
    # should not read as a total failure because one edit did not land.


def _sweep(home: Path) -> None:
    """Remove, from the account's ``home``, what a prior session could steer the next one with:
    its shell start-up files (``SHELL_STARTUP_SWEEP``), the tool config files that can name a
    command (``TOOL_CONFIG_SWEEP``) and the loadable config surfaces under ``.claude``
    (``CLAUDE_HOME_SWEEP`` and each project's ``CLAUDE_HOME_MEMORY_DIR``).

    Keeps the credential and claude's own runtime state -- and everything else in the home,
    ``.claude.json``, a tool's cache or state directory included -- by naming only what it
    removes.
    Best-effort: an entry that is absent or cannot be removed is skipped, and a symlink is
    unlinked rather than followed, so the tree it points at is never touched.

    A target that is still there after the first attempt is tried once more with the modes put
    back first (``_relax``), because this runs as the account whose home it is clearing and
    every directory in it is that account's own. Unlinking a file needs write on the directory
    holding it and reaching one needs search, while ``git``, ``ssh`` and ``claude`` need only to
    read, so a session that plants ``~/.ssh/config`` and then drops either bit on ``~/.ssh`` --
    or on the home itself, which reaches every list at once -- would otherwise keep its plant at
    no cost to itself, and the sweep would report success. The modes are the plant's, not a
    deployment's: the repair is the one ``_remove`` already makes for a workspace tree, and the
    retry is what makes the removal the account's decision rather than the previous session's.
    """
    for target in _sweep_targets(home):
        _remove_swept(target)
        if _exists(target):
            _relax(target.parent)
            if not target.is_symlink():
                # ``os.walk`` refuses to follow a link *below* its top but follows the top
                # itself, and a swept target that is a link is one a session chose: walking it
                # would widen modes across whatever tree it points at -- any size, any place --
                # for no gain, since unlinking a link needs the parent's bits and nothing of
                # its target's. Relaxing the parent above is the whole repair for that case.
                _relax_tree(target)
            _remove_swept(target)
    # After the removals rather than among them, for two reasons that point the same way: an
    # intermediate symlink on the way to it (``~/.config`` replaced by a link) is one of the
    # targets above and is gone by now, so the walk below resolves inside the real home; and
    # this is an edit rather than a removal, so it belongs after every path-level decision has
    # been made.
    _sweep_gh_hosts(home)


def _remove_swept(target: Path) -> None:
    """One attempt at one swept path: the link or file unlinked, the directory removed whole."""
    with contextlib.suppress(OSError):
        if target.is_symlink() or not target.is_dir():
            target.unlink()
        else:
            shutil.rmtree(target, onexc=lambda *_: None)


def _exists(path: Path) -> bool:
    """Whether anything may be at ``path``: ``False`` only for a definite absence.

    The question this answers is "is there still something here to retry", so the two failures
    have to be told apart. A directory the session closed to *search* (``chmod 0600``, ``0000``)
    answers ``EACCES`` rather than ``ENOENT`` for everything inside it, and reading that as an
    empty home would skip the very retry the mode is what makes necessary. Only
    ``FileNotFoundError`` is an absence; anything else is an answer this process cannot get yet,
    and the retry is what gets it. A broken symlink is present, since ``lstat`` does not follow.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"python -m {MODULE}")
    verbs = parser.add_subparsers(dest="verb", required=True)
    run = verbs.add_parser("exec")
    run.add_argument("--env-fd", type=int, required=True)
    run.add_argument("argv", nargs=argparse.REMAINDER)
    kill = verbs.add_parser("kill")
    kill.add_argument("pgid", type=int)
    remove = verbs.add_parser("remove")
    remove.add_argument("path", type=Path)
    sweep = verbs.add_parser("sweep")
    sweep.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.verb == "exec":
        command = list(args.argv)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("exec: no command")
        _exec(args.env_fd, command)
    elif args.verb == "kill":
        _kill(args.pgid)
    elif args.verb == "sweep":
        _sweep(args.path)
    else:
        _remove(args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
