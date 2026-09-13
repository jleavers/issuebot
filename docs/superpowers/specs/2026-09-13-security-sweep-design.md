# A repeatable security sweep

Date: 2026-09-13
Status: approved, not implemented

## Problem

issuebot is about to be made public as a learning tool for other Claude users. That changes
what a security review is for, because it splits the question in two and only one half is
the familiar one.

The familiar half is **exposure**: what does this repository say about the deployment that
has been running it — tokens, DSNs, webhook URLs, a real repository's name — in the tree, in
the history, in the structlog output committed as fixtures, in `tests/fixtures/runs/`, which
is kept byte-for-byte from a real session against scratch issue #7 and excluded from
pre-commit for that reason.

The unfamiliar half is **inheritance**, and it is the sharper one. issuebot's entire job is
to run `claude -p` unattended, in auto mode, holding a `GH_TOKEN` and a shell, against a
working copy an anonymous GitHub issue can influence. A default that is merely *careless*
here is a *vulnerability* in every clone, and a reader learning from this repository will
copy the pattern before they understand the boundary it depends on. `--permission-prompts
none`, the `claude-home` volume that holds a login, `bash -lc` hooks whose text comes from a
configuration file, the `.issuebot/env` file that the agent itself can write and that
`PROTECTED_ENV_NAMES`/`PROTECTED_ENV_PREFIXES` exist to fence, the dashboard that has no
authentication at all: each of those is a deliberate decision with a stated rationale, and
each is one missing sentence in the README away from being a lesson in the wrong thing.

Neither half is answered by a diff-scoped review. The built-in `security-review` skill
reviews the pending changes on a branch; with respect to a reader who has never seen this
code, *every* line is pending.

Four operational failures have spoiled sweeps of this kind before, and the design has to
answer all four or the sweep is not repeatable, only performable:

- **Stale tree.** A sweep run against a local checkout re-finds what was fixed upstream.
  At the time this spec was written, local `main` was five commits behind `origin/main`.
- **Lost work.** A sweep is long and largely parallel. A terminal crash, a dropped VPN or a
  session limit two-thirds of the way through loses every finding if findings live only in
  the session.
- **Clobbered reports.** A fixed report path overwrites the previous run, which is the one
  artefact that would show whether a cluster is new or recurring.
- **Duplicate issues.** A sweep that does not check the tracker files what is already filed.

And one failure of *method*, which is the reason this is a pipeline and not a prompt:

- **The patching treadmill.** A sweep that emits one issue per finding produces a round of
  small, local patches. Those patches are themselves the next round's findings, because
  nothing in the loop ever names the property that was missing. Findings have to be
  clustered by root cause, and each cluster mapped to the structural invariant it touches,
  *before* anything is filed.

## Decision

A checked-in pair: a skill that owns everything deterministic or human-gated, and a workflow
script that owns the fan-out.

```
.claude/skills/security-sweep/SKILL.md    preflight, approval gate, filing, cleanup
.claude/workflows/security-sweep.js       recon -> scan -> verify -> triage -> dedupe
.claude/security-sweeps/<stamp>/          per-run artefacts, gitignored
```

The split is forced by the Workflow tool's own constraints, not chosen for tidiness. A
workflow script has **no filesystem access**; `Date.now()`, `new Date()` and `Math.random()`
**throw**, because they would break resume; and there is no way for a script to stop and ask
a human a question. So the run's timestamp, the git surgery, the report path and the "which
of these do we file" gate all have to live outside the script, and what is left inside it is
exactly the part that benefits from being a script: deterministic fan-out over a fixed set
of lenses.

`.gitignore` already argues, at length, that `.claude/` is ignored *scoped to the transient
parts* — `worktrees/` and `settings.local.json` — precisely so that `.claude/skills/` and
`.claude/settings.json` can be tracked as shared project configuration. A skill and a
workflow checked in there are the convention that file already states.
`.claude/security-sweeps/` joins `worktrees/` on the ignored side, for the same reason
and one more given in §12.

### Why not the alternatives

**A one-off sweep, nothing checked in**, was rejected because the sweep is not a single act.
The repository keeps changing, and the reason for sweeping it — that other people will copy
it — gets *stronger* over time, not weaker. A second sweep that cannot be compared with the
first also cannot tell a recurring cluster from a new one, which is the signal §7 exists to
produce. There is a secondary reason: a public repository whose `.claude/` directory
contains a real, working multi-agent sweep is itself the artefact it is teaching.

**The built-in `security-review` skill** was rejected as the wrong scope, not the wrong
tool. It reviews a branch's pending changes, which is the correct default for ongoing work
and useless for the question "what does this whole tree do to someone who clones it". It
stays the right thing to run on a PR.

**A Python harness under `src/` or `scripts/`**, shelling out to `claude -p`, was the most
tempting rejection, because it would be testable with pytest like everything else here and
would run on both the Windows and Linux hosts AGENTS.md cares about. It was rejected because
it would reimplement fan-out, concurrency limits, retries, structured output validation and
progress reporting — all of which the Workflow tool already has — to gain properties this
particular tool does not need. It is worth revisiting only if the sweep ever has to run in
CI without a human, and that is explicitly out of scope (below).

**Module fan-out** — one agent per package (`agent/`, `web/`, `db/`, `github/`,
`orchestrator/`, Docker and CI) — was rejected because it guarantees file coverage by
destroying exactly the structure §7 needs. A cross-cutting weakness such as "untrusted text
reaches a shell" arrives as five unrelated module findings, and the triage agent's first job
becomes reassembling what the topology broke. Its one real advantage, that no file goes
unread, is recovered far more cheaply by the completeness critic in §8.

**Loop-until-dry** — re-spawning finders until two consecutive rounds surface nothing new —
was rejected as the wrong trade for this repository. On 11.5k lines it converges by finding
progressively weaker things, which inflates the false-positive rate in the one place this
design is least tolerant of it, and its cost is unbounded by construction.

**Three refuters per finding with a majority vote** was rejected on cost alone; it is the
right escalation for a finding that matters, so §6 applies it to the critical and high ones
and says so in the log rather than silently.

**Filing issues automatically** was rejected because it puts the treadmill on rails. A
cluster that survives verification can still be wrong about the invariant, and an issue is
public, notifies, and is harder to withdraw than to never file. The dedupe verdict is
computed by the workflow; the decision is not.

**Tracked reports under `docs/`** would be the better learning exhibit, and were rejected
because a report names weaknesses that are not fixed yet. Committing it publishes them on
the day the repository goes public, ahead of any fix, which is the one outcome this whole
exercise exists to avoid. The public record is the issues that get filed, which are written
for that audience deliberately.

## Design

### 1. Artefacts, and which of them are tracked

| Path | Tracked | What |
|---|---|---|
| `.claude/skills/security-sweep/SKILL.md` | yes | The entry point. Phases 0, 6 and 7. |
| `.claude/workflows/security-sweep.js` | yes | Phases 1 to 5. |
| `.claude/security-sweeps/<stamp>/` | **no** | One directory per run (§12). |
| `.claude/worktrees/security-sweep-<stamp>/` | **no** | Already ignored. |

The only `.gitignore` change is one line, `.claude/security-sweeps/`; `.claude/worktrees/`
has been there since the comment quoted above was written.

`<stamp>` is UTC, `YYYYMMDDTHHMMSSZ` — sortable, filename-safe on both host families, and
unambiguous about the timezone, which a local stamp would not be on a repository used from
two hosts.

### 2. Phase 0: preflight, in the skill

The skill runs, in order:

1. `git fetch origin`.
2. Report whether local `main` is behind `origin/main`. **Informational, not a gate** — see
   below.
3. `git worktree add --detach .claude/worktrees/security-sweep-<stamp> origin/main`.
4. Record `git rev-parse HEAD` in that worktree as the swept SHA.
5. Create `.claude/security-sweeps/<stamp>/` and write `run.json`:
   `{stamp, sha, worktree, run_dir, phases: {}}`.

Cutting the worktree from `origin/main` rather than from local `main` is the whole of the
staleness answer, and it is a stronger one than "fast-forward first, then sweep". It makes
the local checkout's state *irrelevant* to what is audited: whatever the operator has
checked out, in whatever state, the sweep reads the tree as `origin/main` holds it. That is
also the tree a reader will clone, which is the question being asked. Step 2 survives only
because an operator who is seven commits behind should be told so; it does not block,
because there is nothing left for it to protect.

`--detach` is deliberate. A branch would be a second thing to clean up and a name that could
collide across runs; a detached worktree at a known SHA is removable with one command and
re-creatable with the same one.

**Host portability.** AGENTS.md requires Bash on Linux and PowerShell on Windows, and forbids
generating the wrong one. SKILL.md therefore states each preflight step as its intent and
gives the Bash form, with the PowerShell chaining noted inline, rather than shipping a `.sh`
that is wrong on half the hosts this repository is used from. The steps are four git
commands and a `mkdir`; there is nothing in them that needs a script file.

### 3. What the skill passes to the workflow

```js
args = {
  stamp, sha, repo,          // "jleavers/issuebot"
  worktree,                  // absolute; where the agents read code
  runDir,                    // absolute; where the agents write findings
  escalationCap: 3,
}
```

Every one of these exists because the script cannot compute it: no clock, no filesystem, no
shell. `escalationCap` is a parameter rather than a constant so that a run that wants the
stronger verification in §6 can have it without editing the script.

### 4. Phase 1: recon (1 agent)

One agent reads the worktree and writes `01-surface-map.md`, then returns it. It is asked
for four things:

- **Entry points** — every place data enters the process: the GitHub adapter's reads, issue
  and comment bodies, the workflow file and its overlay, `.issuebot/env`, hook command
  strings, the web routes, the refresh NOTIFY payload, `import --from URL`, environment.
- **Trust boundaries** — where the data's provenance changes, named explicitly. The
  `PROTECTED_ENV_*` fence is one; `GhRunner` as "the only subprocess boundary" is another.
- **Secrets inventory** — every name that holds a credential and every place one is
  formatted for output.
- **File inventory** — path and line count for everything in the tree, so §8 can tell what
  was never opened.

The scanners all read this one map rather than each deriving their own. That is partly cost,
but mostly vocabulary: four agents that independently name the same boundary four different
ways produce findings that §7 cannot cluster.

### 5. Phase 2: scan (4 agents, one per threat model)

Each scanner reads `01-surface-map.md`, hunts only in its lane, **writes
`02-findings-<dim>.json` itself before returning**, and returns the same object. The four
lanes, with the surface each is pointed at in this repository:

**`copycat` — what a reader inherits.** The going-public risk proper. `--permission-prompts
none` and what bounds it; the `claude-home` volume holding a login; `bash -lc` hooks taking
their text from configuration; `workspace_environment` and the `.issuebot/env` layering;
`.env.example`'s values and what they imply; published ports and bind hosts in
`compose.yaml`; the Dockerfile's uid, its pinned `CLAUDE_CODE_VERSION` and the opt-in
Postgres and Node toolchains; and — weighted equally with the code — README and CLAUDE.md
passages that teach a pattern by example without stating the boundary that makes it safe.

**`secrets` — what becomes public when the repository does.** `git log -p` and `git
rev-list --objects` over the full history for tokens, DSNs, webhook URLs and the real
repository name; whether `.env` was ever committed before it was ignored; `connection.py`'s
`describe`/`redact` and every call site that formats a URL; structlog output and what
reaches it; `tests/fixtures/runs/`, kept byte-for-byte from a real session; the turn logs
under `.issuebot/runs/` and what `turnlog.capture_turns` copies into Postgres.

**`hostile-issue` — attacker-authored text reaching an unattended agent.** The sharp lane.
An issue body flows through Jinja into a prompt given to a `claude -p` holding `GH_TOKEN`
and a shell: prompt injection and what, if anything, bounds its blast radius; the
`.issuebot/env` trust boundary and whether `PROTECTED_ENV_NAMES`/`PROTECTED_ENV_PREFIXES`
actually close it; `WorkspaceManager`'s sanitised keys and containment; argument injection
into `gh` through issue-derived values; whether `GhRunner` is in fact the only subprocess
boundary; what a hostile branch name or PR title can do downstream.

**`services` — the dashboard, the API and the database.** The web app has no authentication
at all; what is exposed by that, given the pages render issue bodies, transcripts and costs.
`POST /refresh` and its throttle. The CSP, autoescape and `safe_href`. SQL construction in
`queries.py` and `store.py`, including how `repo` and `state` reach a predicate. `import
--from URL` accepting an arbitrary DSN. The `issuebot_refresh` payload handling in
`listen.py`. Uvicorn's bind default and what `--bind` allows.

Finding schema (§13) requires `file`, `line`, `severity`, `claim`, `why_it_matters`,
`evidence` and `attack_path`. `attack_path` is mandatory prose, not a flag: a scanner that
cannot write down how an attacker reaches the code has not found a vulnerability, and making
it a required field is the cheapest available filter.

### 6. Phase 3: verify (4 agents, plus up to `escalationCap` more)

Findings are verified **per dimension, pipelined** — `pipeline()`, not `parallel()` — so the
`secrets` findings are being refuted while `services` is still scanning. There is no
cross-item dependency at this stage, so a barrier would buy nothing and cost the difference
between the fastest and slowest scanner.

Each refuter is a fresh agent that never saw the scan. Its brief is adversarial and its
default is hostile: **refuted unless a concrete, reachable path can be demonstrated in this
code**. It is told explicitly that a finding which is true of the language, the framework or
security in general, but which it cannot reach in this tree, is refuted. It writes
`03-verdicts-<dim>.json` before returning.

Findings the refuter confirms at `critical` or `high` get one further, independent refuter,
each writing `03-escalated-<finding id>.json`, capped at `escalationCap` (3). Escalation
is ordered by severity then by dimension, and
**whatever the cap drops is written to the log with `log()`** — a silent cap reads as full
coverage when it is not.

Agent budget, worst case: 1 recon + 4 scan + 4 verify + 3 escalation + 1 triage + 1 critic
+ 1 dedupe = 15.

### 7. Phase 4: triage — the clustering contract

One agent, and the reason the whole pipeline exists. It reads every `03-verdicts-*.json`,
takes only the confirmed findings, and writes `04-clusters.md`.

It is **forbidden from emitting one cluster per finding.** Every cluster carries four
things, and a cluster missing any of them is not a cluster:

- **Root cause** — the single decision, or the single absence, that produced every finding
  in it. Not a category; a cause.
- **Invariant touched** — the property which, enforced in one place, makes every finding in
  the cluster impossible. This is the anti-treadmill field. If the agent cannot state the
  invariant in one sentence, the cluster is either several clusters or nothing, and it is
  told to say which.
- **Blast radius** — which threat model it lands in, and who is hurt: this deployment, or a
  reader who cloned the repository. Those have different urgencies and the report should not
  blur them.
- **Fix shape** — *where the invariant would live*, not a patch. No diffs. A diff in a
  security report is an invitation to apply it, and applying six diffs is the treadmill.

A confirmed finding that genuinely resists clustering goes to a **singletons** section and
is explicitly *not* proposed as an issue unless its severity is `critical`. That is the
escape hatch for the one isolated bug, deliberately made narrow so it cannot become the
default path back to per-finding issues.

### 8. Phase 4, concurrently: the completeness critic (1 agent)

Runs in parallel with triage, because it needs the findings and not the clusters. It reads
`01-surface-map.md` and every `02-findings-*.json` and answers one question: **what was
never looked at?** Files in the inventory that no finding cites and no scanner's lane
covers; entry points in the map that no `attack_path` mentions; a threat model whose brief
does not reach some surface that exists. It writes `04-gaps.md`.

This is where module fan-out's one advantage is recovered, at the price of one agent instead
of a topology.

### 9. Phase 5: dedupe and report (1 agent)

For each cluster, search **open and closed** issues and open PRs — closed matters more here,
since the three open issues today are all the CI billing block — and return one of `new`,
`duplicate-of-#N` or `related-to-#N` with the reasoning, written to `05-dedupe.json`.
Then write the report.

`<runDir>/report-<stamp>.md`: the swept SHA, the four lanes and what each returned, the
clusters in severity order with their four fields and dedupe verdict, the singletons, the
coverage gaps from §8, and the counts at every stage — found, refuted, confirmed, clustered
— so the shape of the funnel is visible. The stamp is in the filename as well as the
directory: the directory already prevents clobbering, but a report that is mailed, pasted or
copied somewhere else should still say when it was made.

The workflow returns a compact object — clusters, verdicts, counts, report path — for the
skill to present. It does not return the report's prose; that is on disk.

### 10. Phase 6: approval and filing, in the skill

The skill presents the clusters ranked by severity, each with its invariant, blast radius
and dedupe verdict, and asks which to file. Nothing is filed without that answer.

Filing uses `gh api` with the body written to a temp `.md` file in a **separate** call,
per the user's global instruction and the `gh-pr-edit-guard.sh` PreToolUse hook, which
blocks title and body flags on `gh issue create` as well as on `gh pr`. A filed issue gets
the cluster's title, its body built from the four fields, and a link back to nothing — the
run directory is local and stays local, so the issue has to stand alone.

The issue numbers are written back into `06-filed.json`, which is what makes a *later* sweep
able to say "this cluster is the one filed as #N and not yet fixed" rather than re-finding it
as new.

### 11. Phase 7: cleanup

`git worktree remove .claude/worktrees/security-sweep-<stamp>`. Never `rm -rf`, which
AGENTS.md forbids outright and which would leave git's worktree metadata behind. If the
remove fails because the worktree is dirty — it should not be, nothing writes there — the
skill reports it and leaves it, rather than reaching for `--force`.

The run directory is **kept**. It is the previous-run comparison §7 depends on, and it costs
nothing.

### 12. Crash resistance and resume

Three tiers, weakest to strongest, and the reason the artefacts are files rather than return
values:

- **Session alive, script edited or run killed** — `Workflow({scriptPath, resumeFromRunId})`.
  The longest unchanged prefix of `agent()` calls returns from cache; the first changed call
  and everything after it runs live. This is the cheap case and it only works within the
  session.
- **Session gone, run directory intact** — the crash the operator actually fears. Every
  agent writes its artefact **itself, before returning**, so a fresh session reads `run.json`
  and whatever `0N-*` artefacts exist and restarts from the first missing phase. This is
  why the run directory lives in the main checkout and not in the worktree: cleanup, or a
  crash mid-cleanup, must not take the findings with it.
- **Worktree gone as well** — `run.json` records the SHA, so the worktree is re-cuttable at
  exactly the tree that was swept, and a partial run remains comparable with its own
  findings rather than with a moved target.

`Date.now()` and `new Date()` are unavailable inside the script precisely because they would
break the first tier; the stamp arriving through `args` (§3) is what keeps a resumed run
writing to the same directory as the run it resumes.

### 13. Schemas

Three JSON Schemas in the script, passed as `agent(..., {schema})` so validation happens at
the tool-call layer and the model retries on a mismatch rather than the script parsing
prose.

`FINDINGS`: `{findings: [{id, dimension, file, line, severity, claim, why_it_matters,
evidence, attack_path}]}`, severity one of `critical|high|medium|low`.

`VERDICTS`: `{verdicts: [{id, refuted, confidence, reasoning, corrected_severity}]}`.
`corrected_severity` exists so a refuter can downgrade rather than face a binary it will
resolve by keeping things.

`CLUSTERS`: `{clusters: [{title, root_cause, invariant, blast_radius, fix_shape, severity,
finding_ids, dimensions}], singletons: [{finding_id, why_unclustered}]}`. `finding_ids` is
what lets the report show the funnel, and `why_unclustered` is what stops the singletons
section becoming a dumping ground.

### 14. Testing

There is no unit test for a skill or a prompt, and pretending otherwise would be worse than
saying so. What is verified, and how:

- **Preflight and cleanup are dry-run before the skill is committed** — the worktree add,
  the SHA read and the worktree remove, executed once by hand against a throwaway stamp.
  `git worktree remove` failing is the one step that can leave mess behind, so it is proved
  rather than assumed.
- **The first real sweep is the shakedown.** The workflow's own output is the evidence that
  the briefs are right, and a mediocre report is the failure signal. This is accepted
  deliberately rather than mitigated with more design.
- **The script's structure is checked before it is run**: `meta` a pure literal with one
  `phases` entry per `phase()` call, schemas with `required` a subset of `properties`, no
  `Date.now()`, no TypeScript annotations. All four are hard failures at invocation, and all
  four are cheap to read for.
- **The completeness critic is the standing check on coverage** — every run reports what it
  did not look at, and that output is the seed for the next run's briefs.

### 15. Documentation

- **README**: nothing. The sweep is a maintenance tool for this repository, not part of what
  a deployment runs, and the README is already long.
- **CLAUDE.md**: two lines under a new "Security sweeps" heading — the command, and where the
  run artefacts land — because an agent working in this repository should know the sweep
  exists and should not go looking for reports in `docs/`.

## Out of scope

- **Running in CI.** A sweep with no human at the approval gate is either auto-filing, which
  the Decision above rejects, or a report nobody reads. If this is ever wanted, the Python
  harness rejected above becomes the right shape again and this design should be revisited, not
  extended.
- **Fixing anything.** The sweep files issues. Fixes go through the normal branch-and-PR
  route, which is what AGENTS.md requires and what issuebot itself would do with the issues.
- **Dependency and supply-chain scanning as a lane of its own.** Dependabot already covers
  uv, Docker and Actions weekly, and `claude-code-version.yml` covers the one pin Dependabot
  cannot see. The `copycat` lane reads the Dockerfile and the workflows for what they *teach*;
  it does not duplicate a CVE feed.
- **Comparing a run against its predecessor automatically.** §10 writes `06-filed.json` so
  that a later run *can* be told about the previous one, and the telling is manual for now.
  Automating the comparison needs two runs to exist first.
- **Secret rotation.** If the `secrets` lane finds a live credential in history, rotating it
  and rewriting history are both human actions with consequences well outside this tool.
