"""No credential has a value committed to the repository (#78).

The shared store's password is ``ISSUEBOT_DB_PASSWORD``, supplied per deployment through
``.env`` with no default: ``compose.yaml`` names it as a required substitution wherever the
credential is used, ``.env.example`` ships the key empty, and every DSN in the operator-facing
files carries the placeholder as its password or none at all. This session cannot run
``docker compose config`` (the CI ``docker`` job proves the refusal itself), so this pins the
shape the refusal depends on.
"""

from __future__ import annotations

import re
import subprocess
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

# The credential this issue retired, in its two spellings. It may appear in three places and
# nowhere else in the tracked tree: the design documents, which are dated records of what was
# decided and would be falsified by an edit; the scrubber's tests, which hold the DSN as a
# *sample* of the shape they mask; and this file.
RETIRED = re.compile(r"issuebot:issuebot@|POSTGRES_PASSWORD\s*[:=]\s*['\"]?issuebot\b")
RETIRED_ALLOWED = (
    "docs/superpowers/",
    "tests/test_agent_scrub.py",
    "tests/test_compose_credentials.py",
)

# The files an operator or a developer copies a DSN from: here the rule is the invariant, not
# the incident. A DSN may carry the placeholder as its password or none at all, and a
# ``POSTGRES_PASSWORD`` may only ever be the required substitution.
OPERATOR_FACING = [
    "compose.yaml",
    ".env.example",
    "README.md",
    "docs/toolchains.md",
    "CLAUDE.md",
    "Dockerfile",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / ".github" / "workflows").glob("*.yml")),
]
# The password position may hold the bare placeholder or the required form, and nothing else:
# ``${ISSUEBOT_DB_PASSWORD:-issuebot}`` is the retired credential under another spelling.
PLACEHOLDER = r"\$\{ISSUEBOT_DB_PASSWORD(?::\?[^}]*)?\}"
DSN_WITH_PASSWORD = re.compile(rf"postgres(?:ql)?://[^:/@\s]+:(?!{PLACEHOLDER}@)[^@\s]+@")
# The value up to a trailing comment, unquoted, so a legitimate line annotated or quoted is
# judged on its value and a comment is never called a credential.
PASSWORD_ASSIGNMENT = re.compile(
    r"""POSTGRES_PASSWORD\s*[:=]\s*(?P<value>"[^"]*"|'[^']*'|\$\{[^}]*\}|\S+)"""
)


def _services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _env(service: dict) -> dict[str, str]:
    environment = service.get("environment", {})
    assert isinstance(environment, dict), "a mapping, so a key can be asserted on by name"
    return environment


def _tracked_files() -> list[str]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"not a git checkout, or no git: {error}")
    return [name for name in listed.stdout.split("\0") if name]


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


def test_web_password_is_passed_through_with_no_committed_value() -> None:
    """The dashboard's password (#73) reaches ``web`` from ``.env`` and from nowhere else.

    A pass-through rather than the ``:?`` form: compose interpolates before it applies a
    profile, so a required substitution would make every worker-only checkout carry a secret
    it never uses; the app refuses to start without it instead. What this pins is that the
    default is empty, so no spelling of the variable ever carries a committed value.
    """
    value = _env(_services()["web"])["ISSUEBOT_WEB_PASSWORD"]
    assert value == "${ISSUEBOT_WEB_PASSWORD:-}", value
    text = COMPOSE.read_text()
    uses = re.findall(r"\$\{ISSUEBOT_WEB_PASSWORD[^}]*\}", text)
    assert uses == ["${ISSUEBOT_WEB_PASSWORD:-}"], uses
    # Only the web service names it in its `environment:` map. The worker's `env_file: .env`
    # does carry it into the worker process in a hub checkout, which is contained by #75's uid
    # split (the session cannot read /proc/<worker>/environ) and by `agent_environment`'s
    # allow-list, and it is masked by the scrubber wherever the worker holds it.
    assert "ISSUEBOT_WEB_PASSWORD" not in _env(_services()["worker"])


def test_env_example_ships_the_web_password_empty() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    assignments = [line for line in lines if line.startswith("ISSUEBOT_WEB_PASSWORD=")]
    assert assignments == ["ISSUEBOT_WEB_PASSWORD="], assignments
    assert lines[lines.index("ISSUEBOT_WEB_PASSWORD=") - 1].startswith("#")


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
    """A DSN whose password is anything but the placeholder, or a ``POSTGRES_PASSWORD`` set
    to anything but the required substitution."""
    if DSN_WITH_PASSWORD.search(line):
        return True
    assignment = PASSWORD_ASSIGNMENT.search(line)
    if assignment is None:
        return False
    value = assignment.group("value").strip("\"'")
    return not REQUIRED.fullmatch(value)


@pytest.mark.parametrize(
    ("line", "working"),
    [
        ("DATABASE_URL: postgresql://issuebot:changeme@db:5432/issuebot", True),
        ("postgresql://issuebot:${ISSUEBOT_DB_PASSWORD:-issuebot}@127.0.0.1:5432/issuebot", True),
        ("postgresql://issuebot:${ISSUEBOT_DB_PASSWORD-x}@db/issuebot", True),
        ("postgres://a:b@c", True),
        ("POSTGRES_PASSWORD: issuebot", True),
        ("- POSTGRES_PASSWORD=hunter22", True),
        ('POSTGRES_PASSWORD: "${ISSUEBOT_DB_PASSWORD:-x}"', True),
        ("postgresql://issuebot:${ISSUEBOT_DB_PASSWORD}@127.0.0.1:5432/issuebot", False),
        (
            "postgresql://issuebot:${ISSUEBOT_DB_PASSWORD:?ISSUEBOT_DB_PASSWORD unset}@db:5432/x",
            False,
        ),
        ("postgresql://issuebot@/acme_test?host=/tmp", False),
        ("postgresql://issuebot@db:5432/issuebot", False),
        ("POSTGRES_PASSWORD: ${ISSUEBOT_DB_PASSWORD:?set ISSUEBOT_DB_PASSWORD}  # required", False),
        ('POSTGRES_PASSWORD: "${ISSUEBOT_DB_PASSWORD:?set ISSUEBOT_DB_PASSWORD}"', False),
        ("The image reads `POSTGRES_PASSWORD` once, at initdb.", False),
        ("POSTGRES_PASSWORD=", False),
    ],
)
def test_working_credential_predicate(line: str, working: bool) -> None:
    assert _is_working_credential(line) is working


@pytest.mark.parametrize("relative", OPERATOR_FACING)
def test_no_operator_facing_file_holds_a_working_database_credential(relative: str) -> None:
    text = (ROOT / relative).read_text()
    hits = [
        f"{relative}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if _is_working_credential(line)
    ]
    assert hits == [], "\n".join(hits)


def test_retired_credential_appears_nowhere_else_in_the_tracked_tree() -> None:
    hits = []
    for name in _tracked_files():
        if name.startswith(RETIRED_ALLOWED) or not (ROOT / name).is_file():
            continue
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        hits.extend(
            f"{name}:{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), start=1)
            if RETIRED.search(line)
        )
    assert hits == [], "\n".join(hits)
