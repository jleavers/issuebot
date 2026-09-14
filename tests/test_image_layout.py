"""The image draws the line between the session and the worker (#75), and CI proves it.

This session cannot build the image; the CI ``docker`` job runs the checks. This pins the
shape those checks depend on, so a drift in the Dockerfile, the compose file or the job
itself fails here first.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "compose.yaml").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()


def test_two_accounts_and_one_delegation() -> None:
    assert "useradd --create-home --uid 1000 --shell /bin/bash issuebot" in DOCKERFILE
    assert "useradd --create-home --uid 1001 --shell /bin/bash agent" in DOCKERFILE
    assert "'issuebot ALL=(agent) NOPASSWD: ALL'" in DOCKERFILE
    assert "closefrom_override" in DOCKERFILE
    assert "chmod 4750 /usr/bin/sudo" in DOCKERFILE and "chgrp issuebot /usr/bin/sudo" in DOCKERFILE
    assert "ISSUEBOT_AGENT_USER=agent" in DOCKERFILE


def test_the_workers_code_and_claude_are_roots_and_home_is_not_pinned() -> None:
    assert "COPY --from=builder /app /app" in DOCKERFILE
    assert "--chown=issuebot" not in DOCKERFILE
    assert "ENV HOME=" not in DOCKERFILE
    assert "/usr/local/bin/claude" in DOCKERFILE
    assert "/home/issuebot/.local/bin" not in DOCKERFILE


def test_the_login_volume_is_the_sessions_home() -> None:
    assert "claude-home:/home/agent/.claude" in COMPOSE
    assert "/home/issuebot/.claude" not in COMPOSE
    assert "/home/issuebot/.claude" not in DOCKERFILE


def test_the_session_may_run_git_in_the_workspace_the_worker_owns() -> None:
    """The workspace directory is the worker's and the clone inside it the session's, so git's
    ownership check needs an exception -- scoped to the workspace root, never a bare ``*``."""
    assert "git config --system --add safe.directory '/workspaces/*'" in DOCKERFILE
    assert "safe.directory '*'" not in DOCKERFILE
    assert "config --system --get-all safe.directory" in CI
    assert "git config --local --add issuebot.probe 1" in CI


def test_ci_proves_the_boundary_and_runs_hook_shaped_steps_as_the_session() -> None:
    assert "--user agent --entrypoint sudo issuebot:ci" in CI
    assert "/proc/$!/environ" in CI
    assert "issuebot.agent.runas" in CI
    assert CI.count("docker run --rm --user agent -v /tmp/") == 2


def test_ci_proves_the_session_home_sweep() -> None:
    """The shared ``~/.claude`` config a session plants is swept before the next session, and
    the credential and runtime state are kept (#101). Proved in the image, where the uid split
    and the volume are real."""
    assert "issuebot.agent.runas sweep /home/agent/.claude" in CI
    assert "echo poison > /home/agent/.claude/commands/evil.md" in CI
    assert "test ! -e /home/agent/.claude/commands" in CI
    assert "test -f /home/agent/.claude/.credentials.json" in CI
