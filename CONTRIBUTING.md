# Contributing

Thanks for looking. This is a personal project run by one maintainer, so the honest
expectation is: issues and small pull requests are welcome, and a large one is worth raising as
an issue first rather than writing on spec.

[`AGENTS.md`](AGENTS.md) is the binding set of rules in this repository, and it applies to
people as well as to agents. What follows is the practical version.

## Getting set up

[uv](https://docs.astral.sh/uv/) installs Python 3.14 for you; Docker with Compose is needed
for the container stack and for the database tests. On Windows, use WSL.

```bash
uv sync
uv run pytest                        # hermetic: no network, no Docker; the database tests skip
uv run ruff check . && uv run ruff format --check .
uv run pre-commit run --all-files    # what the lint job runs
```

The database tests skip unless `DATABASE_URL` is set. Point them at the throwaway, never at a
long-lived instance:

```bash
docker compose --profile test up -d --wait test-db
DATABASE_URL="postgresql://issuebot@$(docker compose port test-db 5432)/issuebot" uv run pytest
docker compose rm -sf test-db        # not `compose down`, which is project-wide
```

More on the layout and the day-to-day commands is in [`CLAUDE.md`](CLAUDE.md), which is written
for an agent working in this repository and is the closest thing to an architecture guide.

## Pull requests

Everything reaches `main` through a pull request. Without write access to this repository you
will be working from a fork, which comes to the same thing: push your branch there and open the
pull request across. With write access, push a branch here and open one — `main` carries a
ruleset that refuses a direct push, a force push and a deletion, so there is nothing to
remember.

Approvals are not required, because a single maintainer cannot approve their own pull request.
Review is a person reading the diff, not a button.

- Say what changed and why. A reviewer reading the diff alone should not have to guess the
  motivation.
- Keep the tests green, and add one for behaviour you change. Most of this repository's tests
  pin a decision rather than a line of code, and the comment explaining *why* is as much the
  point as the assertion.
- Prose in the documentation and in comments explains the reasoning, not just the mechanism.
  That is deliberate: much of this code exists because a subtler approach was wrong, and the
  note saying so is what stops it coming back.

## Two conventions worth knowing before you trip over them

**Everything executed from outside the tree is pinned to a commit digest, not a tag.** Every
`uses:` in `.github/workflows/` and every `rev:` in `.pre-commit-config.yaml` is a 40-character
commit with its tag in a comment beside it, and `tests/test_pins.py` fails a pull request that
uses a tag. A tag is a name its owner can repoint, and these run in CI and on any host that
runs `pre-commit`. Bump hooks with `uv run pre-commit autoupdate --freeze`.

**Images in the README are generated, not screenshotted by hand.** They are captured from a
dashboard serving fabricated data, so nobody's repository names or issue titles end up in a
public file. If a change moves the dashboard's layout, regenerate them:
[`tools/screenshots/README.md`](tools/screenshots/README.md).

## Reporting a security issue

Not here — see [`SECURITY.md`](SECURITY.md). Use GitHub's private vulnerability reporting
rather than a public issue, particularly because this repository's issues are read by an agent
that acts on them.

## Licence

Contributions are accepted under the [Apache License 2.0](LICENSE), the licence this project is
released under.
