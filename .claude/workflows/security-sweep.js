export const meta = {
  name: 'security-sweep',
  description: 'Sweep the tree for security bugs, cluster them by root cause, check the tracker',
  phases: [
    { title: 'Recon', detail: 'one agent maps entry points, trust boundaries and secrets' },
    { title: 'Scan', detail: 'the selected threat-model lanes hunt in parallel' },
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

// Which lane set to run. `baseline` is the four threat models a first sweep of any tree wants.
// `gaps` re-aims them at what a previous run's completeness critic said nobody owned -- the
// lanes stay threat-shaped, because a brief that is only a reading list produces coverage
// rather than attack paths, and coverage findings are the ones the refuters kill.
const laneSet = args.lanes || 'baseline'

// Optional prose naming what is already known and filed, so a lane does not spend itself
// re-deriving an issue that exists. Findings are still welcome where they go beyond it.
const known = args.known || ''

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

// --- phase 2: the lanes ----------------------------------------------------------------

const BASELINE_LANES = [
  {
    key: 'copycat',
    title: 'what a reader inherits by copying this repository',
    brief: `issuebot is about to be published as a learning tool, and readers will copy its
patterns before they understand the boundaries those patterns depend on. Your question is not
"is this safe for the author" but "what does a reader inherit, and what happens when they
apply it somewhere the author's assumptions do not hold".

Look at, at minimum: \`--permission-prompts none\` and whatever bounds it; the session
account's home and the environment credential it runs on; \`bash -lc\` hooks taking their
command text from a configuration
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

// Re-aimed lanes. Each one is a threat model the baseline set did not own, named by the
// completeness critic of an earlier run. They are still lanes, not reading lists: every brief
// says who the attacker is before it says which files to open.
const GAP_LANES = [
  {
    key: 'runas',
    title: 'the uid delegation itself, and whether its tests are vacuous',
    brief: `\`src/issuebot/agent/runas.py\` is the sandbox the whole tree points at — README
"Safety" calls the container and the uid the real boundary and everything else a convenience —
and no previous sweep read its implementation. One earlier run reasoned about \`agent.run_as\`
as a *setting* and stopped. You read the delegation.

Cover: the memfd that carries the session environment across the uid change and who else could
open it; the exact \`sudo -n -u <user> -C <fd+1>\` argv and what a sudoers rule would have to
say for it to work; the \`exec\` verb, which the worker's root-owned interpreter runs, and what
it installs before it execs; \`kill\` (a process group) and \`remove\` (files under a
workspace), which are the worker uid's two blind spots; and \`probe\`/\`probe_run_as\`, which
the orchestrator refuses to start without.

The surface map's own breach list for this boundary is a to-do nobody worked: the helper module
sitting on an agent-writable path, a writable \`/app\` or venv, the memfd inherited somewhere
else, sudoers widened beyond the one argv, and \`probe_run_as\` passing while the delegation is
actually degraded. Work it.

Then \`tests/fakes/sudo\`, which is what proves this boundary in CI. If the fake accepts argv
shapes real sudo would refuse, the boundary's tests are vacuous and every green run since has
meant less than it looked. **A vacuous-test finding is in scope for this lane**, and its
\`attack_path\` is the regression it would let through: name the change that would break the
delegation, and show that the suite would still pass.`,
  },
  {
    key: 'supply-chain',
    title: 'what gets built, cached and pulled in',
    brief: `Nobody has owned build time. The attacker here does not open an issue — they open a
pull request, or they sit between the build and a registry.

Cover \`.github/workflows/ci.yml\` and \`claude-code-version.yml\` in full: the \`permissions\`
each job runs with; whether a \`pull_request\` build from a fork can write a \`type=gha\` cache
scope that a later \`main\` build reads, and what that buys; any interpolation of
attacker-controllable text (a branch name, a PR title, an issue body) into a \`run:\` step; and
the scheduled bump-and-open-a-PR job, which fetches a version from npm, builds an image with
it, pushes a branch and opens a pull request holding a token.

Then the dependency surface: \`uv.lock\` and whether the seven runtime dependencies are pinned
or floored, \`pyproject.toml\`, \`.pre-commit-config.yaml\` and \`.github/dependabot.yml\` —
what each does and does not cover. Then the \`Dockerfile\`'s fetches, and \`.dockerignore\`
against what a working checkout actually holds.

Note before you start, so you do not spend the lane on them: the two vendored front-end digests
in \`static/vendor/README.md\` were checked by hand and match, and nothing automating that check
is already filed. The \`Dockerfile\`'s \`claude\` install fetch was raised in an earlier run and
refuted — its neighbours rest on the same TLS-to-one-host assumption, so "this one step is
unverified" is not a finding unless you can say what makes it different.`,
  },
  {
    key: 'worker-service',
    title: 'the worker as a service: scheduling, cost and wedging',
    brief: `The previous sweep's \`services\` lane was written around the dashboard and the
database, so nobody owned the worker itself — which is where cost exhaustion, wedging and
scheduling abuse live. An earlier finding (a FIFO blocking the event loop) showed that class is
live in this tree, so treat availability and spend as real consequences, not hypotheticals.

Your attacker is whoever can open an issue on the watched repository, or influence what an
issue's session does once it runs.

Cover \`src/issuebot/orchestrator/orchestrator.py\` and \`state.py\`: the dispatch holds as
fail-safe gates and whether any path claims work while one is engaged; the orphan resume, which
trusts \`session.json\`'s \`last_outcome\`; the retry and backoff schedule and what an issue can
do to it; the stall and terminal sweeps; \`_pinned_mount_complaint\`'s stat logic; and the
escape path's ordering under a hold.

Then the spend surface: \`agent.max_turns\`, \`--max-budget-usd\`, and the \`budget_exceeded\`
category, which is the one error the turn loop deliberately does not fail on. Then
\`src/issuebot/agent/session.py\` — the turn loop, \`issue_moved\` checking, and
\`blocker_from\`, which reads the first non-empty line of the model's own final message and
writes it onto a public issue. Then the parser that consumes model-authored bytes on the
worker's event loop: \`StreamParser\`, the line cap, \`parse_rate_limits\` (an undocumented line
shape whose reading becomes a number on the operator's dashboard) and the stderr tail.`,
  },
  {
    key: 'store-tenancy',
    title: 'one cluster, every repository\'s transcripts',
    brief: `One PostgreSQL cluster now holds every registered repository's issue text, run
errors and full session transcripts, and every worker on the host's shared external network
authenticates to it as the same role. A missing predicate is therefore a cross-tenant read, not
a bug in one page.

Cover \`src/issuebot/db/queries.py\` and \`store.py\` line by line: the f-strings that splice
module-level column lists; how \`repo\` and \`state\` reach a predicate, bound or interpolated;
the \`run_turns\` table, which has no \`repo\` column at all and is scoped only through a join;
and \`load_scope\`, which is supposed to run before every scoped read — check that it does, on
every route, including the raw turn parts.

Then \`src/issuebot/db/listen.py\`: the \`issuebot_refresh\` payload, where the acceptance rule
(empty means all, own repo, else dropped) plus a reconnect loop is the only filter on a NOTIFY
any container reaching the database can send, and an abusive refresh costs unbounded ticks,
unbounded \`gh\` polling and the rate limit that follows.

Then \`migrate.py\` and \`migrations/*.sql\`, run at the start of \`worker\`, \`run-once\` AND
\`web\`: whether the advisory lock actually serialises two workers starting together, what a
partially applied migration leaves behind, and what \`0003_repos.sql\`'s refusal strands — note
that the remedy it names, \`import\`, does not exist at this commit.`,
  },
  {
    key: 'hostile-issue',
    title: 'attacker-authored text reaching an unattended agent',
    brief: `Same lane as the baseline sweep, aimed at what that run did not reach. issuebot
takes a GitHub issue body — which on a public repository anyone may write — renders it into a
prompt, and hands it to a \`claude -p\` running unattended with \`GH_TOKEN\` in its environment
and a shell at its disposal.

**Already filed; do not re-derive these.** The \`.issuebot/env\` read-back with no owner or type
check, the conflict-bounce counter read out of the workpad body, and concurrent sessions sharing
one \`agent.run_as\` account are issue #104. Bare \`labels\` outside the \`<github-text>\`
envelope is #105. The cloned repository's own \`CLAUDE.md\`/\`AGENTS.md\`/\`.claude/\` as a
second instruction channel is #107. A finding that goes materially beyond one of those is
welcome; a restatement of one is not.

Go instead at: \`src/issuebot/github/runner.py\` and whether \`GhRunner\` really is the only
place a process is spawned — the map already records one deliberate exception at
\`workspace.py:237\` — and whether an issue-derived value can land in an argv position \`gh\`
reads as a flag, given that the label, model and branch constraints in \`settings.py\` are the
only thing preventing it. Then \`src/issuebot/config/workflow.py\` and \`resolve.py\`: the
loader is the mechanism by which a file write becomes worker code execution, and no lane has
read it — \`yaml.safe_load\` on a file edited live, the front-matter split, the derived overlay
path, merge rules where null deletes and a list replaces, and \`$VAR\`/\`~\`/relative-path
expansion over designated fields with hook command strings deliberately left unresolved.

Then provenance of issuebot's own artefacts: the \`own_login\` cache across a token change, a
stranger's comment carrying the workpad marker (the tree ships
\`tests/fixtures/gh/comments_impostor.json\` for exactly this and no finding has ever cited it),
case handling in the author comparison, a missing \`isCrossRepository\` read as own, and
\`classify_closed\` treating a human's merged pull request as completion.

For this lane, \`attack_path\` must name the specific attacker-controlled field and the specific
line where it lands.`,
  },
]

const LANE_SETS = { baseline: BASELINE_LANES, gaps: GAP_LANES }
const LANES = LANE_SETS[laneSet]
if (!LANES) {
  throw new Error(`unknown lane set ${laneSet}; expected one of ${Object.keys(LANE_SETS).join(', ')}`)
}

const scanPrompt = (lane) => `${WHERE}

Read ${runDir}/01-surface-map.md before anything else. It is the shared map: use its names for
entry points and boundaries, so that findings from ${LANES.length} lanes can be clustered afterwards.

Your lane is **${lane.key}** — ${lane.title}. Hunt only here. Another agent owns each of the
other lanes; a finding outside yours is their job, not a bonus.

${lane.brief}${known ? `\n\nAlready known in this tree, across every lane:\n\n${known}` : ''}

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

const criticPrompt = (allFindings, judged) => `${WHERE}

You are the completeness critic. ${LANES.length} scanners have finished, running the
\`${laneSet}\` lane set (${LANES.map((l) => l.key).join(', ')}). Your only question is: **what
was never looked at?**

Read ${runDir}/01-surface-map.md, and the raw findings below. Then find the holes:

- Files in the map's inventory that no finding cites and that no lane's brief plainly covers.
  Weight by what a file does, not by its size: a fifty-line file that spawns a process matters
  more than a thousand-line template.
- Entry points in the map that no \`attack_path\` mentions.
- Surfaces that exist in the map but fall between the four lanes, so that nobody owned them.

For each gap give \`surface\` (the file, route or boundary), \`why_it_matters\` (what could be
there), and \`suggested_lane\` — one of ${LANES.map((l) => `\`${l.key}\``).join(', ')}, or
\`new\` if it needs a lane none of these briefs would cover.

A gap that an earlier run already named and that this run still did not reach is worth naming
again, and worth saying so: a surface nobody has owned across two sweeps is a stronger signal
than a fresh one.

Do not audit the gaps yourself; naming them is the whole job. Your output seeds the next
sweep's briefs.

**Arithmetic you may state, and nothing beyond it.** Every number you put in your prose must
come from the two lists below or from a command you actually ran against the worktree. There
were **${allFindings.length}** findings this run, of which
**${judged.filter((v) => v.refuted).length}** were refuted by their lane's refuter (a separate
escalation pass may since have killed more, and you are not shown it). Do not recompute those
figures and do not state any other claim about verdicts: a surface that was examined and cleared is not a gap, and getting that backwards is
the one way this report misleads the next sweep. If you want a coverage figure, derive it by
running a command, and say which.

Findings produced this run, each with the refuter's verdict:

${JSON.stringify(
    allFindings.map((f) => {
      const v = judged.find((x) => x.id === f.id)
      return { ...f, verdict: v ? { refuted: v.refuted, reasoning: v.reasoning } : 'no verdict' }
    }),
    null,
    2,
  )}

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
  () => agent(criticPrompt(allFindings, [...verdictFor.values()]), {
    label: 'critic', phase: 'Triage', schema: GAPS,
  }),
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
