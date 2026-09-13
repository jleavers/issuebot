"""Tests for the session-capture scrubber (hermetic, pure)."""

import pytest

from issuebot.agent.scrub import MIN_SECRET_LENGTH, REDACTED, Scrubber
from issuebot.config import Settings

TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab"
KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
OAUTH = "sk-ant-oat01-abcdefghijklmnopqrstuvwxyz0123456789"
WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXX"


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"github": {"repo": "acme/widgets"}}
    base.update(overrides)
    return Settings.model_validate(base)


# --- known values ----------------------------------------------------------------------------


def test_a_known_value_is_masked_wherever_it_appears() -> None:
    scrubber = Scrubber(secrets=["s3cretvalue!"])
    text = 'export X="s3cretvalue!"; echo s3cretvalue!s3cretvalue!'
    assert scrubber.scrub(text) == f'export X="{REDACTED}"; echo {REDACTED}{REDACTED}'


def test_a_short_known_value_is_left_alone() -> None:
    short = "x" * (MIN_SECRET_LENGTH - 1)
    assert Scrubber(secrets=[short]).scrub(f"{short} and {short}") == f"{short} and {short}"
    long = "x" * MIN_SECRET_LENGTH
    assert Scrubber(secrets=[long]).scrub(long) == REDACTED


def test_a_value_containing_another_is_masked_whole() -> None:
    scrubber = Scrubber(secrets=["innerpart", "outer-innerpart-end"])
    assert scrubber.scrub("outer-innerpart-end") == REDACTED


def test_an_empty_scrubber_masks_shapes_only() -> None:
    scrubber = Scrubber()
    assert scrubber.scrub("hello /home/alice world") == "hello /home/alice world"
    assert scrubber.scrub(f"token {TOKEN}") == f"token {REDACTED}"
    assert (scrubber.secrets, scrubber.home) == (0, False)


# --- shapes ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"GH_TOKEN={TOKEN}", f"GH_TOKEN={REDACTED}"),
        ("gho_" + "a" * 36, REDACTED),
        ("github_pat_11ABCDEFG0123456789abcdefghij_more", REDACTED),
        (f"key {KEY}.", f"key {REDACTED}."),
        (f"CLAUDE_CODE_OAUTH_TOKEN={OAUTH}", f"CLAUDE_CODE_OAUTH_TOKEN={REDACTED}"),
        (f"post to {WEBHOOK} now", f"post to https://hooks.slack.com/services/{REDACTED} now"),
        (
            "postgresql://issuebot:hunter22@db:5432/issuebot",
            f"postgresql://issuebot:{REDACTED}@db:5432/issuebot",
        ),
        ("postgresql://issuebot@db/issuebot", "postgresql://issuebot@db/issuebot"),
        ("https://user:p%40ss@host/", f"https://user:{REDACTED}@host/"),
        ("DATABASE_PASSWORD=abc PGPASSWD=def", f"DATABASE_PASSWORD={REDACTED} PGPASSWD={REDACTED}"),
        ("MY_API_KEY=abc my_secret=def", f"MY_API_KEY={REDACTED} my_secret={REDACTED}"),
        ("--token=abc ?access_token=zz&x=1", f"--token={REDACTED} ?access_token={REDACTED}&x=1"),
        (
            "MIN_SECRET_LENGTH=8 SECRET_NAME=x GH_TOKEN=",
            "MIN_SECRET_LENGTH=8 SECRET_NAME=x GH_TOKEN=",
        ),
        ("Authorization: Bearer abc.def-ghi", f"Authorization: Bearer {REDACTED}"),
        ('-H "authorization: token abc"', f'-H "authorization: token {REDACTED}"'),
        ("ghp_short and sk-ant-x", "ghp_short and sk-ant-x"),
    ],
)
def test_credential_shapes_are_masked(text: str, expected: str) -> None:
    assert Scrubber().scrub(text) == expected


def test_a_value_inside_a_json_string_stops_at_the_escape() -> None:
    """A tool result is one JSON line: its newlines are the two characters ``\\n``."""
    line = '{"text":"GH_TOKEN=abc\\nHOME=/home/alice\\n"}'
    assert Scrubber().scrub(line) == f'{{"text":"GH_TOKEN={REDACTED}\\nHOME=/home/alice\\n"}}'


# --- the home directory ----------------------------------------------------------------------


def test_the_home_directory_reads_tilde() -> None:
    scrubber = Scrubber(home="/home/alice/")
    assert scrubber.home is True
    text = "cwd /home/alice/ws/repo, HOME=/home/alice, not /home/alice2/x nor /home/alice-old"
    assert scrubber.scrub(text) == "cwd ~/ws/repo, HOME=~, not /home/alice2/x nor /home/alice-old"


def test_the_dashed_spelling_of_home_reads_tilde_too() -> None:
    scrubber = Scrubber(home="/home/alice")
    text = "/home/alice/.claude/projects/-home-alice-ws-repo/memory/ but -home-alicex"
    assert scrubber.scrub(text) == "~/.claude/projects/~-ws-repo/memory/ but -home-alicex"


@pytest.mark.parametrize("home", ["", "/", "relative", None])
def test_an_unusable_home_is_ignored(home: str | None) -> None:
    scrubber = Scrubber(home=home)
    assert scrubber.home is False
    assert scrubber.scrub("relative /") == "relative /"


# --- idempotence -----------------------------------------------------------------------------


def test_scrubbing_is_idempotent() -> None:
    scrubber = Scrubber(secrets=["s3cretvalue!"], home="/home/alice")
    text = "\n".join(
        [
            f"GH_TOKEN={TOKEN} s3cretvalue! {KEY} {WEBHOOK}",
            "postgresql://u:p@h/d /home/alice/x -home-alice-y Authorization: Bearer t",
        ]
    )
    once = scrubber.scrub(text)
    assert once != text
    assert scrubber.scrub(once) == once


# --- for_deployment --------------------------------------------------------------------------


def test_for_deployment_collects_the_settings_and_environment_secrets_and_home() -> None:
    config = settings(
        github={"repo": "acme/widgets", "token": "literal-token-value"},
        database={"url": "postgresql://issuebot:db%20passw0rd@db/issuebot"},
        notifications={"slack": {"webhook_url": WEBHOOK}},
    )
    environ = {
        "HOME": "/home/alice",
        "ANTHROPIC_API_KEY": "anthropic-key-value",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-value",
        "GH_TOKEN": "gh-token-value",
        "MY_PASSWORD": "my-password-value",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    scrubber = Scrubber.for_deployment(config, environ)
    assert scrubber.home is True
    text = (
        "literal-token-value db%20passw0rd db passw0rd anthropic-key-value oauth-token-value "
        "gh-token-value my-password-value /usr/bin:/bin C.UTF-8 /home/alice/ws"
    )
    assert scrubber.scrub(text) == " ".join([REDACTED] * 7 + ["/usr/bin:/bin", "C.UTF-8", "~/ws"])


def test_for_deployment_with_nothing_configured_still_masks_shapes() -> None:
    scrubber = Scrubber.for_deployment(settings(), {})
    assert (scrubber.secrets, scrubber.home) == (0, False)
    assert scrubber.scrub(f"{TOKEN} /home/alice") == f"{REDACTED} /home/alice"


def test_for_deployment_ignores_an_unparseable_database_url() -> None:
    config = settings(database={"url": "postgresql://[::1/issuebot"})
    assert Scrubber.for_deployment(config, {}).secrets == 0
