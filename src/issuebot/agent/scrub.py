"""Scrub issuebot's own credentials and the deployment's identity out of captured session text.

Agent-session bytes leave the workspace through ``capture_turns`` alone, and everything it
returns has been through a ``Scrubber`` first: the database, the dashboard's raw views and
the committed fixture all see the scrubbed text and never the file. The scrubber knows three
things. The *values* issuebot holds -- ``github.token``, the ``database.url`` password, the
Slack webhook, whatever secret-named variable the worker's environment carries -- and masks
each wherever it appears, since issuebot put ``GH_TOKEN`` into the environment of the very
process whose stdout is captured and an agent inspecting ``env`` is one command away. The
*shapes* of the credentials it could hold -- a GitHub token, an Anthropic key, a Slack
webhook, a DSN password, an ``env`` line naming a secret, an ``Authorization`` header -- so a
credential issuebot never knew about (a hook's DSN, a key in the target repository's tests)
is masked by its look. And the *home directory*, which reads ``~`` afterwards, because every
absolute path an agent prints names the operator's account.

Scrubbing is idempotent: the mask matches no shape, so a scrubbed text scrubs to itself.
"""

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Self
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from issuebot.config import Settings

REDACTED = "***"
# A known value shorter than this is not masked: masking every "token" or "pass" in a
# transcript would mangle prose to protect a value no real credential is as short as.
MIN_SECRET_LENGTH = 8
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
    # A URL's userinfo password (`postgresql://user:password@host`).
    (
        re.compile(r"\b([a-z][a-z0-9+.-]*://[^\s/:@\"'\\]+:)([^\s/@\"'\\]+)(@)"),
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
# What may follow the home directory for the match to be the directory and not a sibling
# (`/home/alice` in `/home/alice/x` or `/home/alice`, never in `/home/alice2`).
_PATH_BOUNDARY = r"(?![A-Za-z0-9_.-])"
# Claude Code names a project directory by its cwd with `/` swapped for `-`
# (`~/.claude/projects/-home-alice-ws/`), so the home directory has a second spelling.
_DASHED_BOUNDARY = r"(?![A-Za-z0-9_.])"


class Scrubber:
    """Masks known values, credential shapes and the home directory in captured text."""

    def __init__(self, *, secrets: Iterable[str] = (), home: str | None = None) -> None:
        # Longest first, so a value that contains another is masked whole.
        self._secrets = tuple(
            sorted({s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True)
        )
        self._home: tuple[re.Pattern[str], ...] = ()
        home = (home or "").rstrip("/")
        if home.startswith("/") and len(home) > 1:
            self._home = (
                re.compile(re.escape(home) + _PATH_BOUNDARY),
                re.compile(re.escape(home.replace("/", "-")) + _DASHED_BOUNDARY),
            )

    @classmethod
    def for_deployment(cls, settings: Settings, environ: Mapping[str, str]) -> Self:
        """The scrubber for this worker: its settings' secrets, its environment's, its home."""
        secrets: list[str] = []
        if settings.github.token is not None:
            secrets.append(settings.github.token.get_secret_value())
        if settings.database.url is not None:
            secrets.extend(_url_password(settings.database.url.get_secret_value()))
        if settings.notifications.slack.webhook_url is not None:
            secrets.append(settings.notifications.slack.webhook_url.get_secret_value())
        secrets.extend(value for name, value in environ.items() if SECRET_NAME.search(name))
        return cls(secrets=secrets, home=environ.get("HOME"))

    @property
    def secrets(self) -> int:
        """How many known values are masked (what a log line may say; never which)."""
        return len(self._secrets)

    @property
    def home(self) -> bool:
        return bool(self._home)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern, replacement in _SHAPES:
            text = pattern.sub(replacement, text)
        for pattern in self._home:
            text = pattern.sub("~", text)
        return text


def _url_password(url: str) -> list[str]:
    """The password of a URL's userinfo as written and as decoded; nothing when it has none."""
    try:
        password = urlsplit(url).password
    except ValueError:
        return []
    if not password:
        return []
    return list({password, unquote(password)})
