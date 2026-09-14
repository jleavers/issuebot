"""The image draws the line between the session and the worker (#75), and between one
session and the next (#121), and CI proves both.

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
    assert "useradd --create-home --uid 1001 --groups agents --shell /bin/bash agent" in DOCKERFILE
    assert "'issuebot ALL=(%agents) NOPASSWD: ALL'" in DOCKERFILE
    assert "closefrom_override" in DOCKERFILE
    assert "chmod 4750 /usr/bin/sudo" in DOCKERFILE and "chgrp issuebot /usr/bin/sudo" in DOCKERFILE
    assert "ISSUEBOT_AGENT_USER=agent" in DOCKERFILE


def test_the_session_accounts_are_a_pool_the_worker_may_give_a_workspace_to() -> None:
    """#121: N accounts in one group the sudo rule names, each with a home of its own, and the
    worker a member of each account's own group -- the one thing `share_with` needs."""
    assert "ARG ISSUEBOT_AGENT_POOL_SIZE=3" in DOCKERFILE
    assert "groupadd --system agents" in DOCKERFILE
    assert '--uid "$((1010 + n))" --groups agents --shell /bin/bash "agent-${n}"' in DOCKERFILE
    assert 'usermod --append --groups "${account}" issuebot' in DOCKERFILE
    # Every session home is closed to the worker's group membership, which is why it is 0700.
    assert 'chmod 0700 "/home/${account}"' in DOCKERFILE
    assert 'install -d -m 0700 -o "${account}" -g "${account}" "/home/${account}/.claude"' in (
        DOCKERFILE
    )
    assert 'test "$(sudo -n -u agent-1 id -u)" = 1011' in DOCKERFILE


def test_ci_proves_one_session_cannot_enter_another_sessions_workspace() -> None:
    assert "from issuebot.agent.accounts import seal, share_with" in CI
    assert 'test "$(stat -c "%u %g %a" /workspaces/one)" = "1000 1011 1770"' in CI
    assert "wrote into the other session workspace" in CI
    assert "listed the other session workspace" in CI
    assert "a pool account can invoke sudo" in CI
    # An idle workspace is closed to its own account too: it outlives the run, and there
    # are fewer accounts than workspaces.
    assert 'test "$(stat -c "%u %g %a" /workspaces/idle)" = "1000 1011 700"' in CI
    assert "a session entered a sealed workspace bound to its own account" in CI


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
