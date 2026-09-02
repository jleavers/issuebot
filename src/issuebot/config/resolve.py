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


def resolve_path(value: str, *, base_dir: Path) -> Path:
    """Expand ``~``, resolve relative to ``base_dir`` and normalise to absolute."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


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
    if isinstance(root, str):
        root = str(resolve_path(root, base_dir=base_dir))
    _set(config, WORKSPACE_ROOT_FIELD, root)
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
