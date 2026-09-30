"""The shipped workflow's Step 6 `--jq` programs, run with a real jq.

The text tests in ``test_workflow_default.py`` pin what the programs say; this one proves what
they do. jq's ``|`` binds loosest, so an unparenthesised ``select(.author_association | IN(...)
and .user.login != "x")`` reads ``.user`` from the association *string* and errors on any
maintainer's comment (GHSA-f3fm-r55f-2vgm review): a string match cannot see that.
"""

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issuebot.agent.prompt import PromptContext, PromptRenderer
from issuebot.config import load_workflow
from issuebot.github.models import Issue, LinkedPr, StateLabel

WORKFLOW = Path(__file__).resolve().parent.parent / "configs" / "WORKFLOW.md"
LOGIN = "issuebot-agent-1"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")


def _comment(id_: int, login: str, association: str) -> dict[str, object]:
    return {
        "id": id_,
        "user": {"login": login},
        "author_association": association,
        "html_url": f"u{id_}",
        "body": f"body {id_}",
        "path": "a.py",
        "line": 3,
        "state": "COMMENTED",
    }


PAGE = [
    _comment(1, "alice", "OWNER"),
    _comment(2, LOGIN, "COLLABORATOR"),
    _comment(3, "mallory", "NONE"),
]


def _rendered(make_issue: Callable[..., Issue], *, admin: bool = False) -> str:
    workflow = load_workflow(WORKFLOW, environ={"GH_TOKEN": "t"}, overlay=False)
    now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
    issue = make_issue(
        identifier="issuebot-42",
        state=StateLabel.IN_PROGRESS,
        state_labels=("issuebot/in-progress",),
        labels=("issuebot/in-progress",),
        body="Add a subtract function.",
        linked_pr=LinkedPr(
            number=51, url="https://github.com/o/r/pull/51", state="open", merged_at=None
        ),
        created_at=now,
        updated_at=now,
    )
    context = PromptContext(
        issue=issue,
        repo=workflow.config.github.repo,
        login=LOGIN,
        admin=admin,
        labels=workflow.config.github.labels,
        attempt=1,
        turn_number=1,
        max_turns=workflow.config.agent.max_turns,
        rework=True,
        self_review=workflow.config.agent.self_review,
    )
    return PromptRenderer(workflow.prompt_template).render(context)


def _programs(text: str) -> list[str]:
    """The five Step 6 item 2 programs: each `--jq '...'` on a backticked line of that item."""
    start = text.index("Gather feedback from every channel")
    end = text.index("\n3. ", start)
    programs = re.findall(r"--jq '(.*?)'(?:`|\.| )", text[start:end])
    return programs


def _run(program: str) -> list[dict[str, object]]:
    done = subprocess.run(
        ["jq", "-c", program],
        input=json.dumps(PAGE),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, (program, done.stderr)
    return [json.loads(line) for line in done.stdout.splitlines()]


def test_the_five_step_6_programs_run_and_filter(make_issue: Callable[..., Issue]) -> None:
    programs = _programs(_rendered(make_issue))
    assert len(programs) == 5
    for program in programs[:4]:
        results = _run(program)
        assert [item["author"] for item in results] == ["alice"], program
        assert [item["id"] for item in results] == [1], program
        assert results[0]["body"] == "body 1"
    quarantine = _run(programs[4])
    assert quarantine == [{"author": "mallory", "association": "NONE", "url": "u3"}]


def test_on_an_admin_account_the_own_row_is_kept(make_issue: Callable[..., Issue]) -> None:
    """The same five programs with the login term off: the maintainer's own row comes back."""
    programs = _programs(_rendered(make_issue, admin=True))
    assert len(programs) == 5
    for program in programs[:4]:
        assert [item["id"] for item in _run(program)] == [1, 2], program
    assert _run(programs[4]) == [{"author": "mallory", "association": "NONE", "url": "u3"}]
