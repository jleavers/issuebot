"""The image draws the line between the session and the worker (#75), and between one
session and the next (#121), and CI proves both.

This session cannot build the image; the CI ``docker`` job runs the checks. This pins the
shape those checks depend on, so a drift in the Dockerfile, the compose file or the job
itself fails here first. The dashboard's account (#102) and the sweep of the session's shared
``~/.claude`` (#101) are pinned the same way.
"""

import re
from pathlib import Path

import yaml

from issuebot.egress import PROXY_ENV_NAMES

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "compose.yaml").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
SERVICES = yaml.safe_load(COMPOSE)["services"]


def test_two_accounts_and_one_delegation() -> None:
    assert "useradd --create-home --uid 1000 --shell /bin/bash issuebot" in DOCKERFILE
    assert "useradd --create-home --uid 1001 --groups agents --shell /bin/bash agent" in DOCKERFILE
    assert "'issuebot ALL=(%agents) NOPASSWD: ALL'" in DOCKERFILE
    assert "closefrom_override" in DOCKERFILE
    assert "chmod 4750 /usr/bin/sudo" in DOCKERFILE and "chgrp issuebot /usr/bin/sudo" in DOCKERFILE


def test_the_image_records_the_accounts_it_built_and_names_none_in_the_environment() -> None:
    """#142: the pool is the default, expressed once. The list is accumulated *inside* the
    loop that runs `useradd` and written from that accumulator, so it can neither name an
    account the build did not create nor omit one it did -- nothing is re-derived from
    ISSUEBOT_AGENT_POOL_SIZE, whose spelling `seq` and `test` read differently. `agent` alone
    is the fallback for an empty pool, so no image resolves to the host route. And no
    `ENV ISSUEBOT_AGENT_USER` is left to shadow that list with a single account.
    """
    assert "install -d -m 0755 /etc/issuebot" in DOCKERFILE
    assert 'pool="${pool} agent-${n}"' in DOCKERFILE
    assert "printf '%s\\n' ${pool:-agent} > /etc/issuebot/session-accounts" in DOCKERFILE
    assert "chmod 0444 /etc/issuebot/session-accounts" in DOCKERFILE
    assert "ISSUEBOT_AGENT_USER=" not in DOCKERFILE


def test_the_build_reads_its_own_account_list_back() -> None:
    """#142: the list is what `agent.run_as` resolves to in every container, so the build
    asserts it is non-empty and that every account it names resolves on the image."""
    assert "test -s /etc/issuebot/session-accounts" in DOCKERFILE
    assert 'while read -r account; do id -u "${account}" >/dev/null || exit 1; done' in DOCKERFILE


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
    assert 'test "$(stat -c "%u %g %a" /workspaces/one)" = "1000 $(id -g agent-1) 1770"' in CI
    assert 'test "$(id -g agent-1)" != "$(id -g agent-2)"' in CI
    assert "wrote into the other session workspace" in CI
    assert "listed the other session workspace" in CI
    assert "a pool account can invoke sudo" in CI
    # An idle workspace is closed to its own account too: it outlives the run, and there
    # are fewer accounts than workspaces.
    assert 'test "$(stat -c "%u %g %a" /workspaces/idle)" = "1000 $(id -g agent-1) 700"' in CI
    assert "a session entered a sealed workspace bound to its own account" in CI


def test_the_workers_code_and_claude_are_roots_and_home_is_not_pinned() -> None:
    assert "COPY --from=builder /app /app" in DOCKERFILE
    assert "--chown=issuebot" not in DOCKERFILE
    assert "ENV HOME=" not in DOCKERFILE
    assert "/usr/local/bin/claude" in DOCKERFILE
    assert "/home/issuebot/.local/bin" not in DOCKERFILE


def test_no_login_volume_is_mounted_anywhere() -> None:
    """#142: a session account's home holds no login, because nobody logs into it -- the
    credential is in the environment, where every account reads the same one. A volume at that
    path would be a second, stale credential route for the default deployment to disagree with.
    """
    assert "claude-home" not in COMPOSE
    assert "/home/agent/.claude" not in COMPOSE
    assert 'VOLUME ["/workspaces"]' in DOCKERFILE
    assert "/home/agent/.claude" not in DOCKERFILE


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
    """#109: a claude release that dropped any of these would widen what a session may do
    without a word -- its tool set, the servers it loads, or whose files are its configuration
    (#107) -- and would do it one session at a time. The build fails instead."""
    assert "claude --help | grep -q -- '--disallowedTools'" in DOCKERFILE
    assert "claude --help | grep -q -- '--strict-mcp-config'" in DOCKERFILE
    assert "claude --help | grep -q -- '--setting-sources'" in DOCKERFILE
    # `--mcp-config` is matched with its argument, since a bare `--mcp-config` is a substring
    # of `--strict-mcp-config` and of the `--setting-sources` help text beside it.
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
    # 0750 for the two homes no pool loop touches; every session account's is 0700 above, since
    # the worker is a member of each of their groups and of none of these.
    assert "chmod 0750 /home/issuebot /home/web" in DOCKERFILE
    # No line adds it to the worker's group, in any of the spellings that could (CI's `id -Gn`
    # is the invariant itself), and the one rule still names the worker alone.
    assert not re.search(
        r"usermod\b.*\bweb\b|useradd\b.*(?:-G|--groups)\b.*\bweb\b"
        r"|gpasswd\b.*\bweb\b|adduser\b.*\bweb\b",
        DOCKERFILE,
    )
    assert re.findall(r"'(\w+) ALL=\(%?\w+\) NOPASSWD: ALL'", DOCKERFILE) == ["issuebot"]
    # The image's default account stays the worker's: compose is where the web selects its own.
    assert re.findall(r"^USER (\S+)$", DOCKERFILE, re.M) == ["issuebot"]


def test_compose_runs_the_web_as_its_own_account_and_the_worker_as_the_images() -> None:
    assert SERVICES["web"]["user"] == "web"
    # The worker needs the image's `USER issuebot` -- the sudo rule is its -- so it names none;
    # the two services that need no privilege transition at all name accounts of their own.
    assert [name for name, service in SERVICES.items() if "user" in service] == ["egress", "web"]


def test_the_proxy_is_a_fourth_account_with_nothing_of_the_workers() -> None:
    """#126: the proxy container is the one process with a route to the open internet, so it
    runs as an account that can reach nothing else in the image."""
    assert "useradd --create-home --uid 1003 --shell /usr/sbin/nologin egress" in DOCKERFILE
    assert "chmod 0750 /home/issuebot /home/web /home/egress" in DOCKERFILE
    # Nothing puts it in the worker's group (the one the 4750 sudo binary is executable by) or
    # in `agents` (the one the single sudo rule names).
    assert not re.search(
        r"usermod\b.*\begress\b|useradd\b.*(?:-G|--groups)\b.*\begress\b"
        r"|gpasswd\b.*\begress\b|adduser\b.*\begress\b",
        DOCKERFILE,
    )
    assert SERVICES["egress"]["user"] == "egress"
    assert SERVICES["egress"]["command"] == ["egress", "--bind", "0.0.0.0", "--port", "3128"]
    assert SERVICES["egress"]["profiles"] == ["worker"]
    # No volume, no workspace, no credential: a host name off a CONNECT line is all it reads.
    assert "volumes" not in SERVICES["egress"]
    assert list(SERVICES["egress"]["environment"]) == ["ISSUEBOT_EGRESS_ALLOW"]


def test_the_worker_has_no_route_off_the_host_but_the_proxy() -> None:
    """The network half of #126, which is what makes the proxy unavoidable rather than
    advisory: Docker gives a container on internal networks alone no default route."""
    networks = yaml.safe_load(COMPOSE)["networks"]
    assert sorted(SERVICES["worker"]["networks"]) == ["egress", "issuebot-internal"]
    assert networks["egress"]["internal"] is True
    # The shared one is external, so its `--internal` is the operator's to pass and cannot be
    # asserted here -- compose rejects any other attribute beside `external`. The README says
    # so and `validate` probes for a route round the proxy.
    assert networks["issuebot-internal"] == {"external": True}
    assert "--internal issuebot-internal" in (ROOT / "README.md").read_text()
    # The proxy is the only service with a leg on each side.
    assert sorted(SERVICES["egress"]["networks"]) == ["egress", "outside"]
    assert not (networks["outside"] or {}).get("internal")
    assert [
        name for name, service in SERVICES.items() if "outside" in (service.get("networks") or [])
    ] == ["egress"]
    # The hub's database is on both: `issuebot` for the dashboard and its published port,
    # `issuebot-internal` for the worker.
    assert sorted(SERVICES["db"]["networks"]) == ["issuebot", "issuebot-internal"]
    assert SERVICES["web"]["networks"] == ["issuebot"]


def test_the_worker_points_every_client_at_the_proxy() -> None:
    """Both cases of all three names: curl deliberately ignores an upper-case ``HTTP_PROXY``,
    while other clients read only the upper-case spelling."""
    environment = SERVICES["worker"]["environment"]
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert environment[name] == "http://egress:3128"
    for name in ("NO_PROXY", "no_proxy"):
        assert "db" in environment[name].split(",")
    assert set(PROXY_ENV_NAMES) <= set(environment)


def test_ci_proves_a_session_reaches_github_and_nothing_else() -> None:
    assert "docker network create --internal issuebot-internal" in CI
    assert "a session reached a host off the egress allow-list" in CI
    assert "a session left the container without the proxy" in CI
    # Both halves in one step, since either alone proves nothing: a proxy that filters is no
    # bound while there is a route round it, and a closed route is no use if GitHub is closed
    # with it.
    assert "curl -sS -o /dev/null --max-time 30 https://example.com/" in CI
    assert '--noproxy "*"' in CI
    assert "gh api rate_limit --jq .rate.limit" in CI
    # The dashboard's published port still answers, which `db`'s second network could break.
    assert "curl -fsS -o /dev/null http://127.0.0.1:8080/healthz" in CI


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
