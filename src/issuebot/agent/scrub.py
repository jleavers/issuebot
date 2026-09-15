"""Scrub issuebot's own credentials and the deployment's identity out of captured session text.

A run's turn files leave the workspace through ``capture_turns`` alone, and everything it
returns has been through a ``Scrubber`` first: the ``run_turns`` rows, the dashboard's raw
views and the committed fixture all see the scrubbed text and never the file. A failed
turn's ``error`` is built from claude's words too and takes another exit, to ``runs``, Slack
and the blocked-escape workpad block, so ``ClaudeRunner`` scrubs it at the source (#91). The
scrubber knows three things. The *values* issuebot holds -- ``github.token``, the
``database.url`` password, the Slack webhook, whatever secret-named variable the worker's
environment carries -- and masks each wherever it appears, since issuebot put ``GH_TOKEN``
into the environment of the very process whose stdout is captured and an agent inspecting
``env`` is one command away. The *shapes* of the credentials it could hold -- a GitHub
token, an Anthropic key, a Slack webhook, a DSN password, an ``env`` line naming a secret,
an ``Authorization`` header -- so a credential issuebot never knew about (a hook's DSN, a
key in the target repository's tests) is masked by its look. And the *home directory*,
which reads ``~`` afterwards, because every absolute path an agent prints names the
operator's account.

Scrubbing is idempotent: the mask matches no shape, so a scrubbed text scrubs to itself.
"""

import json
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Self

from issuebot.dsn import dsn_secrets

if TYPE_CHECKING:
    from issuebot.config import Settings

REDACTED = "***"
# A known value shorter than this is not masked: masking a word in every transcript would
# mangle prose to protect a value no real credential is as short as -- and a database
# password as short as the eight letters of `issuebot` (the compose default until #78 made
# the password a required per-deployment value) is still masked in DSN form by the DSN
# shape, without needing every label name and repository in the stream to go.
MIN_SECRET_LENGTH = 12
# A known value that is all digits is not masked either: a JSON number in the stream could
# equal it, and `***` in its place would break the line for every reader after. The shapes
# still mask it where it names itself (`PASSWORD=`, a DSN).
_DIGITS = re.compile(r"[0-9]+")
# A variable whose name ends this way holds a credential, wherever it is named.
SECRET_NAME = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)$", re.IGNORECASE)
# What follows a credential as far as one runs: to whitespace, a quote, or the backslash
# that starts a JSON escape (``\n`` inside a tool result is two characters on one line).
_VALUE = r"[^\s\"'\\]+"
# A parameter's value ends at `&` too, for the parameter after it.
_PARAM_VALUE = r"[^\s\"'\\&]+"
_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = (
    # GitHub tokens: classic (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`) and fine-grained.
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), REDACTED),
    # Anthropic API keys and Claude Code OAuth tokens (`sk-ant-oat01-...`).
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), REDACTED),
    # A Slack incoming webhook: the path is the credential.
    (re.compile(r"(https://hooks\.slack\.com/services/)[A-Za-z0-9/_-]+"), rf"\1{REDACTED}"),
    # A URL's userinfo password (`postgresql://user:password@host`; the user may be empty).
    # Possessive, and anchored on the scheme's first letter: a backtracking scheme re-scans
    # from every letter of a long `a.b-c` run, which is quadratic on a 64 KiB line.
    (
        re.compile(r"(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*+://[^\s/:@\"'\\]*+:)([^\s/@\"'\\]+)(@)"),
        rf"\1{REDACTED}\3",
    ),
    # `env` output, a shell assignment, a `--token=` flag or a query parameter naming a secret.
    (
        re.compile(
            rf"\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)=)({_PARAM_VALUE})",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}",
    ),
    # An Authorization header, however the scheme is spelt.
    (
        re.compile(rf"\b(authorization:\s*(?:bearer|token|basic)\s+)({_VALUE})", re.IGNORECASE),
        rf"\1{REDACTED}",
    ),
)
# What may surround the home directory for the match to be the directory and not a sibling
# or a suffix (`/home/alice` in `/home/alice/x` or `file:///home/alice`, never in
# `/home/alice2` or `/mnt/home/alice`).
_LEADING_BOUNDARY = r"(?<![A-Za-z0-9_.-])"
_PATH_BOUNDARY = r"(?![A-Za-z0-9_.-])"
# Claude Code names a project directory by its cwd with `/` swapped for `-`
# (`~/.claude/projects/-home-alice-ws/`), so the home directory has a second spelling
# (with `HOME=/root`, `-root` -- which `--root-dir` must not match).
_DASHED_BOUNDARY = r"(?![A-Za-z0-9_.])"
# A known value is masked as a whole word, so a `12345678` cannot eat the tail of a longer
# number, and in the spelling JSON gives it inside a stream line (`p\"ss` for `p"ss`).
_VALUE_BOUNDARY = r"(?<![0-9A-Za-z])", r"(?![0-9A-Za-z])"


class Scrubber:
    """Masks known values, credential shapes and the home directory in captured text."""

    def __init__(self, *, secrets: Iterable[str] = (), home: str | None = None) -> None:
        usable = {s for s in secrets if len(s) >= MIN_SECRET_LENGTH and not _DIGITS.fullmatch(s)}
        self._count = len(usable)
        spellings = usable | {json.dumps(s)[1:-1] for s in usable}
        # Longest first, so a value that contains another is masked whole.
        before, after = _VALUE_BOUNDARY
        self._secrets = tuple(
            re.compile(before + re.escape(s) + after)
            for s in sorted(spellings, key=len, reverse=True)
        )
        self._home: tuple[re.Pattern[str], ...] = ()
        home = (home or "").rstrip("/")
        if home.startswith("/") and len(home) > 1:
            self._home = (
                re.compile(_LEADING_BOUNDARY + re.escape(home) + _PATH_BOUNDARY),
                re.compile(
                    _LEADING_BOUNDARY + re.escape(home.replace("/", "-")) + _DASHED_BOUNDARY
                ),
            )

    @classmethod
    def for_deployment(cls, settings: Settings, environ: Mapping[str, str]) -> Self:
        """The scrubber for this worker: its settings' secrets, its environment's, its home."""
        secrets: list[str] = []
        if settings.github.token is not None:
            secrets.append(settings.github.token.get_secret_value())
        if settings.database.url is not None:
            secrets.extend(dsn_secrets(settings.database.url.get_secret_value()))
        if settings.notifications.slack.webhook_url is not None:
            secrets.append(settings.notifications.slack.webhook_url.get_secret_value())
        secrets.extend(value for name, value in environ.items() if SECRET_NAME.search(name))
        return cls(secrets=secrets, home=environ.get("HOME"))

    @property
    def secrets(self) -> int:
        """How many known values are masked (what a log line may say; never which)."""
        return self._count

    @property
    def home(self) -> bool:
        return bool(self._home)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = secret.sub(REDACTED, text)
        for pattern, replacement in _SHAPES:
            text = pattern.sub(replacement, text)
        for pattern in self._home:
            text = pattern.sub("~", text)
        return text


# The scrubber a reader gets without a deployment: the credential shapes alone. It is what
# `capture_turns`, `classify_result`, the sink and the orchestrator default to, so no caller can
# get raw text back; the worker replaces it with `for_deployment`, which knows the values too.
DEFAULT_SCRUBBER = Scrubber()
