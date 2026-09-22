# The reused clone is not reset between two sessions on one issue

Date: 2026-09-22
Status: decided -- documented, not changed
Issue: #180 (related to #171, #164, #151, #137, #121, #107, #104, #101, #75)

## Problem

A workspace outlives its run. An issue in `review` keeps its clone for days, and a retry, a
rework or a re-queue reuses it: `create_or_reuse` returns the existing directory whenever
`_is_complete` holds, which is what keeps a continuation from re-cloning a repository from
cold. The clone inside that directory is the *session's* to write (#75) -- the worker owns the
workspace directory and `.issuebot` inside it, and nothing else.

So the clone's own `.git/config` is a channel from one session to the next session on that
issue, in the same shape `.issuebot/env` was before #171 and for the same reason: same account,
same uid, same workspace, and a file that names commands. #171 closed the environment-variable
route and named this as its residual -- the wider blast radius went, and git *run in the clone*
was unchanged.

Measured on `main`@a336a69, git 2.47.3, end to end through issuebot's own seam: a
`WorkspaceManager` creates a workspace, "session 1" writes the clone's config and a file beside
it, and `create_or_reuse` is asked a second time, as a retry or a rework would ask it.

```text
session 1 workspace: created=True
[debug] workspace_reused workspace=/tmp/repro-.../workspaces/example-180
session 2 workspace: created=False same_path=True
  $ git -C <workspace> st                       -> PLANTED-ALIAS-RAN
  $ git -C <workspace> status --porcelain       -> PLANTED-FSMONITOR-RAN
  $ git -C <workspace> commit --allow-empty     -> PLANTED-HOOKSPATH-RAN
```

`core.pager` and `credential.helper` fire the same way (`core.pager` under a pty, which a hook
may well have; `credential.helper` on any authenticated fetch, and directly under
`git credential fill`), and `.git/hooks/post-checkout` is the same shape beside the file
rather than in it.

**Reach.** A workspace key is the issue's identifier, so one workspace belongs to one issue. It
is bound to one session account and opened to that account's group only while a session is
working in it, sealed `0700` back to the worker between runs (#121, #75), and `_is_complete`
re-clones rather than reuses when the binding moved. Issuebot itself runs no git in the clone
after the post-clone setup: the only other git-shaped thing the worker spawns is `gh repo
clone`, in the workspace *root*, before that clone exists. So this is persistence inside the
session's own privilege domain and reaches the next session on this issue, as #101, #137 and
#171 did, not an escalation across one -- #75 closed that and nothing here re-opens it.

## Decision

**Not bounded. The clone is the session's, and reuse hands it over whole.**

Neither `.git/config` nor `.git/hooks/` is reset between two sessions on one issue, and
issuebot ships no setting that resets them. This is recorded here, stated in the README where a
deployment sees a workspace outlive its run, and pinned by a test, so that a later change to
bound it is a deliberate edit rather than a silent one.

### Why: the unit of this channel is the clone, and `.git/config` is a small part of it

The premise of the other half of the argument -- "reset the named keys instead of re-cloning"
-- is that the file can be separated from the directory it is in. It cannot, and the same
measurement says so twice.

**Within `.git/`, a reset of named keys is an enumeration that the file itself defeats.** The
keys that name a command are already several (`core.pager`, `core.editor`, `core.sshCommand`,
`core.fsmonitor`, `core.hooksPath`, `credential.helper`, every `alias.*`, `filter.*.clean`,
`diff.*.textconv`, `uploadpack.packObjectsHook`, `protocol.*.command`) and git adds to them. But
the decisive one is that `include.path` puts the whole set somewhere a reset over `.git/config`
does not look:

```text
$ printf '[alias]\n\tst = "!printf INCLUDED-ALIAS-RAN\\n"\n' > .git/planted-include
$ git config --local include.path planted-include
$ git st
INCLUDED-ALIAS-RAN
```

So a reset must also unset `include.*` -- and then `core.hooksPath`, measured above, points at a
directory of scripts, and `.git/hooks/` is a directory of scripts with no config key at all.
Every one of those lives under `.git/`, which the session owns. #171 argued this class of thing
from evidence rather than taste: two drafts of its list were enumerations and self-review found
each of them incomplete. There the answer was a prefix, because the names had a shared head. Here
there is no prefix short of "the whole of `.git/`", and the whole of `.git/` is the clone.

**Around `.git/`, the working tree is a wider channel of the same shape that no config reset
touches.** Reuse keeps the entire workspace, not just the repository: the tracked tree as the
last session left it, its untracked files, and `<workspace>/.venv`, which the shipped
`after_create` hook builds with `uv sync` and which does not run again on the reuse path
(`test_reuse_skips_clone_and_hooks` pins that). The next session's ordinary work is to run that
repository's tests out of that venv. Resetting `core.pager` while `.venv/bin/pytest` and the
`conftest.py` beside it come through untouched is closing a window in an open wall; the
reproduction above shows it directly, since the `.planted-hooks/` directory the first session
wrote is an ordinary untracked directory that survived reuse on its own account.

The only thing that actually closes the channel is not reusing the workspace -- which is exactly
what `_is_complete` exists to avoid, and what `agent.max_issue_cost_usd` partly pays for. A
continuation that re-cloned from cold would re-fetch the repository, re-run `after_create` and
re-derive what the last session had already established, on every rework bounce and every retry.
That is a real, recurring cost against a channel that crosses no privilege boundary.

### Why: the plant buys its author nothing they did not already have

A session that can write the clone's `.git/config` is a session with the repository checked out,
`GH_TOKEN` in its environment, and a branch and a pull request of its own. Everything a planted
`core.pager` could make the *next* session do -- push a commit, alter a file on the way into the
object store with a clean filter, run a command at that uid -- the session holding the plant can
do directly, now, with the same credential, on the same issue and the same pull request. The
channel buys deferral and nothing else, and the deferral is to the next session on *that same
issue*: the same account, the same token, the same branch, reviewed by the same human reading
the same pull request.

That is the gradient test #101 and #137 were decided on, and it is why those two were closed and
this one is not. The session account's *home* was worth sweeping because the surfaces there are
loaded by `claude` and by every login shell for the container's lifetime, reaching sessions
working **other issues** -- a hostile issue's plant waiting for an honest one next week. This
does not: a workspace is one issue's, and the issue whose session wrote the plant is the issue
whose session reads it.

### Why: the file is one issuebot already depends on

`POST_CLONE_SCRIPT` writes `credential.https://github.com.helper` with `git config --local`, and
the README tells a hook author with a deploy key to write `core.sshCommand` the same way from
`after_create` (#171's "what a hook that needs one should do instead"). Both run on *creation*,
so a per-run reset would have to preserve what they wrote while removing what a session wrote,
in a file that records no difference between the two. It would also have to preserve
`remote.origin.*` and the `branch.<name>.merge` tracking that `git push -u` wrote, which is the
state a continuation needs. A reset that keeps all of that is one more enumeration, and this
time with the deployment's own configuration on the other side of it.

### What a deployment that wants it reset can do

`hooks.before_run` is the per-run seam the issue asks after, and it is already there: it runs on
every run, inside the workspace, at the session account's uid, before turn 1 and before anything
the session itself does. A deployment that wants the clone's config narrowed each run can say so
there --

```yaml
hooks:
  before_run: |
    git config --local --remove-section alias 2>/dev/null || true
    for k in core.pager core.editor core.sshCommand core.fsmonitor core.hooksPath include.path; do
      git config --local --unset-all "$k" 2>/dev/null || true
    done
```

-- with both caveats stated plainly: it is the enumeration this note declines to ship, so it is
as complete as whoever wrote it, and the hook's own `git` invocations run under whatever the last
session left (`git config` itself takes no pager for a write, which is what makes the recipe work
at all). It is not shipped in `configs/WORKFLOW.md`, because shipping it would be bounding the
channel by default, which is the decision this note declines to make.

The blunt instrument is the workspace directory itself: remove it and the next session re-clones
(`create_or_reuse` finds nothing complete and creates), and issuebot removes it on its own when
the issue reaches `issuebot/complete`.

## What is still bounded, and must not be read as widened

This decision is about one directory and changes nothing else. All of the following still hold,
and the README says so where each is described:

- The worker's own state in the workspace -- `.issuebot/session.json`, `.issuebot/runs/`, the
  `created` and `finished` markers -- is the worker's, in a sticky directory, and read back only
  through `Boundary` (#75, #104, #149).
- `.issuebot/env` cannot re-point `claude`, `git`, `gh` or the hook shell for the next session
  (#109, #171, #179).
- The session account's home is swept before every turn and every login shell (#101, #137,
  #151), so nothing reaches a session working a *different* issue through `~/.claude`,
  `~/.profile`, `~/.gitconfig` or `~/.ssh/config`.
- The clone's `CLAUDE.md` and `AGENTS.md` reach the prompt as `<github-text>` data and never as
  `claude` configuration (#107), and `--strict-mcp-config` holds whatever the clone carries
  (#119).
- The session runs at its own uid, on its own account under a pool, behind the egress proxy
  (#75, #121, #126).

## Residuals

- **`.git/hooks/` and the rest of `.git/`** are decided with the file, and for the same reason:
  they are the clone. `.git/config.worktree` (behind `extensions.worktreeConfig`) and
  `.git/modules/*/config` for a submodule are further spellings of it; neither was reproduced
  here, and neither changes the answer, since a reset that reached them would still be an
  enumeration inside a directory the session owns.
- **`<workspace>/.venv` and the working tree** are the wider channel this note rests its
  argument on, and they are equally not reset. The per-account uv cache beside them is #164's
  recorded residual and is a different shape (one account, many workspaces) rather than this
  one.
- **The narrow window in which another session could read this workspace at all** is #121's and
  #164's: an account holds one open workspace at a time, except while the worker runs
  `before_remove` at that uid for another, idle one. Nothing gives such a session a reason to
  run git in this clone, and the seal is what keeps it out of an idle workspace otherwise.
- **If the answer ever changes**, the change is not a reset -- it is not reusing the workspace,
  either always or under a setting, and that is a decision about what a continuation may cost
  rather than about this file. `test_reuse_keeps_the_clones_own_git_config` is the test that
  would have to be edited to make it, which is the point of pinning a decision not to act.

## Tests

`tests/test_agent_workspace.py::test_reuse_keeps_the_clones_own_git_config` creates a workspace,
writes a `--local` key and a `.git/hooks/` script into the clone as a session would, asks
`create_or_reuse` again, and asserts both are still there and that git reads the key back -- the
decided behaviour, stated as a test so that bounding the channel later fails it and forces the
edit here as well. It names this note and the issue, as the sweep lists do.
