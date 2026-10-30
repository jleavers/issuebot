# Copilot as the agent: a deployment that runs GitHub Copilot instead of Claude

Date: 2026-10-30
Status: draft, written ahead of the probe. Every point marked **[P*n*]** waits on the probe
in "What the probe must establish", which needs a Copilot seat that does not exist yet.
Related: #274 (Codex as a second agent), whose seam this shares; #280 and
`docs/superpowers/specs/2026-10-30-github-enterprise-hosts-design.md` (the GHE.com host), which
this builds on.

## Why, and why now

The maintainer is moving some projects to a company enterprise on GHE.com (the GHE spec above),
where colleagues will file and approve issues as the maintainer does. If the worker on that
tenant ran `claude -p` as it does today, every colleague's issue would be paid for out of the
maintainer's personal Claude subscription, and would share its 5-hour and 7-day windows with
the maintainer's own work. The company pays for **GitHub Copilot Business** instead, and its
Copilot policy enables the CLI. So the company's worker should run on the company's Copilot,
with the company's models, and touch the maintainer's Claude account not at all.

Nothing can be measured yet. The maintainer has no Copilot seat on the tenant, and neither
does any bot account, so the Copilot CLI has never run there. This spec fixes what the
maintainer decided, what GitHub documents, and how the design fits issuebot. It leaves each
fact it cannot know as a numbered gap for the probe, and where a gap could change the design,
it says what happens on each answer.

**This repository is public.** The company, its subdomain, its organisation and its region
are never named in a commit, an issue, a pull request, a test or a doc. They are
`SUBDOMAIN.ghe.com`, `ORG` and `REGION` everywhere, including in this file.

## Decisions

Taken by the maintainer in the brainstorm on 2026-10-30:

1. **A Copilot deployment uses Copilot for everything.** Worker sessions, triage and parent
   reviews all run on Copilot. Nothing on that deployment runs Claude.
2. **Models are chosen as they are today.** There is a default model and a model per issue,
   chosen by a label, with the same rules as `claude.model_labels`.
3. **Only models the company allows.** The enterprise policy decides which models a seat may
   use. Once a seat exists, the allowed list is read from Copilot itself, and the maintainer
   chooses which of those models get labels.
4. **GitHub's budget is the cap; issuebot measures it and stops cleanly.** The maintainer has
   been told that Copilot users start with a budget of $50 a month. issuebot relies on that
   cap, with only a backstop of its own (decision 9). It shows what has been spent, alerts in Slack at 75%, 90% and 100%,
   and, when the budget is spent, stops taking issues until it resets, the way it holds on a
   Claude usage limit today. **It never carries on with a fallback model.**

Played back to the maintainer as guesses and confirmed:

5. **The maintainer's Claude login is kept out.** It is not mounted into a Copilot
   deployment's worker at all, so a mistake in the settings cannot put a colleague's issue on
   the maintainer's limits. `validate` warns if a Claude credential is present.
6. **The agent is chosen per deployment, separately from the host.** It shares the seam #274
   designs for Codex. A github.com deployment could run Copilot too, though none of the
   maintainer's would.
7. **Cost is counted in the unit the seat is charged in, and there is no separate dollar cap
   per turn beyond what Copilot offers.** The brainstorm assumed that unit was premium
   requests. GitHub's documentation (below) shows that Copilot Business has billed in **AI
   credits** since 2026, at exactly $0.01 a credit. A Copilot run's cost is therefore shown
   in dollars, as a Claude run's is, and the existing dollar settings apply to it (section 2).
   *(Corrected after the brainstorm and reported to the maintainer.)*

Taken by the maintainer on the reviewer's questions:

8. **A broad Copilot token is accepted only on the bot's account.** If the tenant cannot issue
   a token that uses Copilot and nothing else, work sessions run only when the seat belongs to
   the dedicated bot account, whose reach is the repositories it has been added to. They never
   run on the maintainer's own account, which as an organisation administrator reaches every
   repository in the organisation. Triage is off in that case either way (section 4).
9. **issuebot stops at the declared budget as a backstop.** Decision 4 stands: GitHub's budget
   is the cap. But if the budget turns out to be soft, or there is no hard stop per user,
   the worker also holds when the month's spend reaches 100% of the budget the operator
   declares (section 5).

## What was established

### On the tenant

From a read-only look on 2026-10-30, with the maintainer's own token:

- **The organisation's Copilot billing** (`GET /orgs/ORG/copilot/billing`): plan `business`;
  no seats assigned yet; seat management `unconfigured`; the policies for the CLI, IDE chat and
  platform chat all `enabled`; public code suggestions allowed.
- **The maintainer's Copilot user record** (`GET /copilot_internal/user`, an undocumented
  endpoint the editors use): an enterprise-managed account with no seat (`chat_enabled` and
  `cli_enabled` false, no assignment date, no organisations listed). It names the tenant's own
  Copilot endpoints, all under the tenant's domain rather than github.com's:
  - `copilot-api.SUBDOMAIN.ghe.com`
  - `copilot-proxy.SUBDOMAIN.ghe.com`
  - `origin-tracker.SUBDOMAIN.ghe.com`
  - `copilot-telemetry-service.SUBDOMAIN.ghe.com`
- **The Copilot settings pages** on the tenant redirect to the profile, which is what a
  managed account without a seat sees.

### What GitHub documents

Read on 2026-10-30, when the CLI's latest release was 1.0.105. The pages carry no dates, so
the CLI versions below come from its changelog. Sources:

- [REF] `docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference`
- [PROG] `docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference`
- [AUTH] `docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli`
- [CL] `github.com/github/copilot-cli/blob/main/changelog.md`
- [ALLOW] `docs.github.com/en/copilot/reference/allowlist-reference`
- [DR] `docs.github.com/en/enterprise-cloud@latest/admin/data-residency/github-copilot-with-data-residency`
- [BILL] `docs.github.com/en/copilot/concepts/billing-and-usage/organizations-and-enterprises/billing`
- [BUD] `docs.github.com/en/copilot/concepts/billing-and-usage/organizations-and-enterprises/budgets`
- [USAGE] `docs.github.com/en/enterprise-cloud@latest/rest/billing/usage`

**Billing [BILL] [BUD].**

- Copilot Business and Enterprise bill in **AI credits**, worked out from tokens and the
  model, at 1 credit = $0.01. Premium requests and model multipliers are the older scheme.
- **Allowance.** Each Business seat brings 1,900 credits a month into a pool shared across the
  billing entity, and the pool resets at 00:00 UTC on the 1st. The pool is the organisation's,
  not the seat's.
- **Overage.** Paid usage beyond the pool is on by default. With it off, usage is blocked until
  the 1st. No fallback model is documented.
- **Budgets.** A user-level budget (universal, per cost centre or per individual) is always a
  hard stop, and a $0 one blocks at once. Cost-centre, organisation and enterprise budgets stop
  usage only if "Stop usage when budget limit is reached" is on, which it is not by default.
  The $50 the maintainer was told about is most likely a universal user-level budget, which
  would make it a hard stop.
- **Reading usage.** `GET /organizations/ORG/settings/billing/ai_credit/usage` [USAGE] needs
  an organisation administrator. No documented endpoint lets a seat holder read their own
  remaining credits. The budgets API needs a classic token with `manage_billing:enterprise`
  and refuses fine-grained ones.

**The CLI [REF] [PROG] [AUTH] [CL].**

- **One-shot runs.** `-p` runs a prompt and exits.
  - Approvals are made in advance with `--allow-tool`, `--deny-tool` and `--allow-all-tools`,
    or `--allow-all`/`--yolo` for every kind, with patterns such as `shell(git:*)` and
    `write(path)`. A deny always wins. Under `-p`, anything not approved in advance is denied.
  - `--available-tools` and `--excluded-tools` narrow the tool set itself.
  - `--no-ask-user` stops the agent asking questions.
- **Admin override.** An enterprise policy, `permissions.disableBypassPermissionsMode`, can
  switch off every allow-all flag.
- **Model.** `--model` or `COPILOT_MODEL`, and `--model` errors on a model the seat may not
  use. A custom agent's model, and a trusted repository's `.github/copilot/settings.json`,
  can also pin one.
- **Sessions.** `--session-id UUID` chooses the id, and `--resume=ID` resumes it.
- **Output and logs.** `--output-format json` writes JSONL, but its schema is undocumented.
  Each session also keeps `~/.copilot/session-state/<id>/events.jsonl`. `--log-dir`,
  `--log-level` and `COPILOT_HOME` move the files.
- **Exit codes.** `-p` exits non-zero on auth, quota and network errors, and when a prompt is
  blocked before any reply. Individual codes are not documented.
- **Per-run cap.** `--max-ai-credits` is a soft cap per run, in public preview, with a minimum
  of 30. Under `-p` the run ends when it is reached.
- **Auth.** Token order: `COPILOT_GITHUB_TOKEN`, then `GH_TOKEN`, then `GITHUB_TOKEN`, then a
  stored login, then `gh auth token`.
  - Accepted: OAuth tokens, user-owned fine-grained tokens with the account permission
    **Copilot Requests**, and GitHub App user tokens. Classic tokens are not.
  - An organisation-installed GitHub App with the organisation permission Copilot Requests,
    billed to the organisation, is in public preview.
- **GHE.com.** Supported. `COPILOT_GH_HOST` (1.0.35) overrides `GH_HOST`. The network needs
  `SUBDOMAIN.ghe.com` and names under it, and no github.com host; `origin-tracker` is needed
  only for public-code detection [ALLOW]. A data-residency policy can restrict Copilot to
  models certified in the region, at 10% more credits [DR].
- **Confinement.**
  - **Instruction files.** `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`,
    `.github/copilot-instructions.md`, `.github/instructions/`, `.claude/rules/` and more load
    automatically, and `--no-custom-instructions` turns all of them off.
  - **Built-in MCP servers.** There are four: `github-mcp-server`, `playwright`, `fetch` and
    `time`. `--disable-builtin-mcps` is documented for the first only, and
    `--disable-mcp-server NAME` turns off one at a time.
  - **Trust.** Under `-p`, a folder that is not trusted loads no repository hooks, workspace
    MCP servers or extensions. Trust is saved in `trustedFolders` in `~/.copilot/config.json`,
    and `COPILOT_ALLOW_ALL=true` also trusts the folder.
  - **Telemetry and updates.** `--disable-client-telemetry` (1.0.100) turns off client
    telemetry, not the server's. `--no-auto-update` or `COPILOT_AUTO_UPDATE=false` stops
    updates.
  - **Redaction.** `--secret-env-vars` redacts named variables from output, and
    `COPILOT_GITHUB_TOKEN` and `GITHUB_TOKEN` are redacted by default.
- **Models.** The documented default is `claude-sonnet-5.5`, and the list includes OpenAI,
  Anthropic and Google models. Listing them is documented only through `/model` in an
  interactive session.
- **Automated use.** GitHub's terms allow machine accounts with a named human responsible,
  and the CLI's terms say nothing against automation. GitHub's own route for automation is
  the Actions `GITHUB_TOKEN` with `copilot-requests: write`, billed to the organisation.

## Goals, non-goals, anti-goals

**Goals.**

- A deployment set to Copilot claims, works, reviews and completes issues exactly as a Claude
  deployment does: the same labels, the same workpad, the same pull request, the same holds.
- Its spend is shown, alerted on, and capped per turn and per issue in dollars, and the worker
  holds when GitHub stops it.
- Models are chosen by label from the company's allowed list, and `validate` refuses a model
  the policy does not allow.
- The session boundary is as tight as a Claude session's: confined configuration, no
  instruction files loaded behind issuebot's envelope, the egress allow-list, one account per
  concurrent session.

**Non-goals.**

- **Claude and Copilot in one deployment.** A Copilot deployment runs Copilot alone (decision
  1). Copilot as a second agent chosen per issue in a Claude deployment, as #274 does for
  Codex, is a possible follow-up, not this.
- **Automatic routing between agents** when one is held.
- **Copilot coding agent**, the cloud agent GitHub runs on its own infrastructure from an
  assigned issue. issuebot's value is its own session boundary, workpad and review flow, which
  the cloud agent would replace rather than use.
- **GHES.** As in the GHE spec, nothing here is built or tested against Enterprise Server.

**Anti-goals: it would be a failure if any of these happened, even with everything else
working.**

- **A colleague's issue runs on the maintainer's Claude account**, or draws on its limits.
- **A run silently uses a different model from the one it was given**, and its work is then
  treated as a normal run.
- **A triage session holds a credential that can change a repository.** Today's triage runs
  with no GitHub credential at all (`TriageRunner.child_environment`), so a hostile issue
  cannot make it act on GitHub. Copilot authenticates with a GitHub token, which is exactly
  the kind of credential triage was built without. See section 4.
- **One issue spends the month's budget for everyone.** Colleagues' issues all draw on the
  seat's budget, and the seat draws on the organisation's shared pool before that, so a
  runaway issue must be stopped well before either empties.
- **The tenant's names reach anything public**, including a recorded probe transcript.

## Design

### 1. Choosing the agent: `agent.default`

A new setting, `agent.default`, takes `claude` (the default, and today's behaviour) or
`copilot`. It names the agent that runs every issue without a model label, and every triage
and parent review. *(My call: an explicit setting rather than "a `copilot:` block is
present", which would make an edit to one block silently change which agent runs.)*

In this spec a deployment runs one agent:

- With `agent.default: copilot`, a non-empty `claude.model_labels` is refused when the workflow
  loads, naming both settings, so a Claude label can never reach a Copilot deployment.
- With `agent.default: claude`, a non-empty `copilot.model_labels` is refused the same way
  until the per-issue follow-up exists.
- `triage.model` and `run-once --model` name a model of the default agent.
- **`agent.default: copilot` is refused on github.com for now.** The egress hosts in section 6
  are the ones measured on the tenant, and nothing has measured the CLI against github.com.
  The runner itself is host-independent, so lifting the refusal is a measured host list and
  one end-to-end proof. *(My call: decision 6 keeps the two settings separate; this only
  limits what is built before it is proven.)*

The name `default` leaves room for #274. When Codex lands as a second agent chosen by label,
the rule becomes "labels in another agent's block route to that agent", and these two refusals
are what it relaxes.

### 2. The `copilot:` block

```yaml
agent:
  default: copilot
copilot:
  command: copilot
  model: <a model id from the allowed list>
  model_labels:
    issuebot/model/<short name>: <model id>
  max_budget_usd: 5.0
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
```

- **Model names live in the workflow, never in code,** and labels are version-free, as #274
  requires for Codex. The configured string goes to `--model` unchanged.
- `labels ensure` creates the labels in `copilot.model_labels`, and `validate` warns about
  missing ones, as it does for `claude.model_labels`. Ambiguity follows today's rule: two
  labels naming different models run the default and log `model_labels_ambiguous`.
- **`validate` checks every configured model against the allowed list [P9].** It prints each
  allowed model with its price, and fails on a configured model that the policy does not
  allow, since every run on it would fail. If the list cannot be read, the check warns and
  says why. If the probe finds no way to list models outside an interactive session, the check
  instead runs a one-line `-p` prompt per configured model, which costs a few credits, and
  `--model`'s refusal is the answer. *(My call: an error, not a warning, for a model known to
  be refused.)*
- **`copilot.max_budget_usd`** is the per-turn cap, the counterpart of `claude.max_budget_usd`.
  It goes to `--max-ai-credits` as dollars × 100, and is refused at load below $0.30, the
  flag's minimum of 30 credits. `triage.max_budget_usd` reaches a Copilot triage the same way.
  The flag is a public preview **[P4]**. If a release drops it, `validate` fails on the
  version rather than letting turns run uncapped.
- **`agent.max_issue_cost_usd` applies to Copilot runs** when each turn reports what it cost
  **[P5]**, since a credit is an exact number of cents. If turns report nothing, the cost is
  unknown, the per-issue cap cannot see it, and the README says so beside the setting. The
  per-turn cap and `agent.max_turns` are then what bound an issue.
- **Repository settings cannot override the model.** A trusted repository's
  `.github/copilot/settings.json` can pin one; sessions never trust the folder (section 6), and
  the model check in section 3 catches it if that ever fails.

### 3. `CopilotRunner` behind `TurnRunner`

`TurnRunner` (`agent/runner.py`) is the seam, and `ClaudeRunner` its only implementation. A
`CopilotRunner` implements the same protocol with `-p`, one process per turn.

- **Invocation.** In outline, and confirmed by the probe **[P4]**:

  ```
  copilot -p PROMPT --session-id UUID | --resume=UUID
      --model MODEL --max-ai-credits N
      --allow-all-tools --no-ask-user
      --output-format json --log-dir DIR
      --no-custom-instructions --disable-builtin-mcps
      --disable-mcp-server playwright --disable-mcp-server fetch --disable-mcp-server time
      --disable-client-telemetry --no-auto-update
  ```

  All tools are allowed inside issuebot's boundary, because the run-as account and the egress
  proxy are the sandbox, as they are for Claude. `claude.disallowed_tools`' default, which
  denies the model's own web tools, is matched by turning off the `fetch` server and by
  egress.
- **The session id.** `--session-id` lets issuebot choose the UUID, as `ClaudeRunner` does, so
  #274's change for agent-assigned ids is not needed here.
- **Output [P5].** The JSONL from `--output-format json` is the transcript if its schema
  carries enough. Otherwise the session's `events.jsonl` is, read after the turn. Either way
  the runner records per turn: the model that **actually** ran, tokens, credits if reported,
  the result text and the exit status.
- **Liveness.** The stall check (`stall_timeout_ms`) is fed by each line the runner reads, as
  it is for Claude. If the CLI's stdout does not stream per event **[P5]**, the runner instead
  watches the growth of the session's `events.jsonl`, through the `Boundary` as
  `_stderr_tail` reads stderr, with `COPILOT_HOME`'s session state placed under the run's log
  directory so that it is a file the worker already reads. *(My call.)*
- **The model check.** No fallback model is documented, so this defends against an
  undocumented one. *(My call, all of it.)*
  - **What is checked.** The model of the main agent's own requests only. Subagents,
    compaction and other auxiliary calls are recorded in the transcript but not checked,
    since the prompt may legitimately ask for a smaller reviewer (section 7). If the
    configured id is an alias **[P9]**, the expected id is the one the probe records the
    alias resolving to, and the alias is listed beside it in the findings.
  - **When.** On the first event that names the main agent's model, so a wrong model is
    stopped before it has done anything: the runner terminates the turn as it does on a
    deadline. A turn whose output names no model is checked once it ends.
  - **What it does to the issue.** The issue is blocked, with a reason naming both models,
    whatever the turn had already done. A pull request it opened stays open for a person to
    judge and is never closed by issuebot (`AGENTS.md`). The `issuebot/review` label, if the
    agent had applied it, is replaced by the block, so the work never looks like a normal
    run.
  - **What it does to the deployment.** A mismatch also raises the hold (section 5) on the
    schedule for a refusal that names no limit, because the likely cause of a fallback is the
    budget, and the next run would fall back too. The hold lifts when a probe turn runs on
    the right model.
  - If the output does not name the model at all **[P5]**, the docs say the check is not
    possible and rely on `--model`'s refusal.
- **`TurnResult` changes, shared with #274.** It gains the agent's name, and `cost_usd` becomes
  optional: `None` when unknown, never `0` and never estimated. A Copilot turn that reports
  credits records them as dollars. Claude-specific token fields stay, and a runner that cannot
  fill them leaves them at zero and says so in the transcript header. *(My call.)*
- **Failure classification.** `classify_result` gains Copilot's wording **[P7]** for:
  - auth failures (an expired or revoked token, no seat, SSO not authorised);
  - a refused model;
  - credits or budget exhausted, which classifies as `usage_limited`;
  - the per-turn cap reached, which is a failed turn like Claude's `error_max_budget_usd`.

  The exit codes are not documented, so text is matched, with the probe's recorded samples as
  tests.
- **Bounds.** `copilot.max_budget_usd` per turn, `turn_timeout_ms`, `stall_timeout_ms`,
  `agent.max_turns`, and `agent.max_issue_cost_usd` where costs are reported.
- **The credential probe.** Claude's `claude auth status` feeds the startup refusal, the auth
  hold's per-tick re-probe (`_refresh_auth_hold`) and `validate`'s credential line. Copilot's
  counterpart is a read of `/copilot_internal/user` with the Copilot token, which costs
  nothing and says whether there is a seat and the CLI is enabled **[P8]**. If that read is
  unusable, `validate` and startup run one short `-p` prompt instead, and the auth hold
  re-probes that way hourly rather than every tick, since each costs a few credits. *(My
  call.)*

### 4. Triage and parent review

Triage and parent reviews run on Copilot (decision 1). `TriageRunner` and `ParentReviewRunner`
gain Copilot counterparts, with two differences from Claude's.

**A credential where there was none.** Claude's triage runs with no `GH_TOKEN` and nothing
from the workspace's env file. A Copilot session cannot run without a GitHub token, because
that is how Copilot authenticates. So:

- **The Copilot token is a separate token from `github.token`** (section 6), and must be one
  that can use Copilot and nothing else **[P2]**. The candidates, in order:
  1. **A fine-grained token** owned by the seat holder, with the account permission Copilot
     Requests and **no repository access**. Documented, but not known to be creatable for a
     managed account on GHE.com.
  2. **An organisation GitHub App** with only the Copilot Requests permission, billed to the
     organisation. In public preview, and not documented for GHE.com or for the CLI's own
     token lookup.

  Triage then holds a credential that can spend credits but cannot read or change a
  repository, an issue or a label. That is still a real change to triage's boundary, and
  `docs/security-model.md` says so in those words: the worst a hostile issue can do through
  it is spend, which `triage.max_budget_usd` and `triage.max_turns` bound.
- **Checked, not trusted.** `validate`, and the worker at start, use the Copilot token to read
  `github.repo` and to list the repositories it can see. Triage runs only if the first is
  refused and the second is empty. Otherwise it is off, exactly as in the next point, and
  `validate` says which read succeeded. An operator's word that the token is Copilot-only is
  not enough, since the anti-goal rests on it. *(My call.)*
- **If neither works [P2]**, every working Copilot token would also reach repositories. Then
  **triage is off on a Copilot deployment**: `triage.auto` is refused at load, and an issue
  labelled `issuebot/triage-todo` is blocked with a reason naming this section, until the
  maintainer decides otherwise. *(My call, put to the maintainer with decision 8: the
  alternative is triage holding a repository-capable token.)* Parent reviews fall under the
  same rule, since they run in triage's session.
- **Work sessions, in that case (decision 8)**, run only on the dedicated bot account's seat.
  - **Enforced.** When the token check above finds that the Copilot token reaches
    repositories, the worker refuses to start if the token's account is an owner or
    administrator of the organisation. The maintainer's own account is one, and a bot account
    added to repositories is not.
  - **Reported.** `validate` names every repository the token reaches beyond `github.repo`,
    so the operator sees what a hostile issue could reach through it.
  - **Documented.** `docs/security-model.md` says that the session then holds two
    credentials, and that the Copilot one is as broad as the bot account.

**Read-only tools.** Claude's triage runs with `--restricted` and the tools `Read`, `Grep` and
`Glob`, and denies `/proc`. Copilot's triage narrows the tool set with `--available-tools` to
the CLI's read and search tools, and denies `shell` and `write` with `--deny-tool`, which wins
over everything **[P11]**: the probe records the tool names, and whether the read tool can be
kept out of `/proc`. If it cannot, the docs say Copilot triage is less confined than Claude's
in that one respect.

**The verdict.** Claude's triage returns its verdict through `--json-schema` as
`structured_output`. The Copilot CLI documents no equivalent **[P11]**, so the prompt asks for
the verdict as one JSON object in a fenced block at the end of the final message, and the
existing verdict parser, which already trusts nothing the session returns, extracts and
validates it. A missing or invalid verdict is the same failure it is today.

### 5. Spend, the budget and the hold

**What is spent.** Every turn spends AI credits, from tokens and the model. Until the
organisation's pool for the month is used up, that costs nothing extra. After it, paid usage
continues, if the policy allows it, until the seat holder's user-level budget stops it.
Colleagues' issues all spend through one seat, and that seat draws on the same pool as every
human Copilot user in the organisation.

**The hold.** It extends today's usage hold (`_hold_usage`, `USAGE_HOLD_KEY`) and names the
agent.

- It is raised by a turn classified `usage_limited` (section 3), including a model mismatch.
  GitHub enforces the budget, so this is the hold's main trigger.
- **The backstop (decision 9).** It is also raised before a claim when the month's spend has
  reached 100% of `ISSUEBOT_COPILOT_BUDGET_USD`. The worker never reads billing itself: the
  web stores each reading in the hub's database, with the declared budget beside it, as it
  stores the Actions minutes, and the worker reads the latest one from there. The worker has the database already, and the
  billing token stays in the web.
  - No declared budget, or no reading, means no backstop, and the hold relies on GitHub's
    refusal alone.
  - A reading older than a day is ignored, so a broken reader cannot hold the board.
  - This hold also lifts at 00:00 UTC on the 1st, and its reason says it was issuebot's own
    limit, not GitHub's.
- **How long it lasts** depends on what the refusal says **[P7]**. *(My call.)*
  - **A refusal that names the monthly budget or credits** holds until **00:00 UTC on the
    1st**, the documented reset, which the hold carries as its `until`.
  - **A refusal that names a short rate limit** with a retry time holds until that time, as a
    Claude window does.
  - **Anything else**, including a model mismatch, holds for an hour. Then the worker runs one
    probe turn: a one-line prompt on the default model, with `copilot.max_budget_usd`'s floor
    as its cap. If it runs on the right model, the hold lifts. If it is refused again, the
    hold lasts another hour. Today's one-poll-interval backoff for an undated refusal would
    mean a refused turn every 30 seconds for weeks. The probe records whether a refused turn
    costs anything, and if it does not, the hour can shrink.
  - An administrator who raises the budget mid-month lifts a monthly hold by restarting the
    worker. `docs/operations.md` says so.
- Its reason says `copilot budget reached`, `copilot credits exhausted`, `copilot rate limited`
  or `copilot refused`, with the time it is next tried. A Claude deployment's reason is
  unchanged.

**Reading the spend [P8].** The documented source is the organisation's usage report
(`ai_credit/usage`), which needs an organisation administrator. The web already reads the
organisation's Actions minutes for a tenant repository with a billing token (the GHE spec,
decision 5), so the same token reads this too. The web holds no Copilot credential, and no
workflow, so:

- the worker writes the seat holder's login into its snapshot, and the web filters the report
  to it;
- the budget is declared in `.env` as `ISSUEBOT_COPILOT_BUDGET_USD`, because the budgets API
  needs a classic enterprise-billing token that this design will not hold. *(My call.)*

If the report cannot be filtered to one user, or the token cannot read it, the probe tries the
undocumented quota in `/copilot_internal/user`, read by the worker with the Copilot token after
each run. Whichever source works, a figure issuebot does not have is shown as unknown, never as
zero, and a failure to read never blocks dispatch on its own.

**Which figure.** The tile and the alerts use the figure GitHub's user-level budget counts,
which **[P8]** establishes: the seat holder's total use, or only what was paid beyond the pool.
Until it is known, they use the total, so an alert comes early rather than late. *(My call.)*
Watching the organisation's pool itself is a follow-up.

**The dashboard.** On a Copilot deployment the limits tile shows the seat holder's spend this
month in dollars, against the declared budget when there is one, and the reset date. Each run's
cost shows in dollars, as a Claude run's does.

**Slack.** Alerts at 75% and 90% of the declared budget, and a hit message when the hold is
raised, in the claude-limits-alert spec's wording (spec 2026-10-02) and its
post-once-per-window rule. The window is the calendar month. Without a declared budget there
are no percentage alerts, only the hit. The alert table gains an agent column in a migration
after the GHE spec's `0009`.

### 6. Credentials and the session boundary

- **The Copilot token.** `ISSUEBOT_COPILOT_TOKEN` in `.env`, passed to the CLI as
  `COPILOT_GITHUB_TOKEN`, which the CLI reads before `GH_TOKEN`. The session's `GH_TOKEN`
  stays the repository token for `gh`. `GH_TOKEN` is added to `--secret-env-vars`, since only
  `COPILOT_GITHUB_TOKEN` and `GITHUB_TOKEN` are redacted by default.
- **The host.** `COPILOT_GH_HOST` is set from `github.host` whenever `GH_HOST` is (the GHE
  spec's step 0), so the CLI and `gh` cannot disagree about the host.
- **Whose seat.** The same two arrangements as the GHE spec's step 1: a dedicated managed bot
  account with its own seat (preferred, and the first IT request below), or the maintainer's
  managed account and seat.
- **The pool.** The token is an environment credential with no file to refresh, so every
  session account in the pool can share it, as `CLAUDE_CODE_OAUTH_TOKEN` is shared today.
  `ENV_CREDENTIAL_NAMES`, `credential_complaint()` and the protected and passthrough prefixes
  gain the Copilot counterparts, so a workspace's env file cannot set a `COPILOT_` variable.
- **No Claude credential (decision 5).** On `agent.default: copilot`, `validate` warns if
  `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` is set, or if a session account's
  `~/.claude` holds a login. The worker never passes either variable to a Copilot session.
- **Configuration confinement [P10].** Copilot's equivalent of `CLAUDE_HOME_SWEEP` and
  `--strict-mcp-config`:
  - a `COPILOT_HOME` per session account, swept before each session, including
    `trustedFolders` in `config.json`, so that no session can trust its workspace for the next
    one. That is the same surface as Claude's `hasClaudeMdExternalIncludesApproved` (#135);
  - **the workspace is never trusted**: no `--trust-folder` and no `COPILOT_ALLOW_ALL`, which
    would trust it, so repository hooks, workspace MCP servers, extensions and repository
    settings do not load. The probe confirms that `--allow-all-tools` alone does not trust
    the folder;
  - **all four built-in MCP servers off**, since sessions reach GitHub through `gh` with the
    repository token, and a GitHub MCP server holding the Copilot token is a second route
    nobody reviewed;
  - **`--no-custom-instructions`**, so `CLAUDE.md`, `AGENTS.md` and the rest reach the model
    only through issuebot's envelope. The probe confirms that it covers every file the CLI
    would otherwise load;
  - updates and client telemetry off.
- **If the probe finds a gap in that confinement**, the outcome depends on the surface. *(My
  call.)*
  - **An instruction file still loads.** This is a documented residual. The file is committed
    text the prompt already treats as data, and the gap is that it also arrives outside the
    envelope.
  - **A built-in MCP server cannot be turned off.**
    - The `github-mcp-server` is a **blocker** unless the token is Copilot-only (section 4's
      check). With such a token it can reach nothing, and it is a documented residual.
    - `fetch` and `playwright` are residuals, because egress bounds them.
  - **`--allow-all-tools` trusts the folder.** Sessions use an explicit `--allow-tool` list
    instead, and the plan designs it.
- **Egress [P3].** A Copilot deployment's allow-list drops the Anthropic hosts and adds exactly
  the Copilot hosts the probe sees the CLI dial, by name. issuebot never puts a wildcard on the
  list, whatever the allow-list reference suggests. `origin-tracker` stays off, since the
  organisation allows public code suggestions, and so does the telemetry host unless the CLI
  fails without it. The egress proxy reads no workflow, so it learns the agent the way the GHE
  spec has it learn the host: `ISSUEBOT_AGENT=copilot` in `.env`, with `validate`'s egress
  probe catching a mismatch with `agent.default`. A Claude deployment's list is unchanged.
- **The scrubber.** Fine-grained and OAuth tokens are already masked. If the probe sees any
  other credential shape in the CLI's output or logs **[P10]**, it joins the scrubber with a
  test.

### 7. The prompt

`configs/WORKFLOW.md` names `claude` in a few places, and its self-review step dispatches a
subagent through Claude's Agent tool. A Copilot deployment needs wording that is true for it:

- The places that name `claude` say "the agent", or the agent's name through a template
  variable. *(My call: agent-neutral text where it reads naturally, a variable where the name
  matters.)*
- **Self-review [P12].** If the CLI can dispatch a subagent with its own context, the step
  uses it. If it cannot, the step runs a nested reviewer from inside the turn:
  - The runner renders the exact command into the prompt as a template variable. It is a
    `copilot -p` with the same confinement flags as section 3, read-only tools as in triage,
    and a fresh session id.
  - The agent runs that command with the brief, reads the findings and fixes them, all
    within its own turn, as it does today.
  - Nothing in the orchestrator changes. The nested run's cost is not in the parent turn's
    output, so the docs say a Copilot run's recorded cost leaves it out. *(My call: a
    prompt-only change, rather than a reviewer session the orchestrator would run between
    turns.)*

  `agent.self_review: false` remains the way to turn it off. The probe records what a
  subagent or nested reviewer costs.

### 8. The image

- An opt-in build argument, `COPILOT_CLI_VERSION`, empty by default, installs a pinned CLI.
  The default image is unchanged, so CI's assertion that it carries no `node` or `npm` still
  holds. If the CLI needs a Node runtime **[P13]**, that runtime is private to the CLI under
  `/opt/copilot` and not on the session's `PATH`. *(My call: the default image stays as it is
  for every Claude deployment.)*
- The build asserts `copilot --version` and the flags issuebot relies on, as it does for
  `claude --help`, and verifies a checksum where the release publishes one.
- `MIN_COPILOT_VERSION` beside `MIN_CLAUDE_VERSION`, checked by `validate`: at least 1.0.100,
  the latest of the flags above (`--disable-client-telemetry`), and above 1.0.48, the minimum
  for credit billing.
- The CI `docker` job's opt-in build gains the argument and its assertions.
- A scheduled bump workflow like `claude-code-version.yml` is a follow-up, not part of this.

### 9. Turn logs and the dashboard

- Turn capture (`agent/turnlog.py`), `run_turns` and `web/transcript.py` take the transcript
  section 3 chooses. It is scrubbed on the way into the database as Claude's is. Anything the
  renderer does not understand is shown as raw text, never dropped.
- Every run records its agent and model, which the issue page and the running-agents table
  show. Shared with #274.

### 10. Documentation

- README: `agent.default`, the `copilot:` block, Copilot model labels, and which settings
  apply to Copilot runs.
- `docs/security-model.md`: the Copilot token and why it is separate, triage's changed
  boundary, the egress hosts, the untrusted workspace and configuration confinement, and that
  no Claude credential is present.
- `docs/dashboard.md`: the spend tile and alerts, and the billing token's reach.
- `docs/operations.md`: the Copilot hold, what a refused model looks like, and a runbook for a
  Copilot deployment on a tenant.
- `docs/toolchains.md`, or the README's image section: `COPILOT_CLI_VERSION`.

## What the probe must establish

A throwaway probe, like #294 for Codex: a script and a findings note, no change to issuebot's
runtime code, run against a pinned CLI release once a seat exists. The findings note is
committed under `docs/superpowers/specs/`, dated and named for the Copilot probe, with every
tenant name sanitised. Each gap says what it decides.

- **P1. The tenant.** That the CLI runs against `SUBDOMAIN.ghe.com` with `COPILOT_GH_HOST` and
  a token, with no interactive login. If it cannot, nothing here can be built for the tenant.
- **P2. A Copilot-only token.** Whether a fine-grained token with only Copilot Requests and
  no repository access can be created for a managed account on the tenant and works with the
  CLI; failing that, whether an organisation App's token does. This decides whether triage
  runs (section 4).
- **P3. The hosts.** Every host the CLI dials, recorded through the egress proxy in a logging
  mode, and whether it works with the telemetry host denied.
- **P4. The invocation.** Section 3's flags under `-p`, including `--session-id` with
  `--resume`, and `--max-ai-credits` (a preview), with the exit codes seen.
- **P5. Output.** The JSONL schema, whether stdout streams it event by event or only at the
  end, and whether the JSONL or `events.jsonl` reports the model that ran (the main agent's
  and any subagent's separately), tokens and credits per turn. Samples, scrubbed.
- **P6. Cost.** What a typical issuebot run costs in credits, measured on a small real issue
  with at least two models, so the README can suggest values for the caps.
- **P7. Running out.** What a refusal looks like when the budget is reached, and when a short
  rate limit is hit: its wording, its exit code, whether it names the limit and a retry time,
  whether a refused turn costs anything, and whether any model is substituted. A $0
  user-level budget on a test seat, if IT will set one, produces the budget case without
  spending.
- **P8. Reading the spend.** Several questions:
  - That the tenant bills in AI credits. The organisation's billing summary returned 404 in
    the spike.
  - Whether a user-level budget counts the seat holder's total use or only what was paid
    beyond the pool.
  - Whether the organisation's `ai_credit/usage` report can be filtered to one user and read
    with the web's billing token. Failing that, the fields of `/copilot_internal/user` with a
    seat.
  - Whether that read is a usable credential probe (section 3).
- **P9. The allowed models.** Where the list comes from outside an interactive session, with
  prices, and whether model ids have aliases that track the latest in a family.
- **P10. Confinement.** That `--no-custom-instructions` covers every instruction file, that
  the four built-in MCP servers are all off, that the workspace stays untrusted under
  `--allow-all-tools`, what `COPILOT_HOME` holds after a session, and any credential shape in
  the logs.
- **P11. Triage.** The read and search tools' names, whether they can be kept out of `/proc`,
  and confirmation that there is no schema-checked output mode.
- **P12. Subagents.** Whether the CLI can dispatch a subagent for self-review, and what one
  costs.
- **P13. Install.** A standalone binary or an npm package, the runtime it needs, release
  checksums, and that it runs as a non-root session account in the container behind the
  egress proxy.

## What to ask the company's IT

The maintainer forwards these, and the answers go into the probe's findings:

1. **A Copilot seat** for a dedicated managed bot account, and one for the maintainer, who
   runs the probe.
2. **Whether unattended use is allowed**: a bot account running the Copilot CLI on issues that
   colleagues file, with no person at the keyboard. GitHub's terms need a named person
   responsible for a machine account; that would be the maintainer.
3. **The budget**: the bot's user-level budget (reportedly $50 a month) and whether it can
   differ from a person's, since every colleague's issue draws on it. Also whether paid usage
   beyond the pool is allowed, and whether the bot's spend from the shared pool is acceptable,
   since it reduces what is left for everyone else before overage starts.
4. **Which models are enabled**, and whether the data-residency model restriction is on.
5. **Whether fine-grained tokens are allowed** in the enterprise with the Copilot Requests
   permission and no repository access (P2), or, failing that, an organisation App with
   Copilot Requests and "Allow use of Copilot CLI billed to the organization".
6. **That the allow-all flags are not disabled** by `permissions.disableBypassPermissionsMode`.
   If they are, sessions need an explicit allow-list of tools instead, which the plan would
   then have to design.
7. **Read access to the organisation's AI-credit usage** for the token the dashboard uses
   (P8).

## Shared with #274

Whichever of this and Codex is built first builds these, and the other reuses them:

- `TurnResult` carrying the agent, and an optional dollar cost.
- A usage hold, alert and limits tile that name their agent.
- The credential plumbing (`ENV_CREDENTIAL_NAMES`, `credential_complaint()`, the passthrough
  and protected prefixes) generalised per agent.
- Egress hosts chosen by agent.
- Turn capture and rendering for a non-Claude transcript, and the run's agent and model on the
  dashboard.

## What CI proves, and what needs a person

**CI** proves everything with a fake CLI, as the Claude runner's tests do today:

- the settings rules (section 1's refusals and the per-turn cap's floor);
- the runner's argv and environment, including that no Claude credential reaches the CLI, that
  `COPILOT_GITHUB_TOKEN` is the Copilot token and `GH_TOKEN` the repository token, and that
  nothing would trust the workspace;
- parsing the probe's recorded samples, and classifying its recorded failures;
- the model check;
- the hold and its reset on the 1st, the hour-long hold and its probe turn, the backstop at
  100% (and that a stale reading is ignored), the alert thresholds and the tile, from recorded
  readings;
- the token-reach check: triage off, and the refusal on an organisation administrator's
  account, when a recorded reach includes a repository;
- triage's verdict extraction;
- the opt-in image build and its assertions.

**A person** proves, on the tenant, once a seat exists:

1. `validate` is clean, including the allowed-models check and the egress probe.
2. One issue is claimed, worked, its pull request opened and merged, and the issue completes,
   on Copilot, with the run's cost shown.
3. A model label routes an issue to a second allowed model.
4. Triage runs on an issue, if P2 allowed it.
5. The hold is raised and lifted. This needs a seat whose budget can be spent, or a $0
   user-level budget on a test seat, or a reading faked at the snapshot, and the plan says
   which was used.

## Left to the builder

- The exact names of the settings, the environment variables and the template variable.
- Where the seat holder's login and any quota reading live in the snapshot, and the
  migration's shape.
- Whether triage on Copilot reuses `TriageRunner` through a composition of runner and agent,
  or subclasses per agent.
- How the transcript renderer is split between agents.

## Follow-ups (not filed)

- Copilot as a per-issue second agent in a Claude deployment, through #274's routing.
- A scheduled version-bump workflow for the CLI.
- A per-colleague view of what each person's issues cost, if the company wants one.
