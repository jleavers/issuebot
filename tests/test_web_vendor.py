"""The vendored front-end files are the bytes ``vendor/README.md`` records (#108).

Dependabot does not see ``src/issuebot/web/static/vendor/``, pre-commit excludes it, and CI
never looks at it, so the README's SHA-256 block was a record of what was vendored rather than
a check on what is served. These two files run in the operator's browser under a CSP that
permits them, on pages that render GitHub- and model-authored text, so a silent change to
either is a change to what executes there. The digests are parsed out of the README rather
than repeated here: a deliberate bump stays a one-file edit, and an accidental change to
either file, or to the record, still fails.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

VENDOR = Path(str(files("issuebot.web") / "static" / "vendor"))
README = (VENDOR / "README.md").read_text(encoding="utf-8")

# A table row: the file in backticks, the library, the version, the source, the licence.
TABLE_ROW = re.compile(
    r"^\|\s*`(?P<file>[^`]+)`\s*\|\s*(?P<library>[^|]+?)\s*\|\s*(?P<version>[^|]+?)\s*\|"
)
# The digest block is the shape ``sha256sum`` prints and ``sha256sum -c`` reads: a 64-digit
# hex digest, two spaces, the file name (``sha256sum -b``'s ``*`` marker included). Anchored
# to a line so prose never matches.
DIGEST_SHAPE = "'<64 hex digits>  <file>' (two spaces, at the start of a line)"
DIGEST_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  \*?(?P<file>\S+)$", re.MULTILINE)
# The licence files the table names in its last column, as ``(`htmx.LICENSE`)``.
LICENCE_REF = re.compile(r"\(`(?P<file>[^`]+\.LICENSE)`\)")


def _table() -> dict[str, dict[str, str]]:
    rows = {}
    for line in README.splitlines():
        match = TABLE_ROW.match(line)
        if match and match["file"] != "File":
            rows[match["file"]] = {"library": match["library"], "version": match["version"]}
    return rows


TABLE = _table()
DIGESTS = {match["file"]: match["digest"] for match in DIGEST_LINE.finditer(README)}
LICENCES = set(LICENCE_REF.findall(README))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked(directory: Path) -> set[str]:
    """The names git tracks directly under ``directory``: what a checkout, and so a build, has."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "."],
            cwd=directory,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"not a git checkout, or no git: {error}")
    return {name for name in listed.stdout.split("\0") if name and "/" not in name}


def test_readme_records_the_two_libraries() -> None:
    """The parsers found the record.

    Without this, a README reformatted past the parsers would leave the parametrized tests
    with nothing to check (``empty_parameter_set_mark`` fails that too) and the digest and
    table sets vacuously equal.
    """
    assert set(TABLE) == {"htmx.min.js", "chart.umd.js"}, (
        f"table parsed {sorted(TABLE)}; a row is '| `<file>` | <library> | <version> | ...'"
    )
    assert set(DIGESTS) == set(TABLE), (
        f"digest block parsed {sorted(DIGESTS)}, table parsed {sorted(TABLE)}; "
        f"a digest line is {DIGEST_SHAPE}"
    )
    assert len(LICENCES) == len(TABLE), (
        f"licences parsed {sorted(LICENCES)}: one '(`<file>.LICENSE`)' per table row"
    )


@pytest.mark.parametrize("name", sorted(DIGESTS), ids=sorted(DIGESTS))
def test_vendored_file_is_the_recorded_bytes(name: str) -> None:
    path = VENDOR / name
    assert path.is_file(), f"{name} is recorded in vendor/README.md but not on disk"
    actual = sha256(path)
    assert actual == DIGESTS[name], (
        f"{name} does not match vendor/README.md: recorded {DIGESTS[name]}, on disk {actual}; "
        "a deliberate bump updates the README's table and digest block together"
    )


@pytest.mark.parametrize("name", sorted(TABLE), ids=sorted(TABLE))
def test_vendored_file_carries_the_recorded_version(name: str) -> None:
    """The bytes name their own version, so the table's claim is checked against them too."""
    version = TABLE[name]["version"]
    assert version.encode("utf-8") in (VENDOR / name).read_bytes(), (
        f"{name} does not contain the version {version} that vendor/README.md records"
    )


def test_every_vendored_file_is_recorded() -> None:
    """Nothing is served from ``vendor/`` that the README does not account for.

    Tracked files, not directory entries: an editor's swap file or a ``.DS_Store`` is not
    served, and a red here should always mean a change to the repository.
    """
    tracked = _tracked(VENDOR) - {"README.md"}
    recorded = set(DIGESTS) | LICENCES
    assert tracked == recorded, (
        f"unrecorded in vendor/README.md: {sorted(tracked - recorded)}; "
        f"recorded but not tracked: {sorted(recorded - tracked)}"
    )
