# issuebot

Bespoke version of https://github.com/openai/symphony using Claude and GitHub.

Contributors and AI agents must follow the rules in [`AGENTS.md`](AGENTS.md).

## Development

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 for you) and,
for the container stack, Docker with Compose.

```bash
uv sync
uv run pytest
uv run issuebot validate          # checks ./WORKFLOW.md and the environment
uv run issuebot validate --slack-probe   # same, plus one test message to the Slack webhook
uv run issuebot labels ensure     # once per repository: creates the issuebot/* labels
uv run issuebot run-once 42       # one agent session for issue #42, in the foreground
uv run issuebot worker            # the long-running orchestrator; Ctrl-C stops it
uv run issuebot migrate           # apply the database migrations (worker does this at start)
uv run issuebot status            # what the worker was doing at its last tick
uv run issuebot stats             # issues closed and agents run: last day, week, per day
uv run issuebot refresh           # make a running worker poll GitHub now
uv run issuebot web               # the dashboard and its JSON API (needs DATABASE_URL)
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker + web (http://127.0.0.1:8080)
```

History is optional: with `DATABASE_URL` set (compose sets it for the worker; on the host
export `postgresql://issuebot:issuebot@127.0.0.1:${ISSUEBOT_DB_PORT:-5432}/issuebot` after
`docker compose up -d db`) the worker records every event, run and issue snapshot in
PostgreSQL and `status`, `stats` and `refresh` work; without it the worker runs exactly as
before. The worker applies pending migrations when it starts and fails fast if the database
is configured but unreachable; `validate` reports the schema version. The tests that need a
database read `DATABASE_URL` and are skipped when it is unset.

The dashboard (`issuebot web`; the compose `web` service publishes it on the host's loopback
at `ISSUEBOT_WEB_PORT`, default 8080) shows the Kanban of the five label columns, the hero
stats, two 30-day charts, the running agents and, per issue, its runs with the transcript of
every captured turn; `/api/v1/state`, `/api/v1/issues/<n>`, `/api/v1/stats?window=7d` and
`POST /api/v1/refresh` serve the same as JSON and `/healthz` reports the database and the age
of the worker's last report. It needs `DATABASE_URL` and nothing else, reads `WORKFLOW.md`
once at start, and has no authentication: keep it on loopback (`server.bind: 127.0.0.1` outside
Docker) or behind a reverse proxy. Turn logs are captured into the database when a run ends,
so they outlive the workspace.

Slack notifications are optional: export `SLACK_WEBHOOK_URL` (an incoming webhook,
`https://hooks.slack.com/services/...`) and choose the event kinds in `WORKFLOW.md` under
`notifications.slack.events` (default `state_changed` and `blocked`; add `run_ended` for a
line per run with its cost). The worker reads both at start, so changing either needs a
restart; `validate` warns while the variable is unset.

The design lives in [`docs/superpowers/specs/`](docs/superpowers/specs/); start with
the phased design, then the per-phase specs and plans.
