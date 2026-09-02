---
github:
  repo: jleavers/issuebot
  token: $GH_TOKEN
polling:
  interval_ms: 30000
workspace:
  root: /workspaces
agent:
  max_concurrent_agents: 2
  max_turns: 5
  max_attempts: 3
claude:
  permission_mode: auto
  max_budget_usd: 5.0
notifications:
  slack:
    events: [state_changed, blocked]
---

You are working on GitHub issue `{{ issue.identifier }}`: {{ issue.title }}.

This body is a placeholder. The full workflow prompt lands in Phase 3
(see docs/superpowers/specs/2026-09-02-issuebot-phased-design.md).
