# A repeatable security sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A checked-in `/security-sweep` that audits `origin/main` in a throwaway worktree with twelve to fifteen agents, clusters what survives refutation by root cause, names the invariant each cluster touches, checks the tracker for duplicates, and files only the clusters a human approves.

**Architecture:** A skill owns everything the Workflow tool cannot do — the timestamp, the git surgery, the report path and the human gate — and a workflow script owns the fan-out. `.claude/skills/security-sweep/SKILL.md` fetches, cuts a detached worktree from `origin/main`, creates the run directory and invokes `.claude/workflows/security-sweep.js` with `{stamp, sha, repo, worktree, runDir, escalationCap}`. The script runs recon → four threat-model lanes → an independent refuter per lane → a capped second refuter for critical and high → triage and completeness critic in parallel → dedupe and report. Every agent writes its own artefact to the run directory before returning, so a dropped session loses a phase rather than a run. The skill then presents the clusters, files the approved ones with `gh api`, and removes the worktree.

**Tech Stack:** Claude Code Workflow tool (plain JavaScript ES modules, no filesystem, no clock), Claude Code project skills, `git worktree`, `gh` CLI, Node 24 for syntax checking only. No Python, no new dependencies, no change to `pyproject.toml` or `uv.lock`.

**Spec:** `docs/superpowers/specs/2026-09-13-security-sweep-design.md`.

**Pre-verified:** the git sequence in Task 1 was run for real on this host against stamp `20260913T000000Z` before this plan was written, and the outputs recorded below are that run's. `git worktree add --detach .claude/worktrees/security-sweep-<stamp> origin/main` reported `Preparing worktree (detached HEAD 28e271c)`; `git -C <wt> rev-parse HEAD` and `git rev-parse origin/main` both gave `28e271ccaac41469b01741e6d0db16d730679539`; `git worktree remove <wt>` removed both the directory and its `.git/worktrees/` metadata, leaving only the three pre-existing stale worktrees; and the run directory, being outside the worktree, survived with its contents intact. Task 2's syntax check was also pre-verified, and it found a trap recorded in Global Constraints. Treat a different result as a finding, not as noise. The spec's prose beats this plan's code when they disagree; report the disagreement rather than resolving it silently.

## Global Constraints

Every task's requirements include this section. Every implementer and reviewer dispatch must carry it verbatim.

- Work on branch `issuebot/security-sweep`; the spec and this plan are its first two commits. **Never push to `main`**, never merge or close PRs, never `rm -rf`, `git reset --hard` or `git clean -fd` (AGENTS.md). Linux host: Bash, `&&` chaining, `.sh` not `.ps1`.
- **No Python changes.** Nothing under `src/` or `tests/` is touched, `pyproject.toml` and `uv.lock` do not change, and no new dependency is added. `uv run pytest` is run once at the end only to prove that, not because this work has unit tests.
- **Syntax-checking the workflow script needs two workarounds, both verified on this host.** First, `node --check` on a `.js` file containing `export` *silently passes broken code* — Node 24's CommonJS/ESM detection swallows the error and exits 0, so a file containing only `export const a = {` exits 0 under `node --check file.js` and 1 under `node --input-type=module --check < file.js`. Always use the stdin form. Second, the Workflow runtime wraps the script body in an async function, so a correct script uses top-level `return` and top-level `await`, which plain ESM rejects with `SyntaxError: Illegal return statement`. So the check must wrap the body itself and demote the `export`. The exact command is in Task 2 Step 2; it also catches TypeScript annotations, which the Workflow tool rejects.
- **A Bash-level hook on this host blocks any shell command whose text contains the dot-env filename** (the literal `.` + `env`, including `.example`, heredoc bodies and quoted anchors). `.gitignore` contains such lines, so its edit in Task 1 must be made with the **Edit tool**, never with `sed`, `python - <<EOF` or a heredoc. Say "dot-env" in commit messages and reports.
- **A PreToolUse hook (`~/.claude/hooks/gh-pr-edit-guard.sh`) blocks title and body flags on `gh pr create`, `gh pr edit` and `gh issue create`.** Use `gh api` instead, write the body to a temp `.md` file in a **separate Bash call** from the `gh api` call (the hook aborts the whole call, so a chained heredoc never runs), and pass it with **capital** `-F body=@file.md` — lowercase `-f` posts the literal string. Any file whose own content mentions those flags must be written with the **Write tool**, not a heredoc, or the hook fires on the command text. This plan is such a file.
- `.pre-commit-config.yaml` runs `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-added-large-files` and ruff. `check-yaml` will parse the skill's YAML front matter. Run `uv run pre-commit run --files <paths>` before every commit and `uv run pre-commit run --all-files` before pushing.
- Commit messages: conventional prefix plus the attribution trailer the harness requires as the last lines, with a blank line before them.
- **Artefact paths are fixed by the spec and are load-bearing.** `<runDir>` is `.claude/security-sweeps/<stamp>/`; the files are `run.json`, `01-surface-map.md`, `02-findings-<lane>.json`, `03-verdicts-<lane>.json`, `03-escalated-<id>.json`, `04-clusters.json`, `04-gaps.md`, `05-dedupe.json`, `report-<stamp>.md`, `06-filed.json`. A resume reads these names; renaming one breaks recovery silently. Triage writes **only** the JSON: the shakedown had it write a Markdown twin fifteen seconds earlier and then refine its clustering, leaving the human-readable file a whole cluster short.
- `<stamp>` is UTC, `YYYYMMDDTHHMMSSZ`, produced by `date -u +%Y%m%dT%H%M%SZ`. Never a local timestamp: the repository is used from two hosts.
- **The four lanes are `copycat`, `secrets`, `hostile-issue`, `services`**, spelled exactly that way in the script, the prompts, the artefact filenames and the report. The completeness critic's `suggested_lane` is one of those four or `new`.
- Severities are exactly `critical`, `high`, `medium`, `low`, in that rank order.
- Agent budget: 1 recon + 4 scan + 4 verify + up to 3 escalation + 1 triage + 1 critic + 1 report = **15 maximum**. This host has 8 CPUs, so the Workflow tool's concurrency cap is `min(16, 8-2) = 6`; peak concurrency in this design is 4, so nothing queues.
- **No `model` or `effort` overrides on any `agent()` call.** Agents inherit the session model, which is what the operator chose for the sweep. Adding a cheaper tier to a scanner is a change to the sweep's quality, not an optimisation.

## Decisions the spec left open

1. **Two schemas the spec did not name.** §13 names `FINDINGS`, `VERDICTS` and `CLUSTERS`. §8's critic and §9's dedupe also return structured data, so this plan adds `GAPS` and `DEDUPE`. Elaboration, not contradiction.
2. **A finding with no verdict is dropped.** If a refuter returns verdicts for three of four findings, the fourth is treated as refuted, not as confirmed, and the count is logged. Silence is not confirmation, and this is the direction that fails safe.
3. **Triage writes two files.** `04-clusters.json` for resume and `04-clusters.md` for a human. The spec names only the Markdown; the JSON is what makes the phase resumable.
4. **A lane that returns zero findings skips its refuter.** Saves an agent and cannot lose anything.
5. **The escalation refuter never sees the first refuter's reasoning.** Independence is the whole point of a second vote.
6. **Model and effort are not overridden** — see Global Constraints.
7. **The sweep audits `origin/main` while the tooling runs from the branch.** The worktree is cut from `origin/main`, which will not contain the skill or the workflow until the PR merges; the script itself is resolved from the session's checkout, which does. Deliberate: the sweep audits the tree a reader would clone, and the report's SHA records which tree that was.

## File map

| File | Action | Responsibility |
|---|---|---|
| `.gitignore` | modify | One stanza ignoring `.claude/security-sweeps/`. |
| `.claude/workflows/security-sweep.js` | create | The fan-out: schemas, lane briefs, prompts, pipeline. Nothing else. |
| `.claude/skills/security-sweep/SKILL.md` | create | Preflight, invocation, approval gate, filing, cleanup, resume. |
| `CLAUDE.md` | modify | A short "Security sweeps" section: the command and where artefacts land. |

---

### Task 1: The ignore rule, and the git sequence proved rather than assumed

Spec §14's first bullet requires the worktree add, the SHA read and the worktree remove to be executed by hand before the skill that depends on them is committed. This task does that and adds the one ignore line the run directory needs.

**Files:**
- Modify: `.gitignore` (append one stanza at the end)

**Interfaces:**
- Consumes: nothing.
- Produces: the verified command sequence that Task 3 writes into `SKILL.md`, and the guarantee that `.claude/security-sweeps/` never appears in `git status`.

- [ ] **Step 1: Confirm the ignore rule is actually needed**

An empty directory is invisible to git, which masks the problem. Create a file so the test is real:

```bash
mkdir -p .claude/security-sweeps/probe && echo '{}' > .claude/security-sweeps/probe/run.json
git status --porcelain | head -3
```

Expected: a line showing `.claude/` (collapsed, because `.claude/` holds no tracked files yet) or `.claude/security-sweeps/`. Either proves the rule is needed.

- [ ] **Step 2: Add the stanza with the Edit tool**

**Use the Edit tool, not `sed` or a heredoc** — `.gitignore` contains dot-env lines and the Bash hook fires on the command text (Global Constraints).

Append at the end of `.gitignore`:

```
# Security-sweep run artefacts: one directory per run, holding the surface map, the raw
# per-lane findings, the refuters' verdicts, the clusters and the report. Machine-local and
# per-run by nature, like `.claude/worktrees/` above, and untracked for a second reason that
# matters more: a report names weaknesses that are not fixed yet, and committing it would
# publish them on the day this repository goes public, ahead of any fix. The public record is
# the issues the sweep's approval gate files, which are written for that audience on purpose.
.claude/security-sweeps/
```

- [ ] **Step 3: Verify the rule matches**

```bash
git check-ignore -v .claude/security-sweeps/probe/run.json
git status --porcelain | wc -l
```

Expected: `check-ignore` prints the `.gitignore` line number and pattern; `git status --porcelain` prints `1` — the in-progress `.gitignore` edit itself, and nothing else.

- [ ] **Step 4: Remove the probe**

No `rm -rf` (AGENTS.md):

```bash
rm .claude/security-sweeps/probe/run.json && rmdir .claude/security-sweeps/probe .claude/security-sweeps
```

- [ ] **Step 5: Prove the preflight sequence end to end**

Run it exactly as Task 3 will write it, against a throwaway stamp:

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WT=.claude/worktrees/security-sweep-$STAMP
RD=.claude/security-sweeps/$STAMP
git fetch origin
git rev-list --count main..origin/main
git worktree add --detach "$WT" origin/main
git -C "$WT" rev-parse HEAD
git rev-parse origin/main
mkdir -p "$RD" && echo '{"probe":true}' > "$RD/run.json"
git status --porcelain | wc -l
```

Expected: the worktree is prepared at a detached HEAD; the two `rev-parse` outputs are **identical** (this is the whole staleness guarantee — the worktree is the tree a reader would clone, whatever the local checkout is doing); `git status --porcelain` still prints only the `.gitignore` edit, proving the new worktree and the populated run directory are both ignored.

- [ ] **Step 6: Prove cleanup leaves nothing behind**

```bash
git worktree remove "$WT"
git worktree list
ls .git/worktrees/
cat "$RD/run.json"
```

Expected: `git worktree list` no longer shows `security-sweep-$STAMP`; `.git/worktrees/` no longer holds an entry for it (only the three pre-existing stale ones); and `run.json` still exists, proving the run directory survives cleanup because it lives outside the worktree. **This last one is the point of the whole task** — if the run directory were inside the worktree, cleanup would destroy the findings.

- [ ] **Step 7: Remove the probe run directory**

```bash
rm "$RD/run.json" && rmdir "$RD" .claude/security-sweeps
git status --porcelain
```

Expected: ` M .gitignore` alone.

- [ ] **Step 8: Commit**

```bash
uv run pre-commit run --files .gitignore
git add .gitignore
git commit -m "chore: ignore security-sweep run artefacts"
```

The commit message needs the attribution trailer as its last lines.

---

### Task 2: The workflow script

**Files:**
- Create: `.claude/workflows/security-sweep.js`

**Interfaces:**
- Consumes: `args = {stamp, sha, repo, worktree, runDir, escalationCap}`, supplied by the skill in Task 3.
- Produces: a return value of shape `{stamp, sha, repo, counts, clusters, singletons, gaps, dedupe, report_path, run_dir}`. Task 3's approval gate reads `clusters` (each with `title`, `root_cause`, `invariant`, `blast_radius`, `fix_shape`, `severity`, `finding_ids`, `dimensions`), `singletons` (each with `finding_id`, `why_unclustered`, `severity`), `dedupe` (each with `cluster_title`, `status`, `issue_numbers`, `reasoning`) and `report_path`.
- Produces on disk: every artefact named in Global Constraints except `run.json` and `06-filed.json`, which are the skill's.

- [ ] **Step 1: Write the file**

Create `.claude/workflows/security-sweep.js` with exactly this content:

```javascript
export const meta = {
  name: 'security-sweep',
  description: 'Sweep the tree for security bugs, cluster them by root cause, check the tracker',
  phases: [
    { title: 'Recon', detail: 'one agent maps entry points, trust boundaries and secrets' },
    { title: 'Scan', detail: 'four threat-model lanes hunt in parallel' },
    { title: 'Verify', detail: 'an independent refuter per lane, hostile by default' },
    { title: 'Escalate', detail: 'a second refuter for confirmed critical and high findings' },
    { title: 'Triage', detail: 'cluster by root cause and name the invariant; find coverage gaps' },
    { title: 'Report', detail: 'dedupe against the tracker and write the report' },
  ],
}

if (!args || !args.runDir) {
  throw new Error('security-sweep needs args: {stamp, sha, repo, worktree, runDir, escalationCap}')
}
const { stamp, sha, repo, worktree, runDir, escalationCap } = args

// --- schemas ---------------------------------------------------------------------------

const SEVERITIES = ['critical', 'high', 'medium', 'low']
const severityRank = (s) => {
  const i = SEVERITIES.indexOf(s)
  return i === -1 ? SEVERITIES.length : i
}
const str = { type: 'string' }
const sev = { type: 'string', enum: SEVERITIES }

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: str,
          dimension: str,
          file: str,
          line: { type: 'integer' },
          severity: sev,
          claim: str,
          why_it_matters: str,
          evidence: str,
          attack_path: str,
        },
        required: [
          'id', 'dimension', 'file', 'line', 'severity',
          'claim', 'why_it_matters', 'evidence', 'attack_path',
        ],
      },
    },
  },
  required: ['findings'],
}

const VERDICTS = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: str,
          refuted: { type: 'boolean' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          reasoning: str,
          corrected_severity: sev,
        },
        required: ['id', 'refuted', 'confidence', 'reasoning', 'corrected_severity'],
      },
    },
  },
  required: ['verdicts'],
}

const CLUSTERS = {
  type: 'object',
  properties: {
    clusters: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: str,
          root_cause: str,
          invariant: str,
          blast_radius: str,
          fix_shape: str,
          severity: sev,
          finding_ids: { type: 'array', items: str },
          dimensions: { type: 'array', items: str },
        },
        required: [
          'title', 'root_cause', 'invariant', 'blast_radius',
          'fix_shape', 'severity', 'finding_ids', 'dimensions',
        ],
      },
    },
    singletons: {
      type: 'array',
      items: {
        type: 'object',
        properties: { finding_id: str, why_unclustered: str, severity: sev },
        required: ['finding_id', 'why_unclustered', 'severity'],
      },
    },
  },
  required: ['clusters', 'singletons'],
}

const GAPS = {
  type: 'object',
  properties: {
    gaps: {
      type: 'array',
      items: {
        type: 'object',
        properties: { surface: str, why_it_matters: str, suggested_lane: str },
        required: ['surface', 'why_it_matters', 'suggested_lane'],
      },
    },
  },
  required: ['gaps'],
}

const DEDUPE = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          cluster_title: str,
          status: { type: 'string', enum: ['new', 'duplicate', 'related'] },
          issue_numbers: { type: 'array', items: { type: 'integer' } },
          reasoning: str,
        },
        required: ['cluster_title', 'status', 'issue_numbers', 'reasoning'],
      },
    },
    report_path: str,
  },
  required: ['verdicts', 'report_path'],
}

// --- shared prompt fragments -----------------------------------------------------------

const WHERE = `You are auditing the issuebot repository at commit ${sha}, checked out read-only at:

    ${worktree}

Report every path repository-relative (\`src/issuebot/web/app.py\`), never absolute. Change
nothing in the worktree. You already have this repository's CLAUDE.md; use it for the package
layout rather than rediscovering it.`

const writeBack = (name) => `

**Write this file before any other file you write, and before you return:**

    ${runDir}/${name}

Write exactly the object you are returning, pretty-printed. That file is this run's
crash-resistance record: if the session dies, the sweep resumes from what is on disk. A task
that also asks you for prose writes this JSON first and the prose second -- so that the two can
never disagree about what you concluded, and so that a crash between them costs the prose,
which can be regenerated, rather than the data, which cannot.`

// --- phase 1: recon --------------------------------------------------------------------

const reconPrompt = `${WHERE}

You are the recon pass for a security sweep. You find no vulnerabilities yourself; you build
the map four scanners will share, so that they name the same boundary the same way and their
findings can be clustered afterwards. Write Markdown to ${runDir}/01-surface-map.md and return
its full text.

Cover exactly four things, under these four headings:

## Entry points
Every place data enters the process, with file and line: GitHub reads, issue and comment
bodies, the workflow file and its overlay, \`.issuebot/env\`, hook command strings, every web
route, the \`issuebot_refresh\` NOTIFY payload, \`import --from URL\`, and the environment. For
each, say who can influence it: anyone on the internet, a repository collaborator, or the
operator alone.

## Trust boundaries
Every place the provenance of data changes, named and located. \`PROTECTED_ENV_NAMES\` and
\`PROTECTED_ENV_PREFIXES\` are one. \`GhRunner\` as the only subprocess boundary is another.
For each, say what it asserts and what would breach it.

## Secrets
Every name that holds a credential, every place one is read, and every place one is formatted
for output or storage.

## File inventory
A table of every file in the tree with its line count, so a later pass can tell what was never
opened. Include the non-Python files: Dockerfile, compose.yaml, .github/workflows/*, configs/*,
README.md, CLAUDE.md, AGENTS.md, the dot-env example, .gitignore.

Be exhaustive and terse. This is a map, not an essay.`

// --- phase 2: the four lanes -----------------------------------------------------------

const LANES = [
  {
    key: 'copycat',
    title: 'what a reader inherits by copying this repository',
    brief: `issuebot is about to be published as a learning tool, and readers will copy its
patterns before they understand the boundaries those patterns depend on. Your question is not
"is this safe for the author" but "what does a reader inherit, and what happens when they
apply it somewhere the author's assumptions do not hold".

Look at, at minimum: \`--permission-prompts none\` and whatever bounds it; the \`claude-home\`
volume that holds a login; \`bash -lc\` hooks taking their command text from a configuration
file; \`workspace_environment\` and the \`.issuebot/env\` layering; every value in the dot-env
example and what it implies; published ports and bind hosts in \`compose.yaml\`; the
Dockerfile's uid, its pinned \`CLAUDE_CODE_VERSION\` and the opt-in Postgres and Node
toolchains; and the GitHub Actions workflows' permissions and any interpolation of
attacker-controllable text into a run step.

Weight prose equally with code. A README or CLAUDE.md passage that teaches a pattern by example
without stating the boundary that makes it safe is a finding in this lane, and its \`file\` and
\`line\` are the passage.`,
  },
  {
    key: 'secrets',
    title: 'what becomes public the moment the repository does',
    brief: `The repository is about to be made public. Your question is what that publishes.

Search the full history, not only the tree: \`git log -p\`, \`git log --all --full-history\`
and \`git rev-list --objects --all\` for tokens, DSNs, Slack webhook URLs, API keys and the
real repository name. Check specifically whether the dot-env file was ever committed before it
was ignored.

Then the redaction paths: \`src/issuebot/db/connection.py\`'s \`describe\` and \`redact\`, and
every call site that formats a URL for a log, an error or the database. Then what reaches
structlog. Then \`tests/fixtures/runs/\`, which is kept byte-for-byte from a real session
against scratch issue #7 and is excluded from pre-commit for that reason — read it rather than
assuming it is synthetic. Then what \`turnlog.capture_turns\` copies into Postgres and what the
dashboard renders from it.

A credential that is real and live is \`critical\` however well redacted at one call site.`,
  },
  {
    key: 'hostile-issue',
    title: 'attacker-authored text reaching an unattended agent',
    brief: `This is the sharpest lane. issuebot takes a GitHub issue body — which on a public
repository anyone may write — renders it through Jinja into a prompt, and hands it to a
\`claude -p\` running unattended with \`GH_TOKEN\` in its environment and a shell at its
disposal.

Trace that path end to end and ask what an attacker can make it do. Cover: prompt injection and
what, if anything, bounds its blast radius once the agent acts; the \`.issuebot/env\` trust
boundary, which the agent itself can write — do \`PROTECTED_ENV_NAMES\` and
\`PROTECTED_ENV_PREFIXES\` actually close it, or only the cases the author thought of;
\`WorkspaceManager\`'s sanitised keys and containment, including path traversal; argument
injection into \`gh\` through issue-derived values; whether \`GhRunner\` is in fact the only
place a process is spawned; and what a hostile branch name, PR title or comment body reaches
downstream.

For this lane in particular, \`attack_path\` must name the specific attacker-controlled field
and the specific line where it lands.`,
  },
  {
    key: 'services',
    title: 'the dashboard, the API and the database',
    brief: `The web dashboard has no authentication of any kind. Start there: enumerate what an
unauthenticated visitor can read and do, given the pages render issue bodies, agent
transcripts, costs and the worker's configuration.

Then: \`POST /refresh\` and its throttle; the CSP, Jinja autoescape and \`safe_href\`; SQL
construction in \`src/issuebot/db/queries.py\` and \`store.py\`, including how \`repo\` and
\`state\` reach a predicate and whether any identifier is interpolated rather than bound;
\`import --from URL\` accepting an arbitrary DSN and what connecting to an attacker's server
costs; the \`issuebot_refresh\` payload handling in \`listen.py\`; uvicorn's bind default and
what \`--bind\` allows; and the four security headers — whether they are genuinely on every
response, error responses included.`,
  },
]

const scanPrompt = (lane) => `${WHERE}

Read ${runDir}/01-surface-map.md before anything else. It is the shared map: use its names for
entry points and boundaries, so that findings from four lanes can be clustered afterwards.

Your lane is **${lane.key}** — ${lane.title}. Hunt only here. Another agent owns each of the
other lanes; a finding outside yours is their job, not a bonus.

${lane.brief}

Rules that decide whether something is a finding at all:

- \`attack_path\` is required prose: who the attacker is, what they control, and the sequence
  by which they reach the line you are citing. If you cannot write it, you have not found a
  vulnerability — drop it.
- A claim that is true of Python, FastAPI, Docker or security in general, but which you cannot
  reach in THIS tree, is not a finding. Reaching it means citing the file and the line.
- \`evidence\` quotes the code or command output you are relying on, not a paraphrase of it.
- Severity: \`critical\` if it is exploitable now with real consequence; \`high\` if it is
  exploitable given a plausible precondition; \`medium\` if it weakens a defence without being
  exploitable on its own; \`low\` otherwise.
- Give each finding an \`id\` of \`${lane.key}-1\`, \`${lane.key}-2\`, and so on, and set
  \`dimension\` to \`${lane.key}\`.

Finding nothing is an acceptable result and much better than padding. Return an empty
\`findings\` array rather than a weak one.${writeBack(`02-findings-${lane.key}.json`)}`

// --- phase 3: refutation ---------------------------------------------------------------

const verifyPrompt = (lane, findings) => `${WHERE}

You are an independent refuter. You did not write these findings, you have no stake in them,
and your job is to destroy the ones that do not survive contact with the code.

**Your default is refuted.** A finding survives only if you can follow its attack path in this
tree yourself and reach the same conclusion. If you are uncertain, it is refuted. If it is true
in general but you cannot reach it here, it is refuted. If its evidence does not say what the
finding claims it says, it is refuted.

Read the code. Do not reason from the finding's own text — it is a claim, not a source.

For each finding return a verdict carrying the same \`id\`:

- \`refuted\`: true or false.
- \`confidence\`: how sure you are of the verdict itself.
- \`reasoning\`: what you checked and what you concluded. For a refutation, say what is
  actually true instead.
- \`corrected_severity\`: the severity you would give it. You may downgrade a surviving finding
  rather than face a binary you would resolve by keeping it. Echo the original if you agree.

Findings to refute (lane ${lane.key}):

${JSON.stringify(findings, null, 2)}${writeBack(`03-verdicts-${lane.key}.json`)}`

const escalatePrompt = (finding) => `${WHERE}

One finding has already survived a refuter and is rated ${finding.severity}. Before it reaches
a human it gets a second, independent attempt at refutation, and you are it. You have not been
shown the first refuter's reasoning, deliberately.

**Your default is refuted**, on the same terms: follow the attack path in the code yourself, or
refute it. A finding this severe that turns out to be wrong is more expensive than one that is
missed, because it is the one that gets acted on.

Return a single verdict, inside the \`verdicts\` array, carrying this finding's \`id\`.

${JSON.stringify(finding, null, 2)}${writeBack(`03-escalated-${finding.id}.json`)}`

// --- phase 4: triage and the completeness critic ---------------------------------------

const triagePrompt = (survivors) => `${WHERE}

You are the triage pass, and you are the reason this sweep exists. Below are the findings that
survived refutation. Your job is to stop them becoming a list of small patches.

A sweep that emits one issue per finding produces a round of local fixes, and those fixes are
the next sweep's findings, because nothing in the loop ever names the property that was
missing. So: **cluster by root cause, and name the invariant.**

You are forbidden from emitting one cluster per finding. Every cluster carries four things, and
a cluster missing any of them is not a cluster:

- \`root_cause\`: the single decision, or the single absence, that produced every finding in
  it. Not a category ("input validation") — a cause ("issue-derived text is interpolated into
  argument lists at each call site, with no single point that escapes it").
- \`invariant\`: the property which, enforced in one place, would make every finding in the
  cluster impossible. One sentence. This is the field that matters most. If you cannot state
  it, the cluster is either several clusters or it is nothing — decide which, and act on it.
- \`blast_radius\`: which threat model it lands in, and who is hurt — this deployment, or a
  reader who cloned the repository. Those have different urgencies and must not be blurred.
- \`fix_shape\`: WHERE the invariant would live. No diffs, no patches, no code. A diff in a
  security report is an invitation to apply it, and applying six diffs is the treadmill this
  field exists to prevent.

Set \`severity\` to the highest severity among the cluster's findings, \`finding_ids\` to every
id in it, and \`dimensions\` to the lanes they came from — a cluster spanning two lanes is
usually the most valuable kind.

A finding that genuinely resists clustering goes in \`singletons\`, with \`why_unclustered\`
saying what makes it isolated. Use this sparingly: it is the escape hatch for the one truly
standalone bug, and if most findings end up there you have not done the work.

Findings that survived refutation:

${JSON.stringify(survivors, null, 2)}

Return the JSON object and write nothing else. Do not also write a Markdown version: the report
pass renders the prose from exactly what you return, so a second representation written here
could only drift from it.${writeBack('04-clusters.json')}`

const criticPrompt = (allFindings) => `${WHERE}

You are the completeness critic. Four scanners have finished. Your only question is: **what was
never looked at?**

Read ${runDir}/01-surface-map.md, and the raw findings below. Then find the holes:

- Files in the map's inventory that no finding cites and that no lane's brief plainly covers.
  Weight by what a file does, not by its size: a fifty-line file that spawns a process matters
  more than a thousand-line template.
- Entry points in the map that no \`attack_path\` mentions.
- Surfaces that exist in the map but fall between the four lanes, so that nobody owned them.

For each gap give \`surface\` (the file, route or boundary), \`why_it_matters\` (what could be
there), and \`suggested_lane\` — one of \`copycat\`, \`secrets\`, \`hostile-issue\`,
\`services\`, or \`new\` if it needs a fifth lane.

Do not audit the gaps yourself; naming them is the whole job. Your output seeds the next
sweep's briefs.

Findings produced this run:

${JSON.stringify(allFindings, null, 2)}

Write your gaps as Markdown to ${runDir}/04-gaps.md and return the JSON object.`

// --- phase 5: dedupe and report --------------------------------------------------------

const reportPrompt = (clusters, singletons, gaps, counts) => `${WHERE}

Two jobs, in order.

**First, dedupe.** For every cluster below, search the tracker of \`${repo}\` before it can be
proposed as new:

    gh issue list --repo ${repo} --state all --limit 200 --json number,title,state,labels,body
    gh pr list --repo ${repo} --state open --limit 100 --json number,title,body

Closed issues matter more than open ones here: what you are looking for is something already
reported and fixed, or reported and forgotten. Match on the invariant, not on wording — a
cluster is a duplicate when an existing issue would be closed by the same fix. Return, per
cluster, a \`status\` of \`new\`, \`duplicate\` or \`related\`, the \`issue_numbers\` you
matched (empty for \`new\`), and \`reasoning\`.

**Second, write the report** to:

    ${runDir}/report-${stamp}.md

and set \`report_path\` to that path. A human reads this to decide what to file, so lead with
what they must decide. In this order:

1. A header: the swept commit \`${sha}\`, the stamp \`${stamp}\`, and the repository.
2. The funnel as a table — findings per lane, refuted, confirmed, escalated, clustered. The
   numbers are ${JSON.stringify(counts)}.
3. The clusters in severity order, numbered 1..N in the order you present them. Every reference
   to a cluster anywhere else in the report -- in the summary at the top especially -- uses that
   same number. A summary that says "file cluster 2" while section 2 is a different cluster is
   worse than no summary.
4. The singletons, with why each is unclustered, and a note that only \`critical\` singletons
   are proposed for filing.
5. The coverage gaps, verbatim from the critic — this is what the next sweep starts from.

Clusters:
${JSON.stringify(clusters, null, 2)}

Singletons:
${JSON.stringify(singletons, null, 2)}

Coverage gaps:
${JSON.stringify(gaps, null, 2)}${writeBack('05-dedupe.json')}`

// --- the pipeline ----------------------------------------------------------------------

log(`sweeping ${repo} at ${sha}`)
log(`worktree ${worktree}`)
log(`artefacts ${runDir}`)

phase('Recon')
const surfaceMap = await agent(reconPrompt, { label: 'recon', phase: 'Recon' })
if (!surfaceMap) {
  throw new Error('recon produced no surface map; every later phase reads it')
}

const lanes = await pipeline(
  LANES,
  (lane) => agent(scanPrompt(lane), {
    label: `scan:${lane.key}`, phase: 'Scan', schema: FINDINGS,
  }),
  (scan, lane) => {
    const findings = scan && scan.findings ? scan.findings : []
    if (!findings.length) return { lane, findings, verdicts: [] }
    return agent(verifyPrompt(lane, findings), {
      label: `verify:${lane.key}`, phase: 'Verify', schema: VERDICTS,
    }).then((v) => ({ lane, findings, verdicts: v && v.verdicts ? v.verdicts : [] }))
  },
)

const ok = lanes.filter(Boolean)
const dead = LANES.filter((l) => !ok.some((r) => r.lane.key === l.key))
if (dead.length) log(`lanes that produced nothing usable: ${dead.map((l) => l.key).join(', ')}`)

const allFindings = ok.flatMap((r) => r.findings)
const verdictFor = new Map()
for (const r of ok) {
  for (const v of r.verdicts) verdictFor.set(v.id, v)
}

const unjudged = allFindings.filter((f) => !verdictFor.has(f.id))
if (unjudged.length) {
  log(`${unjudged.length} finding(s) got no verdict and are dropped as unconfirmed: ${unjudged.map((f) => f.id).join(', ')}`)
}

const confirmed = allFindings
  .filter((f) => {
    const v = verdictFor.get(f.id)
    return v ? !v.refuted : false
  })
  .map((f) => {
    const v = verdictFor.get(f.id)
    return { ...f, severity: v.corrected_severity || f.severity, verified_by: v.reasoning }
  })

log(`${allFindings.length} found, ${confirmed.length} survived refutation`)

phase('Escalate')
const candidates = confirmed
  .filter((f) => f.severity === 'critical' || f.severity === 'high')
  .sort((a, b) => severityRank(a.severity) - severityRank(b.severity) || a.id.localeCompare(b.id))
const taken = candidates.slice(0, escalationCap)
const skipped = candidates.slice(escalationCap)
if (skipped.length) {
  log(`escalation cap ${escalationCap}: ${taken.length} of ${candidates.length} critical/high finding(s) get a second refuter; ${skipped.length} do not (${skipped.map((f) => f.id).join(', ')})`)
}

const second = taken.length
  ? await parallel(taken.map((f) => () => agent(escalatePrompt(f), {
    label: `escalate:${f.id}`, phase: 'Escalate', schema: VERDICTS,
  })))
  : []

const killed = new Set()
for (const r of second.filter(Boolean)) {
  for (const v of r.verdicts || []) {
    if (v.refuted) killed.add(v.id)
  }
}
if (killed.size) log(`second refuter killed: ${[...killed].join(', ')}`)
const survivors = confirmed.filter((f) => !killed.has(f.id))

phase('Triage')
const [clusterResult, gapResult] = await parallel([
  () => agent(triagePrompt(survivors), { label: 'triage', phase: 'Triage', schema: CLUSTERS }),
  () => agent(criticPrompt(allFindings), { label: 'critic', phase: 'Triage', schema: GAPS }),
])

const clusters = clusterResult && clusterResult.clusters ? clusterResult.clusters : []
const singletons = clusterResult && clusterResult.singletons ? clusterResult.singletons : []
const gaps = gapResult && gapResult.gaps ? gapResult.gaps : []

const counts = {
  by_lane: Object.fromEntries(ok.map((r) => [r.lane.key, r.findings.length])),
  found: allFindings.length,
  refuted: allFindings.length - confirmed.length,
  confirmed: confirmed.length,
  escalated: taken.length,
  killed_on_escalation: killed.size,
  survivors: survivors.length,
  clusters: clusters.length,
  singletons: singletons.length,
}

phase('Report')
const dedupe = (clusters.length || singletons.length)
  ? await agent(reportPrompt(clusters, singletons, gaps, counts), {
    label: 'dedupe-report', phase: 'Report', schema: DEDUPE,
  })
  : null
if (!dedupe) log('nothing survived to report; the run directory still holds every raw finding')

return {
  stamp,
  sha,
  repo,
  counts,
  clusters,
  singletons,
  gaps,
  dedupe: dedupe && dedupe.verdicts ? dedupe.verdicts : [],
  report_path: dedupe && dedupe.report_path ? dedupe.report_path : null,
  run_dir: runDir,
}
```

- [ ] **Step 2: Syntax-check it as an ES module**

Two traps at once (Global Constraints): `node --check <file>.js` exits 0 on broken code, and a
bare ESM check rejects the top-level `return` that the Workflow runtime requires. Wrap the body
in an async function and demote the `export`:

```bash
{ echo 'async function _check() {'
  sed 's/^export const meta/const meta/' .claude/workflows/security-sweep.js
  echo '}'
} | node --input-type=module --check && echo "ESM syntax OK"
```

Expected: `ESM syntax OK`. Checking it any other way either passes everything or fails a
correct script.

- [ ] **Step 3: Prove the check would have failed**

A test that cannot fail is not a test. Confirm the command actually catches breakage:

```bash
{ echo 'async function _check() {'
  sed 's/^export const meta/const meta/' .claude/workflows/security-sweep.js
  echo 'const broken = {'
  echo '}'
} | node --input-type=module --check >/dev/null 2>&1; echo "rc=$?"
```

Expected: `rc=1`. Substituting `const x: string = "a"` for the unclosed brace must also give
`rc=1`, which is the TypeScript-annotation guard.

- [ ] **Step 4: Check the four hard-failure conditions the Workflow tool imposes**

```bash
f=.claude/workflows/security-sweep.js
grep -nE 'Date\.now|new Date\(|Math\.random' "$f" && echo "FAIL: forbidden clock or randomness" || echo "OK: no clock, no randomness"
grep -cE '^\s*(const|let)\s+\w+\s*:' "$f"
grep -oE "phase\('[A-Za-z]+'\)" "$f" | sort -u
grep -oE "phase: '[A-Za-z]+'" "$f" | sort -u
grep -oE "title: '[A-Za-z]+'" "$f" | sort -u
```

Expected: `OK: no clock, no randomness`; `0` TypeScript-style annotations; and the union of the `phase('X')` and `phase: 'X'` values exactly equal to the `title:` values — `Escalate`, `Recon`, `Report`, `Scan`, `Triage`, `Verify`. A title with no phase is a dead group; a phase with no title gets its own ungrouped box.

- [ ] **Step 5: Check every schema's `required` is a subset of its `properties`**

An unsatisfiable schema throws at `agent()`, three phases into a run. Catch it now:

```bash
node --input-type=module -e '
import fs from "node:fs"
const src = fs.readFileSync(".claude/workflows/security-sweep.js", "utf8")
let bad = 0
const walk = (o, path) => {
  if (!o || typeof o !== "object") return
  if (Array.isArray(o.required) && o.properties) {
    for (const k of o.required) {
      if (!(k in o.properties)) { console.log(`FAIL ${path}: required "${k}" not in properties`); bad++ }
    }
  }
  for (const [k, v] of Object.entries(o)) walk(v, `${path}.${k}`)
}
for (const name of ["FINDINGS", "VERDICTS", "CLUSTERS", "GAPS", "DEDUPE"]) {
  const m = src.match(new RegExp(`const ${name} = (\\{[\\s\\S]*?\\n\\})\\n`))
  if (!m) { console.log(`FAIL: ${name} not found`); bad++; continue }
  walk(eval(`(${m[1].replace(/\bstr\b/g, `{type:"string"}`).replace(/\bsev\b/g, `{type:"string"}`)})`), name)
}
console.log(bad === 0 ? "OK: every required key is declared" : `${bad} schema problem(s)`)
'
```

Expected: `OK: every required key is declared`.

- [ ] **Step 6: Commit**

```bash
uv run pre-commit run --files .claude/workflows/security-sweep.js
git add .claude/workflows/security-sweep.js
git commit -m "feat: the security-sweep workflow, four lanes behind a refuter"
```

---

### Task 3: The skill

**Files:**
- Create: `.claude/skills/security-sweep/SKILL.md`

**Interfaces:**
- Consumes: Task 1's verified git sequence; Task 2's `args` contract and return shape.
- Produces: `/security-sweep`, and on disk `run.json` and `06-filed.json`.

- [ ] **Step 1: Write the file**

Create `.claude/skills/security-sweep/SKILL.md` with exactly this content:

````markdown
---
name: security-sweep
description: Use when sweeping this repository for security bugs - before publishing it, or on a schedule afterwards. Audits origin/main in a throwaway worktree with a multi-agent workflow, clusters findings by root cause, checks the tracker for duplicates, and files only the clusters a human approves.
---

# Security sweep

Audits the tree a reader would clone, not the tree you happen to have checked out.

Findings are clustered by root cause and each cluster names the invariant it touches, because
a sweep that emits one issue per finding produces a round of local patches and those patches
are the next sweep's findings.

**Nothing is filed without the operator saying so.** The workflow computes the dedupe verdict;
it does not make the decision.

## Phase 0: preflight

On Windows chain with `;` and use PowerShell equivalents; the commands below are the Bash form.

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WT=.claude/worktrees/security-sweep-$STAMP
RD=.claude/security-sweeps/$STAMP
git fetch origin
git rev-list --count main..origin/main          # informational only
git worktree add --detach "$WT" origin/main
git -C "$WT" rev-parse HEAD                     # the swept SHA
mkdir -p "$RD"
```

Then write `$RD/run.json`:

```json
{"stamp": "...", "sha": "...", "repo": "jleavers/issuebot",
 "worktree": "<absolute>", "run_dir": "<absolute>", "phases": {}}
```

Three things about this, each load-bearing:

- **The worktree is cut from `origin/main`, not from local `main`.** That makes the local
  checkout's state irrelevant to what is audited, which is a stronger guarantee than
  fast-forwarding first. The behind-count is reported because an operator should know, not
  because anything depends on it.
- **`--detach`** so there is no branch to clean up and no name to collide across runs.
- **The run directory lives in the main checkout, not in the worktree**, so that cleanup — or
  a crash during cleanup — cannot take the findings with it.

## Phase 1-5: the workflow

```
Workflow({
  name: "security-sweep",
  args: {stamp, sha, repo, worktree: <absolute>, runDir: <absolute>, escalationCap: 3}
})
```

`worktree` and `runDir` must be absolute: the agents resolve them directly.

**If that reports `Workflow "security-sweep" not found`, pass `scriptPath` instead:**

```
Workflow({
  scriptPath: "<repo>/.claude/workflows/security-sweep.js",
  args: {...}
})
```

The workflow registry is read once when the session starts, so a session that just created or
edited the file does not see the name — which is every session that works on the sweep itself.
`scriptPath` takes precedence over `name` and always resolves. This is not an error worth
investigating when it happens; it is the expected state until the next session.

It runs recon, then four threat-model lanes (`copycat`, `secrets`, `hostile-issue`,
`services`) with an independent refuter behind each, then a second refuter for up to
`escalationCap` confirmed critical/high findings, then triage and the completeness critic in
parallel, then dedupe and the report. Twelve to fifteen agents.

## Phase 6: present, and get approval

Read `<runDir>/report-<stamp>.md`. Present the clusters ranked by severity; for each give the
title, the **invariant**, the blast radius, and the dedupe verdict with the issue numbers it
matched. Then ask which to file.

Say explicitly:

- which clusters the dedupe pass marked `duplicate` or `related`, and to what;
- that singletons are not proposed unless `critical`, and which ones exist;
- what the completeness critic said was never looked at.

Do not file anything the operator did not name. Do not file a `duplicate` without saying so
first.

## Phase 7: file the approved clusters

`gh issue create` with title or body flags is blocked by a PreToolUse hook. Use `gh api`, and
write the body file in a **separate** Bash call — the hook aborts the whole call, so a chained
heredoc never runs and the API call then fails with a misleading "no such file or directory".

Write the body to a temp `.md` file (Write tool), then:

```bash
gh api repos/jleavers/issuebot/issues -X POST \
  -f title='TITLE' \
  -F body=@bodyfile.md
```

Capital `-F` for the body file; lowercase `-f` posts the literal string `@bodyfile.md`.

The body carries the cluster's root cause, invariant, blast radius and fix shape, and the
finding ids behind it. It does **not** link to the run directory, which is local and stays
local: the issue has to stand alone. It does not carry a patch.

Record what was filed in `<runDir>/06-filed.json` as
`[{"cluster_title": "...", "issue_number": N}]`. A later sweep reads this to say "that is the
cluster filed as #N and not yet fixed" instead of re-finding it as new.

## Phase 8: cleanup

```bash
git worktree remove .claude/worktrees/security-sweep-$STAMP
git worktree list
```

Never `rm -rf` (AGENTS.md), and never `--force`. If the remove fails because the worktree is
dirty — it should not be, nothing writes there — report it and leave it for the operator.

**Keep the run directory.** It is the comparison the next sweep needs.

## Resuming after a crash

The Workflow tool's own resume is same-session only, so there are three tiers:

1. **Same session, run killed or script edited** — `Workflow({scriptPath, resumeFromRunId})`.
   The longest unchanged prefix of `agent()` calls returns from cache.
2. **Session gone, run directory intact** — read `<runDir>/run.json` and see which artefacts
   exist: `01-surface-map.md`, `02-findings-<lane>.json`, `03-verdicts-<lane>.json`,
   `03-escalated-<id>.json`, `04-clusters.json`, `04-gaps.md`, `05-dedupe.json`. Restart from
   the first missing phase, by hand if need be — the files are the contract.
3. **Worktree gone too** — `run.json` records the SHA, so re-cut it:
   `git worktree add --detach <path> <sha>`. A partial run stays comparable with itself
   rather than with a moved target.
````

- [ ] **Step 2: Verify the front matter parses**

`check-yaml` does not look inside Markdown, so check it directly:

```bash
uv run python -c "
import pathlib, yaml
t = pathlib.Path('.claude/skills/security-sweep/SKILL.md').read_text()
assert t.startswith('---\n'), 'no front matter'
fm = yaml.safe_load(t.split('---', 2)[1])
print(sorted(fm)); print(fm['name'])
assert fm['name'] == 'security-sweep', fm['name']
assert len(fm['description']) > 40
print('front matter OK')
"
```

Expected: `['description', 'name']`, `security-sweep`, `front matter OK`.

- [ ] **Step 3: Check the skill is where Claude Code looks for it**

```bash
ls -la .claude/skills/security-sweep/SKILL.md
```

Expected: the file exists at exactly that path. The directory name must equal the front
matter's `name`.

- [ ] **Step 4: Check the two documented hook hazards are stated**

The skill is the only place an operator learns them; a silent drop would reintroduce the bug:

```bash
grep -c 'separate' .claude/skills/security-sweep/SKILL.md
grep -c 'F body=@' .claude/skills/security-sweep/SKILL.md
grep -c 'rm -rf' .claude/skills/security-sweep/SKILL.md
```

Expected: each at least `1` — the third is the prohibition, not a use.

- [ ] **Step 5: Commit**

```bash
uv run pre-commit run --files .claude/skills/security-sweep/SKILL.md
git add .claude/skills/security-sweep/SKILL.md
git commit -m "feat: the security-sweep skill, preflight through approval gate"
```

---

### Task 4: The shakedown run

Spec §14 is explicit that the first real sweep is the shakedown: there is no unit test for a
prompt, and a mediocre report is the failure signal. This task runs it and fixes what it
exposes.

**Files:**
- Modify (only if the run exposes a problem): `.claude/workflows/security-sweep.js`, `.claude/skills/security-sweep/SKILL.md`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: a populated `.claude/security-sweeps/<stamp>/`, and the first set of filed issues.

- [ ] **Step 1: Run the preflight and start the workflow**

Follow `SKILL.md` Phase 0, then invoke the workflow with the real `args`. Note the run id the
tool returns — tier 1 of the resume path needs it.

- [ ] **Step 2: Watch for the failure modes that mean the design is wrong, not the code**

While it runs, check these against the progress output and the artefacts as they land:

- **Artefacts appear during the run, not at the end.** If `02-findings-copycat.json` does not
  exist while `services` is still scanning, the write-back instruction is not being followed
  and the crash-resistance claim is false. Stop and fix the prompt.
- **Refuters actually refute.** A refuter that confirms everything has not understood
  "default is refuted". Check `03-verdicts-*.json` for at least some refutations with
  reasoning that cites code.
- **Lanes are not all finding the same thing.** Heavy overlap means the briefs are not
  disjoint and the clustering will be trivial.

- [ ] **Step 3: Read the report before reading the clusters**

```bash
cat .claude/security-sweeps/<stamp>/report-<stamp>.md
```

Judge it on the funnel first. A run where nothing was refuted, or where every finding became
its own cluster, is a failed shakedown regardless of how the findings read.

- [ ] **Step 4: Check the triage contract was honoured**

```bash
uv run python -c "
import json, pathlib, sys
d = json.loads(pathlib.Path('.claude/security-sweeps/<stamp>/04-clusters.json').read_text())
cl = d['clusters']
print(f'{len(cl)} clusters, {len(d[\"singletons\"])} singletons')
for c in cl:
    ids = c['finding_ids']
    print(f'- {c[\"severity\"]:8} {len(ids)} finding(s): {c[\"title\"]}')
    for k in ('root_cause', 'invariant', 'blast_radius', 'fix_shape'):
        if not c.get(k) or len(c[k]) < 20:
            print(f'    WEAK {k}: {c.get(k)!r}')
solo = [c for c in cl if len(c['finding_ids']) == 1]
print(f'{len(solo)} of {len(cl)} clusters hold a single finding')
"
```

Expected: every cluster has four substantial fields, and single-finding clusters are the
minority. If most clusters hold one finding, the triage prompt has not bitten — that is the
treadmill returning, and it is worth another iteration on the prompt before filing anything.

- [ ] **Step 5: If the run exposed a prompt problem, fix and resume**

Edit the script, then resume rather than re-running from cold — the unchanged prefix comes
from cache:

```
Workflow({scriptPath: "<path from the tool result>", resumeFromRunId: "<run id>"})
```

Commit any prompt change with what the run showed:

```bash
git add .claude/workflows/security-sweep.js
git commit -m "fix: <what the shakedown exposed>"
```

- [ ] **Step 6: Present the clusters and file what is approved**

Follow `SKILL.md` Phases 6 and 7. Write `06-filed.json`.

- [ ] **Step 7: Clean up the worktree**

```bash
git worktree remove .claude/worktrees/security-sweep-<stamp>
git worktree list
git status --porcelain | wc -l
```

Expected: the sweep worktree is gone; `git status` prints `0`, proving the ignore rule from
Task 1 holds against a fully populated run directory.

---

### Task 5: Documentation, and the pull request

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything above.
- Produces: the merged feature.

- [ ] **Step 1: Add the section to `CLAUDE.md`**

Insert after the "Operational rules (from AGENTS.md)" section, before "Creating PRs":

```markdown
## Security sweeps

`/security-sweep` audits `origin/main` — not the local checkout — in a throwaway detached
worktree, with four threat-model lanes behind independent refuters, clusters what survives by
root cause, and files only the clusters a human approves. Run artefacts land in
`.claude/security-sweeps/<UTC stamp>/` and are git-ignored: a report names weaknesses that are
not fixed yet. The skill is `.claude/skills/security-sweep/SKILL.md`, the fan-out is
`.claude/workflows/security-sweep.js`, and the design is
`docs/superpowers/specs/2026-09-13-security-sweep-design.md`.
```

- [ ] **Step 2: Prove nothing Python changed**

```bash
git diff --stat main...HEAD -- src tests pyproject.toml uv.lock
uv run pytest -q 2>&1 | tail -3
```

Expected: the diff is empty, and the suite passes at its existing count.

- [ ] **Step 3: Full pre-commit and commit**

```bash
uv run pre-commit run --all-files
git add CLAUDE.md
git commit -m "docs: the security sweep, and why its reports stay untracked"
```

- [ ] **Step 4: Push the branch**

```bash
git push -u origin issuebot/security-sweep
```

Never to `main`.

- [ ] **Step 5: Write the PR body to a temp file**

**Use the Write tool** — this body mentions the blocked flags, and a heredoc would trip the
hook on its own text. Write to `/tmp/sweep-pr.md`, covering: what the two files are, why the
skill/workflow split is forced rather than chosen, the `origin/main` worktree guarantee, the
triage contract, and what the shakedown run found. End with the attribution lines the harness
requires.

- [ ] **Step 6: Open the PR with the REST API, in a separate call**

```bash
gh api repos/jleavers/issuebot/pulls -X POST \
  -f title='A repeatable security sweep' \
  -f head='issuebot/security-sweep' \
  -f base='main' \
  -F body=@/tmp/sweep-pr.md
```

- [ ] **Step 7: Read the body back**

```bash
gh pr view --json body --jq '.body' | head -20
```

Expected: the body is the file's content, not the literal `@/tmp/sweep-pr.md` — which is what
lowercase `-f` would have produced.
