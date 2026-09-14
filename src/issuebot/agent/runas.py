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
anonymous memory file, passes that one descriptor across the uid change, and the ``exec``
verb of this module -- run by the worker's own interpreter, root-owned in the image --
installs it whole and execs the command. ``HOME``, ``USER`` and ``LOGNAME`` are the target
account's; everything else is exactly what the worker built. ``python -m
issuebot.agent.runas`` is the module's other face, and it has three verbs: ``exec``,
``kill`` (the agent's process group, since the worker's uid may not signal it) and ``remove``
(the agent's files under a workspace, which the worker's uid may not unlink).
"""

import argparse
import contextlib
import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

MODULE = "issuebot.agent.runas"
# A uid change is quick; past this sudo is treated as wedged rather than waited for.
SUDO_TIMEOUT_S = 10
# Removing a workspace as the agent walks a tree the agent wrote, node_modules included.
REMOVE_TIMEOUT_S = 120


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
        fd = os.memfd_create("issuebot-agent-env")
        try:
            os.write(fd, payload)
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
