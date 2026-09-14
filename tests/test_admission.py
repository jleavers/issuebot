"""Tests for the admission gate and the per-issue ledger it decides on (#112)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from issuebot.github import Issue, StateLabel
from issuebot.orchestrator.admission import (
    AdmissionRequest,
    Admitted,
    Hold,
    IssueLedger,
    Ledger,
    Refused,
    admit,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def request(**overrides: object) -> AdmissionRequest:
    fields: dict[str, object] = {"identifier": "repo-42", "slots": 1, "max_attempts": 3}
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
    assert ledger.observed("repo-42", attempt=3).failures == 2
    assert ledger.observed("repo-42", attempt=2).failures == 2
    assert ledger.observed("repo-42", attempt=1).failures == 2
    assert ledger.observed("repo-42", attempt=0).failures == 2


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


def test_a_seeded_ledger_over_the_cap_is_trimmed_at_construction() -> None:
    seed = {f"repo-{n}": IssueLedger(failures=1) for n in range(5)}
    ledger = Ledger(seed, limit=2)
    assert len(ledger) == 2


def test_a_refusal_is_news_once_and_a_dispatch_makes_it_news_again() -> None:
    ledger = Ledger()
    ledger.failed("repo-42")
    assert ledger.refused("repo-42", "spent") is True
    assert ledger.refused("repo-42", "spent") is False
    assert ledger.refused("repo-42", "something else") is True
    ledger.dispatched("repo-42", at=NOW)
    assert ledger.refused("repo-42", "something else") is True


def test_a_refusal_on_an_issue_with_no_history_is_not_remembered() -> None:
    ledger = Ledger()
    assert ledger.refused("repo-42", "busy") is True
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


def test_the_issue_is_not_needed_for_the_checks_that_do_not_need_it() -> None:
    """The retry timer asks before it refreshes; every earlier check still answers."""
    assert isinstance(admit(request(hold=Hold("github", "down"))), Refused)
    assert isinstance(admit(request(slots=0)), Refused)
    # Nothing about the issue refuses an issue nobody has handed over yet.
    assert isinstance(admit(request()), Admitted)


def test_a_spent_attempt_budget_refuses_and_names_the_setting() -> None:
    verdict = admit(request(ledger=IssueLedger(failures=3), max_attempts=3))
    assert isinstance(verdict, Refused)
    assert (verdict.kind, verdict.wait) == ("attempts", None)
    assert "agent.max_attempts is 3" in verdict.reason


def test_a_seeded_chain_over_the_ceiling_still_refuses() -> None:
    verdict = admit(request(ledger=IssueLedger(failures=7), max_attempts=3))
    assert isinstance(verdict, Refused)
    assert "7 worker sessions have failed" in verdict.reason


def test_the_spend_ceiling_is_off_by_default() -> None:
    verdict = admit(request(ledger=IssueLedger(cost_usd=10_000.0)))
    assert isinstance(verdict, Admitted)


def test_a_spent_cost_budget_refuses_and_names_the_setting() -> None:
    verdict = admit(request(ledger=IssueLedger(cost_usd=12.5, runs=4), max_issue_cost_usd=10.0))
    assert isinstance(verdict, Refused)
    assert (verdict.kind, verdict.wait) == ("spend", None)
    assert "$12.50 over 4 runs" in verdict.reason
    assert "agent.max_issue_cost_usd is $10.00" in verdict.reason


def test_the_attempt_budget_is_asked_before_the_spend_one() -> None:
    verdict = admit(request(ledger=IssueLedger(failures=3, cost_usd=99.0), max_issue_cost_usd=1.0))
    assert isinstance(verdict, Refused)
    assert verdict.kind == "attempts"
