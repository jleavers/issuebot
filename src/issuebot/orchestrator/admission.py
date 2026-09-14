"""The one gate every claim goes through: may this issue be dispatched, and on what budget?

#112. Claiming used to happen at two independent call sites -- the tick's sweep over the
candidates and the retry timer -- and each re-derived its preconditions inline, so the two
disagreed. The retry timer honoured the authentication and GitHub holds but could not see the
preflight one, which lived as a local inside ``tick``; and both read the attempt number off
the *live label*, so any move of that label handed the issue a fresh ``agent.max_attempts``.
A failure chain that a label move broke never reached the escape, which is how one issue
could be dispatched without limit while the worker reported that it was claiming nothing.

Everything here is pure. The orchestrator gathers the request from its own state and does
what the verdict says; nothing in this module reaches GitHub, the clock or the workflow.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from issuebot.github import ACTIVE_STATES, Issue
from issuebot.orchestrator.state import DispatchHoldKind, RetryKind

# How many issues the in-process ledger remembers. A worker lives for weeks and sees an
# unbounded number of issues, and this is a process's memory; eviction is a budget reset, so
# the cap is generous and the least recently *run* entry is the one that goes.
LEDGER_LIMIT = 1024

RefusalKind = Literal["stopping", "hold", "slots", "busy", "inactive", "attempts", "spend"]


@dataclass(frozen=True, slots=True)
class Hold:
    """Why this worker will not claim anything at all, before it reaches the snapshot.

    ``kind`` outranks by the order of ``DispatchHoldKind``; ``key`` is what makes two holds
    the same one when the wording is not, so a hold that lasts keeps its ``since``.
    """

    kind: DispatchHoldKind
    reason: str
    key: str | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class IssueLedger:
    """What one issue has cost this worker, across every label it has worn.

    ``failures`` is the chain ``agent.max_attempts`` bounds: worker sessions that failed, one
    after another, for this issue. Only a run that succeeded, or the blocked escape that ends
    the chain by handing the issue to a human, clears it. No label change does -- which is the
    whole point of keeping it here rather than reading it off the issue.

    ``runs``, ``turns`` and ``cost_usd`` are cumulative and are never cleared, so the gate can
    bound what one issue is allowed to spend however many times it is relabelled.

    ``reported_refusal`` is not history: it is the last refusal reason already logged for this
    issue, so a candidate the gate turns away on every tick says so once rather than every
    thirty seconds. It lives here because it is per-issue and must be forgotten exactly when
    the rest of the entry is.
    """

    failures: int = 0
    runs: int = 0
    turns: int = 0
    cost_usd: float = 0.0
    last_run_at: datetime | None = None
    reported_refusal: str | None = None

    @property
    def attempt(self) -> int:
        """The number of the run about to start: one more than the failures behind it."""
        return self.failures + 1


EMPTY_LEDGER = IssueLedger()
_EPOCH = datetime.min.replace(tzinfo=UTC)


def seeded_chain(failures: int, max_attempts: int) -> int:
    """What a chain read from before this process may contribute: one short of the ceiling.

    The escape that escalates a spent chain is something a *run* does, so a chain seeded at
    the ceiling would refuse the issue without ever escalating it -- and the readings this
    process did not take are approximations. The store infers the chain from run rows and
    ``blocked`` events, either of which a dropped write, a ``run-once`` session, or a worker
    killed before its escape retry fired can leave it without; ``session.json`` predates any
    change to ``agent.max_attempts``. Capping one short guarantees every issue gets a run that
    either succeeds or escalates it where a human can see it, which is the only safe direction
    for a reading this process cannot check.
    """
    return min(failures, max(max_attempts - 1, 0))


EvictionCallback = Callable[[str, IssueLedger], None]


class Ledger:
    """Per-issue history, keyed by ``Issue.identifier`` and bounded.

    The identifier rather than the node id, because that is the column the store records runs
    under: a worker with a database can be handed what actually happened before it restarted,
    the way ``initial_rate_limits`` already is, instead of starting every issue's budget again
    on a deployment. Restarting is how this worker is deployed.

    Insertion order is the eviction order: every write moves its entry to the end, so the one
    that goes when the cap is reached is the one this worker has not run for longest. A seed
    arrives in whatever order its reader produced -- the store's is newest first -- so it is
    sorted by ``last_run_at`` on the way in rather than trusted, since getting that backwards
    would evict the issues that ran minutes before the restart and keep the ones that have not
    run for months.
    """

    def __init__(
        self,
        entries: Mapping[str, IssueLedger] | None = None,
        *,
        limit: int = LEDGER_LIMIT,
        on_evict: EvictionCallback | None = None,
    ) -> None:
        self._entries: dict[str, IssueLedger] = {
            identifier: entry
            for identifier, entry in sorted(
                (entries or {}).items(), key=lambda item: item[1].last_run_at or _EPOCH
            )
        }
        self._limit = max(limit, 1)
        self._on_evict = on_evict
        self._trim()

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> Mapping[str, IssueLedger]:
        return dict(self._entries)

    def get(self, identifier: str) -> IssueLedger:
        """This issue's history; the empty one when there is none. Never inserts."""
        return self._entries.get(identifier, EMPTY_LEDGER)

    def dispatched(self, identifier: str, *, at: datetime) -> IssueLedger:
        """One more run for this issue, and it is now the most recently touched."""
        current = self.get(identifier)
        return self._put(
            identifier,
            replace(current, runs=current.runs + 1, last_run_at=at, reported_refusal=None),
        )

    def spent(self, identifier: str, *, turns: int, cost_usd: float) -> IssueLedger:
        """What the run that just ended used, whatever it ended as."""
        current = self.get(identifier)
        return self._put(
            identifier,
            replace(
                current,
                turns=current.turns + max(turns, 0),
                cost_usd=round(current.cost_usd + max(cost_usd, 0.0), 6),
            ),
        )

    def failed(self, identifier: str) -> IssueLedger:
        """One more failure on the chain; the caller compares it with ``agent.max_attempts``."""
        current = self.get(identifier)
        return self._put(identifier, replace(current, failures=current.failures + 1))

    def cleared(self, identifier: str) -> IssueLedger:
        """The chain is over: a run succeeded, or the escape handed the issue to a human.

        The cumulative figures stay. ``max_attempts`` bounds a chain of failures, and the
        README's documented recovery is to fix the cause and relabel; what must not reset the
        chain is the *label move on its own*, which is why this is the only thing that does.
        """
        current = self.get(identifier)
        if current.failures == 0 and current.reported_refusal is None:
            return current
        return self._put(identifier, replace(current, failures=0, reported_refusal=None))

    def observed(self, identifier: str, *, attempt: int, max_attempts: int) -> IssueLedger:
        """A floor from before this process: a session record says the issue was on ``attempt``.

        The workspace's ``session.json`` is durable per-issue history too, and the only kind a
        worker without a database has. It can only raise the chain, never lower it, and only
        as far as ``seeded_chain`` allows.
        """
        current = self.get(identifier)
        failures = seeded_chain(max(attempt - 1, 0), max_attempts)
        if failures <= current.failures:
            return current
        return self._put(identifier, replace(current, failures=failures))

    def refused(self, identifier: str, reason: str) -> bool:
        """Record that the gate turned this issue away; True when that is news.

        An issue with no history is not remembered and not reported: a refusal there is
        ``busy`` or ``inactive``, which is every tick's normal business.
        """
        if identifier not in self._entries:
            return False
        current = self._entries[identifier]
        if current.reported_refusal == reason:
            return False
        self._entries[identifier] = replace(current, reported_refusal=reason)
        return True

    def forget(self, identifier: str) -> None:
        """The issue reached a terminal state; a reopened one starts again from nothing."""
        self._entries.pop(identifier, None)

    def _put(self, identifier: str, entry: IssueLedger) -> IssueLedger:
        self._entries.pop(identifier, None)
        self._entries[identifier] = entry
        self._trim()
        return entry

    def _trim(self) -> None:
        while len(self._entries) > self._limit:
            identifier, entry = next(iter(self._entries.items()))
            del self._entries[identifier]
            if self._on_evict is not None:
                self._on_evict(identifier, entry)


# --- the gate ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class AdmissionRequest:
    """Everything the decision to claim depends on, gathered at one instant.

    ``issue`` is ``None`` for a caller that has not refreshed it yet. The retry timer asks
    first without one -- a worker that may not claim at all should not spend a request finding
    out which issue it may not claim -- and again with the issue in hand, which is also the
    second chance the awaited fetch makes necessary: a slot can go while it is in flight.
    Without an issue the answer covers the worker's own preconditions and stops there.
    """

    issue: Issue | None = None
    ledger: IssueLedger = EMPTY_LEDGER
    hold: Hold | None = None
    slots: int
    busy: bool = False
    stopping: bool = False
    max_attempts: int
    max_issue_cost_usd: float = 0.0


@dataclass(frozen=True, kw_only=True, slots=True)
class Admitted:
    """Claim it, on this attempt number. The number comes from the ledger, never the label."""

    attempt: int
    ledger: IssueLedger


@dataclass(frozen=True, kw_only=True, slots=True)
class Refused:
    """Do not claim it. ``wait`` is how a caller that can wait requeues; ``None`` is "do not".

    A refusal with no ``wait`` names something that will not change by waiting for it: the
    issue is somebody else's now, or it has spent what it was allowed to spend.
    """

    kind: RefusalKind
    reason: str
    wait: RetryKind | None = None


Admission = Admitted | Refused


def admit(request: AdmissionRequest) -> Admission:
    """The single admission decision, in the order the preconditions outrank each other.

    The order is the point. A worker-wide hold is asked before anything about the issue,
    because a held worker claims nothing whichever door the claim arrives at; slots before
    the issue's own budget, because a full worker is a "later", not a "no"; and the budget
    last, because it is the only answer that ends the issue's turn in the queue rather than
    postponing it -- and because acting on it means handing the issue to a human, which needs
    the issue.
    """
    ledger = request.ledger
    if request.stopping:
        return Refused(kind="stopping", reason="the worker is shutting down")
    hold = request.hold
    if hold is not None:
        return Refused(kind="hold", reason=hold.reason, wait=hold.kind)
    if request.slots <= 0:
        return Refused(kind="slots", reason="no available orchestrator slots", wait="slots")
    if request.busy:
        return Refused(kind="busy", reason="the issue is already running or waiting to retry")
    issue = request.issue
    if issue is None:
        # Everything above is about the worker and answers without one. The budget below is
        # about the issue, and a refusal on it is not something a caller that cannot see the
        # issue could act on -- it hands the issue over to a human, and that needs the issue.
        return Admitted(attempt=ledger.attempt, ledger=ledger)
    if not (issue.dispatchable and issue.state in ACTIVE_STATES):
        return Refused(kind="inactive", reason="the issue is not in a state this worker claims")
    if ledger.failures >= request.max_attempts:
        return Refused(
            kind="attempts",
            reason=(
                f"{ledger.failures} worker sessions have failed for this issue in a row and "
                f"agent.max_attempts is {request.max_attempts}"
            ),
        )
    cap = request.max_issue_cost_usd
    if cap > 0 and ledger.cost_usd >= cap:
        return Refused(
            kind="spend",
            reason=(
                f"this issue has cost ${ledger.cost_usd:.2f} over {ledger.runs} runs and "
                f"agent.max_issue_cost_usd is ${cap:.2f}"
            ),
        )
    return Admitted(attempt=ledger.attempt, ledger=ledger)
