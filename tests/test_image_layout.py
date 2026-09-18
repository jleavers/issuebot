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

from issuebot.config import Settings
from issuebot.egress import PROXY_ENV_NAMES

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "compose.yaml").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
SERVICES = yaml.safe_load(COMPOSE)["services"]

# What the `UV_VERSION` stanza writes to /etc/profile.d/issuebot-uv.sh: the `PATH` line the
# hooks need, since Debian's /etc/profile overwrites `PATH` for a login shell, and nothing
# else. It carried a `UV_LINK_MODE=copy` default until #164 moved uv's cache onto the
# workspaces volume, where the hardlink uv would rather use finally works.
UV_PROFILE_SCRIPT = """   && printf 'PATH="/opt/uv/bin:$PATH"\\n' > /etc/profile.d/issuebot-uv.sh \\
"""


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
    # The npm smoke test, the pwsh smoke test, the README's cluster recipe, and the MCP probe
    # (#119): four steps that mount a script from the runner and run it as the session's own
    # account. Counted rather than listed, so a step that quietly stops running as `agent` --
    # the uid every one of them exists to exercise -- fails here.
    assert CI.count("docker run --rm --user agent -v /tmp/") == 4


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


def test_ci_proves_a_planted_shell_profile_does_not_run_for_the_next_sessions_hook() -> None:
    """#137: the other half of the same home. Every hook and the post-clone setup run under
    ``bash -lc``, so a ``~/.profile`` one session leaves is a script the next session's hooks
    run at the same uid. Proved in the image and two-sided, like the MCP proof: the same login
    shell runs before the sweep, where the plant has to *run*, so a ``bash -lc`` that stopped
    sourcing start-up files could not pass this as a no-op. What the sweep does not name --
    ``.claude.json``, ``.npm`` -- has to still be there afterwards, since the home is swept by
    name and never emptied."""
    assert 'echo \\"echo PROFILE-RAN\\" > /home/agent/.profile' in CI
    assert 'echo \\"echo BASHRC-RAN\\" > /home/agent/.bashrc' in CI
    assert 'planted=$(sudo -n -H -u agent bash -lc "echo hook-ran")' in CI
    assert "*PROFILE-RAN*) ;;" in CI
    assert 'test "$(sudo -n -H -u agent bash -lc "echo hook-ran")" = hook-ran' in CI
    assert "test ! -e /home/agent/.profile" in CI
    assert "test ! -e /home/agent/.bashrc" in CI
    assert "test -f /home/agent/.claude.json" in CI
    assert "test -d /home/agent/.npm" in CI
    # And the account still works with them gone: `claude` for it directly (the login recipe
    # of the README) and in a login shell, whose PATH is /etc/profile's rather than a profile
    # the sweep just removed.
    assert "sudo -n -H -u agent claude --version" in CI
    assert 'sudo -n -H -u agent bash -lc "claude --version"' in CI


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


_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _seconds(duration: str) -> float:
    """A compose duration (`30s`, `1m30s`, `500ms`) as seconds.

    `ms` is matched before `m`, which is the whole subtlety: read left to right, `500ms` is
    otherwise 500 minutes.
    """
    total = 0.0
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|[smh])", str(duration)):
        total += float(number) * _UNITS[unit]
    assert total > 0, f"unparsed compose duration: {duration!r}"
    return total


def test_the_worker_waits_for_the_proxy_it_has_no_route_without() -> None:
    """#126: `docker compose run --rm worker validate` is the README's step 2, and it runs
    before anything is up. The worker is on internal networks alone, so without the proxy
    started first it has no GitHub, no Anthropic and a failing `validate` rather than a
    degraded one -- and `run` starts only the service named and its dependencies.

    `egress` is project-local and in the same profile, so compose can order it; the database
    deliberately has no such entry, since it may live in another checkout's project.
    """
    assert SERVICES["worker"]["depends_on"] == {"egress": {"condition": "service_healthy"}}
    # Waiting on health means the health check has to become healthy promptly, or the first
    # `compose run` sits through a whole interval before its first probe -- so the start
    # interval has to be meaningfully shorter than the steady-state one, not merely present.
    health = SERVICES["egress"]["healthcheck"]
    assert _seconds(health["start_interval"]) < _seconds(health["interval"])
    # And the start period has to be short enough that a proxy which can never pass still
    # reaches `unhealthy` quickly, since failures in it do not count against `retries`.
    assert _seconds(health["start_period"]) <= 10
    # An older engine rejects `start_interval` outright, so the floor is a documented one.
    assert "Engine 25.0 or newer" in (ROOT / "README.md").read_text()


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


def test_the_python_toolchain_is_off_by_default_and_reaches_both_kinds_of_shell() -> None:
    """``uv`` is the third optional toolchain, beside the PostgreSQL server (#62) and node
    (#64), and it is built the same way: empty is the default, so the image keeps exactly the
    contents it has without the argument, and a deployment whose target repository is a Python
    project sets ``ISSUEBOT_UV_VERSION``.

    Both paths are needed for the same reason node needs both. The ``ENV`` covers a session's
    own ``claude`` tools, and the ``profile.d`` line covers the hooks, which run under
    ``bash -lc`` -- and Debian's ``/etc/profile`` *overwrites* ``PATH`` for a login shell, so
    the ``ENV`` alone would leave ``after_create``'s ``uv sync`` looking for a binary that is
    on the image's own ``PATH`` and not on the one it was handed.
    """
    assert 'ARG UV_VERSION=""' in DOCKERFILE
    assert "${UV_VERSION:+/opt/uv/bin:}" in DOCKERFILE
    # The whole `printf` run, so that the `PATH` line stays tied to *this* file: two loose
    # substrings would still pass with it written into `issuebot-node.sh`.
    assert UV_PROFILE_SCRIPT in DOCKERFILE
    # Asserted at build for the reason `initdb --version` and `node --version` are: a moved
    # download or a renamed asset has to fail the build, not the first session that runs it.
    assert "/opt/uv/bin/uv --version" in DOCKERFILE
    # The checksum comes from the release's own .sha256, so a tarball that is not the one
    # astral published fails the build rather than being installed.
    assert "sha256sum -c uv.sha256" in DOCKERFILE


def test_the_uv_profile_states_no_link_mode_now_the_cache_is_on_the_volume() -> None:
    """#161 defaulted uv's link mode to ``copy``, because uv's cache was under
    ``$HOME/.cache/uv`` -- in the container's own writable layer -- while the venv it builds is
    ``<workspace>/.venv`` on the mounted volume, and a hardlink cannot cross the two. Every
    ``uv sync`` fell back to a full copy and warned three lines about it on the stderr of
    ``after_create``, the first hook of every session.

    #164 removed the reason instead: the worker puts the cache on the workspaces volume, one
    directory per session account (``agent/uvcache.py``), and hands it to every hook and every
    turn as ``UV_CACHE_DIR``. One filesystem, so uv's own default -- hardlink -- is what works,
    and a ``copy`` default written into the image would now be the one thing stopping it. So
    the profile script is the ``PATH`` line and nothing else, and the runtime stage states
    nothing about the link mode at all.

    A deployment that does want ``copy`` back still has both of #161's routes, unchanged:
    ``uv sync --link-mode=copy`` in the hook line, or ``UV_LINK_MODE=copy`` in an
    ``.issuebot/env`` written from ``before_run``. Neither ``UV_LINK_MODE`` nor
    ``UV_CACHE_DIR`` is in ``PASSTHROUGH_NAMES`` or ``PROTECTED_ENV_NAMES``, which is what
    makes that file the override.
    """
    assert UV_PROFILE_SCRIPT in DOCKERFILE
    # Not a line of the runtime stage mentions it any more: every remaining occurrence is a
    # comment. (The *builder* stage sets it for itself, over the buildkit cache mount, and is a
    # different image.)
    runtime = DOCKERFILE.split("AS runtime", 1)[1]
    mentions = [line.strip() for line in runtime.splitlines() if "UV_LINK_MODE" in line]
    assert mentions, "the reasoning for not setting it belongs in the file"
    assert all(line.startswith("#") for line in mentions)
    # And the same on the image CI actually builds: the opt-in build states nothing, while a
    # value handed in still arrives, which is the `.issuebot/env` route.
    assert (
        'toolchain="$(docker run --rm --entrypoint bash issuebot:ci-toolchain'
        ' -lc \'echo ${UV_LINK_MODE-}\')"\n          test -z "$toolchain"' in CI
    )
    assert (
        "docker run --rm -e UV_LINK_MODE=hardlink --entrypoint bash issuebot:ci-toolchain"
        " -lc 'echo ${UV_LINK_MODE-}'" in CI
    )


def test_the_uv_cache_sits_on_the_volume_that_outlives_the_container() -> None:
    """Half of what #164 is for: a cache in the container's own writable layer is discarded on
    every ``docker compose up -d worker``, so the next session re-downloads it from PyPI.

    The worker keeps it at ``<workspace.root>/.uv-cache/<account>``, and the default root is
    ``/workspaces`` -- which compose mounts from a *named* volume rather than binding or
    tmpfs'ing, so recreating the container remounts the same cache. That is the whole claim,
    and it rests on those three facts together.
    """
    assert Settings.model_validate({"github": {"repo": "o/r"}}).workspace.root == Path(
        "/workspaces"
    )
    assert 'VOLUME ["/workspaces"]' in DOCKERFILE
    assert "workspaces:/workspaces" in SERVICES["worker"]["volumes"]
    volumes = yaml.safe_load(COMPOSE)["volumes"]
    assert "workspaces" in volumes and not volumes["workspaces"]


def test_compose_offers_the_python_toolchain_to_the_worker_alone() -> None:
    """Like the other two: the worker runs the sessions, and the web service builds from the
    same context without it, since the dashboard runs no session and would otherwise carry the
    binaries twice."""
    assert 'UV_VERSION: "${ISSUEBOT_UV_VERSION:-}"' in COMPOSE
    assert "UV_VERSION" not in yaml.safe_dump(SERVICES["web"])


def test_ci_proves_uv_answers_on_both_paths_in_the_toolchain_image() -> None:
    """The opt-in image is built once with every toolchain argument set (#62, #64), and each
    one must answer on its own ``PATH`` and in a login shell. ``uv`` joins that build rather
    than earning one of its own: the checks are about what is on ``PATH`` and under which uid,
    not about the arguments interacting.
    """
    assert "UV_VERSION=" in CI
    assert "docker run --rm --entrypoint uv issuebot:ci-toolchain --version" in CI
    # And the other half of "off by default": the *default* build must carry none of it, which
    # is the assertion that would catch a COPY --from placed outside the argument's guard.
    assert "for tool in initdb node npm uv pwsh; do" in CI
    assert "command -v initdb && command -v node && command -v npm && command -v uv" in CI


def test_the_powershell_toolchain_is_off_by_default_and_reaches_both_kinds_of_shell() -> None:
    """``pwsh`` is the fourth optional toolchain, beside the PostgreSQL server (#62), node
    (#64) and uv (#128), and it is built the same way: empty is the default, so the image
    keeps exactly the contents it has without the argument, and a deployment whose target
    repository is a PowerShell project sets ``ISSUEBOT_PWSH_VERSION``.

    Both paths are needed for the reason uv needs both. The ``ENV`` covers a session's own
    ``claude`` tools, and the ``profile.d`` line covers the hooks, which run under
    ``bash -lc`` -- and Debian's ``/etc/profile`` *overwrites* ``PATH`` for a login shell, so
    the ``ENV`` alone would leave a hook's ``pwsh`` looking for a binary that is on the
    image's own ``PATH`` and not on the one it was handed.
    """
    assert 'ARG PWSH_VERSION=""' in DOCKERFILE
    assert "${PWSH_VERSION:+/opt/powershell/bin:}" in DOCKERFILE
    # The whole `printf`, so that the `PATH` line stays tied to *this* file: two loose
    # substrings would still pass with it written into `issuebot-uv.sh`.
    assert (
        "printf 'PATH=\"/opt/powershell/bin:$PATH\"\\n' > /etc/profile.d/issuebot-pwsh.sh"
        in DOCKERFILE
    )
    # Asserted at build for the reason `initdb --version`, `node --version` and `uv --version`
    # are: a moved download or a renamed asset has to fail the build, not the first session
    # that runs the target repository's suite.
    assert "/opt/powershell/bin/pwsh --version" in DOCKERFILE
    # The checksum comes from the release's own `hashes.sha256` beside the tarball, so an
    # archive that is not the one Microsoft published fails the build rather than being
    # installed. That file is UTF-16, and `sha256sum -c` reads bytes: without the transcode
    # every line is unparseable and the check passes over an empty list of digests, which is
    # the failure mode worth pinning -- it is silent.
    assert "iconv -f UTF-16 -t UTF-8" in DOCKERFILE
    assert "sha256sum -c --ignore-missing hashes.sha256" in DOCKERFILE


def test_the_icu_runtime_rides_on_the_powershell_guard() -> None:
    """.NET reads globalization data from ICU, and the base image carries none: without it
    ``pwsh`` falls back to invariant mode, where ``"{0:N2}"`` stops grouping and a suite that
    formats numbers or compares strings by culture quietly changes its answers.

    So the package is installed *inside* the ``PWSH_VERSION`` guard -- an image built without
    the argument carries no ICU either, which is what "off by default" has to mean for the
    whole arm and not just the tarball -- and it is resolved by name rather than pinned,
    because the package is named after the ABI (``libicu76`` on trixie) and a ``PYTHON_IMAGE``
    bump to the next Debian renames it.
    """
    runtime = DOCKERFILE.split("AS runtime", 1)[1]
    stanza = runtime.split('ARG PWSH_VERSION=""', 1)[1].split("\n    fi", 1)[0]
    assert "apt-cache --names-only search '^libicu[0-9][0-9]*$'" in stanza
    mentions = [
        line
        for line in runtime.splitlines()
        if "libicu" in line and not line.strip().startswith("#")
    ]
    assert mentions and all(line in stanza for line in mentions)


def test_compose_offers_the_powershell_toolchain_to_the_worker_alone() -> None:
    """Like the other three: the worker runs the sessions, and the web service builds from the
    same context without it, since the dashboard runs no session and would otherwise carry a
    180 MB runtime twice."""
    assert 'PWSH_VERSION: "${ISSUEBOT_PWSH_VERSION:-}"' in COMPOSE
    assert "PWSH_VERSION" not in yaml.safe_dump(SERVICES["web"])


def test_ci_proves_pwsh_answers_on_both_paths_and_at_a_session_account() -> None:
    """``pwsh`` joins the one opt-in build rather than earning its own, for the reason uv did:
    the checks are about what is on ``PATH`` and under which uid, not about the arguments
    interacting.

    And it runs a script as ``agent``, which is the half ``--version`` cannot prove. ``pwsh``
    writes a history file and a module cache under ``$HOME`` on first use, so a session
    account whose home it cannot write is a suite that fails at the session's uid and nowhere
    else -- the same shape as the ``npm ci`` fixture (#64), which exists for ``$HOME/.npm``.
    """
    assert "PWSH_VERSION=" in CI
    assert "docker run --rm --entrypoint pwsh issuebot:ci-toolchain --version" in CI
    assert "command -v pwsh" in CI
    assert "docker run --rm --user agent -v /tmp/pwsh-smoke:/pwsh-smoke:ro" in CI
    # And the other half of "off by default": the *default* build must carry none of it.
    assert "for tool in initdb node npm uv pwsh; do" in CI
