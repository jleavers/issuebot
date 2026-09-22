"""The remedy clauses issuebot prints, in the deployment that will run them (#169)."""

from pathlib import Path

import pytest

from issuebot import invocation
from issuebot.invocation import in_container, run_hint

SOURCE = Path(__file__).resolve().parents[1] / "src" / "issuebot"


def test_the_host_clause_is_the_imperative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(invocation, "CONTAINER_MARKER", Path("/nonexistent/issuebot"))
    assert not in_container()
    assert run_hint("labels ensure") == "run issuebot labels ensure"


def test_in_the_image_the_clause_is_the_compose_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The README's own step 2, so an operator can paste it where they ran `validate`."""
    monkeypatch.setattr(invocation, "CONTAINER_MARKER", tmp_path)
    assert in_container()
    assert run_hint("labels ensure") == "docker compose run --rm worker labels ensure"


def test_the_marker_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worker starts long before it complains about anything, and the suite runs in both
    deployments, so the directory is stat'ed per call rather than resolved at import.
    """
    marker = tmp_path / "etc-issuebot"
    monkeypatch.setattr(invocation, "CONTAINER_MARKER", marker)
    assert run_hint("validate") == "run issuebot validate"
    marker.mkdir()
    assert run_hint("validate") == "docker compose run --rm worker validate"


def test_a_marker_that_is_a_file_is_not_the_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`/etc/issuebot` is a directory the build creates; anything else is a host that happens
    to carry the name, and naming a compose service to that operator would be the wrong half
    of the choice.
    """
    marker = tmp_path / "etc-issuebot"
    marker.write_text("")
    monkeypatch.setattr(invocation, "CONTAINER_MARKER", marker)
    assert run_hint("labels ensure") == "run issuebot labels ensure"


# Either wording hard-coded outside the helper is the same defect: one of the two deployments
# would read a command it cannot run. `docker compose build worker` is not among them -- that
# names a build, which has no host spelling at all.
SPELLINGS = ("run issuebot ", "docker compose run --rm ")


def test_no_module_spells_the_remedy_for_itself() -> None:
    """Every site that names `labels ensure` goes through the helper, so one deployment reads
    one wording: `validate`, the worker's startup complaint and both adapters' `not found`.
    """
    offenders = [
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.rglob("*.py")
        if path.name != "invocation.py"
        and any(spelling in path.read_text(encoding="utf-8") for spelling in SPELLINGS)
    ]
    assert offenders == []
