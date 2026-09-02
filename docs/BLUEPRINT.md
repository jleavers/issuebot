# Issuebot Blueprint

## Requirement

- Create a bespoke version of https://github.com/openai/symphony.
- Example Spec: https://github.com/openai/symphony/blob/main/SPEC.md
- Example implementation: https://github.com/openai/symphony/blob/main/elixir/README.md
- Goal: service monitors for new GitHub issues -> picks them up -> works on them autonomously -> opens PR for human review -> opens additional issues where required

### Differences to Symphony

- Use claude instead of codex, using claude -p and auto mode
- Use GitHub issues instead of Linear, using the gh CLI. Labels to be used on issues, e.g. 
    1. issuebot/todo: set by human
    2. issuebot/in-progress: set by agent when work ongoing
    3. issuebot/review: set by agent when PR opened
    4. issuebot/rework: set by human if PR needs more work
    5. issuebot/complete: set automatically when issue closed (by merge of linked PR)
- We *do* want a web dashboard: Kanban view of issue label tatus in columns, plus some hero stats, e.g. number of issues closed in 1 day/7 days, number of agents spun up, both point in time numbers and graph over time
- Slack notifications to channel when status changes
- Query: do we need a CLI to check status / resume following connectivity drop or similar error?

## Infrastructure guidelines

- Docker
- Python 3.14
- PostgreSQL
- GitHub CI: lint, tests, docker build, + dependabot
