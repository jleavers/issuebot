"""The triage verdict: the shape a read-only session returns, and the parser that checks it.

A triage session (triage design, section 4) answers through claude's `structured_output`,
handed to it as `--json-schema`. The schema is a request, not a guarantee: the worker
re-checks every constraint it states, plus the ones a schema cannot express (a split has
steps, a fits has none, titles are distinct and one line), before anything reaches GitHub.
The agent never chooses labels; it names a *kind* per step and the worker maps that on.

Every string is scrubbed *after* decoding. A token written as JSON `\\uXXXX` escapes survives
a scrub of the raw text and only becomes the credential once `json.loads` has run, and on a
public repository the comment this feeds is public (spec section 5).

A `VerdictError` is one line and safe to publish: it names a field, never its content, since
it can end up in a GitHub comment and in Slack.
"""

from dataclasses import dataclass
from typing import Any, Literal

from issuebot.agent.scrub import Scrubber

TITLE_LIMIT = 200
BODY_LIMIT = 20_000
REASON_LIMIT = 2_000

_REASONS = ("summary", "fits_caps", "lands_fast", "needs_human", "ignored_requests")
_FIELDS = ("verdict", *_REASONS, "sub_issues")
_SUB_FIELDS = ("title", "body", "kind")
_KINDS = ("issuebot", "operator")


@dataclass(frozen=True, kw_only=True, slots=True)
class SubIssueDraft:
    title: str
    body: str
    kind: Literal["issuebot", "operator"]


@dataclass(frozen=True, kw_only=True, slots=True)
class TriageVerdict:
    verdict: Literal["fits", "split"]
    summary: str
    fits_caps: str
    lands_fast: str
    needs_human: str
    ignored_requests: str
    sub_issues: tuple[SubIssueDraft, ...]


class VerdictError(ValueError):
    """A verdict the worker refuses; the message is one line and safe to publish."""


def verdict_schema(max_sub_issues: int) -> dict[str, Any]:
    """The JSON Schema passed to claude as `--json-schema`."""
    reason = {"type": "string", "maxLength": REASON_LIMIT}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(_FIELDS),
        "properties": {
            "verdict": {"type": "string", "enum": ["fits", "split"]},
            **{name: dict(reason) for name in _REASONS},
            "sub_issues": {
                "type": "array",
                "maxItems": max_sub_issues,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(_SUB_FIELDS),
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": TITLE_LIMIT},
                        "body": {"type": "string", "minLength": 1, "maxLength": BODY_LIMIT},
                        "kind": {"type": "string", "enum": list(_KINDS)},
                    },
                },
            },
        },
    }


def _text(obj: dict[str, Any], name: str, limit: int, *, required: bool = False) -> str:
    value = obj[name]
    if not isinstance(value, str):
        raise VerdictError(f"{name} must be a string")
    value = value.strip()
    if len(value) > limit:
        raise VerdictError(f"{name} is longer than {limit} characters")
    if required and not value:
        raise VerdictError(f"{name} must not be empty")
    return value


def _fields(obj: object, names: tuple[str, ...], what: str, scrubber: Scrubber) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise VerdictError(f"{what} must be an object")
    for name in obj:
        if name not in names:
            # An unknown key is the agent's text and may be a credential: it is named only
            # when it is a short identifier, and then only after the scrubber has seen it.
            if not isinstance(name, str):
                raise VerdictError(f"unexpected field in {what}")
            shown = scrubber.scrub(name) if name.isidentifier() and len(name) <= 40 else ""
            raise VerdictError(
                f"unexpected field {shown} in {what}" if shown else f"unexpected field in {what}"
            )
    for name in names:
        if name not in obj:
            raise VerdictError(f"missing field {name} in {what}")
    return obj


def parse_verdict(raw: object, *, max_sub_issues: int, scrubber: Scrubber) -> TriageVerdict:
    """Validate `raw` (a decoded `structured_output`) and return it scrubbed."""
    if raw is None:
        raise VerdictError("the session returned no verdict")
    obj = _fields(raw, _FIELDS, "the verdict", scrubber)
    kind = obj["verdict"]
    if kind not in ("fits", "split"):
        raise VerdictError("verdict must be fits or split")
    reasons = {name: _text(obj, name, REASON_LIMIT) for name in _REASONS}
    steps = obj["sub_issues"]
    if not isinstance(steps, list):
        raise VerdictError("sub_issues must be an array")
    if len(steps) > max_sub_issues:
        raise VerdictError(f"a verdict may carry at most {max_sub_issues} sub-issues")
    if kind == "split" and not steps:
        raise VerdictError("a split needs at least one sub-issue")
    if kind == "fits" and steps:
        raise VerdictError("a fits verdict files no sub-issues")

    drafts: list[SubIssueDraft] = []
    seen: set[str] = set()
    for step in steps:
        item = _fields(step, _SUB_FIELDS, "a sub-issue", scrubber)
        title = _text(item, "title", TITLE_LIMIT, required=True)
        if len(title.splitlines()) != 1:
            raise VerdictError("a title is one line")
        body = _text(item, "body", BODY_LIMIT, required=True)
        if item["kind"] not in _KINDS:
            raise VerdictError("kind must be issuebot or operator")
        key = title.casefold()
        if key in seen:
            raise VerdictError("sub-issue titles must be distinct")
        seen.add(key)
        drafts.append(SubIssueDraft(title=title, body=body, kind=item["kind"]))

    # Scrub last, field by field: scrubbing can shorten a string but never lengthens it past
    # a limit already checked, and a title scrubbed to a mask is still a title.
    return TriageVerdict(
        verdict=kind,
        **{name: scrubber.scrub(text) for name, text in reasons.items()},
        sub_issues=tuple(
            SubIssueDraft(
                title=scrubber.scrub(d.title).strip(), body=scrubber.scrub(d.body), kind=d.kind
            )
            for d in drafts
        ),
    )
