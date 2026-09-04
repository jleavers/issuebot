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
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker (issuebot worker)
```

Slack notifications are optional: export `SLACK_WEBHOOK_URL` (an incoming webhook,
`https://hooks.slack.com/services/...`) and choose the event kinds in `WORKFLOW.md` under
`notifications.slack.events` (default `state_changed` and `blocked`; add `run_ended` for a
line per run with its cost). The worker reads both at start, so changing either needs a
restart; `validate` warns while the variable is unset.

The design lives in [`docs/superpowers/specs/`](docs/superpowers/specs/); start with
the phased design, then the per-phase specs and plans.
