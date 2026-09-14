"""No credential has a value committed to the repository (#78).

The shared store's password is ``ISSUEBOT_DB_PASSWORD``, supplied per deployment through
``.env`` with no default: ``compose.yaml`` names it as a required substitution wherever the
credential is used, ``.env.example`` ships the key empty, and every DSN in the operator-facing
files is a placeholder over it. This session cannot run ``docker compose config`` (the CI
``docker`` job proves the refusal itself), so this pins the shape the refusal depends on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "compose.yaml"
ENV_EXAMPLE = ROOT / ".env.example"

# ``${ISSUEBOT_DB_PASSWORD:?message}``: the ``:?`` form fails on unset *and* empty, which is
# what makes a copied-but-unfilled ``.env`` a refusal rather than an empty password; the
# message has to name the variable so the operator learns which one.
REQUIRED = re.compile(r"\$\{ISSUEBOT_DB_PASSWORD:\?[^}]*ISSUEBOT_DB_PASSWORD[^}]*\}")
HUB_DSN = re.compile(rf"^postgresql://issuebot:{REQUIRED.pattern}@db:5432/issuebot$")

# The files an operator or a developer copies a DSN from. ``tests/`` is excluded on purpose:
# the scrubber's tests hold ``issuebot:issuebot`` as a *sample* of the DSN shape they mask.
OPERATOR_FACING = [
    "compose.yaml",
    ".env.example",
    "README.md",
    "CLAUDE.md",
    "Dockerfile",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / ".github" / "workflows").glob("*.yml")),
]


def _services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _env(service: dict) -> dict[str, str]:
    environment = service.get("environment", {})
    assert isinstance(environment, dict), "a mapping, so a key can be asserted on by name"
    return environment


def test_db_password_is_a_required_substitution_with_no_default() -> None:
    password = _env(_services()["db"])["POSTGRES_PASSWORD"]
    assert REQUIRED.fullmatch(password), password


@pytest.mark.parametrize("service", ["worker", "web"])
def test_hub_dsn_is_built_from_the_required_password(service: str) -> None:
    dsn = _env(_services()[service])["DATABASE_URL"]
    assert HUB_DSN.fullmatch(dsn), dsn


def test_password_has_no_default_anywhere_in_compose() -> None:
    # ``${ISSUEBOT_DB_PASSWORD:-x}`` or ``${ISSUEBOT_DB_PASSWORD-x}`` would be a committed
    # working credential again, under another spelling.
    text = COMPOSE.read_text()
    assert not re.search(r"\$\{ISSUEBOT_DB_PASSWORD:?[-+]", text)
    # And every use is the required form, so no service reads it as an optional value.
    uses = re.findall(r"\$\{ISSUEBOT_DB_PASSWORD[^}]*\}", text)
    assert uses and all(REQUIRED.fullmatch(use) for use in uses), uses


def test_throwaway_test_db_carries_no_credential() -> None:
    env = _env(_services()["test-db"])
    assert "POSTGRES_PASSWORD" not in env
    assert env["POSTGRES_HOST_AUTH_METHOD"] == "trust"


def test_env_example_ships_the_key_empty() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    assignments = [line for line in lines if line.startswith("ISSUEBOT_DB_PASSWORD=")]
    assert assignments == ["ISSUEBOT_DB_PASSWORD="], assignments
    # And the key is documented, not just present: a comment introduces it.
    index = lines.index("ISSUEBOT_DB_PASSWORD=")
    assert lines[index - 1].startswith("#"), "the key wants a comment saying what it guards"


def _is_working_credential(line: str) -> bool:
    """A DSN with the old password, or a ``POSTGRES_PASSWORD`` set to anything but the
    required substitution."""
    if "issuebot:issuebot@" in line:
        return True
    return bool(re.search(r"POSTGRES_PASSWORD:\s*\S", line)) and not REQUIRED.search(line)


@pytest.mark.parametrize("relative", OPERATOR_FACING)
def test_no_operator_facing_file_holds_a_working_database_credential(relative: str) -> None:
    text = (ROOT / relative).read_text()
    hits = [
        f"{relative}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if _is_working_credential(line)
    ]
    assert hits == [], "\n".join(hits)
