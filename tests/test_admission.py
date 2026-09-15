"""Tests for the admission gate and the per-issue ledger it decides on (#112)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from issuebot.github import Issue, StateLabel
from issuebot.orchestrator.admission import (
    AdmissionRequest,
    Admitted,
    Hold,
    IssueLedger,
    Ledger,
    Refused,
    admit,
    seeded_chain,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def request(**overrides: object) -> AdmissionRequest:
    fields: dict[str, object] = {"slots": 1, "max_attempts": 3}
    fields.update(overrides)
    return AdmissionRequest(**fields)  # type: ignore[arg-type]


# --- the ledger ------------------------------------------------------------------------


def test_the_empty_ledger_puts_the_first_run_on_attempt_one() -> None:
    ledger = Ledger()
    assert len(ledger) == 0
    assert ledger.get("repo-42") == IssueLedger()
    assert ledger.get("repo-42").attempt == 1
    # `get` never inserts: asking about an issue is not history.
    assert len(ledger) == 0


def test_failures_accumulate_and_only_a_clear_resets_them() -> None:
    ledger = Ledger()
    assert ledger.failed("repo-42").failures == 1
    assert ledger.failed("repo-42").attempt == 3
    assert ledger.cleared("repo-42").failures == 0
    assert ledger.get("repo-42").attempt == 1


def test_the_cumulative_figures_survive_a_cleared_chain() -> None:
    """What ``max_attempts`` bounds resets; what the issue has cost never does."""
    ledger = Ledger()
    ledger.dispatched("repo-42", at=NOW)
    ledger.spent("repo-42", turns=3, cost_usd=1.5)
    ledger.failed("repo-42")
    ledger.dispatched("repo-42", at=NOW + timedelta(minutes=1))
    ledger.spent("repo-42", turns=2, cost_usd=0.75)
    entry = ledger.cleared("repo-42")
    assert (entry.failures, entry.runs, entry.turns, entry.cost_usd) == (0, 2, 5, 2.25)
    assert entry.last_run_at == NOW + timedelta(minutes=1)


def test_a_cleared_chain_that_is_already_clear_inserts_nothing() -> None:
    ledger = Ledger()
    assert ledger.cleared("repo-42").failures == 0
    assert len(ledger) == 0


def test_a_session_record_can_only_raise_the_chain() -> None:
    ledger = Ledger()
    assert ledger.observed("repo-42", attempt=3, max_attempts=5).failures == 2
    assert ledger.observed("repo-42", attempt=2, max_attempts=5).failures == 2
    assert ledger.observed("repo-42", attempt=1, max_attempts=5).failures == 2
    assert ledger.observed("repo-42", attempt=0, max_attempts=5).failures == 2


def test_a_session_record_is_capped_one_short_of_the_ceiling() -> None:
    """A record written before `agent.max_attempts` was lowered must not strand the issue."""
    ledger = Ledger()
    assert ledger.observed("repo-42", attempt=9, max_attempts=3).failures == 2
    assert ledger.observed("repo-42", attempt=9, max_attempts=1).failures == 2


@pytest.mark.parametrize(
    ("failures", "max_attempts", "expected"),
    [(0, 3, 0), (1, 3, 1), (2, 3, 2), (3, 3, 2), (99, 3, 2), (0, 1, 0), (5, 1, 0)],
)
def test_a_seeded_chain_stops_one_short_of_the_ceiling(
    failures: int, max_attempts: int, expected: int
) -> None:
    """History this process did not take must leave room for a run that can escalate."""
    assert seeded_chain(failures, max_attempts) == expected


def test_negative_spend_and_turns_are_ignored() -> None:
    ledger = Ledger()
    entry = ledger.spent("repo-42", turns=-4, cost_usd=-2.0)
    assert (entry.turns, entry.cost_usd) == (0, 0.0)


def test_a_terminal_issue_is_forgotten_so_a_reopened_one_starts_again() -> None:
    ledger = Ledger()
    ledger.failed("repo-42")
    ledger.forget("repo-42")
    assert ledger.get("repo-42") == IssueLedger()
    ledger.forget("repo-42")  # forgetting twice is not an error


def test_the_ledger_is_bounded_and_says_what_it_evicted() -> None:
    evicted: list[tuple[str, IssueLedger]] = []
    ledger = Ledger(limit=2, on_evict=lambda name, entry: evicted.append((name, entry)))
    ledger.failed("repo-1")
    ledger.failed("repo-2")
    ledger.failed("repo-3")
    assert [name for name, _ in evicted] == ["repo-1"]
    assert evicted[0][1].failures == 1
    assert len(ledger) == 2
    assert ledger.get("repo-1") == IssueLedger()


def test_a_write_makes_an_entry_the_most_recently_touched() -> None:
    evicted: list[str] = []
    ledger = Ledger(limit=2, on_evict=lambda name, _entry: evicted.append(name))
    ledger.failed("repo-1")
    ledger.failed("repo-2")
    ledger.failed("repo-1")  # repo-2 is now the oldest
    ledger.failed("repo-3")
    assert evicted == ["repo-2"]


def test_a_seeded_ledger_over_the_cap_keeps_the_most_recently_run() -> None:
    """The store answers newest first, and getting this backwards is a budget reset."""
    seed = {
        f"repo-{n}": IssueLedger(failures=1, last_run_at=NOW - timedelta(hours=n)) for n in range(5)
    }
    ledger = Ledger(seed, limit=2)
    assert set(ledger.entries()) == {"repo-0", "repo-1"}


def test_a_seeded_entry_with_no_run_time_is_the_first_to_go() -> None:
    ledger = Ledger(
        {"old": IssueLedger(failures=1), "new": IssueLedger(failures=1, last_run_at=NOW)},
        limit=1,
    )
    assert set(ledger.entries()) == {"new"}


def test_a_seed_is_evicted_before_a_run_this_process_saw() -> None:
    ledger = Ledger({"seeded": IssueLedger(failures=1, last_run_at=NOW)}, limit=2)
    ledger.dispatched("fresh", at=NOW + timedelta(hours=1))
    ledger.failed("newest")
    assert set(ledger.entries()) == {"fresh", "newest"}


def test_a_cleared_chain_makes_a_refusal_news_again() -> None:
    ledger = Ledger()
    ledger.failed("repo-42")
    assert ledger.refused("repo-42", "spent") is True
    ledger.cleared("repo-42")
    assert ledger.refused("repo-42", "spent") is True


def test_a_refusal_is_news_once_and_a_dispatch_makes_it_news_again() -> None:
    ledger = Ledger()
    ledger.failed("repo-42")
    assert ledger.refused("repo-42", "spent") is True
    assert ledger.refused("repo-42", "spent") is False
    assert ledger.refused("repo-42", "something else") is True
    ledger.dispatched("repo-42", at=NOW)
    assert ledger.refused("repo-42", "something else") is True


def test_an_escalation_is_announced_once_and_only_a_run_makes_it_news_again() -> None:
    """The conflict bounce moves the label and the escape moves it back; neither is a run.

    Nor is `cleared`, which the escape itself triggers -- so if that reset the mark, the very
    next bounce would announce the same escalation over again. An issue the ledger has never
    heard of answers `True` every time: that cannot happen (a refusal on either ceiling needs
    a figure only a run produces), and announcing is the safe direction if it ever did.
    """
    ledger = Ledger()
    ledger.spent("repo-42", turns=1, cost_usd=1.0)  # the run that put it over the ceiling
    assert ledger.escalate("repo-42") is True
    assert ledger.escalate("repo-42") is False
    ledger.cleared("repo-42")
    assert ledger.escalate("repo-42") is False
    ledger.dispatched("repo-42", at=NOW)
    assert ledger.escalate("repo-42") is True


def test_a_terminal_issue_forgets_it_was_escalated() -> None:
    """A reopened issue starts again from nothing, the mark included."""
    ledger = Ledger()
    ledger.spent("repo-42", turns=1, cost_usd=1.0)
    assert ledger.escalate("repo-42") is True
    assert ledger.get("repo-42").escalated is True
    ledger.forget("repo-42")
    ledger.spent("repo-42", turns=1, cost_usd=1.0)
    assert ledger.escalate("repo-42") is True


def test_marking_an_escalation_does_not_move_the_entry_down_the_eviction_queue() -> None:
    """Eviction is by least recently *run*, and an escalation is not a run."""
    ledger = Ledger(limit=2)
    ledger.dispatched("old", at=NOW)
    ledger.dispatched("new", at=NOW + timedelta(minutes=1))
    assert ledger.escalate("old") is True
    ledger.dispatched("newest", at=NOW + timedelta(minutes=2))
    assert sorted(ledger.entries()) == ["new", "newest"]  # `old` still went first


def test_a_refusal_on_an_issue_with_no_history_is_neither_kept_nor_reported() -> None:
    ledger = Ledger()
    assert ledger.refused("repo-42", "busy") is False
    assert len(ledger) == 0


# --- the gate --------------------------------------------------------------------------


def test_an_issue_with_a_clean_ledger_is_admitted_on_attempt_one(
    make_issue: Callable[..., Issue],
) -> None:
    verdict = admit(request(issue=make_issue()))
    assert isinstance(verdict, Admitted)
    assert verdict.attempt == 1


def test_the_attempt_number_comes_from_the_ledger_not_the_label(
    make_issue: Callable[..., Issue],
) -> None:
    """The whole point: the same issue, two labels, one answer."""
    ledger = IssueLedger(failures=2)
    for state, label in ((StateLabel.TODO, "todo"), (StateLabel.REWORK, "rework")):
        verdict = admit(
            request(
                ledger=ledger,
                issue=make_issue(state=state, state_labels=(f"issuebot/{label}",)),
            )
        )
        assert isinstance(verdict, Admitted)
        assert verdict.attempt == 3


def test_shutdown_outranks_everything() -> None:
    verdict = admit(request(stopping=True, slots=0, hold=Hold("auth", "no")))
    assert isinstance(verdict, Refused)
    assert verdict.kind == "stopping"
    assert verdict.wait is None


def test_a_hold_refuses_with_its_own_reason_and_kind() -> None:
    for kind in ("preflight", "auth", "github"):
        verdict = admit(request(hold=Hold(kind, f"{kind} is unhappy")))  # type: ignore[arg-type]
        assert isinstance(verdict, Refused)
        assert verdict.kind == "hold"
        assert verdict.reason == f"{kind} is unhappy"
        # The caller waits under the hold's own name, which is how `retrying` says which.
        assert verdict.wait == kind


def test_a_hold_outranks_the_slots_and_the_budget() -> None:
    verdict = admit(
        request(hold=Hold("preflight", "no claude"), slots=0, ledger=IssueLedger(failures=9))
    )
    assert isinstance(verdict, Refused)
    assert verdict.kind == "hold"


def test_no_slots_is_a_later_not_a_no() -> None:
    verdict = admit(request(slots=0))
    assert isinstance(verdict, Refused)
    assert (verdict.kind, verdict.wait) == ("slots", "slots")


def test_an_issue_already_claimed_is_refused_with_nothing_to_wait_for() -> None:
    verdict = admit(request(busy=True))
    assert isinstance(verdict, Refused)
    assert (verdict.kind, verdict.wait) == ("busy", None)


def test_an_issue_the_worker_does_not_claim_is_refused(
    make_issue: Callable[..., Issue],
) -> None:
    for issue in (
        make_issue(dispatchable=False),
        make_issue(state=StateLabel.REVIEW, state_labels=("issuebot/review",)),
        make_issue(state=None, state_labels=()),
    ):
        verdict = admit(request(issue=issue))
        assert isinstance(verdict, Refused)
        assert verdict.kind == "inactive"


def test_without_an_issue_the_answer_covers_the_worker_and_stops_there() -> None:
    """The retry timer asks before it refreshes; the worker's own checks still answer."""
    assert isinstance(admit(request(hold=Hold("github", "down"))), Refused)
    assert isinstance(admit(request(slots=0)), Refused)
    assert isinstance(admit(request(busy=True)), Refused)
    # The budget is about the issue, and a refusal on it hands the issue to a human, which
    # the caller cannot do without one. It asks again after the refresh.
    assert isinstance(admit(request(ledger=IssueLedger(failures=9))), Admitted)
    assert isinstance(
        admit(request(ledger=IssueLedger(cost_usd=99.0), max_issue_cost_usd=1.0)), Admitted
    )


def test_a_spent_attempt_budget_refuses_and_names_the_setting(
    make_issue: Callable[..., Issue],
) -> None:
    verdict = admit(request(issue=make_issue(), ledger=IssueLedger(failures=3), max_attempts=3))
    assert isinstance(verdict, Refused)
    assert (verdict.kind, verdict.wait) == ("attempts", None)
    assert "agent.max_attempts is 3" in verdict.reason


def test_a_chain_over_the_ceiling_still_refuses(make_issue: Callable[..., Issue]) -> None:
    verdict = admit(request(issue=make_issue(), ledger=IssueLedger(failures=7), max_attempts=3))
    assert isinstance(verdict, Refused)
    assert "7 worker sessions have failed" in verdict.reason


def test_the_spend_ceiling_is_off_by_default(make_issue: Callable[..., Issue]) -> None:
    verdict = admit(request(issue=make_issue(), ledger=IssueLedger(cost_usd=10_000.0)))
    assert isinstance(verdict, Admitted)


def test_a_spent_cost_budget_refuses_and_names_the_setting(
    make_issue: Callable[..., Issue],
) -> None:
    verdict = admit(
        request(
            issue=make_issue(), ledger=IssueLedger(cost_usd=12.5, runs=4), max_issue_cost_usd=10.0
        )
    )
    assert isinstance(verdict, Refused)
    assert (verdict.kind, verdict.wait) == ("spend", None)
    assert "$12.50 over 4 runs" in verdict.reason
    assert "agent.max_issue_cost_usd is $10.00" in verdict.reason


def test_the_attempt_budget_is_asked_before_the_spend_one(
    make_issue: Callable[..., Issue],
) -> None:
    verdict = admit(
        request(
            issue=make_issue(),
            ledger=IssueLedger(failures=3, cost_usd=99.0),
            max_issue_cost_usd=1.0,
        )
    )
    assert isinstance(verdict, Refused)
    assert verdict.kind == "attempts"
