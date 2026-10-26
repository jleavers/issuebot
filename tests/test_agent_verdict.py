"""Tests for the triage verdict: its schema and the parser that trusts neither side."""

import json

import pytest

from issuebot.agent.scrub import Scrubber
from issuebot.agent.verdict import VerdictError, parse_verdict, verdict_schema

SCRUB = Scrubber(secrets=["s3cret-token"])
GOOD_SPLIT = {
    "verdict": "split",
    "summary": "Too big.",
    "fits_caps": "No.",
    "lands_fast": "No.",
    "needs_human": "Yes: two designs.",
    "ignored_requests": "",
    "sub_issues": [
        {
            "title": "Brainstorm the feature",
            "body": "Do it.\n\n- [ ] spec committed",
            "kind": "operator",
        },
        {"title": "Write the plan", "body": "From the spec.", "kind": "issuebot"},
    ],
}


def test_parse_accepts_a_split() -> None:
    verdict = parse_verdict(GOOD_SPLIT, max_sub_issues=8, scrubber=SCRUB)
    assert verdict.verdict == "split"
    assert [d.kind for d in verdict.sub_issues] == ["operator", "issuebot"]


def test_parse_accepts_a_fits_with_no_sub_issues() -> None:
    raw = {**GOOD_SPLIT, "verdict": "fits", "sub_issues": []}
    verdict = parse_verdict(raw, max_sub_issues=8, scrubber=SCRUB)
    assert verdict.verdict == "fits"
    assert verdict.sub_issues == ()


@pytest.mark.parametrize(
    "change, message",
    [
        ({"verdict": "maybe"}, "verdict must be fits or split"),
        ({"sub_issues": []}, "a split needs at least one sub-issue"),
        ({"verdict": "fits"}, "a fits verdict files no sub-issues"),
        ({"sub_issues": [GOOD_SPLIT["sub_issues"][0]] * 2}, "sub-issue titles must be distinct"),
        ({"sub_issues": [{**GOOD_SPLIT["sub_issues"][0], "title": "a\nb"}]}, "a title is one line"),
        ({"sub_issues": [{**GOOD_SPLIT["sub_issues"][0], "title": "x" * 201}]}, "longer than 200"),
        (
            {"sub_issues": [{**GOOD_SPLIT["sub_issues"][0], "kind": "human"}]},
            "kind must be issuebot or operator",
        ),
        ({"summary": 7}, "summary must be a string"),
        ({"extra": "x"}, "unexpected field extra"),
    ],
)
def test_parse_refuses_malformed_verdicts(change: dict, message: str) -> None:
    with pytest.raises(VerdictError, match=message):
        parse_verdict({**GOOD_SPLIT, **change}, max_sub_issues=8, scrubber=SCRUB)


def test_parse_refuses_more_steps_than_the_cap() -> None:
    raw = {
        **GOOD_SPLIT,
        "sub_issues": [{"title": f"t{i}", "body": "b", "kind": "issuebot"} for i in range(4)],
    }
    with pytest.raises(VerdictError, match="at most 3 sub-issues"):
        parse_verdict(raw, max_sub_issues=3, scrubber=SCRUB)


def test_parse_refuses_a_missing_verdict() -> None:
    with pytest.raises(VerdictError, match="no verdict"):
        parse_verdict(None, max_sub_issues=8, scrubber=SCRUB)


def test_an_error_never_echoes_verdict_content() -> None:
    raw = {**GOOD_SPLIT, "sub_issues": [{"title": "leak\nme", "body": "b", "kind": "issuebot"}]}
    with pytest.raises(VerdictError) as info:
        parse_verdict(raw, max_sub_issues=8, scrubber=SCRUB)
    assert "leak" not in str(info.value)
    assert "\n" not in str(info.value)


def test_every_string_is_scrubbed_after_decoding() -> None:
    raw = json.loads(
        '{"verdict":"fits","summary":"key \\u0073\\u0033cret-token here",'
        '"fits_caps":"s3cret-token","lands_fast":"","needs_human":"",'
        '"ignored_requests":"","sub_issues":[]}'
    )
    verdict = parse_verdict(raw, max_sub_issues=8, scrubber=SCRUB)
    assert "s3cret-token" not in repr(verdict)


def test_sub_issue_strings_are_scrubbed_too() -> None:
    raw = {
        **GOOD_SPLIT,
        "sub_issues": [{"title": "use s3cret-token", "body": "s3cret-token", "kind": "issuebot"}],
    }
    verdict = parse_verdict(raw, max_sub_issues=8, scrubber=SCRUB)
    assert "s3cret-token" not in repr(verdict)


def test_the_schema_matches_the_parser() -> None:
    schema = verdict_schema(5)
    assert schema["properties"]["sub_issues"]["maxItems"] == 5
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "verdict",
        "summary",
        "fits_caps",
        "lands_fast",
        "needs_human",
        "ignored_requests",
        "sub_issues",
    }


def test_an_unknown_key_that_is_a_credential_is_not_echoed() -> None:
    for key in ("s3cret-token", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab"):
        with pytest.raises(VerdictError) as info:
            parse_verdict({**GOOD_SPLIT, key: "x"}, max_sub_issues=8, scrubber=SCRUB)
        assert key not in str(info.value)


def test_a_non_string_key_is_a_verdict_error() -> None:
    with pytest.raises(VerdictError):
        parse_verdict({**GOOD_SPLIT, 7: "x"}, max_sub_issues=8, scrubber=SCRUB)


# C1: what `gh api -f` hands `create_subprocess_exec` cannot hold a NUL, and what reaches its
# stdin cannot hold a lone surrogate. Either one raised `ValueError` past every `GitHubError`
# handler and stopped the worker, so the parser refuses both before anything is filed.
STEP = GOOD_SPLIT["sub_issues"][1]


@pytest.mark.parametrize(
    "change, message",
    [
        ({"sub_issues": [{**STEP, "body": "Do it.\x00"}]}, "body has a control character in it"),
        ({"sub_issues": [{**STEP, "body": "Do\x1b[2Jit."}]}, "body has a control character in it"),
        ({"summary": "Too\x00big."}, "summary has a control character in it"),
        ({"needs_human": "No\x7f."}, "needs_human has a control character in it"),
        ({"sub_issues": [{**STEP, "title": "Write\x00the plan"}]}, "title has a control"),
        ({"sub_issues": [{**STEP, "title": "Write\tthe plan"}]}, "title has a control"),
        ({"sub_issues": [{**STEP, "title": "Write\x9bthe plan"}]}, "title has a control"),
        ({"sub_issues": [{**STEP, "title": "Write \ud800"}]}, "title is not valid Unicode"),
        ({"sub_issues": [{**STEP, "body": "From \udc80 it."}]}, "body is not valid Unicode"),
        ({"lands_fast": "\udfff"}, "lands_fast is not valid Unicode"),
    ],
)
def test_parse_refuses_text_no_process_can_carry(change: dict, message: str) -> None:
    with pytest.raises(VerdictError, match=message) as info:
        parse_verdict({**GOOD_SPLIT, **change}, max_sub_issues=8, scrubber=SCRUB)
    # One line, and the field's name only: the offending character is never echoed.
    text = str(info.value)
    assert text.isprintable() and text.encode("utf-8")


def test_a_lone_surrogate_from_json_escapes_is_refused() -> None:
    raw = json.loads(json.dumps({**GOOD_SPLIT, "summary": "x"}).replace('"x"', '"\\ud83d"'))
    with pytest.raises(VerdictError, match="summary is not valid Unicode"):
        parse_verdict(raw, max_sub_issues=8, scrubber=SCRUB)


def test_newlines_and_tabs_in_bodies_and_reasons_are_kept() -> None:
    raw = {
        **GOOD_SPLIT,
        "summary": "One.\n\tTwo.",
        "sub_issues": [{**STEP, "body": "- [ ] a\n\t- [ ] b"}],
    }
    verdict = parse_verdict(raw, max_sub_issues=8, scrubber=SCRUB)
    assert verdict.summary == "One.\n\tTwo."
    assert verdict.sub_issues[0].body == "- [ ] a\n\t- [ ] b"
