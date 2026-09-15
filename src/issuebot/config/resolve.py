"""Resolve ``$VAR`` references, ``~`` and relative paths for designated config fields.

Only the fields listed here are touched. Hook scripts and every other string are
passed to validation verbatim.
"""

import copy
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from issuebot.config.errors import MissingEnvironmentVariable

ENV_REF = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")

SECRET_FIELDS: dict[tuple[str, ...], str] = {
    ("github", "token"): "GH_TOKEN",
    ("database", "url"): "DATABASE_URL",
    ("notifications", "slack", "webhook_url"): "SLACK_WEBHOOK_URL",
}
WORKSPACE_ROOT_FIELD: tuple[str, ...] = ("workspace", "root")
WORKSPACE_ROOT_FALLBACK = "ISSUEBOT_WORKSPACE_ROOT"
WORKSPACE_ROOT_DEFAULT = "/workspaces"
AGENT_RUN_AS_FIELD: tuple[str, ...] = ("agent", "run_as")
AGENT_RUN_AS_FALLBACK = "ISSUEBOT_AGENT_USER"
# The accounts the worker image built, written by the same ``useradd`` loop that creates them
# (#142). Below the variable and above the host route, so the image's default is the accounts
# it actually has and a pool raised at build time cannot disagree with the names the worker
# resolves at run time.
SESSION_ACCOUNTS_FILE = Path("/etc/issuebot/session-accounts")
MCP_CONFIG_FIELD: tuple[str, ...] = ("claude", "mcp_config")


def resolve_env_value(
    value: Any,
    *,
    field: str,
    fallback: str | None,
    environ: Mapping[str, str],
) -> Any:
    """Apply the ``$VAR`` rules to one value.

    ``None`` (absent) -> the fallback variable if set and non-empty, else ``None``.
    ``"$NAME"`` -> that variable; unset or empty raises ``MissingEnvironmentVariable``.
    Anything else is returned unchanged.
    """
    if value is None:
        if fallback and environ.get(fallback):
            return environ[fallback]
        return None
    if isinstance(value, str) and (match := ENV_REF.match(value)):
        name = match.group(1)
        resolved = environ.get(name)
        if not resolved:
            raise MissingEnvironmentVariable(variable=name, field=field)
        return resolved
    return value


def built_session_accounts() -> list[str] | None:
    """The session accounts this image was built with, or ``None`` outside one.

    The built fact rather than a number to re-derive from: compose passes the operator's own
    ``ISSUEBOT_AGENT_POOL_SIZE`` into the container through ``env_file``, so a runtime copy of
    the size describes the file they just edited and not the image they are running (#142).
    Read at call time, never at import, so a test can point the constant at a file of its own.
    """
    try:
        text = SESSION_ACCOUNTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    accounts = [line.strip() for line in text.splitlines() if line.strip()]
    return accounts or None


def resolve_path(value: str, *, base_dir: Path) -> Path:
    """Expand ``~``, resolve relative to ``base_dir`` and normalise to absolute."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def resolve_mcp_config_entry(value: Any, *, base_dir: Path) -> Any:
    """Resolve one ``claude.mcp_config`` entry that names a file; pass a JSON document through.

    ``claude -p`` runs with the clone as its working directory, and the clone is the session's
    to write, so a relative path handed to ``--mcp-config`` verbatim would be read from there:
    turn one could rewrite it and turn two would load the rewritten server set (#109). Resolved
    against the workflow's directory instead, like ``workspace.root``, so the file named is the
    operator's. A JSON string (``{`` or ``[`` first) is the servers themselves and is left as
    written; anything that is not a string is left for validation to report.
    """
    if not isinstance(value, str) or not value.strip():
        return value
    if value.lstrip()[0] in "{[":
        return value
    return str(resolve_path(value, base_dir=base_dir))


def resolve_config(
    raw: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    base_dir: Path,
) -> dict[str, Any]:
    """Return a deep copy of ``raw`` with the designated fields resolved."""
    config: dict[str, Any] = copy.deepcopy(dict(raw))

    for keypath, fallback in SECRET_FIELDS.items():
        resolved = resolve_env_value(
            _get(config, keypath), field=".".join(keypath), fallback=fallback, environ=environ
        )
        _set(config, keypath, resolved)

    root = resolve_env_value(
        _get(config, WORKSPACE_ROOT_FIELD),
        field=".".join(WORKSPACE_ROOT_FIELD),
        fallback=WORKSPACE_ROOT_FALLBACK,
        environ=environ,
    )
    if root is None:
        root = WORKSPACE_ROOT_DEFAULT
    if isinstance(root, str) and root:
        root = str(resolve_path(root, base_dir=base_dir))
    _set(config, WORKSPACE_ROOT_FIELD, root)

    # The image writes its account list; the variable and the front matter both win over it,
    # and a host has neither (#75, #142). An explicit ``agent.run_as`` in WORKFLOW.md wins over
    # everything, as for the root.
    run_as = resolve_env_value(
        _get(config, AGENT_RUN_AS_FIELD),
        field=".".join(AGENT_RUN_AS_FIELD),
        fallback=AGENT_RUN_AS_FALLBACK,
        environ=environ,
    )
    if run_as is None:
        run_as = built_session_accounts()
    _set(config, AGENT_RUN_AS_FIELD, run_as)

    entries = _get(config, MCP_CONFIG_FIELD)
    if isinstance(entries, list):
        _set(
            config,
            MCP_CONFIG_FIELD,
            [resolve_mcp_config_entry(entry, base_dir=base_dir) for entry in entries],
        )
    return config


def _get(config: Mapping[str, Any], keypath: tuple[str, ...]) -> Any:
    node: Any = config
    for key in keypath:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _set(config: dict[str, Any], keypath: tuple[str, ...], value: Any) -> None:
    """Set ``value`` at ``keypath``; skip when it would create a key just to hold ``None``
    or would overwrite a non-mapping intermediate (left for validation to report)."""
    *parents, leaf = keypath
    node: Any = config
    for key in parents:
        if key not in node:
            if value is None:
                return
            node[key] = {}
        node = node[key]
        if not isinstance(node, dict):
            return
    if value is None and leaf not in node:
        return
    node[leaf] = value
