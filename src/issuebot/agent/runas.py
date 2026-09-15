"""Running the session as another account: the one privilege the worker delegates (#75).

With ``agent.run_as`` set, ``claude -p``, every hook, the clone and the post-clone setup run
as that account, a different uid from the worker's, so the worker's code and interpreter,
its environment (``/proc/<pid>/environ``), its home and the state it keeps in a workspace
are out of the session's reach. The worker itself stays unprivileged: in the image ``sudo``
carries exactly one rule, ``issuebot`` may become ``agent`` and nobody else, and the binary
is executable by the worker's group alone, so the account the session runs as cannot invoke
it at all.

sudo's environment policy never shapes what the session sees. The worker serialises the
environment it built (``agent_environment`` plus the workspace's ``.issuebot/env``) into an
anonymous file (``anonymous_fd``), passes that one descriptor across the uid change, and the
``exec`` verb of this module -- run by the worker's own interpreter, root-owned in the image
-- installs it whole and execs the command. ``HOME``, ``USER`` and ``LOGNAME`` are the target
account's; everything else is exactly what the worker built. ``python -m
issuebot.agent.runas`` is the module's other face, and it has three verbs: ``exec``,
``kill`` (the agent's process group, since the worker's uid may not signal it) and ``remove``
(the agent's files under a workspace, which the worker's uid may not unlink).
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

MODULE = "issuebot.agent.runas"
# A uid change is quick; past this sudo is treated as wedged rather than waited for.
SUDO_TIMEOUT_S = 10
# Removing a workspace as the agent walks a tree the agent wrote, node_modules included.
REMOVE_TIMEOUT_S = 120


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

    def _delegate(self, *args: str, timeout: float) -> None:
        argv = [*self._sudo(), "--", *self._helper(*args)]
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(argv, capture_output=True, timeout=timeout, check=False)

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


def _remove(path: Path) -> None:
    """Remove every entry the caller owns under ``path``, opening its own directories first.

    Anything else -- the worker's ``.issuebot`` state, a directory it cannot empty -- is left
    for the worker, and no failure is reported: the worker's own removal is what decides.
    """
    me = os.getuid()
    for dirpath, dirnames, _filenames in os.walk(path):
        for name in dirnames:
            child = os.path.join(dirpath, name)
            with contextlib.suppress(OSError):
                st = os.lstat(child)
                if st.st_uid == me and not stat.S_ISLNK(st.st_mode):
                    os.chmod(child, st.st_mode | stat.S_IRWXU)
    shutil.rmtree(path, onexc=lambda *_: None)


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
    else:
        _remove(args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
