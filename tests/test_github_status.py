"""Tests for the githubstatus.com reader: total parsing, and a fetch that never raises (#88)."""

import json
from typing import Any

import pytest

from issuebot.github.status import (
    MAX_DETAIL_CHARS,
    MAX_NAMED_COMPONENTS,
    SUMMARY_URL,
    fetch_status_summary,
    parse_status_summary,
)


def summary(**overrides: Any) -> str:
    document: dict[str, Any] = {
        "page": {"name": "GitHub"},
        "status": {"indicator": "none", "description": "All Systems Operational"},
        "components": [
            {"id": "a", "name": "Git Operations", "status": "operational", "group": False},
            {"id": "b", "name": "Pull Requests", "status": "operational", "group": False},
        ],
        "incidents": [],
        "scheduled_maintenances": [],
    }
    document.update(overrides)
    return json.dumps(document)


# --- parsing ---------------------------------------------------------------------


def test_an_operational_page_reads_as_operational() -> None:
    status = parse_status_summary(summary())
    assert status is not None
    assert status.operational
    assert status.detail == "All Systems Operational"


def test_an_impaired_component_is_named_the_way_an_operator_would_say_it() -> None:
    status = parse_status_summary(
        summary(
            status={"indicator": "major", "description": "Partial System Outage"},
            components=[
                {"name": "Git Operations", "status": "operational", "group": False},
                {"name": "Pull Requests", "status": "major_outage", "group": False},
                {"name": "Actions", "status": "degraded_performance", "group": False},
            ],
        )
    )
    assert status is not None
    assert not status.operational
    assert status.detail == "Pull Requests, major outage; Actions, degraded performance"


def test_a_group_row_is_skipped_so_one_outage_is_not_named_twice() -> None:
    """Statuspage mirrors a group's worst child into the group."""
    status = parse_status_summary(
        summary(
            status={"indicator": "major", "description": "Partial System Outage"},
            components=[
                {"name": "GitHub", "status": "major_outage", "group": True},
                {"name": "Pull Requests", "status": "major_outage", "group": False},
            ],
        )
    )
    assert status is not None
    assert status.impaired == ("Pull Requests, major outage",)


def test_incidents_answer_when_no_component_is_marked() -> None:
    status = parse_status_summary(
        summary(
            status={"indicator": "minor", "description": "Minor Service Outage"},
            incidents=[{"name": "Incident with Codespaces", "impact": "minor"}],
        )
    )
    assert status is not None
    assert not status.operational
    assert status.detail == "Incident with Codespaces"


def test_components_alone_when_both_are_present() -> None:
    """ "Pull Requests, major outage" is the more useful half of the two GitHub gives."""
    status = parse_status_summary(
        summary(
            status={"indicator": "major", "description": "Partial System Outage"},
            components=[{"name": "Pull Requests", "status": "major_outage", "group": False}],
            incidents=[{"name": "Incident with Pull Requests", "impact": "major"}],
        )
    )
    assert status is not None
    assert status.detail == "Pull Requests, major outage"
    assert status.incidents == ("Incident with Pull Requests",)


def test_an_unresolved_incident_alone_is_not_operational() -> None:
    """The page can carry an incident before any component has been marked."""
    status = parse_status_summary(
        summary(incidents=[{"name": "Incident with Actions", "impact": "none"}])
    )
    assert status is not None
    assert not status.operational


def test_the_named_components_and_the_detail_are_both_bounded() -> None:
    """A third party's body reaches a log line, a jsonb column and an HTML page."""
    components = [
        {"name": f"Component {index} with a long name", "status": "major_outage", "group": False}
        for index in range(20)
    ]
    status = parse_status_summary(summary(components=components))
    assert status is not None
    assert len(status.impaired) == MAX_NAMED_COMPONENTS
    assert len(status.detail) <= MAX_DETAIL_CHARS
    assert status.detail.endswith("…")


@pytest.mark.parametrize(
    "payload",
    [None, "", "not json", "[]", '"a string"', "null", "{}", '{"components": "not a list"}'],
)
def test_anything_unusable_reads_as_no_answer(payload: str | None) -> None:
    """The shape is Statuspage's and can change without issuebot being told."""
    assert parse_status_summary(payload) is None


def test_a_page_with_only_a_description_is_still_a_reading() -> None:
    document = '{"status": {"description": "All Systems Operational"}}'
    assert parse_status_summary(document) is not None


def test_a_missing_description_does_not_leave_the_detail_empty() -> None:
    status = parse_status_summary('{"status": {"indicator": "major"}}')
    assert status is not None
    assert status.detail == "status unknown"


def test_components_that_are_not_mappings_are_ignored() -> None:
    status = parse_status_summary(
        summary(components=["nonsense", 7, {"name": "Pages", "status": "partial_outage"}])
    )
    assert status is not None
    assert status.impaired == ("Pages, partial outage",)


# --- fetching --------------------------------------------------------------------


def test_a_fetch_that_cannot_connect_answers_none_rather_than_raising() -> None:
    """Fail open: a status page that cannot answer must not break the caller that asked."""
    assert fetch_status_summary("http://127.0.0.1:1/summary.json", timeout_s=0.5) is None


@pytest.mark.parametrize("url", ["not a url", "file:///etc/hostname", "", "ftp://example.invalid/"])
def test_a_url_that_is_not_fetchable_answers_none(url: str) -> None:
    assert fetch_status_summary(url, timeout_s=0.5) is None


def test_the_summary_url_is_the_statuspage_one() -> None:
    assert SUMMARY_URL == "https://www.githubstatus.com/api/v2/summary.json"
