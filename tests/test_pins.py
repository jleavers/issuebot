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
# Building or running an image: the `docker` verbs and the `docker/...` actions that wrap
# them. Matched against a step's `uses` and `run` together, so neither spelling escapes it.
EXECUTES = re.compile(r"\bdocker(?:/|\s+(?:run|build|buildx|compose)\b)")


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


def _bump_split(name: str, runner: str, writer: str) -> tuple[dict, dict]:
    """The two halves of a bump job: the one that runs unreviewed code, the one that pushes.

    Both bump workflows fetch third-party code at a referent nobody has looked at yet -- the
    very thing the digest pins above exist for -- and both then have to push a branch and
    open a pull request. The rule (#129 for the hooks, #138 for the claude pin) is that those
    are never the same job. The half that executes holds read scopes only and checks out with
    ``persist-credentials: false``, so ``actions/checkout`` leaves no pushable token in
    ``.git/config`` for that code to read out; the half that holds the write scopes takes the
    result across a job boundary as an artefact and executes none of it.
    """
    config = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    run_job, write_job = config["jobs"][runner], config["jobs"][writer]

    assert set(run_job["permissions"].values()) == {"read"}, f"{name}: {run_job['permissions']}"
    checkout = next(s for s in run_job["steps"] if "actions/checkout@" in s.get("uses", ""))
    assert checkout["with"]["persist-credentials"] is False, name
    assert write_job["needs"] == runner, name
    assert write_job["permissions"] == {"contents": "write", "pull-requests": "write"}, name
    return run_job, write_job


def test_the_pre_commit_bump_job_runs_the_hooks_without_a_write_token() -> None:
    """The hooks are unreviewed upstream code, so they run in the job that cannot push.

    ``autoupdate --freeze`` fetches each hook repository at a tag nobody has looked at yet
    and runs its entry points. That job holds read scopes only and checks out without a
    persisted credential; the job that commits and opens the pull request runs no hook.
    """
    freeze, open_pr = _bump_split("pre-commit-version.yml", "freeze", "open-pr")

    # The invocation, not the word: open-pr copies `.pre-commit-config.yaml` about and its
    # pull request body quotes the command in prose. Every hook this workflow runs, it runs
    # through the project's own environment.
    invokes = re.compile(r"\buv run pre-commit\b")
    assert [step for step in freeze["steps"] if invokes.search(step.get("run", ""))], (
        "the freeze job runs no hooks"
    )
    assert not [step for step in open_pr["steps"] if invokes.search(step.get("run", ""))]


def _commands(script: str) -> str:
    """A step's script with its comment lines dropped: the invocation, not the word.

    Several of these scripts explain in a comment what they deliberately do *not* do, so a
    match against the raw text would read the explanation as the thing it rules out.
    """
    return "\n".join(ln for ln in script.splitlines() if not ln.lstrip().startswith("#"))


def test_the_claude_bump_job_builds_and_runs_the_new_version_without_a_write_token() -> None:
    """The new claude release is installed and executed in the job that cannot push (#138).

    The image build pipes ``claude.ai/install.sh`` into ``bash`` at a version published
    moments earlier, and the proof step then executes the binary that produced. Neither may
    sit beside a credential that can push to this repository or move its pull requests, so
    the build job holds read scopes only and the job that commits downloads the rewritten
    ``Dockerfile`` as an artefact, re-checks the pin it carries, and builds nothing.
    """
    build, open_pr = _bump_split("claude-code-version.yml", "build", "open-pr")

    def executes(job: dict) -> list[dict]:
        return [
            s
            for s in job["steps"]
            if EXECUTES.search(f"{s.get('uses', '')}\n{_commands(s.get('run', ''))}")
        ]

    # Named one at a time: `EXECUTES` also matches `docker/setup-buildx-action`, so asserting
    # only that `build` executes *something* would still hold with both real steps deleted.
    assert [s for s in build["steps"] if "docker/build-push-action@" in s.get("uses", "")]
    assert [s for s in build["steps"] if "docker run" in _commands(s.get("run", ""))]

    # The load-bearing half: nothing in the job that can push goes near the image.
    assert not executes(open_pr), "the job that pushes builds or runs the new version"

    # No `--build-arg` either: it would override the ARG the build job just wrote into the
    # Dockerfile, so the image would prove the argument rather than the file open-pr commits.
    for job in (build, open_pr):
        assert not [s for s in job["steps"] if "build-args" in (s.get("with") or {})]

    # open-pr keeps the credential it needs, which is the other half of the split: the point
    # is not that nobody persists one, it is that the job that does executes nothing.
    pushes = next(s for s in open_pr["steps"] if "actions/checkout@" in s.get("uses", ""))
    assert (pushes.get("with") or {}).get("persist-credentials") is not False

    # The artefact crossed a job boundary from the job that ran the unreviewed release, so
    # the job that commits re-checks what it carries before writing a branch. Each check is
    # named separately: they guard different things, and asserting the grep alone let the
    # artefact's own check be deleted while the reuse path's identical one kept the test green.
    opens = next(step for step in open_pr["steps"] if "gh api" in step.get("run", ""))
    # -F, so the version's dots are dots and not any-character.
    pin = 'grep -qxF "ARG CLAUDE_CODE_VERSION=${LATEST}"'
    assert f"{pin} /tmp/bump/Dockerfile" in opens["run"], "the artefact's pin is not re-checked"
    assert f"{pin} Dockerfile" in opens["run"], "a reused branch's pin is not re-checked"
    # ... and the copy may move that one line and nothing else.
    assert "git diff --numstat -- Dockerfile" in opens["run"]

    # Dropping the persisted credential also drops git's own access to the remote, and this
    # repository is private: an unauthenticated `git ls-remote` fails outright rather than
    # reporting an absent branch, and the `if` around it would read that failure as "no
    # branch" and lose the reuse path. So the lookup asks the API, which still has GH_TOKEN.
    # The invocation, not the word: the line that replaced it says why in a comment.
    check = next(step for step in build["steps"] if step.get("id") == "check")
    assert 'gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${branch}"' in check["run"]
    assert "git ls-remote" not in _commands(check["run"]), "asks git for a remote ref"
