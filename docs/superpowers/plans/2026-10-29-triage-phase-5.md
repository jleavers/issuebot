# Triage Phase 5 (The Dashboard's Two Columns) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The dashboard's board gains two columns left of Todo. **Triage** holds the open issues
labelled `issuebot/triage-todo`, waiting or being triaged. **Triaged** holds those labelled
`issuebot/triage-complete` that carry no state label, so their verdict is filed and they wait
for the operator's `todo`. A split parent stays in Triaged until it closes. The README's two
images are regenerated to show the new board.

**Architecture:**
- **The store records the triage track.** A new nullable `issues.triage` column (migration
  0008) holds `triage_todo`, `triage_complete` or NULL. The worker computes it from the issue's
  labels whenever it upserts a polled issue, and sets it straight away when it publishes a
  move on the triage track.
- **One extra read per poll.** A worker with a store reads the open issues that carry either
  triage label in one GraphQL query, at 1 point a page of 33. The board poll only reads by state
  label, so without this read the store would never see an issue in Triaged. The answer is the
  complete set of open triage-track issues, so the store also clears `triage` on any open row
  missing from it. That is how an issue someone closed or unlabelled leaves the column.
- **The board's column is the state label if there is one, otherwise the triage track.** The
  queries, the views and the stylesheet iterate one tuple, `BOARD_COLUMNS`, in board order.

**Tech Stack:** Python 3.14, the `gh` CLI (GraphQL), PostgreSQL through psycopg, FastAPI with
Jinja2 templates, plain CSS, Playwright and Pillow (the screenshots), and pytest with
pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-10-25-triage-design.md`. Phase 5 is section 7's "The
dashboard" bullet, plus section 9 (polling cost) for the new read and "Documentation to update"
for `docs/dashboard.md`. Read them before starting: this plan argues from the spec.

## Rulings on what the spec leaves to the builder

Task 6 records these in the spec as "Decided while planning phase 5".

1. **The triage track is its own column, `issues.triage`**, and does not reuse `state`. Every
   other reader of `state` (the stats, the ledger seed, `IssueRow.state`) takes it to mean a
   state label. Its values are `GitHubLabels` field names, `triage_todo` and
   `triage_complete`, computed against the deployment's configured label names,
   case-insensitively. An issue carrying both is `triage_todo`: a human re-triaging a parent
   adds `triage-todo` and may leave `triage-complete` on, and the issue is waiting again. Cost
   if wrong: a migration, which `tools/upgrade/upgrade.py` already orders across every
   checkout.
2. **A state label wins.** An issue's board column is `coalesce(state, triage)`, so an issue
   with `todo` and `triage-complete` is in Todo, and the column headers and `/stats` count it
   once. This is spec section 1's "a state label wins over the triage track", drawn.
3. **The column keys are `triage_todo` and `triage_complete`.** Each heading is the label's own
   name (`issuebot/triage-todo`), as every other column's is. The docs call them the Triage and
   Triaged columns.
4. **One read, every poll, only with a store.** The read is
   `fetch_issues_with_any_label((triage_todo, triage_complete))`. It sits in `_fetch_issues`,
   which both the dispatching tick and the held tick (`_poll_issues`) call, so the board stays
   fresh during a hold. Like `review`, which rides along only for the store, a worker with no
   store does not make it. GitHub's `labels:` filter matches any of the labels given. This was
   checked read-only on 2026-10-29 against this repository: `todo` alone returned 1 issue,
   `triage-complete` alone 4, and both together 5, each read costing 1 point. Cost: 1 point
   per poll per deployment, 120 an hour at the default 30 s interval.
5. **The read is a complete set, so the store reconciles.** After a successful read the store
   clears `triage` on every open row of the repository missing from the answer, guarded by
   `seen_at <= read time`. No other read sees a closed `triage-complete` issue (the terminal
   sweep looks for state labels), so this is the only way one leaves the board. A failed read,
   or one past the page ceiling, reconciles nothing. It logs `triage_track_fetch_failed` and is
   not counted towards the GitHub hold (#88), as the triage candidates' read is not.
6. **A published move sets `triage` only when it lands on a triage label.**
   `StateChanged(... to_label=triage-todo | triage-complete)` sets the column at once. A move
   whose `to_label` is something else leaves it alone, because the event does not say which
   labels remain, and the same tick's reconcile settles it.
7. **A running triage is shown the way a running work issue is: in the Running panel.** Its row
   is marked `(triage)`, or `(parent review)` for a parent review. Cards do not change, since
   the spec's non-goals rule out progress on cards. `running_entry` gains `kind`, an additive
   change to the `/state` API.
8. **The issue page names the triage label** in the header pill when the issue has no state
   label, rather than saying "unlabelled". Every card on the two new columns links to that page.
9. **The colours are new hues, not the labels' hexes.** GitHub renders `triage-todo` as
   `FEF2C0` and `triage-complete` as `D4C5F9`, which are 1.1:1 and 1.5:1 on white, so neither
   can be a mark on the board. Their darkened siblings would be the amber of In progress and the
   purple of Complete. So the plan picks distinct hues that clear every floor in
   `tests/test_web_theme.py`:

   | Token | Light | Dark |
   |---|---|---|
   | `--triage_todo` | `#1b7c83` | `#39c5cf` |
   | `--triage_complete` | `#bf3989` | `#db61a2` |

   Measured against the floors: in light, `#1b7c83` is 4.93:1 on `--panel` and 4.60:1 on
   `--bg`, and `#bf3989` is 5.05:1 and 4.71:1. In dark, `#39c5cf` is 7.93:1 on `--panel` and
   `#db61a2` is 4.92:1. `--on-solid` is at least 4.9:1 on all four. Cost if wrong: four hex
   values to change.
10. **Seven columns, `repeat(7, minmax(150px, 1fr))`, with `overflow-x: auto` on `.kanban`.** A
    window too narrow for seven columns scrolls the board inside its own box, not the whole
    page. At the 1180 px the GIF is captured at, the 1140 px content box fits the 1110 px the
    columns need.
11. **The journey GIF starts in Triage**, in six stages: `triage-todo` → `triage-complete`
    (verdict `fits`) → `todo` → in progress → review → complete. A journey that began at Todo
    would skip the first two columns of the board it is drawn over. The standing board gains a
    Triage card, two Triaged cards (one of them a split parent) and a running triage in the
    Running panel, so `dashboard.png` shows all of it. If the GIF exceeds 500 KB, lower
    `capture.py`'s `--width` default before `--colours`, as `tools/screenshots/README.md`
    says.
12. **`/issues?state=` and `/stats`'s `by_state` take the two new keys.** The column header's
    "N more" link points at the list page, which therefore has to accept them. This is an
    additive API change.

## Global Constraints

- `jleavers/issuebot` is public. No private repository names in code, fixtures, commits, docs
  or the PR. The screenshots use the placeholder `acme/frontend` and invented titles only.
- Never run `docker compose build`, `up`, `restart`, `down` or `rm` on anything but `test-db`.
  The live hub runs from the main checkout. DB tests run against `test-db`:
  `docker compose --profile test up -d --wait test-db`, then
  `DATABASE_URL=postgresql://issuebot@$(docker compose port test-db 5432)/issuebot uv run pytest`,
  then `docker compose rm -sf test-db`. Never pass `ISSUEBOT_DB_PORT` inline.
- Match the surrounding code: its comment density, naming and idiom. Each module's docstrings
  explain *why*; new code does the same, briefly.
- `uv run ruff check . && uv run ruff format --check .` must be clean. Python 3.14 (`except A,
  B:` without parentheses when there is no `as`; see the repo's ruff config).
- Each image under `docs/images/` must stay under 500 KB. `check-added-large-files` enforces
  this, and `capture.py` checks it first.
- `CLAUDE.md` has a byte budget (`tests/test_instruction_bounds.py`), and so does
  `docs/package-layout.md`. Add at most a clause to either.
- A markdown link carrying an anchor, or a quoted heading title, is checked by
  `tests/test_doc_pointers.py`. Renaming a heading that something points at fails it.
- Never `git add` the dot-env file or `.env.example`, and never mention their names in a shell
  command (a hook rejects it). Stage edits to them with `git add -u`.
- Commits end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_019agwxcWUZKkqVuL73ZfE76`.

## Review Focus

These are the five inputs the spec implies and no task's happy path exercises, most likely
first. Each has its test in the task named.

1. **An issue in Triaged that a human closes, or strips of its label**, leaves the board on the
   next poll rather than staying there for ever. Tests: Task 2's
   `test_reconcile_clears_open_rows_missing_from_the_track`, and Task 3's
   `test_the_triage_track_is_read_once_per_poll_when_a_store_watches`.
2. **An issue carrying a state label and a triage label** is drawn once, in its state's column,
   and counted once in the headers and `/stats`. Task 4:
   `test_a_state_label_wins_over_the_triage_track`.
3. **An issue carrying both triage labels** is in Triage. Task 2:
   `test_triage_todo_wins_when_an_issue_carries_both`.
4. **A deployment that renamed its triage labels** (`github.labels.triage_todo`), or whose labels
   differ in case, is still drawn. Task 2:
   `test_triage_track_reads_the_configured_names_case_insensitively`.
5. **A triage-track read that fails or is over its ceiling** reconciles nothing, holds nothing,
   and leaves the board as it was. Task 3: `test_a_failed_triage_track_read_is_a_warning_not_a_hold`.

---

### Task 1: GitHub — one read for every open issue carrying either triage label

**Files:**
- Modify: `src/issuebot/github/adapter.py` (protocol, after `fetch_issues_with_label`)
- Modify: `src/issuebot/github/ghcli.py` (`_issues_query`, `fetch_issues_with_label`,
  `_issues_with_label`)
- Modify: `src/issuebot/github/fake.py` (after `fetch_issues_with_label`)
- Create: `tests/fixtures/gh/list_triage_track.json`, beside `list_triage_todo.json`, in the same
  shape. It holds one open issue with `issuebot/triage-todo` (#60) and one with
  `issuebot/triage-complete` (#61).
- Test: `tests/test_github_ghcli.py`, `tests/test_github_fake.py`

**Interfaces:**
- Produces: `GitHubAdapter.fetch_issues_with_any_label(self, labels: Sequence[str]) ->
  list[Issue]`. It returns the open issues carrying any of `labels`, each once, oldest first by
  `(created_at, number)`, and `[]` with no request for an empty input. It makes one read, and
  past `MAX_ISSUE_PAGES` it fails exactly as `fetch_issues_with_label` does.

- [ ] **Step 1: Write the failing tests**

In `tests/test_github_ghcli.py`, beside `test_fetch_issues_with_label_asks_for_that_label`:

```python
async def test_fetch_issues_with_any_label_asks_for_every_label_in_one_read() -> None:
    runner = StubRunner()
    runner.on(has("label1=issuebot/triage-complete"), stdout=fixture("list_triage_track.json"))
    issues = await make_adapter(runner).fetch_issues_with_any_label(
        ("issuebot/triage-todo", "issuebot/triage-complete")
    )
    assert [issue.number for issue in issues] == [60, 61]
    assert len(runner.calls) == 1
    query = query_of(runner.argv(0))
    assert "labels: [$label0, $label1]" in query
    assert "$label0: String!" in query and "$label1: String!" in query
    assert "states: [OPEN]" in query
    assert has("label0=issuebot/triage-todo")(runner.argv(0))


async def test_fetch_issues_with_any_label_of_nothing_asks_nothing() -> None:
    runner = StubRunner()
    assert await make_adapter(runner).fetch_issues_with_any_label(()) == []
    assert runner.calls == []
```

Add a ceiling test that copies the shape of the existing `fetch_issues_with_label` ceiling test
(find it with `grep -n "MAX_ISSUE_PAGES" tests/test_github_ghcli.py`): `MAX_ISSUE_PAGES` pages,
each with `hasNextPage: true`, raise the same error type.

In `tests/test_github_fake.py`:

```python
async def test_fetch_issues_with_any_label_is_the_union_case_insensitively() -> None:
    gh = FakeGitHub()
    gh.add_issue(1, labels=["issuebot/triage-todo"])
    gh.add_issue(2, labels=["Issuebot/Triage-Complete"])
    gh.add_issue(3, labels=["issuebot/todo"])
    gh.add_issue(4, labels=["issuebot/triage-complete"], state="closed")
    found = await gh.fetch_issues_with_any_label(
        ("issuebot/triage-todo", "issuebot/triage-complete")
    )
    assert [issue.number for issue in found] == [1, 2]
```

Use the fake's real constructor and `add_issue` keywords, which you can read in `fake.py`. Keep
the assertions as they are.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_github_ghcli.py tests/test_github_fake.py -k any_label -v`
Expected: FAIL with `AttributeError: ... 'fetch_issues_with_any_label'`.

- [ ] **Step 3: Implement**

- `_issues_query(states: str, labels: int = 1) -> str`. With `labels == 1` it emits exactly the
  query it emits today, `$label: String!` and `labels: [$label]`, so the existing reads and
  their tests stay byte-for-byte the same. With `labels > 1` it declares `$label0: String!` up
  to `$label{n-1}: String!` and filters `labels: [$label0, ..., $label{n-1}]`. Numbered string
  variables, because `_graphql` sends only strings and ints.
- Generalise `_issues_with_label(label, query, login, max_pages)` so that its first parameter is
  the label variables, as a `Mapping[str, str]`, merged into `variables`.
  `fetch_issues_with_label` passes `{"label": label}`. Nothing else in the loop changes.
- `fetch_issues_with_any_label(labels)`: deduplicate with `dict.fromkeys`, return `[]` for
  none, and delegate one name to `fetch_issues_with_label`. Otherwise build
  `_issues_query("OPEN", len(names))` and call the generalised loop with
  `{f"label{i}": name for i, name in enumerate(names)}` and `MAX_ISSUE_PAGES`. The docstring
  says GitHub ORs the labels, and that the read was measured at 1 point a page on 2026-10-29.
- Protocol method in `adapter.py`, with the docstring from **Interfaces**.
- Fake: the open records carrying any of the names, case-insensitively, through
  `self._snapshots(...)`, after `self._enter("fetch_issues_with_any_label", tuple(labels))`.

- [ ] **Step 4: Run the GitHub tests**

Run: `uv run pytest tests/test_github_ghcli.py tests/test_github_fake.py -q`
Expected: all pass, including every existing `fetch_issues_with_label` and board-poll test.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/github tests/test_github_ghcli.py tests/test_github_fake.py tests/fixtures/gh/list_triage_track.json
git commit -m "github: fetch_issues_with_any_label, both triage labels in one read"
```

---

### Task 2: The store records the triage track

**Files:**
- Create: `src/issuebot/db/migrations/0008_triage_track.sql`
- Modify: `src/issuebot/db/queries.py` (`IssueRow`, and new `TRIAGE_COLUMNS`)
- Modify: `src/issuebot/db/store.py` (`UPSERT_ISSUE`, `issue_row`, `apply_event`, `Store`, new
  SQL)
- Modify: `src/issuebot/db/sink.py` (new `record_triage_track`, its queue item, `_take`,
  `_apply`, `_describe`)
- Modify: `tests/fakes/database.py` (`FakeStore.reconcile_triage_track`), `tests/fakes/web.py`
  (`issue_row` default `triage=None`)
- Test: `tests/test_db_store.py`, `tests/test_db_sink.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `issuebot.db.queries.TRIAGE_COLUMNS: tuple[str, str] = ("triage_todo",
    "triage_complete")`, the values of `issues.triage`.
  - `IssueRow.triage: str | None`, placed after `state_label`.
  - `issuebot.db.store.triage_track(names: Iterable[str], labels: GitHubLabels) -> str | None`.
  - `issue_row(snapshot: IssueSnapshot, labels: GitHubLabels) -> dict[str, Any]`, whose row now
    carries `"triage"`.
  - `Store.reconcile_triage_track(self, numbers: Sequence[int], at: datetime) -> None`.
  - `PostgresSink.record_triage_track(self, issues: Sequence[Issue]) -> None`.

- [ ] **Step 1: Write the failing tests**

Hermetic, in `tests/test_db_store.py`:

```python
def test_triage_track_reads_the_configured_names_case_insensitively() -> None:
    labels = GitHubLabels(triage_todo="Needs/Sizing", triage_complete="sized")
    assert triage_track(["needs/sizing"], labels) == "triage_todo"
    assert triage_track(["SIZED", "issuebot/todo"], labels) == "triage_complete"
    assert triage_track(["issuebot/triage-todo"], labels) is None


def test_triage_todo_wins_when_an_issue_carries_both() -> None:
    labels = GitHubLabels()
    assert triage_track([labels.triage_complete, labels.triage_todo], labels) == "triage_todo"


def test_upsert_issue_placeholders_match_issue_row() -> None:
    row = issue_row(snapshot(make_plain_issue(), 0), GitHubLabels())
    assert set(re.findall(r"%\((\w+)\)s", UPSERT_ISSUE)) == set(row) | {"repo"}
```

If the module has no hermetic issue builder, build the `Issue` inline as
`tools/screenshots/seed.py`'s `_issue` does. Against `test-db` (the module's `store`, `rows`,
`snapshot`, `moved` and `make_issue` helpers):

```python
async def test_a_polled_issue_records_its_triage_track(store, db_url, make_issue) -> None:
    await store.upsert_issues([snapshot(make_issue(labels=["issuebot/triage-complete"]), 0)])
    (row,) = await rows(db_url, "SELECT state, triage FROM issues")
    assert row == {"state": None, "triage": "triage_complete"}


async def test_a_triage_label_move_records_the_track(store, db_url, make_issue) -> None:
    await store.upsert_issues([snapshot(make_issue(labels=["issuebot/triage-todo"]), 0)])
    await store.apply_event(
        moved(from_label="issuebot/triage-todo", to_label="issuebot/triage-complete")
    )
    (row,) = await rows(db_url, "SELECT triage FROM issues")
    assert row["triage"] == "triage_complete"


async def test_a_move_off_the_triage_track_leaves_it_for_the_poll(
    store, db_url, make_issue
) -> None:
    await store.upsert_issues([snapshot(make_issue(labels=["issuebot/triage-todo"]), 0)])
    await store.apply_event(moved(from_label="issuebot/triage-todo", to_label=None))
    (row,) = await rows(db_url, "SELECT triage FROM issues")
    assert row["triage"] == "triage_todo"


async def test_reconcile_clears_open_rows_missing_from_the_track(store, db_url, make_issue) -> None:
    await store.upsert_issues(
        [
            snapshot(make_issue(number=60, labels=["issuebot/triage-todo"]), 0),
            snapshot(make_issue(number=61, labels=["issuebot/triage-complete"]), 0),
            snapshot(make_issue(number=62, labels=["issuebot/triage-complete"], closed=True), 0),
            snapshot(make_issue(number=63, labels=["issuebot/triage-complete"]), 20),
        ]
    )
    await store.reconcile_triage_track([60], at(10))
    found = await rows(db_url, "SELECT number, triage FROM issues ORDER BY number")
    assert [(r["number"], r["triage"]) for r in found] == [
        (60, "triage_todo"),  # in the set
        (61, None),  # open, missing: closed or unlabelled on GitHub
        (62, "triage_complete"),  # already closed in the store: on no column either way
        (63, "triage_complete"),  # seen after the read began: newer than its answer
    ]
```

`make_issue` (`tests/conftest.py`) takes `Issue` field overrides and defaults to an open `todo`
issue. The shorthand above means: `make_issue(number=60, labels=("issuebot/triage-todo",),
state=None, state_labels=())`, plus `github_state="closed"` for `closed=True`, and a distinct
`id` per number.
`snapshot(issue, seconds)` is the module's own helper. The existing
`test_a_triage_label_move_leaves_the_recorded_state_alone` must keep passing unchanged.

In `tests/test_db_sink.py`, with the module's `FakeStore`:

```python
async def test_record_triage_track_writes_the_snapshots_before_the_reconcile() -> None:
    # record_triage_track([issue 60, issue 61]); drain.
    # FakeStore saw upsert_issues with 60 and 61, then reconcile_triage_track([60, 61], at),
    # where `at` is the seen_at the snapshots carry.

async def test_an_empty_triage_track_still_reconciles() -> None:
    # record_triage_track([]) -> one reconcile_triage_track([], at), no upsert.

async def test_a_full_queue_drops_the_reconcile_and_counts_it() -> None:
    # at QUEUE_LIMIT, record_triage_track queues nothing, dropped += 1, logs db_queue_full.
```

Write these three bodies in the style of the module's existing `record_issues` tests.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_db_store.py tests/test_db_sink.py -k "triage or reconcile or placeholders" -v`
Expected: FAIL on the import of `triage_track` and on `record_triage_track`. The DB tests skip
without `DATABASE_URL`, so run them against `test-db` (see Global Constraints).

- [ ] **Step 3: Implement**

- `0008_triage_track.sql`, with a header comment in `0007_run_kind.sql`'s style (spec section
  7, what the column holds, that NULL means neither label):
  `ALTER TABLE issues ADD COLUMN triage text;`
- `queries.py`: `TRIAGE_COLUMNS`, plus `IssueRow.triage: str | None` after `state_label`.
  `ISSUE_COLUMNS` follows from the dataclass.
- `store.py`:
  - `triage_track()`, which compares lower-cased names with the configured
    `labels.triage_todo` and `labels.triage_complete`. `triage_todo` wins.
  - `issue_row(snapshot, labels)` adds `"triage"`. `UPSERT_ISSUE` gains the column in its
    insert list, its values and its `DO UPDATE SET`. `upsert_issues` passes `self._labels`.
  - `TRIAGE_CHANGED`:
    `UPDATE issues SET triage = %(triage)s, seen_at = %(at)s WHERE repo = %(repo)s AND
    number = %(number)s AND seen_at <= %(at)s`.
    The triage-track branch of `apply_event` executes it only when `event.to_label` maps to a
    column through `triage_track([event.to_label], self._labels)`. Update
    `_on_the_triage_track`'s docstring: the state is still left alone, and the track is now
    recorded.
  - `RECONCILE_TRIAGE`:
    `UPDATE issues SET triage = NULL WHERE repo = %(repo)s AND triage IS NOT NULL AND
    github_state = 'open' AND NOT (number = ANY(%(numbers)s)) AND seen_at <= %(at)s`.
    `reconcile_triage_track(numbers, at)` runs it through `_guard()` and `_stamp()`, as every
    write does. The docstring explains why the guard does not bump `seen_at`: the sink drains in
    order, so every snapshot from the same read has already been written.
  - Add `reconcile_triage_track` to the `Store` protocol.
- `sink.py`: a frozen `_TrackItem(numbers: tuple[int, ...], at: datetime)`, added to `_Item` and
  `_Work`. `record_triage_track(issues)` takes one `seen_at = self._now()`, merges the snapshots
  as `record_issues` does (factor the merge into a helper both call, so `seen_at` is the same
  value), and then queues the `_TrackItem`. Under the `QUEUE_LIMIT` check it drops the item,
  incrementing `dropped` and logging `db_queue_full`, as `handle` does. `_take` passes the item
  through, `_apply` calls `reconcile_triage_track`, and `_describe` reports
  `{"kind": "triage_track", "count": len(numbers)}`. The class docstring's list of enqueue
  methods gains `record_triage_track`.
- `tests/fakes/database.py`: `FakeStore.reconcile_triage_track` records
  `("reconcile", tuple(numbers), at)` in the call order the sink tests read.
  `tests/fakes/web.py`'s `issue_row` adds `triage=None`.

- [ ] **Step 4: Run the db tests, and the migration tests, against `test-db`**

Run: `DATABASE_URL=... uv run pytest tests/test_db_store.py tests/test_db_sink.py tests/test_db_migrate.py tests/test_db_queries.py -q`
Expected: all pass. If `test_db_migrate.py` pins the number of migrations or the newest one,
update that number as part of this task.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/db tests/test_db_store.py tests/test_db_sink.py tests/fakes
git commit -m "db: record the triage track on issues, and reconcile it from a complete read"
```

---

### Task 3: The worker reads the triage track once a poll

**Files:**
- Modify: `src/issuebot/orchestrator/orchestrator.py` (`__init__`, `_fetch_issues`, new
  `_observe_triage_track`)
- Modify: `src/issuebot/cli.py` (the `Orchestrator(...)` call that passes `on_issues`)
- Test: `tests/test_orchestrator.py` (its harness takes `on_issues=`; give it
  `on_triage_track=` the same way)

**Interfaces:**
- Consumes: `fetch_issues_with_any_label` (Task 1) and `PostgresSink.record_triage_track` (Task 2).
- Produces: `Orchestrator(..., on_triage_track: Callable[[Sequence[Issue]], None] | None =
  None)`.

- [ ] **Step 1: Write the failing tests**

```python
def track_reads(h: Harness) -> list[tuple[Any, ...]]:
    return [args for name, args in h.github.calls if name == "fetch_issues_with_any_label"]


async def test_the_triage_track_is_read_once_per_poll_when_a_store_watches(tmp_path) -> None:
    seen: list[list[int]] = []
    h = Harness(tmp_path, on_triage_track=lambda issues: seen.append([i.number for i in issues]))
    # Both Triaged: a `triage-todo` issue would be dispatched for triage on the first tick, and
    # its run would move its labels under the test. The held test below covers `triage-todo`.
    h.github.add_issue(60, labels=["issuebot/triage-complete"])
    h.github.add_issue(61, labels=["issuebot/triage-complete"])
    h.github.add_issue(62, labels=["issuebot/triage-complete"], state="closed")
    await h.tick()
    await h.tick()
    assert track_reads(h) == [(("issuebot/triage-todo", "issuebot/triage-complete"),)] * 2
    assert seen == [[60, 61], [60, 61]]


async def test_no_store_no_triage_track_read(tmp_path) -> None:
    h = Harness(tmp_path)
    await h.tick()
    assert track_reads(h) == []


async def test_the_triage_track_is_read_while_dispatch_is_held(tmp_path) -> None:
    seen: list[list[int]] = []
    h = Harness(tmp_path, on_triage_track=lambda issues: seen.append([i.number for i in issues]))
    h.add_issue(1, "todo")
    await h.tick()
    await h.exit(h.run_for(1), **USAGE_LIMITED)  # the usage hold: no claims, the board still read
    h.github.add_issue(60, labels=["issuebot/triage-todo"])
    seen.clear()
    await h.tick()
    assert h.orchestrator.snapshot().dispatch_hold is not None
    assert seen == [[60]]


async def test_a_failed_triage_track_read_is_a_warning_not_a_hold(tmp_path) -> None:
    seen: list[list[int]] = []
    h = Harness(tmp_path, on_triage_track=lambda issues: seen.append([i.number for i in issues]))
    h.github.fail_next("transport", times=MAX_FETCH_FAILURES + 1, on="fetch_issues_with_any_label")
    with capture_logs() as logs:
        for _ in range(MAX_FETCH_FAILURES + 1):
            await h.tick()
    assert h.orchestrator.snapshot().dispatch_hold is None
    assert seen == []
    failed = [entry for entry in logs if entry["event"] == "triage_track_fetch_failed"]
    assert len(failed) == MAX_FETCH_FAILURES + 1
```

`FakeGitHub.add_issue` takes the title first, then `number=` and `labels=` keywords. Close one
with the fake's own close helper (`grep -n "def close" src/issuebot/github/fake.py`), and check
the `"transport"` category name against `ErrorCategory`. The harness's `__init__` gains
`on_triage_track=`, passed through to `Orchestrator` as `on_issues` already is. `PageCeilingError` subclasses `GitHubError`, so the one
`except GitHubError` covers the ceiling too, and needs no second test. The assertions are the
requirement.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_orchestrator.py -k triage_track -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'on_triage_track'`.

- [ ] **Step 3: Implement**

- `__init__` stores `on_triage_track` beside `on_issues`.
- `_observe_triage_track()`:
  - It returns at once without an observer.
  - It reads `fetch_issues_with_any_label((labels.triage_todo, labels.triage_complete))` using
    the live workflow's labels.
  - On `GitHubError` it logs `triage_track_fetch_failed` with `error=str(exc)` and returns. It
    calls neither `_note_fetch_failure` nor `_note_fetch_success`.
  - On success it hands the list to the observer inside `try/except Exception`, logging
    `triage_track_consumer_failed`, as `_report_issues` does.
  - The docstring cites spec section 7 and rulings 4 and 5, and says why the read is not part
    of the hold.
- `_fetch_issues` calls it after `self._report_issues(issues)` and before
  `_bounce_conflicts`. That way the board's snapshots are queued before the reconcile, and a
  failed board read, which returns early, skips it too.
- `_poll_issues`'s early return, taken when nobody watches and the conflict bounce is off,
  also requires `self._on_triage_track is None`. Otherwise a held tick with conflicts off would
  skip the read. Update its docstring's last sentence to match.
- `cli.py`: pass `on_triage_track=postgres.record_triage_track if postgres is not None else
  None` next to `on_issues`, and extend the comment above `on_snapshot`.

- [ ] **Step 4: Run the orchestrator and CLI tests**

Run: `uv run pytest tests/test_orchestrator.py tests/test_orchestrator_triage.py tests/test_orchestrator_auto_triage.py tests/test_cli.py -q`
Expected: all pass. A test that asserts the exact list of adapter calls in one tick may need the
new read added. That is the expected change, and only where an observer is attached.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/orchestrator/orchestrator.py src/issuebot/cli.py tests/test_orchestrator.py
git commit -m "orchestrator: read the triage track once a poll for the store"
```

---

### Task 4: The board, the list page, the Running panel and the issue page

**Files:**
- Modify: `src/issuebot/db/queries.py` (`BOARD_COLUMNS`, `OPEN_ISSUES`, `ISSUE_LIST`,
  `STATE_COUNTS`, `issues_by_state`, `issues_for_state`, `state_counts`)
- Modify: `src/issuebot/web/views.py` (`dashboard_context`, `is_board_state`,
  `issue_filters`, `running_entry`, new `issue_pill`)
- Modify: `src/issuebot/web/app.py` (the issue page passes `pill`)
- Modify: `src/issuebot/web/templates/partials/dashboard.html` (the Running row's marker)
- Modify: `src/issuebot/web/templates/issue.html` (the header pill)
- Test: `tests/test_db_queries.py`, `tests/test_web_pages.py`, `tests/test_web_app.py`

**Interfaces:**
- Consumes: `TRIAGE_COLUMNS` and `IssueRow.triage` (Task 2).
- Produces:
  - `issuebot.db.queries.BOARD_COLUMNS: tuple[str, ...] = (*TRIAGE_COLUMNS, *(role.value for
    role in StateLabel))`. These are the column keys in board order, and Task 5's stylesheet
    test counts them.
  - `issue_pill(issue: IssueRow, labels: GitHubLabels) -> dict[str, str]` returns `{"role":
    ..., "text": ...}`.

- [ ] **Step 1: Write the failing tests**

Against `test-db`, in `tests/test_db_queries.py` (seed through the module's `seeded` fixture or
`store.upsert_issues`, as its neighbours do):

```python
async def test_the_board_draws_the_triage_track_left_of_todo(seeded) -> None:
    # seed: #60 triage-todo; #61 triage-complete; #62 both triage labels; #63 todo + triage-complete
    groups = await queries.issues_by_state()
    assert list(groups) == list(BOARD_COLUMNS)
    assert [r.number for r in groups["triage_todo"]] == [62, 60]   # newest first, as every column
    assert [r.number for r in groups["triage_complete"]] == [61]
    assert 63 in [r.number for r in groups["todo"]]


async def test_a_state_label_wins_over_the_triage_track(seeded) -> None:
    # #63 todo + triage-complete is counted in todo only
    counts = await queries.state_counts()
    assert set(counts) == set(BOARD_COLUMNS)
    assert counts["todo"] == <todo rows seeded> and counts["triage_complete"] == 1


async def test_a_closed_triage_issue_is_on_no_column(seeded) -> None:
    # closed, triage-complete, no state label -> absent from issues_by_state, state_counts and
    # issues_for_state(None)


async def test_issues_for_state_lists_a_triage_column(seeded) -> None:
    assert [r.number for r in await queries.issues_for_state("triage_complete")] == [61]
```

Replace `<todo rows seeded>` with the fixture's actual count. Hermetic, in `tests/test_web_pages.py`
(with the `Harness`, `h.queries.groups` and `issue_row` fakes):

```python
def test_the_board_has_a_column_per_board_column_in_order(h) -> None:
    html = page(h, "/r/acme/frontend/")    # the module's own helper for the dashboard partial
    classes = re.findall(r'<div class="column ([a-z_]+)">', html)
    assert classes == list(BOARD_COLUMNS)
    assert "issuebot/triage-todo" in html and "issuebot/triage-complete" in html


def test_a_triage_run_is_marked_in_the_running_panel(h) -> None:
    # snapshot running: one row kind="triage", one kind="parent_review", one kind="work"
    # -> "(triage)" and "(parent review)" each once in the Running table; the work row has neither


def test_the_issue_page_names_the_triage_label_when_there_is_no_state(h) -> None:
    h.queries.issue_rows[9] = issue_row(number=9, state=None, state_label=None, triage="triage_complete")
    html = ...  # GET the issue page
    assert 'class="state triage_complete"' in html and "issuebot/triage-complete" in html
    assert "unlabelled" not in html


def test_the_list_page_filters_by_a_triage_column(h) -> None:
    # GET /issues?state=triage_todo -> 200, the filter row carries all seven columns and
    # marks triage_todo current; GET /issues?state=nonsense is refused exactly as today
```

In `tests/test_web_app.py`, add `running_entry({...,"kind": "triage"})["kind"] == "triage"`,
and check that a row with no `kind` (a snapshot from before phase 1) reads `"work"`. Also check
that `stats_document(...)["by_state"]` passes the two triage keys through.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_web_pages.py tests/test_web_app.py -k "triage or column or running" -v`
Then run the DB ones against `test-db`. Expected: FAIL on `BOARD_COLUMNS` and the missing markup.

- [ ] **Step 3: Implement**

- `queries.py`:
  - `OPEN_ISSUES` filters `coalesce(state, triage) IS NOT NULL` in place of `state IS NOT
    NULL`.
  - `issues_by_state` keys `groups` by `BOARD_COLUMNS` and files each row under `row["state"]
    or row["triage"]`.
  - `STATE_COUNTS` selects and groups `coalesce(state, triage) AS state` under
    `coalesce(state, triage) IS NOT NULL AND (github_state = 'open' OR state = 'complete')`.
  - `ISSUE_LIST` filters `coalesce(state, triage) = ANY(%(roles)s)`.
  - `issues_for_state` and `state_counts` use `BOARD_COLUMNS` for their known keys.
  - Update the docstrings that say "five", and say why a closed triage-only issue is on no
    column: the predicate only lets a closed issue in through `complete`.
- `views.py`:
  - `dashboard_context` and `issue_filters` iterate `BOARD_COLUMNS`, labelling each column
    `names[key]`. `GitHubLabels.model_dump()` already carries `triage_todo` and
    `triage_complete`. "all" sums all seven.
  - `is_board_state` checks `BOARD_COLUMNS`.
  - `running_entry` adds `"kind": entry.get("kind") or "work"`.
  - `issue_pill(issue, labels)` returns the state's role and label when there is one. Failing
    that it returns the triage key and `labels.model_dump()[key]`, and failing that
    `{"role": "unlabelled", "text": "unlabelled"}`.
- `app.py`: the issue page's context gains `pill=issue_pill(issue, scope.labels)`. `issue.html`
  renders `<span class="state {{ pill.role }}">{{ pill.text }}</span>`.
- `partials/dashboard.html`: after the `(resumed)` marker, `{% if entry.kind == "triage" %}
  <span class="muted">(triage)</span>{% elif entry.kind == "parent_review" %} <span
  class="muted">(parent review)</span>{% endif %}`.

- [ ] **Step 4: Run the web and query tests**

Run: `uv run pytest tests/test_web_pages.py tests/test_web_app.py tests/test_web_theme.py -q`, then
`DATABASE_URL=... uv run pytest tests/test_db_queries.py tests/test_web_app_db.py -q`
Expected: all pass. A test asserting five columns, or the "all" total over five, is updated to
`BOARD_COLUMNS`. Each such change names `BOARD_COLUMNS` rather than the literal 7.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/db/queries.py src/issuebot/web tests/test_db_queries.py tests/test_web_pages.py tests/test_web_app.py
git commit -m "web: the Triage and Triaged columns, the list page, and run kinds in Running"
```

---

### Task 5: The stylesheet — two colours, seven columns

**Files:**
- Modify: `src/issuebot/web/static/app.css` (the three token blocks, the column dots, the card
  stripes, the `.state` pills, `.kanban`)
- Test: `tests/test_web_theme.py`

**Interfaces:**
- Consumes: `BOARD_COLUMNS` (Task 4), and the class names `column triage_todo`, `column
  triage_complete`, `state triage_todo` and `state triage_complete` that Task 4's markup emits.

- [ ] **Step 1: Write the failing tests**

- Add `"--triage_todo"` and `"--triage_complete"` to the token lists of four tests:
  `test_marks_clear_the_contrast_floor_on_the_panel`, `test_the_kanban_card_marks_clear_the_mark_floor`,
  `test_a_pill_label_is_legible_on_every_solid_colour`, and the `roles` tuple of
  `test_the_column_role_is_a_dot_and_keeps_its_state_token` (as `triage_todo` and
  `triage_complete`).
- New tests:

```python
def test_the_triage_tokens_are_not_the_triage_labels_which_cannot_be_marks() -> None:
    """Ruling 9: GitHub's triage hexes are too pale to be a mark, and their darkened siblings
    are In progress's amber and Complete's purple, so the two columns take hues of their own."""
    for style, token in (
        (TRIAGE_TODO_LABEL_STYLE, "--triage_todo"),
        (TRIAGE_COMPLETE_LABEL_STYLE, "--triage_complete"),
    ):
        assert contrast(f"#{style.color}", LIGHT["--panel"]) < MARK_FLOOR
        assert LIGHT[token] != f"#{style.color.lower()}"
    for token in ("--triage_todo", "--triage_complete"):
        for theme in (LIGHT, OS_DARK):
            assert contrast(theme[token], theme["--bg"]) >= MARK_FLOOR


def test_the_kanban_has_a_column_per_board_column() -> None:
    assert f"repeat({len(BOARD_COLUMNS)}, minmax(150px, 1fr))" in KANBAN
    assert "overflow-x: auto" in KANBAN
```

`test_the_two_dark_blocks_agree` already requires the new tokens in both dark blocks.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_web_theme.py -v`
Expected: FAIL with `KeyError: '--triage_todo'` and the grid assertion.

- [ ] **Step 3: Implement**

- Add tokens with ruling 9's hexes to `:root`, `:root:where(:not([data-theme="light"]))` and
  `:root[data-theme="dark"]`, each beside `--complete`. Add one comment, in the light block, on
  why these two are not GitHub's hexes, in the voice of the `--in_progress` comment.
- Add `.column.triage_todo h3 .name::before` and `.column.triage_complete ...` beside the
  five, plus `.column.triage_todo .card` and `.column.triage_complete .card` stripes and
  `.state.triage_todo` and `.state.triage_complete` pill fills. Copy the existing `.state.todo`
  rule's shape; find it with `grep -n "\.state\." src/issuebot/web/static/app.css`.
- Change `.kanban` to `grid-template-columns: repeat(7, minmax(150px, 1fr)); overflow-x:
  auto;`. Add a comment on the 150 px: seven columns fit the 1240 px content box of a 1280 px
  window, and the GIF's 1180 px capture.
- Fix the comment above `.column h3 .name` that says "The five state tokens".

- [ ] **Step 4: Run the theme and page tests**

Run: `uv run pytest tests/test_web_theme.py tests/test_web_pages.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/issuebot/web/static/app.css tests/test_web_theme.py
git commit -m "web: colours for the triage columns, and a seven-column board"
```

---

### Task 6: The README's images, and the docs

**Files:**
- Modify: `tools/screenshots/seed.py` (docstring, `BOARD`, `STAGES`, `_snapshot`, `main`)
- Modify: `tools/screenshots/capture.py` (`CAPTIONS`, `HOLD_MS`, the stage loops, the hero stage,
  `--width` only if the GIF is over 500 KB)
- Regenerate: `docs/images/dashboard.png`, `docs/images/issue-journey.gif`,
  `docs/images/social-preview.png`
- Modify: `README.md` (the alt text at lines 16 and 53, and one sentence in the triage
  paragraph at lines 63–78)
- Modify: `docs/dashboard.md` (the alt text at line 9, the description at line 13, and a new
  section)
- Modify: `docs/operations.md` (the poll-cost paragraph at lines 45–53)
- Modify: `docs/package-layout.md` (`issuebot.github`, `issuebot.db`, `issuebot.orchestrator`,
  `issuebot.web`)
- Modify: `docs/superpowers/specs/2026-10-25-triage-design.md` (the status line, and "Decided
  while planning phase 5")
- Test: `tests/test_tools_screenshots.py` if it exists (`ls tests | grep -i screenshot`);
  otherwise the full suite and the capture's own size check.

**Interfaces:**
- Consumes: everything above. The seed writes through `store.upsert_issues`, so Task 2's
  `triage_track` files the seeded issues into their columns with no special casing.

- [ ] **Step 1: Seed the new board and the six-stage journey**

- `seed.py`:
  - **`BOARD`** gains three invented `acme/frontend` issues, each with `role=None` and the label
    passed through `markers`:
    - #60 "Dark-mode toggle forgets the choice on reload", `triage-todo`;
    - #59 "Export the activity feed as CSV", `triage-complete`;
    - #58 "Checkout redesign", `triage-complete`.
  - **`STAGES`** becomes six entries for #42, under the same title:
    1. `role=None` with `markers=(LABELS.triage_todo,)`;
    2. `role=None` with `markers=(LABELS.triage_complete,)`;
    3. `"todo"`, with the `triage_complete` marker;
    4. `"in_progress"`, with the same marker;
    5. `"review"` with `pr=121`, with the same marker;
    6. today's stage 4 (complete, closed, merged).
  - **The running snapshot.** A module constant `HERO_STAGE = 4` replaces the `2` in
    `_snapshot(running=2 if stage == 2 else 1)`. `_snapshot` gains a running row for #60 with
    `kind="triage"`, `state=None`, `turns=1` and a start 1 minute ago, so the hero's Running
    panel shows `(triage)`. Keep `max_concurrent_agents=3`, so three rows still fit.
  - **The docstring.** Change "1 todo ... 4 complete" to the six stages.
- `capture.py`:
  - **Stages.** `range(1, 7)` in both loops and in the cleanup. The hero seeds
    `str(seed.HERO_STAGE)`, or a matching constant if importing `seed` is awkward, with a
    comment saying it must match.
  - **`CAPTIONS`:**
    1. `1.  a maintainer opens an issue; auto-triage labels it  issuebot/triage-todo`
    2. `2.  a read-only session sizes it and files its verdict:  fits`
    3. `3.  you label it  issuebot/todo`
    4. `4.  the worker claims it, clones, and runs  claude -p`
    5. `5.  it pushes a branch, opens a PR, and asks for review`
    6. `6.  you merge; the issue closes and lands in  issuebot/complete`
  - **`HOLD_MS`** is `(2300,) * 5 + (3000,)`.

- [ ] **Step 2: Regenerate the images**

Follow `tools/screenshots/README.md`, "Every time", exactly. Use `test-db` with `seed.py
history`, then `uv run issuebot web --port 8099 &` with the billing token, the webhook and the
password set as shown, then `capture.py`, then `kill %1` and `docker compose rm -sf test-db`.
Playwright's chromium is already in `~/.cache/ms-playwright`. Expected: the capture prints three
sizes, each under 500 KB, and exits 0. If the GIF is over the limit, lower `--width`'s default in
`capture.py` in steps of 40 until it fits, and say so in the commit message. Open each image with
the Read tool and check four things:
- the board has seven columns, all legible;
- the Triage and Triaged cards are present;
- the hero's Running panel shows `(triage)`;
- the GIF's first frame has #42 in Triage.

- [ ] **Step 3: Update the docs**

- `README.md`:
  - **Line 16's alt text.** Change "the Kanban board of the five label columns" to "the Kanban
    board, from Triage and Triaged through the five state labels".
  - **Line 53's alt text.** It now describes the six stages in the captions' order.
  - **The triage paragraph** gains one sentence: "On the dashboard an issue waiting for triage
    sits in a Triage column and one with a verdict in Triaged, left of Todo." Link it as
    `[dashboard](docs/dashboard.md#the-board)`.
- `docs/dashboard.md`:
  - Make the same alt-text change at line 9, and change line 13's "the Kanban of the five label
    columns" to "the Kanban of the label columns".
  - Add a new `## The board` section before `## The hero's six tiles`. It says:
    - there are seven columns in board order;
    - Triage holds `triage-todo` (waiting or being triaged), and Triaged holds `triage-complete`
      with no state label (waiting for your `todo`), where a split parent stays until it
      closes;
    - a state label wins, so an issue is in one column only;
    - a triage or parent-review run shows in Running marked `(triage)` or `(parent review)`;
    - a worker with a store reads the triage track once a poll, at 1 GraphQL point a page of
      33, and an issue closed or unlabelled on GitHub leaves its column on the next poll;
    - `/stats`'s `by_state` and `/issues?state=` take `triage_todo` and `triage_complete`, and
      `/state`'s `running[]` carries `kind`.
- `docs/operations.md`, the poll-cost paragraph:
  - **The idle figure** is now "about 650" an hour, computed as 120 polls × (4 state labels + 1
    triage read) plus the sweep's ~50.
  - **The cost rule** is "one point per state label per poll, and one for the two triage labels
    together, while no label holds more than 33 open issues".
  - **The capacity claim** is "so one account carries six to seven workers". Compute the result
    afresh rather than trusting this plan's arithmetic if the paragraph's inputs differ.
- `docs/package-layout.md`: at most one sentence each:
  - `issuebot.github`: `fetch_issues_with_any_label`.
  - `issuebot.db`: the `triage` column, `TRIAGE_CHANGED` and `reconcile_triage_track`, and the
    sink's `record_triage_track`.
  - `issuebot.orchestrator`: `_observe_triage_track` and why it is outside the hold.
  - `issuebot.web`: `BOARD_COLUMNS` and the run kind in Running.
- The spec:
  - The status line says phase 5 is built.
  - Add a "Decided while planning phase 5:" list under section 7, one bullet per ruling 1–12
    above, each in a sentence or two, in the style of the phase 3 list.
  - In "Left to the builder", strike nothing new: phase 5 settles none of the remaining items.
  - Leave the closed phase 2 note at line 364 as it is: it records why phase 2 did not
    change the card.

- [ ] **Step 4: Run everything**

Run: `uv run ruff check . && uv run ruff format --check .`, then `uv run pre-commit run --all-files`,
then the full suite against `test-db` and once without it.
Expected: all clean and green. `tests/test_doc_pointers.py` resolves `#the-board`, and
`tests/test_instruction_bounds.py` and `tests/test_readme_bounds.py` pass.

- [ ] **Step 5: Commit**

```bash
git add tools/screenshots docs/images README.md docs
git commit -m "docs: the Triage and Triaged columns, and the README's images regenerated"
```

## Deploying (for the PR body)

- This phase has a migration, `0008_triage_track.sql`. Run `python3 tools/upgrade/upgrade.py`
  from the hub checkout, which stops every worker, upgrades the hub first and migrates once.
  `labels ensure` is not needed.
- The two columns fill on the first poll after the upgrade. Rows written before it carry no
  `triage` until their issue is next read.
- `docs/images/social-preview.png` changes. GitHub does not read it from the tree, so someone
  with admin on the repository re-uploads it under **Settings → General → Social preview**.
