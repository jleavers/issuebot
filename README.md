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
uv run issuebot labels ensure     # once per repository: creates the issuebot/* labels
uv run issuebot run-once 42       # one agent session for issue #42, in the foreground
uv run issuebot worker            # the long-running orchestrator; Ctrl-C stops it
cp .env.example .env              # then fill in GH_TOKEN and Claude auth
docker compose up --build         # postgres:18 + worker (issuebot worker)
```

The design lives in [`docs/superpowers/specs/`](docs/superpowers/specs/); start with
the phased design, then the per-phase specs and plans.
