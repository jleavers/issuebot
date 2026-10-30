# The board poll in one GraphQL query (#362)

Date: 2026-10-30
Status: step 1 of 2 of #362 (this is #363, an operator step); #364 plans and builds from it

## Why

The board poll, `GhCliAdapter.fetch_issues_by_states` (`src/issuebot/github/ghcli.py`), sends
one GraphQL query per state label: `_collect` loops over the roles and calls
`_issues_with_label` once for each, with `labels: [$label]`. A worker with a store polls the
four `OBSERVED_STATES` (`todo`, `in-progress`, `rework`, and `review`, which is read for the
store and the conflict bounce), so every poll costs at least 4 points -- 480 an hour at the
default `polling.interval_ms` of 30 s, out of the 5,000 an hour that every worker on one
account shares (`docs/operations.md`).

GitHub's `labels:` filter on `repository.issues` matches an issue carrying *any* of the labels
given. One query naming every polled state label therefore returns the same issues for 1 point
a page of 33. That takes an idle worker from about 770 points an hour to about 410 (section 6).

## 1. Decision: one page ceiling over the union, not scaled

`MAX_ISSUE_PAGES` (30 pages of `ISSUE_PAGE_SIZE`, 33 issues: 990) applies to the polled state
labels **together**, not to each label. The ceiling is not multiplied by the number of labels,
and no new constant is added.

Why:

- The ceiling exists to bound a read that anyone who can get an issue labelled can grow
  (#139), at a size no board reaches by working. 990 open issues across the board's state
  labels together is still such a size: the operator confirmed on 2026-10-30 that no
  deployment comes anywhere near a thousand open issues.
- It is the choice phase 5 already made. `fetch_issues_with_any_label`, the triage-track read,
  applies the same single `MAX_ISSUE_PAGES` to the union of its two labels.
- Scaling it (`30 * len(labels)` pages) would keep a number that no longer means anything per
  label. One label could hold 3,900 issues while the other three sat empty. It would also keep
  the poll's worst case at 120 points, where the combined ceiling makes it 30.

What changes for a deployment: a board that used to pass the ceiling label by label, say 600
issues in `todo` and 500 in `review`, now fails the poll instead. That is accepted. The failure
is the same as today's: `PageCeilingError` ("more than 990 issues carry issuebot/todo or
issuebot/in-progress or ..."), which `_fetch_issues` counts towards the `github` dispatch hold
(#88), and which `issuebot status`, `/healthz` and the dashboard then report.

## 2. Decision: no ordering against triage phase 5

Phase 5 merged in #365. `fetch_issues_with_any_label` is on `main`, and so is
`_issues_query(states, labels)`, which takes numbered `$labelN: String!` variables because
`_graphql` sends only strings and ints. This work builds on both and does not add a second
helper. The parent issue's question "wait for phase 5, or introduce the helper here" no longer
arises.

## 3. The shape

These are my calls, not the operator's; the builder may refine how they are expressed but not
what they guarantee.

- **`fetch_issues_by_states(states)` keeps its signature**, and so does the `GitHubAdapter`
  protocol. The orchestrator, `fetch_states`, `OBSERVED_STATES` and `CANDIDATE_STATES` do not
  change. It maps the roles, deduplicated in order as today, to their configured names
  (`label_name`), and reads them in one query through the same path as
  `fetch_issues_with_any_label`. One label uses the plain `$label` query, as
  `fetch_issues_with_any_label` already does for a single name. Several labels use
  `_issues_query("OPEN", n)`. No roles still means no request.
- **`issuebot issues list` changes with it.** It is the method's other caller
  (`cmd_issues_list`, `src/issuebot/cli.py`). Without `--state` it passes all five roles,
  including `complete`, which an open issue rarely carries. So it also becomes one query under
  the same combined ceiling, and past the ceiling it still prints `[FAIL] issues: ...`. With
  `--state` it passes one role and sends the plain `$label` query, exactly as today.
- **Deduplicated by number and sorted by `(created_at, number)` in code**, not left to GitHub's
  order. The live check (section 7) found GitHub's order matched, but it also saw issues
  created in the same second, and two such pairs are not a guarantee. The builder decides where
  the sort lives. Putting it in the shared path would make `fetch_issues_with_any_label`'s
  docstring ("each once, oldest first") enforced rather than assumed.
- **`_collect` belongs to the terminal sweep alone.** Its `per_role` flag loses its only
  `False` caller, so remove it and have `_collect` always skip a role that is over its ceiling.
  The poll's all-or-nothing reasoning in its docstring ("four roles are not a board") moves to
  `fetch_issues_by_states`. There it describes one read, which can only fail whole.
- **`MAX_ISSUE_PAGES`'s comment is rewritten.** The poll reads issues carrying any polled state
  label, once per tick, not once per role. The comment's "a thousand open issues under one
  state label" becomes "across the board's state labels together". The constant stays shared
  with `fetch_issues_with_label`, `fetch_issues_with_any_label` and `fetch_issues_opened_since`.
  The `MAX_TERMINAL_PAGES` comment beside it explains the poll's failure as "all-or-nothing
  because four roles are not a board", so reword it the same way: the poll now fails all at
  once because it is one read.

## 4. What stays unchanged

- **The board poll fails the whole read past the ceiling** (#139). It is now one read, so it
  could not do anything else.
- **The terminal sweep stays one query per label.** `fetch_terminal_issues` still goes through
  `_collect` with `CLOSED_ISSUES_QUERY` and `MAX_TERMINAL_PAGES`. A role over its ceiling is
  still an `issue_role_skipped` warning while the others are swept, so one overgrown label
  cannot stop the rest closing out.
- **Results are deduplicated and sorted oldest first by `(created_at, number)`.** An issue
  carrying two state labels still comes back once, with `state` `None`. That is the conflict
  case `test_fetch_by_states_paginates_merges_and_sorts` already pins as issue 43.
- **`FakeGitHub.fetch_issues_by_states` is unchanged.** It already returns the union, recorded
  as one call. The orchestrator tests that count calls count the fake's calls, which are
  already one per tick, so they do not change. The "three fewer" calls #362 mentions are the
  adapter tests' `runner.calls` in `tests/test_github_ghcli.py`.
- **The triage reads stay separate queries.** Dispatch's `triage-todo` read
  (`fetch_issues_with_label`) and the triage-track read (`fetch_issues_with_any_label`) are
  untouched.

## 5. Non-goals

- **Folding the triage labels into the board's query.** `triage-complete` stays on every open
  issue that has been triaged, so it grows with triage volume: 15 open on this repository
  today, against 3 across the four state labels. Inside the board's query those issues would
  count towards a ceiling whose failure holds dispatch. The track read's failures are kept out
  of the `github` hold deliberately (`2026-10-25-triage-design.md`, section 7, "Decided while
  planning phase 5", item 5), and that would undo it. #366 folds the `triage-todo` read into the track read,
  separately.
- **Changing the terminal sweep, `MAX_TERMINAL_PAGES`, `ISSUE_PAGE_SIZE` or `ISSUE_FIELDS`.**

## 6. Documentation

- **`docs/operations.md`**, the paragraph that begins "Tokens that belong to one GitHub account
  share one budget". Recompute it as follows:
  - **Idle cost:** 120 polls of three points (one for the four state labels together, one for
    the triage-candidates read, one for the triage-track read), plus about 50 for the terminal
    sweep, makes about 410 an hour.
  - **The page condition:** "while no label holds more than 33 open issues" becomes "while the
    four state labels together hold no more than 33 open issues", alongside the existing
    sentence about the triage union.
  - **The maximum:** `triage.auto`'s 30 and sessions' 120 bring it to about 560 an hour.
  - **Workers per account:** 5,000 / 560 is 8.9 and 5,000 / 410 is 12.2, so "eight to twelve
    workers", not "five to six".
  - **Correct the existing sum:** the paragraph's "about 890 an hour at most" omits the 30 it
    has just named. Today's figure is 920, and the recompute replaces it anyway.
  - **#366:** if it lands first, it removes the separate `triage-todo` read. Whichever of the
    two lands second recomputes against the other: both together are two points a poll, about
    290 idle and 440 at most, or eleven to seventeen workers.
- **`README.md`**, the `github.request_timeout_ms` row of the configuration reference.
  Change "a board poll reads at most 1,000 open issues under one state label" to say the
  limit applies across the polled state labels together. The rest of the row, including the
  sweep's 5,000 per label, stays true. `tests/test_readme_bounds.py` does not pin that row.
- **`docs/package-layout.md`**, the `issuebot.github` entry. Change `_issues_with_label` "walks
  at most `MAX_ISSUE_PAGES` (30) pages per role per tick" to say it walks them once per tick,
  over the polled labels together. Change the sweep's `_collect(per_role=True)` to `_collect`.
  "The poll keeps the all-or-nothing rule" stays.
- **`docs/superpowers/specs/2026-09-14-resource-ceilings-design.md`**. Append an "*Amended by
  #362:*" paragraph to the `GhCliAdapter._issues_with_label` bullet, after the paragraph that
  ends "which is the answer a deployment can act on". It records four things:
  - the board poll is now one query over every polled state label, so the ceiling bounds them
    together, at 990 in all, for the reasons in section 1;
  - it fails as before;
  - `_collect`'s `per_role` flag is gone, and the per-label isolation described in the
    following paragraphs is now the sweep's alone, with no flag;
  - the bullet's own figures predate #243. Since #243 the poll's ceiling is 30 pages of 33 (990
    issues) rather than 10 of 100, and `MAX_TERMINAL_PAGES` is 150 (4,950) rather than 50.

  The original text stays as written, as the #128 and #149 amendments left it.
- **Docstrings** that name `per_role` (`_collect`'s own, and
  `test_board_poll_refuses_every_role_when_one_is_over_the_ceiling`'s) follow the code.
  `PageCeilingError`'s docstring (`src/issuebot/github/errors.py`) still describes `_collect`
  correctly, because the sweep still catches the error there.

## 7. Live check (2026-10-30, read-only)

Run against this repository with the production query's shape: numbered `$labelN` variables,
`states: [OPEN]`, `CREATED_AT` ascending, cursor paging. Each run was compared with today's
method, one read per label then deduplicated and sorted by `(created_at, number)`.

| Labels | Open issues | `first:` | Pages | Points |
|---|---|---|---|---|
| the four state labels | 3 | 1 | 3 | 3 |
| the four state labels | 3 | 33 | 1 | 1 |
| state + both triage labels (6) | 16 | 1 | 16 | 16 |
| state + both triage labels (6) | 16 | 5 | 4 | 4 |
| state + both triage labels (6) | 16 | 33 | 1 | 1 |

Every run passed four checks:

- no issue was repeated;
- the set was the per-label union exactly;
- the order was `(created_at, number)` exactly;
- every issue carried at least one of the labels asked for.

#366 and #367 each carry two of the six labels and came back once each, including at
`first: 1`, where they fall on different pages. The six-label union held two pairs of issues
created in the same second. GitHub ordered both pairs by number, but the sort stays in code
(section 3). Phase 5 had already measured the full `IssueFields` page at 1 point with two
labels. The number of labels does not change the connection count, so four labels cost the
same.

## 8. Acceptance

- **One request while the polled state labels together hold at most 33 open issues.** An
  adapter test over `OBSERVED_STATES` asserts `len(runner.calls) == 1`, and that the query
  declares and filters on `$label0` .. `$label3`. This corrects #362's wording, "while no
  label holds more than 33": the page count is on the union, so 20 issues in `todo` and 20 in
  `review` are two requests. In general a poll costs `max(1, ceil(union / 33))` requests,
  which is never more than today's sum, over the labels, of `max(1, ceil(n / 33))`.
- **The same issues in the same order as today.** An adapter test pins that an issue carrying
  two state labels comes back once, and that issues created in the same second come back
  sorted by number, whatever order the stub's pages hold them in.
- **Past the ceiling, the poll fails whole.** A union that keeps reporting `hasNextPage` fails
  after exactly `MAX_ISSUE_PAGES` requests, with a non-retryable `response`
  `PageCeilingError` naming every polled label.
- **The terminal sweep's tests pass unchanged**, apart from any mention of `per_role` in their
  wording.
- **Documentation:** `docs/operations.md`, the `README.md` row, `docs/package-layout.md` and
  the ceilings spec's amendment are as in section 6.

## 9. Left to the builder

- Whether `fetch_issues_by_states` calls `fetch_issues_with_any_label` or shares a private
  helper with it, and where the deduplication and sort live.
- How the adapter tests' stubs are reshaped. Today they match `label=issuebot/todo`; a
  multi-label poll sends `label0=` .. `label3=`. Keep at least one adapter test with a role
  list of one, which still sends `$label`, as `issuebot issues list --state` does.
