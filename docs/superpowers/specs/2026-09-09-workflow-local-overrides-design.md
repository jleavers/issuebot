# Local overrides for `configs/WORKFLOW.md`

Date: 2026-09-09
Status: approved, not implemented

## Problem

Cloning issuebot to watch a new repository means editing `configs/WORKFLOW.md` — at
minimum `github.repo`, in practice also the budget, the model, the concurrency and the
hooks. That file is tracked, so the edit is a permanent modification in every deployment
clone. Two consequences follow, and they pull against each other:

- `git status` is never clean, and the deployment's own configuration is one `git add -A`
  away from being committed to a branch of issuebot.
- `git pull` to take an issuebot enhancement conflicts, or at best merges, on the one file
  the operator has deliberately rewritten.

The second is the sharper one, because `configs/WORKFLOW.md` is not only configuration.
Its front matter is the operator's; everything after the front matter is the agent's prompt
template, which is issuebot's own work and which keeps improving. A deployment that forks
the whole file to get `github.repo` also stops receiving prompt improvements, silently and
for as long as nobody notices.

So the requirement is: **front matter is per-deployment and untracked; the prompt template
keeps arriving on `git pull` with no merge.**

## Decision

A gitignored `configs/WORKFLOW.local.md`, discovered as a sibling of the workflow file,
whose front matter is deep-merged over the tracked file's. The prompt body comes from the
tracked file unless the overlay supplies one.

A deployment's whole configuration then becomes:

```yaml
---
github:
  repo: acme/frontend
claude:
  max_budget_usd: 3.0
---
```

`git pull` updates the prompt template and every default with no merge, and `git status`
stays clean.

### Why not the alternatives

**A private full copy of the file** (`ISSUEBOT_WORKFLOW` pointed at an untracked file) was
rejected because it is the fork described above: correct on day one, and a prompt template
frozen at the day of the clone thereafter, with nothing to signal the drift.

**A permanent local commit rebased on every pull** was rejected because, although git
3-way-merges the body correctly and surfaces conflicts exactly where a human should look,
it puts every clone on a branch and makes `git pull --rebase` mandatory rather than
optional. It also leaves the operator's `github.repo` inside the repository's history.

**An `extends:` key in the overlay's own front matter**, making the overlay the entry point
that names its base, was rejected as generality nobody needs: there is exactly one base.
It also costs a reserved key that has to be stripped before `Settings` validation, and two
wiring steps downstream instead of one.

**Splitting the file** — `WORKFLOW.md` becomes prompt-only, configuration moves to
`configs/issuebot.yaml` plus `configs/issuebot.local.yaml` — is the cleanest separation of
the two concerns, and was rejected because it breaks the "one `WORKFLOW.md` is one worker"
story the README is built on, invalidates every existing deployment and every document, and
delivers nothing the overlay does not.

## Design

### 1. The overlay file and how it is found

The overlay path is derived from the workflow path, never configured separately:

```python
path.with_name(f"{path.stem}.local{path.suffix}")
```

So `configs/WORKFLOW.md` yields `configs/WORKFLOW.local.md`, and `--workflow frontend.md`
yields `frontend.local.md`. There is no chaining: pointing `--workflow` at an overlay would
look for `WORKFLOW.local.local.md`, which will not exist, and that is the whole of the
handling it gets.

Deriving rather than configuring is what makes the overlay a sibling **by construction**,
and that matters for more than tidiness. #46 established that a workflow file reached
through a single-file bind mount is pinned to an inode and goes stale on the first atomic
save, and the fix was to mount the directory `./configs` and name the file inside it. An
overlay that could be pointed anywhere would reintroduce exactly that failure for the file
holding the settings an operator changes most often. A sibling of a file inside a mounted
directory is reached through the same directory entry, so it live-reloads for the same
reason the base does.

`load_workflow` grows one keyword argument:

```python
def load_workflow(
    path: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
    overlay: bool = True,
) -> Workflow
```

`overlay=False` exists for one caller. `tests/test_workflow_default.py` loads this
repository's real `configs/WORKFLOW.md`, and `configs/` is precisely where a developer
working on issuebot would keep their own overlay; without the switch, one developer's local
settings would break the suite for them alone.

A missing overlay is the normal case and is not an error. An overlay path that exists but
is not a regular file raises a `ConfigError` naming it, rather than surfacing as a confusing
"workflow file unreadable" from a directory read.

### 2. Merge semantics

Three rules, applied to the two raw front-matter mappings:

1. **Two mappings merge**, key by key, recursively. Both sides must be mappings; a mapping
   in the overlay where the base holds a scalar or a list replaces it under rule 2, and so
   does the reverse.
2. **Everything else replaces** — scalars, and lists as a whole.
3. **An explicit `null` in the overlay deletes the key**, so the setting falls back to its
   `Settings` default. A `null` naming a key the base does not set is a no-op.

Rule 2 covers lists deliberately. `notifications.slack.events` and `claude.setting_sources`
are choices, not accumulations; appending would make it impossible for a deployment to
subscribe to fewer event kinds than the tracked file does.

Rule 3 is the one that is not obvious, and it is what makes the overlay complete rather than
merely additive. Without it there is no way for a deployment to say "I do not want the
shipped `after_create` unshallow hook", or "drop `claude.model: opus` and take Claude Code's
default". It costs one branch in the merge. The price is that a key cannot deliberately be
set *to* null, which no setting in `Settings` wants: every optional field expresses absence
by being absent.

Rule 1 has a useful consequence worth naming: `claude.model_labels` is a mapping, so a
deployment adds `issuebot/model/haiku` without restating the two shipped entries, and clears
one of them with `issuebot/model/fable: null` under rule 3.

### 3. Loading, resolution and validation

Merge the two **raw** front-matter mappings first, then `resolve_config` once, then
`Settings.model_validate` once.

Resolving each file separately and merging the results would be wrong, not merely wasteful.
`resolve_config` fills in fallbacks for *absent* fields — `GH_TOKEN` for `github.token`,
`ISSUEBOT_WORKSPACE_ROOT` and then `/workspaces` for `workspace.root`. An overlay that never
mentions `workspace` would therefore emerge from resolution carrying `/workspaces`, and that
manufactured value would then clobber whatever the base file set. Merging the raw mappings
avoids the problem entirely.

`resolve_config` takes `base_dir` = the **base** file's parent. Because §1 makes the overlay
a sibling, this is also the overlay's parent, so a relative `workspace.root` in either file
resolves against the same directory and the rule needs no exception.

Validating once, after the merge, means `extra="forbid"` catches a typo in the overlay
exactly as it catches one in the base — which preserves the README's promise that a typo
fails at `validate` rather than being silently ignored.

`SettingsValidationError` keeps `path` = the base file, so every existing consumer is
unaffected, but its rendered header names both files when an overlay is in play:

```
/configs/WORKFLOW.md (+ WORKFLOW.local.md): 1 invalid setting(s)
  claude.max_budget_usd: Input should be a valid number
```

Per-key provenance — tracking which file each leaf came from so the error can point at one —
is not worth building for two layers over a mapping a human can read in full.

**The prompt body** comes from the overlay when the overlay has one — meaning a body that is
still non-empty once `parse_workflow_text` has stripped it, so an overlay that is nothing but
front matter inherits — and from the base otherwise. This keeps "I need to tune the prompt for this particular repository" inside the
same mechanism instead of requiring a second one, and it is opt-in, so the default remains
that prompt improvements arrive on `git pull`.

### 4. Freshness and reload

#46 replaced the single mtime with an identity triple: `Workflow` carries `source_mtime_ns`,
`source_dev` and `source_ino`, exposes `source_identity`, and `_reload_workflow` reloads when
that triple moves. The overlay mirrors that shape rather than replacing it, because the
existing fields and their tests are newly landed and there is nothing wrong with them:

```python
overlay_path: Path | None = None
overlay_mtime_ns: int = 0
overlay_dev: int = 0
overlay_ino: int = 0


@property
def overlay_identity(self) -> tuple[int, int, int]: ...
```

The defaults keep every existing constructor compiling, including the hand-built `Workflow`
in `tests/test_agent_session.py`, and inherit #46's convention that all-zero means unknown
and never equals a real stat.

`_reload_workflow` stats the base as it does today, and additionally stats the overlay path.
A `FileNotFoundError` there is the normal "no overlay" answer; any other `OSError` is a
reload failure reported the way an unreadable base already is, since a file that exists and
cannot be stat'ed is not something to guess about. It reloads when **either** identity has
moved **or** the overlay's presence has changed. Creating an overlay on a running worker therefore takes effect on the next
tick, and so does deleting one — the same guarantee the base file already has.

`_pinned_mount_complaint` runs over both files rather than only the base, reporting the first
that complains and naming which file it is about. An overlay mounted individually — the
arrangement §1 makes impossible under Compose but which a Kubernetes `subPath` could still
produce — has precisely the disease #46 describes, and it holds the settings most likely to
be edited. The existing constraint carries over unchanged: the complaint is only ever
consulted on the branch where nothing has changed, so a wrong answer costs a log line and a
`config_error`, never a setting.

### 5. Deployment

**`compose.yaml` does not change.** `./configs` is mounted as a directory at `/configs`, so
`configs/WORKFLOW.local.md` is already inside the container, and `ISSUEBOT_WORKFLOW` already
names the base file that the overlay is derived from. The same is true of the Dockerfile's
`ENV ISSUEBOT_WORKFLOW=/configs/WORKFLOW.md`.

**`.gitignore`** gains `configs/WORKFLOW.local.md`, with a comment in the file's house style
explaining that the deployment's own settings are machine-local by nature, the same reasoning
already recorded there for `.claude/worktrees/` and `settings.local.json`.

**No example file.** An earlier draft of this design shipped a tracked
`WORKFLOW.local.md.example` and made `cp WORKFLOW.local.md.example WORKFLOW.local.md` a step
in the README, because a single-file bind mount cannot be conditional and the file had to
exist for Compose to start. The directory mount removed that constraint: the overlay is now
genuinely optional, a deployment that never creates one behaves exactly as it does today, and
a fenced block in the README is one fewer tracked file to keep in step with `Settings`.

**No migration.** The change is purely additive. Existing deployments keep working untouched,
and adopting the overlay is: create the file, move your edits into it, `git checkout
configs/WORKFLOW.md`.

### 6. Reporting

The overlay creates one new question — "is the worker actually running my overrides?" — and
nothing existing answers it. Two surfaces do:

- **`validate`**: the existing `workflow` check's detail becomes
  `/configs/WORKFLOW.md + WORKFLOW.local.md (3 overrides)`, where the count is the number of
  leaf keypaths the overlay sets (a null-delete counting as one). It stays one check; this is
  not a fourteenth.
- **`RuntimeSnapshot`** gains `workflow_overlay_path: str | None`, which reaches
  `issuebot status`, `/api/v1/state` and the dashboard's worker line through
  `views._WORKER_KEYS`. `to_dict` walks fields generically into the existing `jsonb` column,
  as `credential` and `rate_limits` established, so **no migration is required**.

### 7. Testing

New `tests/test_workflow_overlay.py`:

- each merge rule: nested mappings merge; scalars replace; a list replaces whole;
  `model_labels` merges key-by-key
- an explicit `null` deletes a key and the setting falls back to its default
- an unknown key in the overlay still fails `extra="forbid"`, and the error names both files
- `$VAR` in the overlay resolves, and a relative `workspace.root` in the overlay resolves
  against the shared directory
- discovery: found as a sibling; `overlay=False` ignores it; a missing overlay is not an
  error; an overlay that is not a regular file is a `ConfigError` naming it
- the prompt body is inherited when the overlay has none and replaced when it has one
- no chaining: an overlay does not itself get an overlay

Changed `tests/test_workflow_default.py`: loads with `overlay=False`, so a developer's own
`configs/WORKFLOW.local.md` cannot break the suite.

`tests/test_orchestrator.py`: reload fires when the overlay is created, edited and deleted,
and the mount complaint reports an overlay that is a mount point, naming it.

`tests/test_cli.py`: the `validate` workflow line with and without an overlay, and the
`status` workflow line.

Web: `workflow_overlay_path` reaches `/api/v1/state` and the worker line.

### 8. Documentation

- **README**: a short "Local overrides" block under "What is configured where" carrying the
  four-line example; the front-matter table's introduction pointing at the overlay as the
  place to make the required `github.repo` change; "Configuration changes" noting that the
  overlay reloads on the same terms as the base and must stay inside `configs/` for the same
  reason; "More than one repository" setting `github.repo` per checkout in the overlay rather
  than the tracked file; "Upgrades" gaining the sentence that a clean `git pull` is now the
  expected experience.
- **CLAUDE.md**: the `issuebot.config` bullet gains the overlay, its three merge rules and
  the `overlay=` switch.

## Out of scope

- Per-key provenance in validation errors (§3).
- More than two layers, or an overlay in a directory of its own.
- Publishing the image so a deployment needs no clone at all. That is the other answer to
  this problem and it remains open; this design deliberately assumes the clone-per-repository
  arrangement the README documents today.
