"""Every third-party action and hook is pinned to a commit digest (#111).

A tag is a name its owner can repoint; a digest is the referent. ``uv.lock`` already
hash-pins every Python artefact, and this pins the rest of what CI and a developer's
``pre-commit run`` execute: the ``uses:`` lines of every workflow and the ``rev:`` lines of
the pre-commit config. Each carries its tag beside it, which is what the two bump channels
(Dependabot's ``github-actions`` ecosystem, the ``pre-commit-version`` workflow) rewrite.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted(
    path for ext in ("*.yml", "*.yaml") for path in (ROOT / ".github" / "workflows").glob(ext)
)
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

# owner/repo[/path]@<40 hex> # <tag>
USES = re.compile(r"^\s*(?:- )?uses: [\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40} # \S+$")
# A local action is this repository's own file, and a container image is pinned by its own
# digest if at all; neither names a third party's tag, so neither is this rule's business.
UNPINNABLE = re.compile(r"^\s*(?:- )?uses: (?:\./|docker://)")
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
            if UNPINNABLE.match(line):
                continue
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


def test_the_action_digests_still_have_a_bump_channel() -> None:
    """Dependabot moves the ``uses:`` digests; without it they would freeze forever.

    A digest pin is only as good as the channel that moves it, and that channel is one
    ``package-ecosystem`` entry in a file nothing else in the tree reads.
    """
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    ecosystems = {entry.get("package-ecosystem") for entry in config["updates"]}
    assert "github-actions" in ecosystems, f"no github-actions ecosystem in {DEPENDABOT.name}"


def test_the_pre_commit_bump_job_runs_the_hooks_without_a_write_token() -> None:
    """The hooks are unreviewed upstream code, so they run in the job that cannot push.

    ``autoupdate --freeze`` fetches each hook repository at a tag nobody has looked at yet
    and runs its entry points. That job holds read scopes only and checks out without a
    persisted credential; the job that commits and opens the pull request runs no hook.
    """
    config = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "pre-commit-version.yml").read_text(encoding="utf-8")
    )
    freeze, open_pr = config["jobs"]["freeze"], config["jobs"]["open-pr"]

    assert set(freeze["permissions"].values()) == {"read"}, freeze["permissions"]
    checkout = next(step for step in freeze["steps"] if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"]["persist-credentials"] is False

    # The invocation, not the word: open-pr copies `.pre-commit-config.yaml` about and its
    # pull request body quotes the command in prose. Every hook this workflow runs, it runs
    # through the project's own environment.
    invokes = re.compile(r"\buv run pre-commit\b")
    assert [step for step in freeze["steps"] if invokes.search(step.get("run", ""))], (
        "the freeze job runs no hooks"
    )
    assert not [step for step in open_pr["steps"] if invokes.search(step.get("run", ""))]
    assert open_pr["permissions"] == {"contents": "write", "pull-requests": "write"}
