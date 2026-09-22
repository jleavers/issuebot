"""Tests for the claude -p runner."""

import asyncio
import io
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from issuebot.agent import runner as runner_module
from issuebot.agent.runner import (
    FIXED_ENVIRONMENT,
    MIN_CLAUDE_VERSION,
    PROTECTED_ENV_NAMES,
    PROTECTED_ENV_PREFIXES,
    PROXY_ENV_NAMES,
    SHELL_ENV_NAMES,
    TOOL_CONFIG_ENV_NAMES,
    TOOL_CONFIG_ENV_PREFIXES,
    WORKSPACE_ENV_LIMIT,
    ClaudeAuth,
    ClaudeRunner,
    RateLimitWindow,
    StreamParser,
    TurnEvent,
    TurnResult,
    agent_environment,
    classify_result,
    claude_auth_status,
    claude_md_allowlist,
    describe_claude_auth,
    is_auth_failure,
    merge_workspace_env,
    parse_claude_version,
    parse_workspace_env,
    read_workspace_env,
    settings_for_labels,
    workspace_environment,
)
from issuebot.agent.scrub import REDACTED, Scrubber
from issuebot.config import Settings
from issuebot.log import configure_logging

FAKE_CLAUDE = Path(__file__).parent / "fakes" / "claude"
FIXTURES = Path(__file__).parent / "fixtures" / "claude"
SESSION_ID = "11111111-2222-4333-8444-555555555555"
RECORDED_SESSION_ID = "00000000-0000-4000-8000-000000000000"


def settings(root: Path, **claude: object) -> Settings:
    return Settings.model_validate(
        {
            "github": {"repo": "example/repo"},
            "workspace": {"root": str(root)},
            "claude": {"command": str(FAKE_CLAUDE), **claude},
        }
    )


# --- argv and environment --------------------------------------------------------------


def test_build_argv_fresh_session_has_fixed_flags(tmp_path: Path) -> None:
    runner = ClaudeRunner(settings(tmp_path), environ={"HOME": "/home/agent"})
    assert runner.build_argv(session_id=SESSION_ID, resume=False, workspace=tmp_path / "ws") == [
        str(FAKE_CLAUDE),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "auto",
        "--permission-prompts",
        "none",
        "--strict-mcp-config",
        "--max-budget-usd",
        "5.0",
        # Never claude's default (#107): the clone's files are not its configuration.
        "--setting-sources",
        "user",
        # #135: CLAUDE.md and its `@` includes are kept to this workspace and the two paths
        # the account's own config directory holds as user memory, so an approval in
        # `~/.claude.json` names nothing outside them.
        "--settings",
        '{"claudeMdExcludes": ["!{%s/**,/home/agent/.claude/rules/**,'
        '/home/agent/.claude/CLAUDE.md}"]}' % (tmp_path / "ws"),
        "--session-id",
        SESSION_ID,
        "--disallowedTools",
        "WebFetch",
        "WebSearch",
    ]


def test_the_tool_policy_is_a_setting_and_nothing_else_widens_it(tmp_path: Path) -> None:
    """#109: the session's tools are fixed by the argv, from the front matter. The default
    denies the model's own network tools and loads no MCP server; an operator widens the deny
    list by emptying it, and even then the MCP flag stays."""
    argv = ClaudeRunner(settings(tmp_path, disallowed_tools=[]), environ={}).build_argv(
        session_id=SESSION_ID, resume=False, workspace=tmp_path / "ws"
    )
    assert "--disallowedTools" not in argv
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv
    # The MCP half is widened the same way, by naming the servers in the front matter, and
    # the strict flag stays so nothing else joins them.
    argv = ClaudeRunner(settings(tmp_path, mcp_config=["a.json", "b.json"]), environ={}).build_argv(
        session_id=SESSION_ID, resume=False, workspace=tmp_path / "ws"
    )
    assert argv[-3:] == ["--mcp-config", "a.json", "b.json"]
    assert "--strict-mcp-config" in argv


def test_build_argv_resume_and_every_optional_flag(tmp_path: Path) -> None:
    runner = ClaudeRunner(
        settings(
            tmp_path,
            model="opus",
            permission_mode="bypassPermissions",
            max_budget_usd=2.5,
            setting_sources=["project", "local"],
            append_system_prompt="Be terse.",
            allowed_tools=["Read", "Bash(git *)"],
            disallowed_tools=["WebFetch"],
            mcp_config=["/etc/issuebot/mcp.json"],
        ),
        environ={},
    )
    argv = runner.build_argv(session_id=SESSION_ID, resume=True, workspace=tmp_path / "ws")
    assert argv[6] == "bypassPermissions"
    assert argv[9] == "--strict-mcp-config"
    assert argv[11] == "2.5"
    assert argv[12:14] == ["--setting-sources", "project,local"]
    assert argv[14:16] == ["--resume", SESSION_ID]
    assert argv[16:] == [
        "--model",
        "opus",
        "--append-system-prompt",
        "Be terse.",
        "--allowedTools",
        "Read",
        "Bash(git *)",
        "--disallowedTools",
        "WebFetch",
        "--mcp-config",
        "/etc/issuebot/mcp.json",
    ]
    assert "--session-id" not in argv


# --- the MCP config a session must not be able to plant (#119) -------------------------


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({}, id="defaults"),
        pytest.param({"setting_sources": ["project"]}, id="setting-sources-project"),
        pytest.param({"setting_sources": ["user", "project"]}, id="setting-sources-user-project"),
        pytest.param({"permission_mode": "bypassPermissions"}, id="bypass-permissions"),
        pytest.param({"allowed_tools": ["Read"]}, id="allowed-tools"),
    ],
)
def test_build_argv_always_confines_mcp_to_the_command_line(
    tmp_path: Path, resume: bool, extra: dict[str, object]
) -> None:
    """No setting reaches ``--strict-mcp-config``, on a fresh session or a resumed one.

    The session account's ``~/.claude.json`` outlives every session in one container, and
    ``claude`` loads ``mcpServers`` from it, so the flag is what stops a planted server being
    offered to the next issue's session. The cases are the settings that might look as though
    they already cover it -- ``setting_sources: [project]`` does suppress the same entry, while
    ``[user, project]`` does not and neither does the default, which is ``[user]`` since #107 --
    and each is named, so a failure says which shape broke rather than which loop iteration.
    """
    runner = ClaudeRunner(settings(tmp_path, **extra), environ={})  # type: ignore[arg-type]
    argv = runner.build_argv(session_id=SESSION_ID, resume=resume, workspace=tmp_path / "ws")
    assert "--strict-mcp-config" in argv
    # No `--mcp-config` beside it: the flag keeps only the servers named there, and none of
    # these settings names one, so the loadable set is nothing. The one that does is
    # `claude.mcp_config` (#109), the front matter's, empty by default and pinned in
    # `test_the_tool_policy_is_a_setting_and_nothing_else_widens_it`.
    assert "--mcp-config" not in argv


@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resumed"])
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({}, id="default"),
        pytest.param({"setting_sources": ["user"]}, id="setting-sources-user"),
        pytest.param({"setting_sources": ["user", "project"]}, id="setting-sources-user-project"),
        pytest.param({"setting_sources": ["project", "local"]}, id="setting-sources-project-local"),
        pytest.param({"permission_mode": "bypassPermissions"}, id="bypass-permissions"),
    ],
)
def test_build_argv_always_confines_claude_md_to_the_workspace_and_the_account(
    tmp_path: Path, resume: bool, extra: dict[str, object]
) -> None:
    """No setting reaches the CLAUDE.md allow-list either (#135), fresh or resumed.

    `hasClaudeMdExternalIncludesApproved`, in the session account's `~/.claude.json` under the
    git root of the workspace, is what lets a Project or Local `CLAUDE.md` `@`-include a path
    outside the clone -- measured, and the key is one a `-p` session can write for the next
    session at that path. `claude.setting_sources` is what decides whether such a CLAUDE.md is
    loaded at all, so it might look as though `[user]` already covers this; it does not, since
    the *user* CLAUDE.md's own external includes are on whatever the key says. So the flag is
    unconditional, like `--strict-mcp-config`, and these are the settings that might look as
    though they cover it, each named so a failure says which shape broke.
    """
    runner = ClaudeRunner(settings(tmp_path, **extra), environ={"HOME": "/home/agent"})  # type: ignore[arg-type]
    argv = runner.build_argv(session_id=SESSION_ID, resume=resume, workspace=tmp_path / "ws")
    assert json.loads(argv[argv.index("--settings") + 1]) == {
        "claudeMdExcludes": [
            f"!{{{tmp_path / 'ws'}/**,/home/agent/.claude/rules/**,/home/agent/.claude/CLAUDE.md}}"
        ]
    }


def test_the_claude_md_allow_list_is_one_negated_pattern_over_every_arm() -> None:
    """One brace, not one pattern per arm (#135).

    picomatch matches a list when *any* pattern matches, so a negation per arm would have each
    one match everything outside its own arm, and they would OR together to "exclude
    everything" -- which is not a weaker allow-list but a session with no instructions at all.
    """
    allowlist = claude_md_allowlist(
        trees=[Path("/ws/a"), Path("/home/agent/.claude/rules")],
        files=[Path("/home/agent/.claude/CLAUDE.md")],
    )
    assert json.loads(allowlist) == {
        "claudeMdExcludes": [
            "!{/ws/a/**,/home/agent/.claude/rules/**,/home/agent/.claude/CLAUDE.md}"
        ]
    }
    # One arm needs no brace: `{a}` is not reliably one arm to picomatch.
    assert json.loads(claude_md_allowlist(trees=[Path("/ws/a")])) == {
        "claudeMdExcludes": ["!/ws/a/**"]
    }
    assert claude_md_allowlist() is None


def test_the_claude_md_allow_list_keeps_the_account_home_out_of_the_tree(tmp_path: Path) -> None:
    """The config directory is allowed by its two memory paths and never wholesale (#135).

    `claude` reads exactly `CLAUDE.md` and `rules/` from it as user memory. The rest is
    `.credentials.json`, the transcripts of other sessions at the same uid that
    `CLAUDE_HOME_SWEEP` deliberately keeps, and the caches -- none of it instructions, and all
    of it inside the one directory a clone's CLAUDE.md would most want to `@` include.
    """
    runner = ClaudeRunner(settings(tmp_path), environ={"HOME": "/home/agent"})
    argv = runner.build_argv(session_id=SESSION_ID, resume=False, workspace=tmp_path / "ws")
    (pattern,) = json.loads(argv[argv.index("--settings") + 1])["claudeMdExcludes"]
    assert "/home/agent/.claude/CLAUDE.md" in pattern
    assert "/home/agent/.claude/rules/**" in pattern
    assert "/home/agent/.claude/**" not in pattern


def test_the_claude_md_allow_list_follows_claude_config_dir(tmp_path: Path) -> None:
    """User memory moves with `$CLAUDE_CONFIG_DIR`, so the allow-list has to (#135).

    `claude` resolves `CLAUDE.md` and `rules/` against `$CLAUDE_CONFIG_DIR` when one is set,
    and `CLAUDE_` is a `PASSTHROUGH_PREFIXES` entry, so a deployment that sets one in `.env`
    gets it in the child. Naming `~/.claude` regardless would leave the operator's own user
    memory excluded by the very flag meant to preserve it -- and silently.
    """
    runner = ClaudeRunner(
        settings(tmp_path), environ={"HOME": "/home/agent", "CLAUDE_CONFIG_DIR": "/etc/cfg"}
    )
    argv = runner.build_argv(session_id=SESSION_ID, resume=False, workspace=tmp_path / "ws")
    assert json.loads(argv[argv.index("--settings") + 1]) == {
        "claudeMdExcludes": [f"!{{{tmp_path / 'ws'}/**,/etc/cfg/rules/**,/etc/cfg/CLAUDE.md}}"]
    }


@pytest.mark.parametrize(
    "root",
    [
        pytest.param("/ws/a,b", id="comma-splits-the-arms"),
        pytest.param("/ws/{a}", id="brace-nests-the-arms"),
        pytest.param("/ws/a*", id="star"),
        pytest.param("/ws/a?", id="question-mark"),
        pytest.param("/ws/[a]", id="character-class"),
        pytest.param("/ws/!a", id="negation"),
        # Relative is the dangerous one: `claudeMdExcludes` is matched against absolute paths,
        # so a relative arm matches nothing and the negation then matches everything.
        pytest.param("ws/a", id="relative"),
    ],
)
def test_the_claude_md_allow_list_refuses_a_root_it_cannot_spell(root: str) -> None:
    """A path a glob would re-read is no flag at all, rather than a pattern meaning something
    else (#135). The failure that matters is not "too little is excluded" but "everything is":
    a mis-parsed arm excludes every CLAUDE.md, the clone's and the user's alike, and a session
    that silently lost its instructions looks like a session that ignored them."""
    assert claude_md_allowlist(trees=[Path(root)], files=[Path("/a/CLAUDE.md")]) is None
    assert claude_md_allowlist(trees=[Path("/a")], files=[Path(root)]) is None


def test_an_unspellable_workspace_leaves_no_confinement_and_says_so(tmp_path: Path) -> None:
    """The fail-open branch through the public surface, and the warning that marks it (#135).

    `claude_md_allowlist` declines a path it cannot spell rather than emitting a pattern that
    means something else, so a deployment whose `workspace.root` carries a glob metacharacter
    runs with no confinement at all. That is the safer of the two failures -- the other loses
    every instruction file -- but it is not one to make silently, since nothing downstream
    would show it.
    """
    stream = io.StringIO()
    configure_logging(level="WARNING", fmt="json", stream=stream)
    try:
        root = tmp_path / "work[1]"
        runner = ClaudeRunner(settings(root), environ={"HOME": "/home/agent"})
        argv = runner.build_argv(session_id=SESSION_ID, resume=False, workspace=root / "ws")
    finally:
        configure_logging(stream=io.StringIO())
    assert "--settings" not in argv
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    [warning] = [line for line in lines if line["event"] == "claude_md_allowlist_unavailable"]
    assert warning["level"] == "warning"
    assert warning["reason"] == "path not expressible"


def test_no_claude_md_allow_list_without_a_home(tmp_path: Path) -> None:
    """A workspace-only allow-list would stop the user memory loading, so an unresolved config
    directory omits the argument instead (#135): the residual it would buy against is bounded
    and recorded, where dropping instructions a deployment means to load shows up nowhere."""
    runner = ClaudeRunner(settings(tmp_path), environ={})
    assert "--settings" not in runner.build_argv(
        session_id=SESSION_ID, resume=False, workspace=tmp_path / "ws"
    )


# --- per-issue model override ----------------------------------------------------------


MODEL_LABELS = {"issuebot/model/sonnet": "sonnet", "issuebot/model/fable": "fable"}


def test_settings_for_labels_is_a_no_op_without_model_labels(tmp_path: Path) -> None:
    base = settings(tmp_path, model="opus")
    assert settings_for_labels(base, ("issuebot/model/sonnet",)) is base


def test_settings_for_labels_applies_the_matching_label(tmp_path: Path) -> None:
    base = settings(tmp_path, model="opus", model_labels=MODEL_LABELS, max_budget_usd=2.5)
    resolved = settings_for_labels(base, ("issuebot/todo", "Issuebot/Model/Sonnet"))
    assert resolved.claude.model == "sonnet"
    assert resolved.claude.max_budget_usd == 2.5
    assert resolved.workspace.root == base.workspace.root
    assert base.claude.model == "opus"


def test_settings_for_labels_keeps_the_default_without_a_match(tmp_path: Path) -> None:
    base = settings(tmp_path, model="opus", model_labels=MODEL_LABELS)
    assert settings_for_labels(base, ("issuebot/todo",)) is base


def test_settings_for_labels_keeps_the_default_when_labels_disagree(tmp_path: Path) -> None:
    base = settings(tmp_path, model="opus", model_labels=MODEL_LABELS)
    labels = ("issuebot/model/sonnet", "issuebot/model/fable")
    assert settings_for_labels(base, labels).claude.model == "opus"


def test_the_overridden_model_reaches_argv(tmp_path: Path) -> None:
    base = settings(tmp_path, model="opus", model_labels=MODEL_LABELS)
    resolved = settings_for_labels(base, ("issuebot/model/fable",))
    argv = ClaudeRunner(resolved, environ={}).build_argv(
        session_id=SESSION_ID, resume=False, workspace=tmp_path / "ws"
    )
    assert argv[argv.index("--model") + 1] == "fable"


def test_agent_environment_passes_only_the_allowed_names() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": "sk-1",
        "CLAUDE_CONFIG_DIR": "/cfg",
        "GIT_AUTHOR_NAME": "issuebot",
        "GIT_COMMITTER_EMAIL": "bot@example.com",
        "GH_TOKEN": "parent-token",
        "AWS_SECRET_ACCESS_KEY": "nope",
        "SSH_AUTH_SOCK": "/tmp/sock",
        "GIT_DIR": "/elsewhere",
    }
    assert agent_environment(parent, token=None) == {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": "sk-1",
        "CLAUDE_CONFIG_DIR": "/cfg",
        "GIT_AUTHOR_NAME": "issuebot",
        "GIT_COMMITTER_EMAIL": "bot@example.com",
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "NO_COLOR": "1",
        "GH_PAGER": "cat",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }


def test_agent_environment_carries_the_proxy_to_the_session_and_every_hook() -> None:
    """#126: the session's one route off the host is the address in these six variables, so
    `claude`, `gh`, `git`, `uv`, `pip`, `npm` and a session's own `curl` all have to see them.

    Both cases of all three, because they are not interchangeable: curl deliberately ignores an
    upper-case `HTTP_PROXY`, while other clients read only the upper-case spelling.
    """
    parent = {"PATH": "/usr/bin", **dict.fromkeys(PROXY_ENV_NAMES, "http://egress:3128")}
    env = agent_environment(parent, token=None)
    assert {name: env.get(name) for name in PROXY_ENV_NAMES} == dict.fromkeys(
        PROXY_ENV_NAMES, "http://egress:3128"
    )


@pytest.mark.parametrize("key", sorted(PROXY_ENV_NAMES))
def test_a_workspace_env_line_cannot_take_the_proxy_out_from_under_a_turn(key: str) -> None:
    """For the reason `PATH` is protected and no stronger one: what bounds egress is the
    container's lack of a route, so a line emptying these would take `gh`, `git` and the next
    turn's `claude` off the network without admitting anything off the allow-list."""
    assert key in PROTECTED_ENV_NAMES
    base = {key: "http://egress:3128"}
    merged, refused = merge_workspace_env(base, {key: "", "FOO": "bar"})
    assert refused == [key]
    assert merged[key] == "http://egress:3128"


def test_agent_environment_turns_auto_memory_off_and_a_hook_cannot_turn_it_back_on() -> None:
    """Auto memory is read whatever `--setting-sources` says and lives in the shared session
    home (#101): fixed off, and protected like the rest of the fixed entries, so a workspace
    env line cannot re-enable it for the next turn."""
    parent = {"PATH": "/usr/bin", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}
    assert agent_environment(parent, token=None)["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" in PROTECTED_ENV_NAMES


def test_agent_environment_adds_the_configured_token() -> None:
    env = agent_environment({"PATH": "/usr/bin"}, token=SecretStr("sekret"))
    assert env["GH_TOKEN"] == "sekret"


def test_child_environment_uses_the_settings_token(tmp_path: Path) -> None:
    cfg = Settings.model_validate(
        {
            "github": {"repo": "example/repo", "token": "from-settings"},
            "workspace": {"root": str(tmp_path)},
        }
    )
    runner = ClaudeRunner(cfg, environ={"PATH": "/usr/bin", "GH_TOKEN": "from-parent"})
    assert runner.child_environment()["GH_TOKEN"] == "from-settings"


def test_parse_workspace_env_accepts_the_shapes_a_hook_writes() -> None:
    text = (
        "# a comment\n"
        "\n"
        "export ACME_DATABASE_URL=postgresql://issuebot@/db?host=/tmp/s\r\n"
        "  ACME_JS_HARNESS=1\n"
        "EQUALS=a=b=c\n"
        "EMPTY=\n"
        "  export SPACED=  padded\n"
    )
    env, warnings = parse_workspace_env(text)
    assert env == {
        "ACME_DATABASE_URL": "postgresql://issuebot@/db?host=/tmp/s",
        "ACME_JS_HARNESS": "1",
        "EQUALS": "a=b=c",
        "EMPTY": "",
        "SPACED": "  padded",
    }
    assert warnings == []


def test_parse_workspace_env_warns_by_line_number_only() -> None:
    # The text before a missing `=` can be most of a DSN, so a complaint never repeats it.
    env, warnings = parse_workspace_env(
        "postgresql://user:hunter2@host\nOK=1\nlower case=2\n9LIVES=3\n"
    )
    assert env == {"OK": "1"}
    assert warnings == [
        "line 1: not KEY=VALUE",
        "line 3: not a variable name",
        "line 4: not a variable name",
    ]
    assert "hunter2" not in " ".join(warnings)


def test_parse_workspace_env_of_nothing_is_nothing() -> None:
    assert parse_workspace_env("") == ({}, [])


def test_parse_workspace_env_refuses_a_null_byte() -> None:
    # An environment cannot hold one, and `create_subprocess_exec` raises `ValueError` for it,
    # which is not an `OSError` and would escape the turn loop.
    env, warnings = parse_workspace_env("FOO=a\x00b\nOK=1\n")
    assert env == {"OK": "1"}
    assert warnings == ["line 1: the value has a null byte in it"]


@pytest.mark.parametrize("key", ["PATH", "HOME", "GH_TOKEN", "NO_COLOR", "GH_PAGER"])
def test_merge_workspace_env_refuses_the_protected_names(key: str) -> None:
    base = {"PATH": "/usr/bin", "HOME": "/home/x", "GH_TOKEN": "sekret", **FIXED_ENVIRONMENT}
    merged, refused = merge_workspace_env(base, {key: "hijacked", "FOO": "bar"})
    assert refused == [key]
    assert merged[key] == base[key]
    assert merged["FOO"] == "bar"


@pytest.mark.parametrize(
    "key", ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"]
)
def test_merge_workspace_env_refuses_the_agents_own_configuration(key: str) -> None:
    # The file lives in the agent's workspace, so the session can write it; it must not be able
    # to re-point or re-credential the `claude` issuebot launches for the next turn.
    merged, refused = merge_workspace_env({"ANTHROPIC_API_KEY": "sk-real"}, {key: "sk-theirs"})
    assert refused == [key]
    assert merged == {"ANTHROPIC_API_KEY": "sk-real"}


@pytest.mark.parametrize(
    "key",
    [
        # A config file at a path of the line's choosing, in either of git's two spellings for
        # the user level, and in the system one that a derived image's /etc/gitconfig uses.
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        # Any key at all, `alias.x = !...` included, with no file involved.
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        # A command named outright. `GIT_EDITOR` is the one that matters most: it runs on a
        # plain `git commit`, which a session does constantly, and needs no terminal.
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        "GIT_ASKPASS",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_PAGER",
        "GIT_PROXY_COMMAND",
        # A directory of commands: git's own subcommands, and the hooks copied into the next
        # repository `git init` creates.
        "GIT_EXEC_PATH",
        "GIT_TEMPLATE_DIR",
        # Which repository is being operated on at all.
        "GIT_DIR",
        "GIT_WORK_TREE",
        # The commit identity a pull request carries. Refused here, and unaffected as the
        # deployment sets it: `.env` -> the worker -> `PASSTHROUGH_PREFIXES`.
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_EMAIL",
        # `gh`, the one tool in the session holding `GH_TOKEN`. `GH_CONFIG_DIR` takes
        # precedence over `$XDG_CONFIG_HOME/gh` for the same shell aliases, and `GH_PAGER`
        # was already protected as a fixed entry -- an asymmetry with no reason behind it.
        "GH_CONFIG_DIR",
        "GH_EDITOR",
        "GH_BROWSER",
        "GH_HOST",
        # Not a git or gh variable at all: it moves the config directory of everything
        # following the base-directory specification, `$XDG_CONFIG_HOME/gh/config.yml`
        # among them.
        "XDG_CONFIG_HOME",
        # The tails of git's and gh's own precedence chains, whose heads are covered by the
        # prefixes above and whose config rung `TOOL_CONFIG_SWEEP` (#151) removes from the
        # home -- so these are the whole of what is left of each chain. Protecting a head and
        # leaving its tail closes nothing.
        "SSH_ASKPASS",  # GIT_ASKPASS -> core.askPass -> this
        "SSH_ASKPASS_REQUIRE",
        "EDITOR",  # GIT_EDITOR/GH_EDITOR -> core.editor -> VISUAL -> this
        "VISUAL",
        "PAGER",  # GIT_PAGER/GH_PAGER -> core.pager -> this
        "BROWSER",  # GH_BROWSER -> this
        "EMAIL",  # GIT_AUTHOR_EMAIL -> user.email -> this
        "GITHUB_TOKEN",  # GH_TOKEN -> this
        "GITHUB_ENTERPRISE_TOKEN",  # GH_ENTERPRISE_TOKEN -> this
    ],
)
def test_merge_workspace_env_refuses_the_tools_own_configuration(key: str) -> None:
    """#171: the same rule as `PATH` one step in. `PATH` decides which binary `git` and `gh`
    are; each of these decides what that binary does and which further commands it runs."""
    merged, refused = merge_workspace_env({"PATH": "/usr/bin"}, {key: "/tmp/theirs", "FOO": "1"})
    assert refused == [key]
    assert key not in merged
    assert merged["FOO"] == "1"


def test_the_tool_config_protections_are_pinned() -> None:
    """Two halves with two different rules, and both have to be a deliberate edit here as well
    as in `runner.py`. `GIT_` and `GH_` whole rather than an enumeration, which is what makes
    the bound stay true: `GIT_CONFIG_GLOBAL` and `GIT_SSH_COMMAND` name a command, but so do
    `GIT_EDITOR`, `GIT_PAGER`, `GIT_TEMPLATE_DIR` and `GH_CONFIG_DIR`, and successive drafts of
    this list missed some of them. The names are then the rungs of git's and gh's documented
    precedence chains that fall outside those prefixes -- checkable against `git-var(1)`,
    `git-commit(1)` and `gh environment`, and finite because a chain has an end -- plus
    `XDG_CONFIG_HOME`, which is on no chain and moves the directory `gh` reads its aliases
    from. Names and not prefixes, because `SSH_AUTH_SOCK` is a legitimate route for the
    deploy-key case this bound has to leave a hook author."""
    assert sorted(TOOL_CONFIG_ENV_NAMES) == [
        "BROWSER",
        "EDITOR",
        "EMAIL",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_TOKEN",
        "PAGER",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
        "VISUAL",
        "XDG_CONFIG_HOME",
    ]
    assert list(TOOL_CONFIG_ENV_PREFIXES) == ["GIT_", "GH_"]
    assert TOOL_CONFIG_ENV_NAMES <= PROTECTED_ENV_NAMES
    assert set(TOOL_CONFIG_ENV_PREFIXES) <= set(PROTECTED_ENV_PREFIXES)


@pytest.mark.parametrize(
    "key",
    [
        # No underscore, so not the `GIT_`/`GH_` prefixes however much it looks like one.
        "GITHUB_WORKSPACE",
        "GHOSTSCRIPT_HOME",
        # The base-directory specification's other roots: neither `git` nor `gh` reads them,
        # and a hook pointing a cache somewhere is exactly what this file is for.
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_DIRS",
        # The legitimate route for a forwarded deploy key, which is why `SSH_ASKPASS` is a
        # name here and `SSH_` is not a prefix.
        "SSH_AUTH_SOCK",
    ],
)
def test_merge_workspace_env_does_not_over_reach_past_the_tool_config_entries(key: str) -> None:
    """The bound is on configuring the tooling issuebot launches, and a prefix is only safe to
    state as a prefix if it stops where it says it does."""
    merged, refused = merge_workspace_env({"PATH": "/usr/bin"}, {key: "/tmp/theirs"})
    assert refused == []
    assert merged[key] == "/tmp/theirs"


def test_the_deployments_own_git_identity_still_reaches_the_session() -> None:
    """`.issuebot/env` was never the deployment's channel for these and is not the loser here:
    `GIT_AUTHOR_`/`GIT_COMMITTER_` are set in `.env`, reach the worker's own environment and
    are inherited through `PASSTHROUGH_PREFIXES`. What the `GIT_` prefix refuses is the
    session-writable file re-pointing them."""
    assert agent_environment({"GIT_AUTHOR_NAME": "issuebot"}, token=None) == {
        "GIT_AUTHOR_NAME": "issuebot",
        **FIXED_ENVIRONMENT,
    }
    # And a line in the file cannot take that value out from under the next turn.
    merged, refused = merge_workspace_env(
        {"GIT_AUTHOR_NAME": "issuebot"}, {"GIT_AUTHOR_NAME": "someone else"}
    )
    assert refused == ["GIT_AUTHOR_NAME"]
    assert merged["GIT_AUTHOR_NAME"] == "issuebot"


def test_a_workspace_env_line_re_pointing_git_never_reaches_the_environment(
    tmp_path: Path,
) -> None:
    """The channel end to end (#171): what a session leaves in `.issuebot/env` is what the
    *next* session on that issue is handed, and the complaint names the key that was dropped.
    Each of these was measured naming a command that ran -- `[alias] st = !...` out of the file
    `GIT_CONFIG_GLOBAL` and `XDG_CONFIG_HOME` name, the `GIT_CONFIG_COUNT` triple's `alias.st`
    with no file at all, `GIT_SSH_COMMAND` as the transport git execs, and `GIT_EDITOR` on a
    plain `git commit`."""
    (tmp_path / ".issuebot").mkdir()
    (tmp_path / ".issuebot" / "env").write_text(
        "GIT_CONFIG_GLOBAL=/tmp/theirs/gitconfig\n"
        "XDG_CONFIG_HOME=/tmp/theirs/xdg\n"
        "GIT_SSH_COMMAND=/tmp/theirs/payload.sh\n"
        "GIT_CONFIG_COUNT=1\n"
        "GIT_CONFIG_KEY_0=alias.st\n"
        "GIT_CONFIG_VALUE_0=!/tmp/theirs/payload.sh\n"
        "GIT_EDITOR=/tmp/theirs/payload.sh\n"
        "EDITOR=/tmp/theirs/payload.sh\n"
        "EMAIL=someone@example.invalid\n"
        "DATABASE_URL=postgresql://issuebot@127.0.0.1/issuebot\n"
    )
    base = agent_environment({"PATH": "/usr/bin", "HOME": "/home/agent-1"}, token=None)
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    try:
        merged, applied = workspace_environment(base, tmp_path)
    finally:
        configure_logging(stream=io.StringIO())
    # The turn's environment carries what the hook had a reason to hand over, and nothing that
    # would re-point `git` or `gh` for it.
    assert applied == ["DATABASE_URL"]
    assert merged["DATABASE_URL"] == "postgresql://issuebot@127.0.0.1/issuebot"
    assert not [name for name in merged if name.startswith(("GIT_", "XDG_"))]
    assert "EDITOR" not in merged and "EMAIL" not in merged
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    ignored = [r["reason"] for r in records if r["event"] == "workspace_env_ignored"]
    assert ignored == [
        "GIT_CONFIG_GLOBAL is protected",
        "XDG_CONFIG_HOME is protected",
        "GIT_SSH_COMMAND is protected",
        "GIT_CONFIG_COUNT is protected",
        "GIT_CONFIG_KEY_0 is protected",
        "GIT_CONFIG_VALUE_0 is protected",
        "GIT_EDITOR is protected",
        "EDITOR is protected",
        "EMAIL is protected",
    ]


@pytest.mark.parametrize(
    "key",
    [
        # The file `bash` sources before the command it was given. #137's `~/.profile` channel
        # in one line of a file the session can write.
        "BASH_ENV",
        # No command of their own: the `set -o` and `shopt` options `bash` enables from the
        # environment before any start-up file is read, `xtrace` among them.
        "SHELLOPTS",
        "BASHOPTS",
        # Which `xtrace` makes live: `PS4` is expanded before every traced command, command
        # substitution and all, the first of them inside `/etc/profile`.
        "PS4",
        # `PATH`'s rule for directories: a hook's `cd sub` resolves through it, so the relative
        # command after the `cd` is a file in a tree the line chose.
        "CDPATH",
    ],
)
def test_merge_workspace_env_refuses_the_shells_own_start_up(key: str) -> None:
    """#179: the same rule again for the one tool every script issuebot runs goes through.
    `WorkspaceManager.hook_shell` is `bash -lc`, so these decide what the post-clone setup and
    all four hooks run before -- or instead of -- what the hook actually wrote."""
    merged, refused = merge_workspace_env({"PATH": "/usr/bin"}, {key: "/tmp/theirs", "FOO": "1"})
    assert refused == [key]
    assert key not in merged
    assert merged["FOO"] == "1"


def test_the_shell_protections_are_pinned() -> None:
    """A deliberate edit here as well as in `runner.py`, as the sweep lists and the tool-config
    entries are. The rule is what `bash` itself reads out of the environment it is handed and
    acts on before, or around, the commands the hook wrote: the file it sources (`BASH_ENV`),
    the options it enables and the prompt one of them expands (`SHELLOPTS`/`BASHOPTS`, `PS4`)
    and the directories `cd` resolves through (`CDPATH`, which is `PATH`'s rule for the one
    lookup `PATH` does not cover). Each was measured firing under the image's own bash 5.2."""
    assert sorted(SHELL_ENV_NAMES) == [
        "BASHOPTS",
        "BASH_ENV",
        "CDPATH",
        "PS4",
        "SHELLOPTS",
    ]
    assert SHELL_ENV_NAMES <= PROTECTED_ENV_NAMES


@pytest.mark.parametrize(
    "key",
    [
        # POSIX's start-up file for an *interactive* shell, and nothing issuebot runs is one.
        # Measured unread by `bash -lc`, by `bash --posix -c`, by `bash` invoked as `sh` and by
        # `sh -c` (dash), so protecting it would close nothing: the list states what was shown
        # to work, as #171's does.
        "ENV",
        # The descriptor `xtrace` writes to, and the prompts an interactive shell draws. None
        # of them runs anything, and a hook that wants its own output shaped is welcome to.
        "BASH_XTRACEFD",
        "PS1",
        "PS2",
    ],
)
def test_merge_workspace_env_does_not_over_reach_past_the_shell_entries(key: str) -> None:
    """The bound is on what makes the shell run something the hook did not write."""
    merged, refused = merge_workspace_env({"PATH": "/usr/bin"}, {key: "/tmp/theirs"})
    assert refused == []
    assert merged[key] == "/tmp/theirs"


def test_a_workspace_env_line_re_pointing_the_shell_never_reaches_the_environment(
    tmp_path: Path,
) -> None:
    """The channel end to end (#179): what a session leaves in `.issuebot/env` is what the
    *next* session's hooks are run with, and the complaint names the key that was dropped.
    Before this change the same file was measured sourcing the planted script under
    `bash -lc 'echo hook-ran'` and running the substitution in `PS4` during `/etc/profile`."""
    (tmp_path / ".issuebot").mkdir()
    (tmp_path / ".issuebot" / "env").write_text(
        "BASH_ENV=/tmp/theirs/plant.sh\n"
        "SHELLOPTS=xtrace\n"
        "BASHOPTS=xpg_echo\n"
        "PS4=$(/tmp/theirs/plant.sh)+ \n"
        "CDPATH=/tmp/theirs\n"
        "DATABASE_URL=postgresql://issuebot@127.0.0.1/issuebot\n"
    )
    base = agent_environment({"PATH": "/usr/bin", "HOME": "/home/agent-1"}, token=None)
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    try:
        merged, applied = workspace_environment(base, tmp_path)
    finally:
        configure_logging(stream=io.StringIO())
    assert applied == ["DATABASE_URL"]
    assert merged["DATABASE_URL"] == "postgresql://issuebot@127.0.0.1/issuebot"
    assert not [name for name in merged if name in SHELL_ENV_NAMES]
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    ignored = [r["reason"] for r in records if r["event"] == "workspace_env_ignored"]
    assert ignored == [
        "BASH_ENV is protected",
        "SHELLOPTS is protected",
        "BASHOPTS is protected",
        "PS4 is protected",
        "CDPATH is protected",
    ]


def test_a_workspace_env_line_cannot_export_a_shell_function(tmp_path: Path) -> None:
    """`bash` imports a function from an environment entry named `BASH_FUNC_<name>%%`, which was
    measured defining a command in the shell it starts. The key pattern is what refuses it, so
    the refusal is a parse complaint naming the line and not the value -- recorded here because
    it is the reason that spelling is not in `SHELL_ENV_NAMES`."""
    (tmp_path / ".issuebot").mkdir()
    (tmp_path / ".issuebot" / "env").write_text(
        "BASH_FUNC_git%%=() { /tmp/theirs/plant.sh; }\nDATABASE_URL=postgresql://issuebot@/db\n"
    )
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    try:
        merged, applied = workspace_environment({"PATH": "/usr/bin"}, tmp_path)
    finally:
        configure_logging(stream=io.StringIO())
    assert applied == ["DATABASE_URL"]
    assert not [name for name in merged if name.startswith("BASH_FUNC")]
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    ignored = [r["reason"] for r in records if r["event"] == "workspace_env_ignored"]
    assert ignored == ["line 1: not a variable name"]
    assert "plant.sh" not in stream.getvalue()


def test_merge_workspace_env_overrides_an_unprotected_name() -> None:
    merged, refused = merge_workspace_env({"LANG": "C", "TZ": "UTC"}, {"LANG": "en_GB.UTF-8"})
    assert (merged["LANG"], merged["TZ"], refused) == ("en_GB.UTF-8", "UTC", [])


def test_workspace_environment_without_a_file_changes_nothing(tmp_path: Path) -> None:
    base = agent_environment({"PATH": "/usr/bin"}, token=None)
    assert workspace_environment(base, tmp_path) == (base, [])


def test_workspace_environment_reads_the_file(tmp_path: Path) -> None:
    (tmp_path / ".issuebot").mkdir()
    (tmp_path / ".issuebot" / "env").write_text("export FOO=bar\nPATH=/hijacked\n")
    merged, applied = workspace_environment({"PATH": "/usr/bin"}, tmp_path)
    assert merged == {"PATH": "/usr/bin", "FOO": "bar"}
    assert applied == ["FOO"]


def test_workspace_environment_survives_an_unreadable_file(tmp_path: Path) -> None:
    # A directory where the file should be: a hook's problem, not a failed turn.
    (tmp_path / ".issuebot" / "env").mkdir(parents=True)
    assert workspace_environment({"PATH": "/usr/bin"}, tmp_path) == ({"PATH": "/usr/bin"}, [])


def test_read_workspace_env_caps_the_file_at_a_line_boundary(tmp_path: Path) -> None:
    (tmp_path / ".issuebot").mkdir()
    filler = "".join(f"K{n}=x\n" for n in range(WORKSPACE_ENV_LIMIT // 6))
    (tmp_path / ".issuebot" / "env").write_text(filler + "LAST=kept\n")
    env, warnings = read_workspace_env(tmp_path)
    assert warnings[0] == f"longer than {WORKSPACE_ENV_LIMIT} bytes: the rest was ignored"
    assert "LAST" not in env
    # Cut at a line boundary, so no half-written value survives.
    assert all(value == "x" for value in env.values())


def test_workspace_environment_logs_what_it_used_and_what_it_ignored(tmp_path: Path) -> None:
    (tmp_path / ".issuebot").mkdir()
    (tmp_path / ".issuebot" / "env").write_text("FOO=bar\nGH_TOKEN=stolen\nnonsense\n")
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    try:
        workspace_environment({"GH_TOKEN": "sekret"}, tmp_path)
    finally:
        configure_logging(stream=io.StringIO())
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    ignored = [r["reason"] for r in records if r["event"] == "workspace_env_ignored"]
    assert ignored == ["line 3: not KEY=VALUE", "GH_TOKEN is protected"]
    applied = [r for r in records if r["event"] == "workspace_env_applied"]
    assert [r["keys"] for r in applied] == [["FOO"]]
    # The keys, never the values.
    assert "bar" not in stream.getvalue()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.1.259 (Claude Code)", (2, 1, 259)),
        ("v2.2.0\n", (2, 2, 0)),
        ("", None),
        (None, None),
        ("Claude Code", None),
    ],
)
def test_parse_claude_version(text: str | None, expected: tuple[int, int, int] | None) -> None:
    assert parse_claude_version(text) == expected


def test_minimum_version_is_the_permission_prompts_release() -> None:
    assert MIN_CLAUDE_VERSION == (2, 1, 259)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (None, ClaudeAuth("unreadable", "could not read auth status (no output)", "unknown")),
        ("", ClaudeAuth("unreadable", "could not read auth status (no output)", "unknown")),
        (
            "error: unknown command auth\n",
            ClaudeAuth(
                "unreadable",
                "could not read auth status (unparseable output 'error: unknown command auth')",
                "unknown",
            ),
        ),
        (
            "[1, 2]",
            ClaudeAuth(
                "unreadable", "could not read auth status (unparseable output '[1, 2]')", "unknown"
            ),
        ),
        (
            '{"loggedIn": false, "authMethod": "none"}',
            ClaudeAuth(
                "logged_out",
                "not logged in; set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), "
                "or run claude auth login on the host",
                "unknown",
            ),
        ),
        (
            "{}",
            ClaudeAuth(
                "logged_out",
                "not logged in; set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), "
                "or run claude auth login on the host",
                "unknown",
            ),
        ),
        (
            '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}',
            ClaudeAuth("ok", "logged in (claude.ai, max)", "subscription"),
        ),
        (
            '{"loggedIn": true, "authMethod": "claude.ai"}',
            ClaudeAuth("ok", "logged in (claude.ai)", "subscription"),
        ),
        (
            '{"loggedIn": true, "authMethod": "oauth_token"}',
            ClaudeAuth("ok", "logged in (CLAUDE_CODE_OAUTH_TOKEN)", "subscription"),
        ),
        (
            '{"loggedIn": true, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}',
            ClaudeAuth("ok", "logged in (API key from ANTHROPIC_API_KEY)", "api_key"),
        ),
        (
            '{"loggedIn": true, "authMethod": "api_key"}',
            ClaudeAuth("ok", "logged in (API key)", "api_key"),
        ),
        (
            '{"loggedIn": true, "authMethod": "claude.ai", "apiKeySource": "ANTHROPIC_API_KEY"}',
            ClaudeAuth(
                "ambiguous",
                "logged in (claude.ai) with ANTHROPIC_API_KEY also set; "
                "unset one to be sure which credential is used",
                "unknown",
            ),
        ),
    ],
)
def test_describe_claude_auth(output: str | None, expected: ClaudeAuth) -> None:
    assert describe_claude_auth(output) == expected


def test_a_logged_out_probe_names_the_container_route_first() -> None:
    """The container has no interactive login since #142, so the line that tells an operator
    what to do names the variable; `claude auth login` is the host route and says so."""
    auth = describe_claude_auth('{"loggedIn": false}')
    assert auth.verdict == "logged_out"
    assert auth.detail == (
        "not logged in; set CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), "
        "or run claude auth login on the host"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the stub is a POSIX script")
def test_claude_auth_status_runs_under_the_agent_environment(tmp_path: Path) -> None:
    stub = tmp_path / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        'test "$1 $2 $3" = "auth status --json" || exit 2\n'
        'printf \'{"args": "%s", "secret": "%s", "home": "%s"}\' "$*" "$SECRET" "$HOME"\n'
    )
    stub.chmod(0o755)
    environ = {"PATH": "/usr/bin:/bin", "HOME": "/home/agent", "SECRET": "leaked?"}
    output = claude_auth_status(str(stub), environ)
    assert json.loads(output or "") == {
        "args": "auth status --json",
        "secret": "",
        "home": "/home/agent",
    }


def test_claude_auth_status_is_none_when_the_command_cannot_run(tmp_path: Path) -> None:
    assert claude_auth_status(str(tmp_path / "missing"), {"PATH": "/usr/bin"}) is None


# --- stream parsing --------------------------------------------------------------------


def _lines(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()


def test_parser_reads_init_activity_and_result() -> None:
    parser = StreamParser(turn_number=1, expected_session_id=RECORDED_SESSION_ID)
    events: list[TurnEvent] = []
    for line in _lines("success"):
        events.extend(parser.feed(line))
    assert [event.kind for event in events] == [
        "session_started",
        "rate_limits",
        "turn_activity",
        "turn_activity",
        "turn_activity",
    ]
    assert events[0].session_id == RECORDED_SESSION_ID
    assert events[0].detail == "claude-opus-5[1m]"
    assert [event.message_type for event in events[2:]] == [
        "assistant",
        "user",
        "assistant",
    ]
    assert events[2].tool_name == "Read"
    assert all(event.turn_number == 1 for event in events)
    assert parser.model == "claude-opus-5[1m]"
    assert parser.api_key_source == "none"
    assert parser.result is not None
    assert parser.result["subtype"] == "success"
    assert parser.unparseable == 0


def test_parser_reads_the_rate_limit_windows() -> None:
    parser = StreamParser(turn_number=1, expected_session_id=RECORDED_SESSION_ID)
    for line in _lines("success"):
        events = parser.feed(line)
        if events and events[0].kind == "rate_limits":
            break
    else:  # pragma: no cover - the fixture carries one
        pytest.fail("the success fixture has no rate_limit_event line")
    limits = events[0].rate_limits
    assert limits is not None
    assert limits.five_hour == RateLimitWindow(
        utilization=0.13, resets_at=datetime(2026, 9, 3, 11, 50, tzinfo=UTC)
    )
    assert limits.seven_day == RateLimitWindow(
        utilization=0.3, resets_at=datetime(2026, 9, 4, 5, 0, tzinfo=UTC)
    )
    assert parser.rate_limits == limits


@pytest.mark.parametrize(
    "info",
    [
        None,
        "not a mapping",
        {"unifiedWindows": None},
        {"unifiedWindows": {}},
        {"unifiedWindows": {"five_hour": {"utilization": "lots", "resetsAt": 1788436200}}},
        {"unifiedWindows": {"five_hour": {"utilization": 0.5}}},
        {"unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": 1e30}}},
    ],
)
def test_parser_shrugs_off_a_rate_limit_event_it_cannot_read(info: object) -> None:
    """The shape is undocumented, so an unreadable one is activity, never an exception."""
    parser = StreamParser(turn_number=1, expected_session_id="x")
    line = json.dumps({"type": "rate_limit_event", "rate_limit_info": info})
    [event] = parser.feed(line)
    assert event.kind == "turn_activity"
    assert event.message_type == "rate_limit_event"
    assert event.rate_limits is None
    assert parser.rate_limits is None


def test_parser_keeps_the_latest_rate_limit_reading() -> None:
    parser = StreamParser(turn_number=1, expected_session_id="x")
    for utilization in (0.10, 0.55):
        parser.feed(
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "unifiedWindows": {
                            "five_hour": {"utilization": utilization, "resetsAt": 1788436200}
                        }
                    },
                }
            )
        )
    assert parser.rate_limits is not None
    assert parser.rate_limits.five_hour is not None
    assert parser.rate_limits.five_hour.utilization == 0.55
    assert parser.rate_limits.seven_day is None


def test_rate_limits_clamp_a_utilization_outside_the_unit_range() -> None:
    parser = StreamParser(turn_number=1, expected_session_id="x")
    [event] = parser.feed(
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "unifiedWindows": {
                        "five_hour": {"utilization": 1.4, "resetsAt": 1788436200},
                        "seven_day": {"utilization": -0.2, "resetsAt": 1788498000},
                    }
                },
            }
        )
    )
    limits = event.rate_limits
    assert limits is not None and limits.five_hour is not None and limits.seven_day is not None
    assert (limits.five_hour.utilization, limits.seven_day.utilization) == (1.0, 0.0)


def test_parser_tolerates_blank_and_unparseable_lines() -> None:
    parser = StreamParser(turn_number=2, expected_session_id="x")
    assert parser.feed("") == []
    assert parser.feed("   \n") == []
    [event] = parser.feed("not json at all")
    assert event.kind == "turn_activity"
    assert event.message_type == "unparseable"
    [event] = parser.feed('["a", "list"]')
    assert event.message_type == "unparseable"
    [event] = parser.feed('{"type": "future_kind"}')
    assert event.message_type == "future_kind"
    [event] = parser.feed('{"no": "type"}')
    assert event.message_type == "unknown"
    assert parser.overrun().message_type == "unparseable"
    assert parser.unparseable == 3


def test_parser_reports_one_activity_per_tool_use_block() -> None:
    parser = StreamParser(turn_number=1, expected_session_id="x")
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                    {"type": "tool_use", "name": "Edit", "input": {}},
                ]
            },
        }
    )
    assert [event.tool_name for event in parser.feed(line)] == ["Bash", "Edit"]
    [event] = parser.feed('{"type": "assistant", "message": {"content": "text only"}}')
    assert event.tool_name is None


@pytest.mark.parametrize(
    ("result", "exit_code", "category", "needle"),
    [
        ({"subtype": "success", "is_error": False}, 0, None, None),
        (None, 2, "process_exit", "status 2"),
        (
            {"subtype": "error_max_budget_usd", "is_error": True, "result": "over"},
            1,
            "budget_exceeded",
            "over",
        ),
        ({"subtype": "error_max_budget_usd", "is_error": True}, 1, "budget_exceeded", "cap"),
        (
            {"subtype": "error_during_execution", "is_error": True, "errors": ["a", "b"]},
            1,
            "turn_failed",
            "a; b",
        ),
        ({"subtype": "success", "is_error": True, "result": "bad"}, 0, "turn_failed", "bad"),
        ({"subtype": "success", "is_error": False}, 1, "process_exit", "reported success"),
        ({}, 0, "turn_failed", "unknown subtype"),
        # A lapsed credential, as claude reports it: in a failing result, or on stderr with no
        # result at all. Either way the category names authentication (#20).
        (
            {
                "subtype": "error_during_execution",
                "is_error": True,
                "result": 'API Error: 401 {"type":"authentication_error"}',
            },
            1,
            "auth_failed",
            "authentication_error",
        ),
        ({"subtype": "error_during_execution", "is_error": True}, 1, "turn_failed", "execution"),
        # The agent's own final message is not read for markers: only a failing result is.
        (
            {"subtype": "success", "is_error": True, "result": "invalid api key"},
            0,
            "turn_failed",
            "invalid api key",
        ),
    ],
)
def test_classify_result(
    result: dict[str, object] | None, exit_code: int, category: str | None, needle: str | None
) -> None:
    got_category, message = classify_result(result, exit_code, "last stderr line")
    assert got_category == category
    if needle is None:
        assert message is None
    else:
        assert message is not None
        assert needle in message


def test_classify_result_reads_stderr_for_a_credential_that_stopped_working() -> None:
    """claude that exits before any result: its stderr is the only evidence there is."""
    category, message = classify_result(None, 1, "Invalid API key \u00b7 Please run /login")
    assert category == "auth_failed"
    assert message is not None and "Please run /login" in message


def test_classify_result_names_the_login_whose_refresh_was_refused() -> None:
    """The shape a lapsed login actually arrives in, captured from a live worker's
    ``run_turns`` row on 2026-09-14: ``subtype: "success"`` with ``is_error`` and status 1,
    carrying claude's own sentence. Reading the subtype alone made this ``turn_failed``, so
    every issue on the board burned ``max_attempts`` and dispatch was never held (#20).

    The agent's own final message is still not mined for markers: that is status 0, which
    ``test_classify_result`` pins as ``turn_failed`` with the same words in it.
    """
    result = {
        "subtype": "success",
        "is_error": True,
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    }
    category, message = classify_result(result, 1, "")
    assert category == "auth_failed"
    assert message == (
        "success: Failed to authenticate: OAuth session expired and could not be refreshed"
    )


def test_classify_result_stderr_makes_a_failing_result_an_auth_failure() -> None:
    result = {"subtype": "error_during_execution", "is_error": True, "result": "stopped"}
    category, _ = classify_result(result, 1, "OAuth token has expired")
    assert category == "auth_failed"


TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab"


def test_classify_result_scrubs_the_result_text_and_the_stderr_line() -> None:
    """The message is claude's words and leaves the workspace without `capture_turns` (#91)."""
    result = {
        "subtype": "error_during_execution",
        "is_error": True,
        "result": f"env holds GH_TOKEN={TOKEN} and literal-token-value",
    }
    scrubber = Scrubber(secrets=["literal-token-value"])
    category, message = classify_result(result, 1, "x", scrubber=scrubber)
    assert category == "turn_failed"
    assert message == f"error_during_execution: env holds GH_TOKEN={REDACTED} and {REDACTED}"
    category, message = classify_result(
        None,
        1,
        f"fetch failed\nfatal: https://x:{TOKEN}@github.com/ literal-token-value",
        scrubber=scrubber,
    )
    assert category == "process_exit"
    assert message == (
        f"claude exited with status 1 before reporting a result: "
        f"fatal: https://x:{REDACTED}@github.com/ {REDACTED}"
    )


def test_classify_result_scrubs_with_the_shapes_alone_by_default() -> None:
    result = {"subtype": "error_during_execution", "is_error": True, "result": f"see {TOKEN}"}
    assert classify_result(result, 1, "")[1] == f"error_during_execution: see {REDACTED}"


def test_classify_result_scrubs_before_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential straddling the 500-character cut must not leave its head in the message:
    the mask goes on first and the cut lands on the mask."""
    monkeypatch.setattr(runner_module, "_MESSAGE_LIMIT", 8)
    result = {"subtype": "error_during_execution", "is_error": True, "result": f"at {TOKEN} end"}
    _, message = classify_result(result, 1, "")
    assert message == f"error_during_execution: at {REDACTED} e"
    _, message = classify_result(None, 1, f"first line\n{TOKEN} uvwxyz")
    assert message == f"claude exited with status 1 before reporting a result: {REDACTED} uvwx"


def test_classify_result_still_reads_the_raw_stderr_tail_for_an_auth_failure() -> None:
    """The verdict is read off the words, which the mask leaves alone."""
    tail = f"Invalid API key {TOKEN} \u00b7 Please run /login"
    category, message = classify_result(None, 1, tail)
    assert category == "auth_failed"
    assert message is not None and TOKEN not in message and "Please run /login" in message
    # The result path reads the *scrubbed* text; the markers are prose the mask leaves alone.
    result = {"subtype": "error_during_execution", "is_error": True, "result": tail}
    category, message = classify_result(result, 1, "")
    assert category == "auth_failed"
    assert message is not None and TOKEN not in message and "Please run /login" in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("API Error: 401 authentication_error", True),
        ("Invalid API key \u00b7 Please run /login", True),
        ("your OAuth token has expired", True),
        # What a login whose refresh is refused actually says (a live worker, 2026-09-14).
        ("Failed to authenticate: OAuth session expired and could not be refreshed", True),
        ("the OAuth session is invalid", True),
        ("run claude auth login to fix it", True),
        ("AUTHENTICATION FAILED", True),
        ("tool execution failed", False),
        ("the issue asks about a revoked API key", False),
        ("", False),
        (None, False),
    ],
)
def test_is_auth_failure(text: str | None, expected: bool) -> None:
    assert is_auth_failure(text) is expected


def test_is_auth_failure_reads_every_text_it_is_given() -> None:
    assert is_auth_failure("nothing here", None, "Invalid API key") is True
    assert is_auth_failure("nothing here", None, "still nothing") is False


# --- run_turn against the fake claude ---------------------------------------------------

posix = pytest.mark.skipif(
    sys.platform == "win32", reason="tests/fakes/claude is a POSIX shebang script"
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[TurnEvent] = []

    def on_turn_event(self, event: TurnEvent) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspaces" / "example-42"
    path.mkdir(parents=True)
    return path


def runner_for(
    workspace: Path,
    *,
    scenario: str = "success",
    turn_timeout_ms: int = 30_000,
    extra_env: dict[str, str] | None = None,
    token: str | None = None,
    command: str | None = None,
) -> ClaudeRunner:
    github: dict[str, object] = {"repo": "example/repo"}
    if token is not None:
        github["token"] = token
    cfg = Settings.model_validate(
        {
            "github": github,
            "workspace": {"root": str(workspace.parent)},
            "claude": {"command": command or str(FAKE_CLAUDE), "turn_timeout_ms": turn_timeout_ms},
        }
    )
    environ = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "CLAUDE_FAKE_SCENARIO": scenario,
        **(extra_env or {}),
    }
    return ClaudeRunner(cfg, environ=environ)


async def run(
    runner: ClaudeRunner,
    workspace: Path,
    *,
    turn_number: int = 1,
    resume: bool = False,
    observer: Recorder | None = None,
    cancel: asyncio.Event | None = None,
    log_dir: Path | None = None,
    deadline: float | None = None,
) -> TurnResult:
    return await runner.run_turn(
        prompt="Do the thing",
        workspace=workspace,
        session_id=SESSION_ID,
        resume=resume,
        turn_number=turn_number,
        log_dir=log_dir or workspace / ".issuebot" / "runs" / "run-1",
        observer=observer,
        cancel=cancel,
        deadline=deadline,
    )


async def wait_for_file(path: Path) -> int:
    for _ in range(50):
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        await asyncio.sleep(0.1)
    raise AssertionError(f"{path} was not written")


async def assert_gone(pid: int) -> None:
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"process {pid} is still alive")


@posix
async def test_run_turn_hands_the_agent_the_workspace_env_file(
    workspace: Path, tmp_path: Path
) -> None:
    (workspace / ".issuebot").mkdir()
    (workspace / ".issuebot" / "env").write_text(
        "export ACME_DATABASE_URL=postgresql://issuebot@/db\n"
        "ACME_JS_HARNESS=1\n"
        # The two a typo must not take out from under a running turn.
        "PATH=/hijacked\n"
        "GH_TOKEN=stolen\n"
    )
    record = tmp_path / "record.json"
    runner = runner_for(
        workspace,
        extra_env={
            "CLAUDE_FAKE_RECORD": str(record),
            "CLAUDE_FAKE_RECORD_ENV": "ACME_DATABASE_URL,ACME_JS_HARNESS,PATH",
        },
        token="sekret",
    )
    turn = await run(runner, workspace)
    assert turn.ok
    recorded = json.loads(record.read_text())["env"]
    assert recorded["ACME_DATABASE_URL"] == "postgresql://issuebot@/db"
    assert recorded["ACME_JS_HARNESS"] == "1"
    assert recorded["PATH"] == os.environ["PATH"]
    assert recorded["GH_TOKEN"] == "sekret"


@posix
async def test_run_turn_without_a_workspace_env_file_is_unchanged(
    workspace: Path, tmp_path: Path
) -> None:
    record = tmp_path / "record.json"
    runner = runner_for(
        workspace,
        extra_env={"CLAUDE_FAKE_RECORD": str(record), "CLAUDE_FAKE_RECORD_ENV": "FOO"},
    )
    turn = await run(runner, workspace)
    assert turn.ok
    assert json.loads(record.read_text())["env"]["FOO"] is None


@posix
async def test_run_turn_runs_with_an_unreadable_workspace_env_file(
    workspace: Path, tmp_path: Path
) -> None:
    (workspace / ".issuebot" / "env").mkdir(parents=True)
    record = tmp_path / "record.json"
    runner = runner_for(workspace, extra_env={"CLAUDE_FAKE_RECORD": str(record)})
    turn = await run(runner, workspace)
    assert turn.ok
    assert turn.error_category is None


@posix
async def test_run_turn_survives_a_null_byte_in_the_workspace_env_file(workspace: Path) -> None:
    # `create_subprocess_exec` raises `ValueError` for one, and that is not an `OSError`: before
    # the parser refused it, it escaped `run_turn` and killed the worker task.
    (workspace / ".issuebot").mkdir()
    (workspace / ".issuebot" / "env").write_bytes(b"FOO=a\x00b\n")
    turn = await run(runner_for(workspace), workspace)
    assert turn.ok
    assert turn.error_category is None


@posix
async def test_run_turn_counts_the_applied_keys_on_its_start_line(workspace: Path) -> None:
    (workspace / ".issuebot").mkdir()
    (workspace / ".issuebot" / "env").write_text("FOO=bar\nBAZ=qux\nPATH=/hijacked\n")
    stream = io.StringIO()
    configure_logging(level="INFO", fmt="json", stream=stream)
    try:
        await run(runner_for(workspace), workspace)
    finally:
        configure_logging(stream=io.StringIO())
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    (started,) = [r for r in records if r["event"] == "claude_turn_started"]
    assert started["workspace_env_count"] == 2


@posix
async def test_run_turn_scrubs_the_argv_it_logs(workspace: Path) -> None:
    """#109: every other element is a flag, a model name, a tool name or a session id, but
    `claude.mcp_config` takes a JSON document as well as a path, and an MCP server definition
    carries its credentials in its own `env` block -- so an operator who inlines one would put
    it in this line, and from there into whatever ships the worker's logs."""
    cfg = Settings.model_validate(
        {
            "github": {"repo": "example/repo", "token": "ghp_0123456789abcdef"},
            "workspace": {"root": str(workspace.parent)},
            "claude": {
                "command": str(FAKE_CLAUDE),
                "turn_timeout_ms": 30_000,
                # A value no *shape* rule would catch on its own, so what is proved is the
                # name beside it rather than the accident that MCP credentials often look
                # like Anthropic keys.
                "mcp_config": [
                    '{"mcpServers": {"x": {"env": {"LINEAR_API_KEY": "lin_oo_notakey"}}}}'
                ],
            },
        }
    )
    runner = ClaudeRunner(
        cfg,
        environ={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
            "CLAUDE_FAKE_SCENARIO": "success",
        },
    )
    stream = io.StringIO()
    configure_logging(level="INFO", fmt="json", stream=stream)
    try:
        await run(runner, workspace)
    finally:
        configure_logging(stream=io.StringIO())
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    (started,) = [r for r in records if r["event"] == "claude_turn_started"]
    logged = " ".join(started["argv"])
    assert "lin_oo_notakey" not in logged
    assert "***" in logged
    # Scrubbed, not dropped: the flag and the shape of the document are still readable.
    assert "--mcp-config" in started["argv"]
    assert "mcpServers" in logged


@posix
async def test_run_turn_rereads_the_workspace_env_file_every_turn(
    workspace: Path, tmp_path: Path
) -> None:
    # A hook may rewrite it between turns, and a session resumed after a retry never reruns
    # `before_run`, so the file is read again rather than cached from the first turn.
    env_file = workspace / ".issuebot" / "env"
    env_file.parent.mkdir()
    env_file.write_text("FOO=first\n")
    record = tmp_path / "record.json"
    runner = runner_for(
        workspace,
        extra_env={"CLAUDE_FAKE_RECORD": str(record), "CLAUDE_FAKE_RECORD_ENV": "FOO"},
    )
    await run(runner, workspace, turn_number=1)
    assert json.loads(record.read_text())["env"]["FOO"] == "first"
    env_file.write_text("FOO=second\n")
    await run(runner, workspace, turn_number=2, resume=True)
    assert json.loads(record.read_text())["env"]["FOO"] == "second"


@posix
async def test_run_turn_success_parses_everything(workspace: Path, tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    runner = runner_for(
        workspace,
        extra_env={"CLAUDE_FAKE_RECORD": str(record), "SSH_AUTH_SOCK": "/leak"},
        token="sekret",
    )
    recorder = Recorder()
    log_dir = workspace / ".issuebot" / "runs" / "run-1"
    turn = await run(runner, workspace, observer=recorder, log_dir=log_dir)
    assert turn.ok
    assert turn.error_category is None
    assert turn.error is None
    assert turn.exit_code == 0
    assert turn.session_id == SESSION_ID
    assert turn.model == "claude-opus-5[1m]"
    assert turn.api_key_source == "none"
    assert turn.subtype == "success"
    assert turn.is_error is False
    assert turn.num_turns == 2
    assert (turn.input_tokens, turn.cache_creation_input_tokens) == (4, 6681)
    assert (turn.cache_read_input_tokens, turn.output_tokens) == (26750, 123)
    assert turn.total_input_tokens == 4 + 6681 + 26750
    assert turn.cost_usd == pytest.approx(0.084256)
    assert turn.duration_ms == 3660
    assert turn.permission_denials == 0
    assert turn.result_text == "hello issuebot"
    recorded = json.loads(record.read_text())
    assert recorded["stdin"] == "Do the thing"
    assert recorded["cwd"] == str(workspace.resolve())
    assert recorded["argv"][:2] == ["-p", "--output-format"]
    assert recorded["argv"][recorded["argv"].index("--session-id") + 1] == SESSION_ID
    # The default tool policy reaches the process, not just the argv builder (#109).
    assert recorded["argv"][-3:] == ["--disallowedTools", "WebFetch", "WebSearch"]
    assert "--strict-mcp-config" in recorded["argv"]
    assert recorded["env"]["GH_TOKEN"] == "sekret"
    assert recorded["env"]["NO_COLOR"] == "1"
    assert recorded["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert recorded["env"]["SSH_AUTH_SOCK"] is None
    assert recorder.kinds == [
        "session_started",
        "rate_limits",
        "turn_activity",
        "turn_activity",
        "turn_activity",
        "process_exit",
        "turn_completed",
    ]
    assert recorder.events[1].rate_limits is not None
    assert recorder.events[2].tool_name == "Read"
    assert recorder.events[-2].detail == "0"
    assert turn.stdout_path == log_dir / "turn-1.jsonl"
    assert turn.stderr_path == log_dir / "turn-1.stderr.log"
    assert len(turn.stdout_path.read_text().splitlines()) == 6
    assert SESSION_ID in turn.stdout_path.read_text()
    assert turn.stderr_path.read_text() == ""
    assert (log_dir / "turn-1.prompt.md").read_text() == "Do the thing"


@posix
async def test_run_turn_resume_passes_the_resume_flag(workspace: Path, tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    runner = runner_for(workspace, extra_env={"CLAUDE_FAKE_RECORD": str(record)})
    turn = await run(runner, workspace, turn_number=2, resume=True)
    argv = json.loads(record.read_text())["argv"]
    assert argv[argv.index("--resume") + 1] == SESSION_ID
    assert "--session-id" not in argv
    assert turn.stdout_path.name == "turn-2.jsonl"


@posix
async def test_error_result_is_turn_failed(workspace: Path) -> None:
    recorder = Recorder()
    turn = await run(runner_for(workspace, scenario="error_result"), workspace, observer=recorder)
    assert turn.error_category == "turn_failed"
    assert turn.error is not None
    assert "error_during_execution" in turn.error
    assert "tool execution failed" in turn.error
    assert turn.exit_code == 1
    assert turn.session_id == SESSION_ID
    assert turn.cost_usd == pytest.approx(0.0412)
    assert recorder.kinds[-2:] == ["process_exit", "turn_failed"]


@posix
async def test_a_turn_that_quotes_its_environment_is_scrubbed_at_the_source(
    workspace: Path, tmp_path: Path
) -> None:
    """The runner put the token into claude's environment and knows the home directory, so
    the turn's error, its result text and the turn_failed event all come out masked (#91)."""
    home = tmp_path / "home"
    recorder = Recorder()
    runner = runner_for(
        workspace,
        scenario="leaky",
        token="literal-token-value",
        extra_env={"HOME": str(home)},
    )
    turn = await run(runner, workspace, observer=recorder)
    assert turn.error_category == "turn_failed"
    assert turn.error is not None
    assert "literal-token-value" not in turn.error and str(home) not in turn.error
    assert (
        turn.error
        == f"error_during_execution: push failed: GH_TOKEN={REDACTED} could not write ~/ws"
    )
    assert turn.result_text == f"push failed: GH_TOKEN={REDACTED} could not write ~/ws"
    (failed,) = [event for event in recorder.events if event.kind == "turn_failed"]
    assert failed.detail == turn.error
    # The files on disk are claude's stdout and stderr byte for byte; `capture_turns` is
    # their scrubbing step (#79), not the runner.
    assert "literal-token-value" in turn.stderr_path.read_text()
    assert "literal-token-value" in turn.stdout_path.read_text()


@posix
async def test_the_runners_own_messages_read_the_home_directory_as_tilde(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    runner = ClaudeRunner(
        settings(root), environ={"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    )
    turn = await run(runner, outside, log_dir=tmp_path / "logs")
    assert turn.error_category == "invalid_workspace_cwd"
    assert turn.error == "~/elsewhere is not a directory inside ~/workspaces"


@posix
async def test_budget_result_is_budget_exceeded(workspace: Path) -> None:
    turn = await run(runner_for(workspace, scenario="budget"), workspace)
    assert turn.error_category == "budget_exceeded"
    assert turn.error is not None
    assert "Budget limit" in turn.error
    assert turn.cost_usd == pytest.approx(5.02)


@posix
async def test_crash_after_init_is_process_exit_with_stderr(workspace: Path) -> None:
    turn = await run(runner_for(workspace, scenario="crash_after_init"), workspace)
    assert turn.error_category == "process_exit"
    assert turn.exit_code == 2
    assert turn.error is not None
    assert "status 2" in turn.error
    assert "fake claude crashed" in turn.error
    assert turn.session_id == SESSION_ID
    assert turn.result_text is None


@posix
async def test_a_credential_that_stopped_working_is_read_from_the_whole_stderr_tail(
    workspace: Path,
) -> None:
    """The reason is not the last line claude prints, so the tail is what gets scanned (#20)."""
    turn = await run(runner_for(workspace, scenario="logged_out"), workspace)
    assert turn.error_category == "auth_failed"
    assert turn.exit_code == 1
    assert turn.error is not None
    # The message still quotes the last line; the category comes from the tail above it.
    assert "docs.claude.com" in turn.error


@posix
async def test_no_init_is_process_exit_without_a_session(workspace: Path) -> None:
    recorder = Recorder()
    turn = await run(runner_for(workspace, scenario="no_init"), workspace, observer=recorder)
    assert turn.error_category == "process_exit"
    assert turn.session_id is None
    assert turn.exit_code == 1
    assert turn.error is not None
    assert "No conversation found" in turn.error
    assert recorder.kinds == ["turn_activity", "process_exit", "turn_failed"]
    assert recorder.events[0].message_type == "unparseable"


@posix
async def test_silence_times_out_and_kills(workspace: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    runner = runner_for(
        workspace,
        scenario="silent",
        turn_timeout_ms=1500,
        extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)},
    )
    recorder = Recorder()
    turn = await run(runner, workspace, observer=recorder)
    assert turn.error_category == "turn_timeout"
    assert turn.session_id == SESSION_ID
    assert turn.exit_code == -signal.SIGTERM
    await assert_gone(int(pidfile.read_text()))
    assert recorder.kinds[-2:] == ["process_exit", "turn_timeout"]


@posix
async def test_the_runs_deadline_ends_a_turn_that_keeps_printing(
    workspace: Path, tmp_path: Path
) -> None:
    """The silence timer restarts with every line; the run's deadline does not (#110)."""
    pidfile = tmp_path / "pid"
    runner = runner_for(
        workspace,
        scenario="slow",  # a line every 200 ms, six lines
        turn_timeout_ms=30_000,
        extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)},
    )
    recorder = Recorder()
    started = time.monotonic()
    turn = await run(runner, workspace, observer=recorder, deadline=started + 0.5)
    assert time.monotonic() - started < 1.1
    assert turn.error_category == "run_timeout"
    assert turn.error == "run deadline reached: 14400s of wall clock (agent.run_timeout_ms)"
    assert turn.exit_code == -signal.SIGTERM
    await assert_gone(int(pidfile.read_text()))
    assert recorder.kinds[-2:] == ["process_exit", "turn_timeout"]
    assert recorder.events[-1].detail == turn.error


@posix
async def test_a_deadline_still_ahead_leaves_the_turn_alone(workspace: Path) -> None:
    runner = runner_for(workspace, scenario="slow", turn_timeout_ms=30_000)
    turn = await run(runner, workspace, deadline=time.monotonic() + 30)
    assert turn.error_category is None
    assert turn.exit_code == 0


@posix
async def test_a_deadline_already_passed_ends_the_turn_at_once(workspace: Path) -> None:
    runner = runner_for(workspace, scenario="silent", turn_timeout_ms=30_000)
    started = time.monotonic()
    turn = await run(runner, workspace, deadline=started - 1)
    assert turn.error_category == "run_timeout"
    assert time.monotonic() - started < 5
    assert turn.exit_code is not None  # terminated and reaped, not waited out


@posix
async def test_stubborn_child_is_killed_after_the_grace_period(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("issuebot.agent.runner.TERMINATE_GRACE_S", 0.5)
    pidfile = tmp_path / "pid"
    runner = runner_for(
        workspace,
        scenario="stubborn",
        turn_timeout_ms=1500,
        extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)},
    )
    turn = await run(runner, workspace)
    assert turn.error_category == "turn_timeout"
    assert turn.exit_code == -signal.SIGKILL
    await assert_gone(int(pidfile.read_text()))


@posix
async def test_cancel_event_stops_the_turn(workspace: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    runner = runner_for(workspace, scenario="slow", extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)})
    cancel = asyncio.Event()
    recorder = Recorder()
    task = asyncio.create_task(run(runner, workspace, observer=recorder, cancel=cancel))
    pid = await wait_for_file(pidfile)
    cancel.set()
    turn = await task
    assert turn.error_category == "cancelled"
    assert turn.exit_code == -signal.SIGTERM
    await assert_gone(pid)
    assert recorder.kinds[-2:] == ["process_exit", "turn_failed"]


@posix
async def test_task_cancellation_kills_and_reaps(workspace: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    runner = runner_for(
        workspace, scenario="silent", extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)}
    )
    recorder = Recorder()
    task = asyncio.create_task(run(runner, workspace, observer=recorder))
    pid = await wait_for_file(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await assert_gone(pid)
    assert recorder.kinds[-1] == "process_exit"
    assert recorder.events[-1].detail == str(-signal.SIGTERM)


@posix
async def test_long_lines_are_parsed(workspace: Path) -> None:
    recorder = Recorder()
    turn = await run(runner_for(workspace, scenario="long_line"), workspace, observer=recorder)
    assert turn.ok
    assert len(turn.stdout_path.read_text().splitlines()) == 7
    assert [e.message_type for e in recorder.events].count("user") == 2


@posix
async def test_overlong_line_is_dropped_and_the_turn_continues(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("issuebot.agent.runner.STREAM_LINE_LIMIT", 64 * 1024)
    recorder = Recorder()
    turn = await run(runner_for(workspace, scenario="long_line"), workspace, observer=recorder)
    assert turn.ok
    assert turn.result_text == "hello issuebot"
    assert sum(1 for event in recorder.events if event.message_type == "unparseable") >= 1


@posix
async def test_reader_failure_terminates_the_child(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "pid"
    runner = runner_for(workspace, scenario="slow", extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)})

    async def fake_read_stream(
        self: ClaudeRunner,
        process: asyncio.subprocess.Process,
        parser: object,
        emit: object,
        stdout_path: Path,
        deadline: float | None,
    ) -> str:
        await asyncio.sleep(0.3)
        raise OSError("disk full")

    monkeypatch.setattr(ClaudeRunner, "_read_stream", fake_read_stream)
    recorder = Recorder()
    task = asyncio.create_task(run(runner, workspace, observer=recorder))
    pid = await wait_for_file(pidfile)
    turn = await task
    assert turn.error_category == "process_exit"
    assert turn.error is not None
    assert "disk full" in turn.error
    assert turn.exit_code == -signal.SIGTERM
    await assert_gone(pid)
    assert recorder.kinds[-2:] == ["process_exit", "turn_failed"]


@posix
async def test_workspace_outside_the_root_is_rejected_without_spawning(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    runner = ClaudeRunner(settings(root), environ={"PATH": os.environ["PATH"]})
    log_dir = tmp_path / "logs"
    turn = await run(runner, outside, log_dir=log_dir)
    assert turn.error_category == "invalid_workspace_cwd"
    assert not log_dir.exists()
    turn = await run(runner, root, log_dir=log_dir)
    assert turn.error_category == "invalid_workspace_cwd"
    turn = await run(runner, root / "missing", log_dir=log_dir)
    assert turn.error_category == "invalid_workspace_cwd"


@posix
async def test_missing_executable_is_claude_not_found(workspace: Path) -> None:
    runner = runner_for(workspace, command="/nonexistent/claude")
    turn = await run(runner, workspace)
    assert turn.error_category == "claude_not_found"
    assert turn.error is not None
    assert "/nonexistent/claude" in turn.error
    assert turn.exit_code is None
    assert (workspace / ".issuebot" / "runs" / "run-1" / "turn-1.prompt.md").exists()


# --- Phase 4 hardening: group kill after the leader exited, pre-set cancel ------------


@posix
async def test_orphaned_grandchild_is_killed_with_the_group(
    workspace: Path, tmp_path: Path
) -> None:
    pidfile = tmp_path / "grandchild.pid"
    runner = runner_for(
        workspace,
        scenario="orphan",
        turn_timeout_ms=500,
        extra_env={"CLAUDE_FAKE_PIDFILE": str(pidfile)},
    )
    recorder = Recorder()
    turn = await run(runner, workspace, observer=recorder)
    grandchild = await wait_for_file(pidfile)
    assert turn.error_category == "turn_timeout"
    assert turn.exit_code == 0
    await assert_gone(grandchild)
    assert recorder.kinds[-2:] == ["process_exit", "turn_timeout"]


@posix
async def test_preset_cancel_spawns_nothing(workspace: Path, tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    runner = runner_for(workspace, extra_env={"CLAUDE_FAKE_RECORD": str(record)})
    cancel = asyncio.Event()
    cancel.set()
    recorder = Recorder()
    log_dir = workspace / ".issuebot" / "runs" / "run-1"
    turn = await run(runner, workspace, observer=recorder, cancel=cancel, log_dir=log_dir)
    assert turn.error_category == "cancelled"
    assert turn.exit_code is None
    assert turn.session_id is None
    assert not record.exists()
    assert not log_dir.exists()
    assert recorder.kinds == []


# --- the boundary (#104): what may sit at `.issuebot/env` -----------------------------------


@posix
def test_read_workspace_env_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    """A FIFO at the name used to block the event loop for good; now it is refused unread."""
    import threading

    (tmp_path / ".issuebot").mkdir()
    os.mkfifo(tmp_path / ".issuebot" / "env")
    results: list[tuple[dict[str, str], list[str]]] = []
    thread = threading.Thread(target=lambda: results.append(read_workspace_env(tmp_path)))
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "read_workspace_env blocked on the fifo"
    env, warnings = results[0]
    assert env == {}
    assert warnings == [f"refused {tmp_path / '.issuebot' / 'env'}: not a regular file (a fifo)"]


@posix
def test_read_workspace_env_refuses_a_symbolic_link(tmp_path: Path) -> None:
    """A link is not followed, so a file the worker's uid can read never reaches the session."""
    outside = tmp_path / "operator.env"
    outside.write_text("ISSUEBOT_DB_PASSWORD=hunter2\n")
    (tmp_path / "ws" / ".issuebot").mkdir(parents=True)
    os.symlink(outside, tmp_path / "ws" / ".issuebot" / "env")
    env, warnings = read_workspace_env(tmp_path / "ws")
    assert env == {}
    assert warnings == [f"refused {tmp_path / 'ws' / '.issuebot' / 'env'}: a symbolic link"]


def test_read_workspace_env_refuses_a_directory_by_reason(tmp_path: Path) -> None:
    (tmp_path / ".issuebot" / "env").mkdir(parents=True)
    env, warnings = read_workspace_env(tmp_path)
    assert env == {}
    assert warnings == [
        f"refused {tmp_path / '.issuebot' / 'env'}: not a regular file (a directory)"
    ]


def test_read_workspace_env_refuses_a_file_nobody_declared_wrote(tmp_path: Path) -> None:
    from issuebot.agent.boundary import Boundary

    (tmp_path / ".issuebot").mkdir()
    (tmp_path / ".issuebot" / "env").write_text("FOO=bar\n")
    me = os.getuid()
    stranger = Boundary(worker_uid=me + 1, session_uid=me + 2)
    env, warnings = read_workspace_env(tmp_path, boundary=stranger)
    assert env == {}
    assert warnings == [f"refused {tmp_path}: owned by uid {me}, not by the worker"]


def test_read_workspace_env_bounds_the_bytes_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is on the read, not on a string built after the whole file came in."""
    from issuebot.agent import boundary as boundary_module

    calls: list[int] = []
    real_read = os.read

    def counting_read(fd: int, size: int) -> bytes:
        calls.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(boundary_module.os, "read", counting_read)
    (tmp_path / ".issuebot").mkdir()
    (tmp_path / ".issuebot" / "env").write_bytes(b"K=v\n" * (WORKSPACE_ENV_LIMIT // 2))
    env, warnings = read_workspace_env(tmp_path)
    assert sum(calls) <= WORKSPACE_ENV_LIMIT + boundary_module._READ_CHUNK
    assert env == {"K": "v"}
    assert warnings == [f"longer than {WORKSPACE_ENV_LIMIT} bytes: the rest was ignored"]


@posix
async def test_run_turn_survives_a_fifo_at_the_workspace_env_file(workspace: Path) -> None:
    """The turn runs, logs the refusal, and the fake claude never sees a variable from it."""
    (workspace / ".issuebot").mkdir(parents=True)
    os.mkfifo(workspace / ".issuebot" / "env")
    runner = runner_for(workspace)
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    try:
        turn = await asyncio.wait_for(run(runner, workspace), timeout=30)
    finally:
        configure_logging(stream=io.StringIO())
    assert turn.ok, turn.error
    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    ignored = [r["reason"] for r in records if r["event"] == "workspace_env_ignored"]
    assert ignored == [f"refused {workspace / '.issuebot' / 'env'}: not a regular file (a fifo)"]


@posix
async def test_run_turn_refuses_a_log_directory_that_is_not_the_workers(
    workspace: Path, tmp_path: Path
) -> None:
    """A directory the session placed at the run's log path is not written through."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (workspace / ".issuebot" / "runs").mkdir(parents=True)
    os.symlink(elsewhere, workspace / ".issuebot" / "runs" / "planted")
    runner = runner_for(workspace)
    turn = await run(runner, workspace, log_dir=workspace / ".issuebot" / "runs" / "planted")
    assert turn.error_category == "invalid_workspace_cwd"
    assert turn.error is not None and "a symbolic link" in turn.error
    assert list(elsewhere.iterdir()) == []
