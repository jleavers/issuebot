"""`tools/watch/watch.py`: the pure half, and the one claim its docstring makes.

That claim is that everything it prints goes through the scrubber. It matters because the
files it reads are the one place a credential is *not* masked on disk: `capture_turns` is the
scrubbing step for them (#79), and it runs on the way into the database, so a reader that goes
straight to the bytes is a second exit past the step that exists to stop exactly that. A
docstring saying so is not a guard; this is.

The tool is not a package -- `tools/` holds scripts an operator runs, like `tools/screenshots`
-- so it is loaded by path rather than imported.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from issuebot.agent.scrub import DEFAULT_SCRUBBER, Scrubber

WATCH_PATH = Path(__file__).resolve().parent.parent / "tools" / "watch" / "watch.py"
# A token-shaped value the scrubber's shapes catch wherever it appears, and a deployment secret
# only a `Scrubber` built with it would know to mask.
GH_TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
DEPLOYMENT_SECRET = "a-database-password-nobody-should-see"


def _load() -> ModuleType:
    name = "issuebot_tools_watch"
    spec = importlib.util.spec_from_file_location(name, WATCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed, because `@dataclass` resolves `cls.__module__` through
    # `sys.modules` to decide whether an annotation is `KW_ONLY`, and a module loaded by path
    # alone is not there yet -- which fails at import with an unrelated-looking AttributeError.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


watch = _load()


def _assistant(*blocks: dict[str, object]) -> str:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})


def test_a_token_in_a_tool_call_is_masked() -> None:
    """The commonest shape: a `Bash` command that echoes or exports the session's token."""
    line = _assistant(
        {"type": "tool_use", "name": "Bash", "input": {"command": f"curl -H 'x: {GH_TOKEN}' u"}}
    )
    rendered = [entry.detail for entry in watch.decode(line, DEFAULT_SCRUBBER)]
    assert rendered
    assert GH_TOKEN not in " ".join(rendered)


def test_a_token_in_the_agents_own_text_is_masked() -> None:
    line = _assistant({"type": "text", "text": f"I will use {GH_TOKEN} for this"})
    rendered = [entry.detail for entry in watch.decode(line, DEFAULT_SCRUBBER)]
    assert rendered
    assert GH_TOKEN not in " ".join(rendered)


def test_a_token_in_the_result_line_is_masked() -> None:
    line = json.dumps(
        {"type": "result", "subtype": "success", "is_error": True, "result": f"failed {GH_TOKEN}"}
    )
    rendered = [entry.detail for entry in watch.decode(line, DEFAULT_SCRUBBER)]
    assert rendered
    assert GH_TOKEN not in " ".join(rendered)


def test_the_deployments_own_secret_is_masked_when_its_scrubber_is_built() -> None:
    """The half the shapes cannot reach: a DSN password is not token-shaped, so only a
    `Scrubber` carrying the value masks it. That is why the tool builds the deployment's own
    where it can, and says in its header which one it got."""
    scrubber = Scrubber(secrets=[DEPLOYMENT_SECRET])
    line = _assistant(
        {"type": "tool_use", "name": "Bash", "input": {"command": f"psql {DEPLOYMENT_SECRET}"}}
    )
    rendered = [entry.detail for entry in watch.decode(line, scrubber)]
    assert rendered
    assert DEPLOYMENT_SECRET not in " ".join(rendered)
    # And the shapes alone would not have: the guard above is load-bearing rather than
    # incidental, which is the whole reason the tool reports which scrubber it built.
    bare = [entry.detail for entry in watch.decode(line, DEFAULT_SCRUBBER)]
    assert DEPLOYMENT_SECRET in " ".join(bare)


def test_a_partial_first_line_is_counted_rather_than_raised() -> None:
    """`tail -c` starts mid-line by construction, so this is the normal case, not an edge."""
    good = _assistant({"type": "text", "text": "carrying on"})
    lines = list(watch.decode('t":"assistant","message":{}}\n' + good, DEFAULT_SCRUBBER))
    assert any(entry.detail == "carrying on" for entry in lines)
    assert any(entry.kind == "~" and "not parsed" in entry.detail for entry in lines)


def test_a_tail_cut_inside_a_character_is_read_rather_than_raised(tmp_path: Path) -> None:
    """`tail -c` counts bytes, not characters, and claude writes its stream as raw UTF-8 -- a
    long turn log carries hundreds of `—` -- so under `--follow` the cut lands inside one sooner
    or later. A strict decode then raised in `Source.shell`, before `decode` ever saw the tail,
    and ended the watch with a traceback while the session carried on."""
    earlier = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "a — b"}]}},
        ensure_ascii=False,
    )
    good = _assistant({"type": "text", "text": "carrying on"})
    data = f"{earlier}\n{good}\n".encode()
    # One byte into the `—` (E2 80 94): the tail begins on a continuation byte.
    cut = data.index("—".encode()) + 1
    assert 0x80 <= data[cut] <= 0xBF
    log = tmp_path / "turn-1.jsonl"
    log.write_bytes(data)
    local = watch.Source(workspaces=str(tmp_path), service=None, project_directory=tmp_path)

    tail = watch.read_tail(local, str(log), len(data) - cut)

    lines = list(watch.decode(tail, DEFAULT_SCRUBBER))
    assert any(entry.detail == "carrying on" for entry in lines)
    assert any(entry.kind == "~" and "not parsed" in entry.detail for entry in lines)
    # The broken character sits in the partial first line, which is counted and dropped, so the
    # replacement never reaches the screen.
    assert not any("�" in entry.detail for entry in lines)


def test_a_routine_rate_limit_reading_is_not_drawn_but_a_refusal_is() -> None:
    """An `allowed` reading arrives every few tool calls and says nothing about the session;
    a refusal is the turn about to fail on the account's window (`usage_limited`)."""
    allowed = json.dumps(
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1}}
    )
    assert list(watch.decode(allowed, DEFAULT_SCRUBBER)) == []
    rejected = json.dumps(
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1}}
    )
    [line] = list(watch.decode(rejected, DEFAULT_SCRUBBER))
    assert line.kind == "limit"
    assert "rejected" in line.detail


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"description": "Do the thing", "command": "ls"}, "Do the thing"),
        ({"command": "ls -la"}, "ls -la"),
        ({"file_path": "/tmp/x"}, "/tmp/x"),
        ({"unknown": "shape"}, '{"unknown": "shape"}'),
        ("not a mapping", "not a mapping"),
    ],
)
def test_the_tool_detail_is_the_field_that_says_what_it_will_do(
    payload: object, expected: str
) -> None:
    assert watch._tool_detail("Bash", payload) == expected


def test_a_long_detail_is_clipped_to_the_terminal() -> None:
    detail = watch._tool_detail("Bash", {"command": "x" * 500})
    assert len(detail) == watch.DETAIL_WIDTH
    assert detail.endswith("…")
