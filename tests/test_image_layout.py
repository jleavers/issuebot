"""The image draws the line between the session and the worker (#75), and CI proves it.

This session cannot build the image; the CI ``docker`` job runs the checks. This pins the
shape those checks depend on, so a drift in the Dockerfile, the compose file or the job
itself fails here first. The dashboard's account (#102) and the sweep of the session's shared
``~/.claude`` (#101) are pinned the same way.
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
    # The npm smoke test, the README's cluster recipe, and the MCP probe (#119): three steps
    # that mount a script from the runner and run it as the session's own account.
    assert CI.count("docker run --rm --user agent -v /tmp/") == 3


def test_ci_proves_a_planted_mcp_server_is_not_loaded_from_the_sessions_home() -> None:
    """Both directions, against the image's own claude (#119).

    Without the flag the planted server must be *listed*, or the proof would pass equally
    against a claude that had stopped reading ``~/.claude.json`` -- at which point the step
    would be testing nothing while still going green.
    """
    assert "--strict-mcp-config" in CI
    # Beside `--permission-prompts`: both are passed on every turn and neither is a setting,
    # so a release that drops either must fail the build rather than a session.
    assert "claude --help | grep -q -- '--permission-prompts'" in DOCKERFILE
    assert "claude --help | grep -q -- '--strict-mcp-config'" in DOCKERFILE
    assert '"mcpServers":{"planted"' in CI
    assert "*'\"planted\"'*) ;;" in CI
    assert "claude did not load the planted server, so this proves nothing" in CI
    assert "*'\"mcp_servers\":[]'*) ;;" in CI


def test_the_flags_the_sessions_authority_depends_on_are_asserted_at_build() -> None:
    """#109: a claude release that dropped either flag would widen every session's tool set
    without a word; the build fails instead."""
    assert "claude --help | grep -q -- '--disallowedTools'" in DOCKERFILE
    assert "claude --help | grep -q -- '--strict-mcp-config'" in DOCKERFILE
    # `--mcp-config` is the only route by which a server reaches a session, so a rename there
    # would break the deployments that name one, one session at a time.
    # Matched with its argument, since a bare `--mcp-config` is a substring of
    # `--strict-mcp-config` and of the `--setting-sources` help text beside it.
    assert "claude --help | grep -q -- '--mcp-config <'" in DOCKERFILE


def test_ci_proves_the_session_home_sweep() -> None:
    """The shared ``~/.claude`` config a session plants is swept before the next session, and
    the credential and transcripts are kept (#101). Proved in the image, where the uid split
    and the home are real, and through ``RunAs.sweep_home`` with its default target, so the
    worker's own code path is what passes -- not the helper verb run by hand."""
    assert 'RunAs(\\"agent\\").sweep_home()' in CI
    assert "issuebot.agent.runas sweep" not in CI
    assert "cd /home/agent/.claude" in CI
    for planted in ("commands", "skills", "rules", "projects/-workspaces-issuebot-7/memory"):
        assert f"test ! -e {planted}" in CI, planted
    assert "echo poison > commands/evil.md" in CI
    assert "echo poison > skills/evil/SKILL.md" in CI
    assert "echo poison > rules/evil.md" in CI
    assert "echo poison > projects/-workspaces-issuebot-7/memory/MEMORY.md" in CI
    assert "test -f .credentials.json" in CI
    assert "test -f projects/-workspaces-issuebot-7/keep.jsonl" in CI


def test_the_dashboard_is_a_third_account_that_cannot_invoke_sudo() -> None:
    """The ``web`` service takes HTTP from a browser and needs no privilege transition, so it
    runs as an account that is not the worker's (#102): outside group ``issuebot``, which is
    the only group the ``4750`` sudo binary is executable by, and with no shell to log in to."""
    assert "useradd --create-home --uid 1002 --shell /usr/sbin/nologin web" in DOCKERFILE
    assert "chmod 0750 /home/issuebot /home/agent /home/web" in DOCKERFILE
    # No line adds it to the worker's group, in any of the spellings that could (CI's `id -Gn`
    # is the invariant itself), and the one rule still names the worker alone.
    assert not re.search(
        r"usermod\b.*\bweb\b|useradd\b.*(?:-G|--groups)\b.*\bweb\b"
        r"|gpasswd\b.*\bweb\b|adduser\b.*\bweb\b",
        DOCKERFILE,
    )
    assert re.findall(r"'(\w+) ALL=\(\w+\) NOPASSWD: ALL'", DOCKERFILE) == ["issuebot"]
    # The image's default account stays the worker's: compose is where the web selects its own.
    assert re.findall(r"^USER (\S+)$", DOCKERFILE, re.M) == ["issuebot"]


def test_compose_runs_the_web_as_its_own_account_and_the_worker_as_the_images() -> None:
    assert SERVICES["web"]["user"] == "web"
    # The worker needs the image's `USER issuebot` -- the sudo rule is its -- so it names none;
    # neither does anything else, since only the dashboard has an account of its own.
    assert [name for name, service in SERVICES.items() if "user" in service] == ["web"]
    assert "claude-home" not in " ".join(SERVICES["web"].get("volumes", []))


def test_ci_proves_the_dashboards_account_the_way_it_proves_the_sessions() -> None:
    assert "--user web --entrypoint sudo issuebot:ci -n -u agent id -u" in CI
    assert "--user agent --entrypoint sudo issuebot:ci -n -u agent id -u" in CI
    assert 'test "$(docker run --rm --user web --entrypoint id issuebot:ci -u)" = 1002' in CI
    assert "test ! -x /usr/bin/sudo" in CI
    # The app is started under it for real, as far as a database it has none of.
    assert "docker run --rm --user web -e ISSUEBOT_WEB_PASSWORD=ci-only" in CI
    # With a database named, since `not configured` shares the prefix and is printed before
    # any attempt: the grep has to see the refused connection.
    assert "-e DATABASE_URL=postgresql://issuebot@127.0.0.1:1/issuebot" in CI
    assert "grep -q '^\\[FAIL\\] database: cannot connect: ' /tmp/web-start.txt" in CI
    # Under `set -e` only the last command of an AND list is enforced, so the sandbox scripts
    # keep one `test` per line.
    assert not re.search(r"^\s*test .* && test ", CI, re.M)
    # And the compose side: the service selects the account rather than inheriting the image's.
    assert 'jq -r .services.web.user)" = web' in CI
