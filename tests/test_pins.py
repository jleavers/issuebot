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
# them, matched against a step's `uses` and `run` together. A guard on what these two
# workflows actually contain, not a general one -- it knows nothing of `podman`, `nerdctl`,
# or a `run:` that shells out to a script that builds.
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


# `git` verbs that talk to a remote. A job whose checkout persists no credential can run
# none of them against this private repository, whatever it does locally with `git diff`.
REACHES_REMOTE = re.compile(r"\bgit\s+(?:ls-remote|fetch|pull|push|clone|remote\s+update)\b")


def test_the_bump_jobs_look_up_their_branch_through_the_api_not_git() -> None:
    """Neither half that executes unreviewed code can ask git about the remote (#138, #148).

    ``persist-credentials: false`` is what the split above rests on, and dropping the
    persisted credential drops git's own access to the remote with it. This repository is
    private, so an unauthenticated ``git ls-remote`` fails to authenticate rather than
    reporting an absent branch, and an ``if`` around it reads that failure as "the branch is
    not there" -- so ``reuse`` is false whatever is on the remote.

    That is not a lost optimisation. The reuse path is the recovery one: a branch with no
    pull request is the wreckage of a run that pushed and then failed before opening it, and
    reusing the branch is how the next run finishes the job. Without it the pushing half
    branches off the default branch instead, and its push is rejected as a non-fast-forward
    on every rerun until someone deletes the branch by hand.

    So the lookup asks the API. ``gh`` still holds ``GH_TOKEN`` in that step and reading a
    ref is ``contents: read``, which both jobs already have. ``matching-refs`` and not
    ``git/ref``: it answers an absent branch with 200 and an empty list, so absence is a
    *successful* reply and every non-zero exit is a real failure, with no parsing of gh's
    English error text to tell "no branch" from "could not ask". It is a prefix query, hence
    the exact-ref filter.

    The invocation and not the word, in both directions: each of these scripts explains in a
    comment what it deliberately does not do.
    """
    bumps = (("claude-code-version.yml", "build"), ("pre-commit-version.yml", "freeze"))
    for name, runner in bumps:
        config = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
        job = config["jobs"][runner]

        check = next(step for step in job["steps"] if step.get("id") == "check")
        commands = _commands(check["run"])
        assert "git/matching-refs/heads/${branch}" in commands, name
        assert 'select(.ref == \\"refs/heads/${branch}\\")' in commands, name
        # A lookup that fails fails the run: answering "no branch" to a 5xx or a rate limit
        # would have the pushing half create a branch that is already there.
        assert "::error::could not read refs/heads/${branch}" in commands, name
        # ... and that the answer is acted on. Asserting the question alone would hold with
        # the arm that sets `reuse` deleted, which is the state this issue found it in.
        assert '[ "${found}" != "0" ]' in commands, f"{name}: the lookup's answer is not used"
        assert "reuse=true" in commands, name

        # And nothing else in the job reaches the remote either, `git ls-remote` included.
        for step in job["steps"]:
            reaching = REACHES_REMOTE.search(_commands(step.get("run", "")))
            assert not reaching, f"{name}: {runner} reaches the remote: {reaching.group()}"


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
    # ... and the copy may move that one line and nothing else. A reused branch is held to
    # the same shape against the base, since `build` built the base plus the pin and the
    # pull request body says so.
    assert "git diff --numstat -- Dockerfile" in opens["run"]
    assert 'git diff --numstat "origin/${GITHUB_REF_NAME}" "${BRANCH}"' in opens["run"]

    # The branch name is derived from the validated version rather than carried over from
    # the job that ran the unreviewed release: it names what gets written to the repository.
    assert 'BRANCH="claude-code-${LATEST}"' in opens["run"]
    assert "BRANCH" not in (opens.get("env") or {}), "the branch name is taken on trust"


def test_the_pre_commit_bump_job_re_checks_the_branch_it_reuses() -> None:
    """A reused branch is whatever is on the remote, so it is held to what ``freeze`` proved.

    The reuse arm is the recovery path for a run that pushed and then failed before opening
    the pull request, and until #148 it could never run: the lookup that sets ``reuse`` asked
    git for a ref it had no credential for and always answered "no branch". So nothing had
    ever looked at what that arm checks out, and it is live now.

    What it checks out is not this job's work. The branch is on the remote, where anyone who
    can push here could have written it, and the pull request body this step goes on to write
    says the hooks were run over the whole tree at these digests. That claim is true of the
    config ``freeze`` froze and of no other, so the branch is reused only when it carries
    that file byte for byte and changes it alone -- the rule ``claude-code-version.yml``
    holds its own reused branch to (#138).
    """
    _, open_pr = _bump_split("pre-commit-version.yml", "freeze", "open-pr")
    opens = next(step for step in open_pr["steps"] if "gh api" in step.get("run", ""))
    commands = _commands(opens["run"])

    # The artefact's shape is re-checked in the job that pushes, ahead of both arms: it is
    # what the reuse arm compares against, so a tag smuggled into it would be a tag accepted
    # on the branch as well as one written to a fresh branch. Both halves of that check, since
    # the first reports only the lines that are *wrong* and says nothing about a config with
    # no `rev:` line at all.
    assert "grep -E '^ *rev:' /tmp/frozen/pre-commit-config.yaml" in commands
    assert (
        "grep -qE '^ *rev: [0-9a-f]{40}  # frozen: ' /tmp/frozen/pre-commit-config.yaml" in commands
    ), "a config pinning no hook at a digest passes the shape check"

    # Each check named separately: they guard different things, and one assertion over the
    # pair would let either be deleted while the other kept the test green.
    # Read out of the branch, since `cmp` follows a symlink and the artefact is a path on
    # this runner: a `.pre-commit-config.yaml` that links to it would compare equal to itself.
    assert 'git cat-file blob "${BRANCH}:.pre-commit-config.yaml"' in commands, (
        "a reused branch's config is read off the disk rather than out of the branch"
    )
    assert "cmp -s - /tmp/frozen/pre-commit-config.yaml" in commands, (
        "a reused branch's config is not compared with the one the hooks were run over"
    )
    # Measured from the merge base, which is what the pull request will show and what a
    # shallow checkout cannot compute for itself. A local `git diff <base tip> <branch>`
    # reads every commit merged since the branch was pushed as another path the branch
    # touches, so it refuses a week-old branch -- the ordinary case for a rerun -- for
    # somebody else's change, and demands by hand exactly the deletion the reuse path exists
    # to spare.
    assert "/compare/${GITHUB_REF_NAME}...${BRANCH}" in commands, (
        "a reused branch is not held to the shape of the change it claims to be"
    )
    assert "git diff --numstat" not in commands, "compares two tips rather than the change"
    # The question and what is done with the answer, since each can be deleted alone: a
    # comparison nothing reads, and one whose failure reads as "no files", both leave a
    # workflow that asks and then pushes anyway.
    assert '[ "${moved}" != ".pre-commit-config.yaml" ]' in commands, "the answer is not used"
    assert "::error::could not compare" in commands, "a failed comparison is not a failure"

    # And the artefact is the config the branch name was taken from -- the one link between
    # the name `freeze` committed to before it ran the hooks and the file that arrives here
    # after they have run.
    assert "git hash-object /tmp/frozen/pre-commit-config.yaml | cut -c1-12" in commands
    assert '[ "${BRANCH}" != "pre-commit-hooks-${hash}" ]' in commands, (
        "the artefact is not tied to the branch name it is pushed under"
    )

    # And the branch has to arrive under a name before any of that can look at it. An
    # explicit refspec, so what the checkout resolves is a ref this step wrote, rather than
    # whatever `remote.origin.fetch` the checkout left behind and git's opportunistic update
    # of a remote-tracking ref under it.
    assert "+refs/heads/${BRANCH}:refs/heads/${BRANCH}" in commands, "no explicit refspec"
