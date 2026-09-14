"""Every third-party action and hook is pinned to a commit digest (#111).

A tag is a name its owner can repoint; a digest is the referent. ``uv.lock`` already
hash-pins every Python artefact, and this pins the rest of what CI and a developer's
``pre-commit run`` execute: the ``uses:`` lines of every workflow and the ``rev:`` lines of
the pre-commit config. Each carries its tag beside it, which is what the two bump channels
(Dependabot's ``github-actions`` ecosystem, the ``pre-commit-version`` workflow) rewrite.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"

# owner/repo[/path]@<40 hex> # <tag>
USES = re.compile(r"^\s*(?:- )?uses: [\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40} # \S+$")
# <40 hex>  # frozen: <tag>, as `pre-commit autoupdate --freeze` writes it
REV = re.compile(r"^\s*rev: [0-9a-f]{40}  # frozen: \S+$")


def _lines(path: Path, key: str) -> list[str]:
    """The lines that set ``key`` (not a comment that mentions it)."""
    pattern = re.compile(rf"^\s*(?:- )?{key}:")
    return [line for line in path.read_text(encoding="utf-8").splitlines() if pattern.match(line)]


def test_every_action_is_pinned_to_a_digest_with_its_tag_beside_it() -> None:
    assert WORKFLOWS, "no workflows found"
    for workflow in WORKFLOWS:
        lines = _lines(workflow, "uses")
        assert lines, f"{workflow.name}: no uses: lines"
        for line in lines:
            assert USES.match(line), f"{workflow.name}: not a digest pin: {line.strip()}"


def test_every_hook_is_frozen_at_a_digest_with_its_tag_beside_it() -> None:
    lines = _lines(PRE_COMMIT, "rev")
    assert lines, "no rev: lines"
    for line in lines:
        assert REV.match(line), f"not a frozen digest: {line.strip()}"


def test_the_bump_jobs_identify_their_pull_request_by_provenance_not_branch_name() -> None:
    for name in ("claude-code-version.yml", "pre-commit-version.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "pr list --head" not in text, name
        assert 'select(.head.repo.full_name == \\"${GITHUB_REPOSITORY}\\"' in text, name
        assert '.user.login == \\"github-actions[bot]\\"' in text, name
        assert "head=${GITHUB_REPOSITORY_OWNER}:${branch}" in text, name


def test_the_pre_commit_bump_job_freezes_rather_than_retagging() -> None:
    text = (ROOT / ".github" / "workflows" / "pre-commit-version.yml").read_text(encoding="utf-8")
    assert "pre-commit autoupdate --freeze" in text
    assert "pre-commit run --all-files" in text
