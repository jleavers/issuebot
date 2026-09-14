"""The image draws the line between the session and the worker (#75), and CI proves it.

This session cannot build the image; the CI ``docker`` job runs the checks. This pins the
shape those checks depend on, so a drift in the Dockerfile, the compose file or the job
itself fails here first. The dashboard's account (#102) is pinned the same way.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "compose.yaml").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
SERVICES = yaml.safe_load(COMPOSE)["services"]


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


def test_the_dashboard_is_a_third_account_that_cannot_invoke_sudo() -> None:
    """The ``web`` service takes HTTP from a browser and needs no privilege transition, so it
    runs as an account that is not the worker's (#102): outside group ``issuebot``, which is
    the only group the ``4750`` sudo binary is executable by, and with no shell to log in to."""
    assert "useradd --create-home --uid 1002 --shell /usr/sbin/nologin web" in DOCKERFILE
    assert "chmod 0750 /home/issuebot /home/agent /home/web" in DOCKERFILE
    # No line adds it to the worker's group, and the one rule still names the worker alone.
    assert not re.search(r"usermod\b.*\bweb\b|useradd\b.*-G\b.*\bweb\b", DOCKERFILE)
    assert re.findall(r"'(\w+) ALL=\(\w+\) NOPASSWD: ALL'", DOCKERFILE) == ["issuebot"]
    # The image's default account stays the worker's: compose is where the web selects its own.
    assert re.findall(r"^USER (\S+)$", DOCKERFILE, re.M) == ["issuebot"]


def test_compose_runs_the_web_as_its_own_account_and_the_worker_as_the_images() -> None:
    assert SERVICES["web"]["user"] == "web"
    # The worker needs the image's `USER issuebot` -- the sudo rule is its -- so it names none;
    # neither does anything else, since only the dashboard has an account of its own.
    assert [name for name, service in SERVICES.items() if "user" in service] == ["web"]
    assert "issuebot_claude-home" not in SERVICES["web"].get("volumes", [])
    assert "claude-home" not in " ".join(SERVICES["web"].get("volumes", []))


def test_ci_proves_the_dashboards_account_the_way_it_proves_the_sessions() -> None:
    assert "--user web --entrypoint sudo issuebot:ci -n -u agent id -u" in CI
    assert "--user agent --entrypoint sudo issuebot:ci -n -u agent id -u" in CI
    assert 'test "$(docker run --rm --user web --entrypoint id issuebot:ci -u)" = 1002' in CI
    assert "test ! -x /usr/bin/sudo" in CI
    assert "docker run --rm --user web issuebot:ci web --help" in CI
    # And the compose side: the service selects the account rather than inheriting the image's.
    assert 'jq -r .services.web.user)" = web' in CI
