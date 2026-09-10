"""The one-off import of a version-2, single-repository database into the hub (spec §4).

The source is read with the version-2 SQL below, written here and nowhere else, because
``Queries`` speaks version 3. Rows are streamed in batches through a server-side cursor so a
large ``run_turns`` never has to fit in memory. Everything lands in one target transaction.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from issuebot.config import GitHubLabels
from issuebot.db.connection import Connector, connect, error_text, redact
from issuebot.db.errors import ImportRefused
from issuebot.db.migrate import apply_migrations, schema_version
from issuebot.db.store import INSERT_TURN, REGISTER_REPO

SOURCE_VERSION = 2
BATCH = 500

READ_ISSUES = """
SELECT number, identifier, title, state, state_label, github_state, url, labels, pr_number,
       pr_url, pr_state, pr_merged_at, created_at, updated_at, closed_at, seen_at
FROM issues ORDER BY number
"""
WRITE_ISSUE = """
INSERT INTO issues (repo, number, identifier, title, state, state_label, github_state, url, labels,
                    pr_number, pr_url, pr_state, pr_merged_at, created_at, updated_at, closed_at,
                    seen_at)
VALUES (%(repo)s, %(number)s, %(identifier)s, %(title)s, %(state)s, %(state_label)s,
        %(github_state)s, %(url)s, %(labels)s, %(pr_number)s, %(pr_url)s, %(pr_state)s,
        %(pr_merged_at)s, %(created_at)s, %(updated_at)s, %(closed_at)s, %(seen_at)s)
"""

READ_RUNS = """
SELECT run_id, issue_number, issue_identifier, attempt, session_id, started_at, ended_at, outcome,
       error, turns, input_tokens, output_tokens, cost_usd, duration_s, workspace_path, log_dir
FROM runs ORDER BY started_at, run_id
"""
WRITE_RUN = """
INSERT INTO runs (repo, run_id, issue_number, issue_identifier, attempt, session_id, started_at,
                  ended_at, outcome, error, turns, input_tokens, output_tokens, cost_usd,
                  duration_s, workspace_path, log_dir)
VALUES (%(repo)s, %(run_id)s, %(issue_number)s, %(issue_identifier)s, %(attempt)s, %(session_id)s,
        %(started_at)s, %(ended_at)s, %(outcome)s, %(error)s, %(turns)s, %(input_tokens)s,
        %(output_tokens)s, %(cost_usd)s, %(duration_s)s, %(workspace_path)s, %(log_dir)s)
"""

READ_TURNS = """
SELECT run_id, turn_number, model, subtype, is_error, num_turns, input_tokens,
       cache_creation_input_tokens, cache_read_input_tokens, output_tokens, cost_usd, duration_ms,
       result_text, prompt, prompt_bytes, stream, stream_bytes, stream_lines, omitted_lines,
       stderr, stderr_bytes, truncated
FROM run_turns ORDER BY run_id, turn_number
"""
# INSERT_TURN (store.py) sets captured_at = now(); the original capture time is not carried,
# which the transcript page does not show for an imported run anyway.

READ_EVENTS = "SELECT at, kind, issue_number, run_id, payload FROM events ORDER BY id"
WRITE_EVENT = """
INSERT INTO events (repo, at, kind, issue_number, run_id, payload)
VALUES (%(repo)s, %(at)s, %(kind)s, %(issue_number)s, %(run_id)s, %(payload)s)
"""

READ_SNAPSHOT = "SELECT at, written_at, data FROM runtime_snapshot"
WRITE_SNAPSHOT = """
INSERT INTO runtime_snapshot (repo, at, written_at, data)
VALUES (%(repo)s, %(at)s, %(written_at)s, %(data)s)
"""

IS_REGISTERED = "SELECT 1 FROM repos WHERE repo = %(repo)s"


@dataclass(frozen=True, slots=True)
class ImportResult:
    counts: dict[str, int]  # table -> rows copied, in copy order


def _as_is(row: dict[str, Any]) -> dict[str, Any]:
    return row


def _json_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "payload": Jsonb(row["payload"])}


def _json_data(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "data": Jsonb(row["data"])}


# (table, read, write, adapt) in foreign-key order.
TABLES: tuple[tuple[str, str, str, Callable[[dict[str, Any]], dict[str, Any]]], ...] = (
    ("issues", READ_ISSUES, WRITE_ISSUE, _as_is),
    ("runs", READ_RUNS, WRITE_RUN, _as_is),
    ("run_turns", READ_TURNS, INSERT_TURN, _as_is),
    ("events", READ_EVENTS, WRITE_EVENT, _json_payload),
    ("runtime_snapshot", READ_SNAPSHOT, WRITE_SNAPSHOT, _json_data),
)


async def import_repo(
    source_url: str,
    target_url: str,
    *,
    repo: str,
    labels: GitHubLabels,
    workflow_path: str | None,
    connect: Connector = connect,
) -> ImportResult:
    """Copy a version-2 database into the hub, every row stamped with ``repo``.

    Refuses (``ImportRefused``) a source that is not at version 2 and a repository already in
    the target's registry; connection and statement failures are ``ImportRefused`` too, with
    both URLs redacted. Migrates the target first, so an empty hub needs no separate step.
    """
    try:
        source = await connect(source_url)
    except psycopg.Error as exc:
        raise ImportRefused(
            redact(f"cannot connect to the source: {error_text(exc)}", source_url)
        ) from exc
    try:
        try:
            target = await connect(target_url)
        except psycopg.Error as exc:
            raise ImportRefused(
                redact(f"cannot connect to the target: {error_text(exc)}", target_url)
            ) from exc
        try:
            return await _import(
                source, target, repo=repo, labels=labels, workflow_path=workflow_path
            )
        except psycopg.Error as exc:
            message = redact(
                redact(f"{type(exc).__name__}: {error_text(exc)}", source_url), target_url
            )
            raise ImportRefused(message) from exc
        finally:
            await target.close()
    finally:
        await source.close()


async def _import(
    source: AsyncConnection,
    target: AsyncConnection,
    *,
    repo: str,
    labels: GitHubLabels,
    workflow_path: str | None,
) -> ImportResult:
    version = await schema_version(source)
    if version != SOURCE_VERSION:
        raise ImportRefused(f"source is at schema version {version}, expected {SOURCE_VERSION}")
    await apply_migrations(target)
    found = await (await target.execute(IS_REGISTERED, {"repo": repo})).fetchone()
    if found is not None:
        raise ImportRefused(
            f"{repo} is already registered in the target; delete its rows first if you mean to "
            "import again"
        )
    counts: dict[str, int] = {}
    async with target.transaction(), source.transaction():
        await target.execute(
            REGISTER_REPO,
            {"repo": repo, "labels": Jsonb(labels.model_dump()), "workflow_path": workflow_path},
        )
        for table, read, write, adapt in TABLES:
            counts[table] = await _copy(source, target, read, write, adapt, repo)
    return ImportResult(counts=counts)


async def _copy(
    source: AsyncConnection,
    target: AsyncConnection,
    read: str,
    write: str,
    adapt: Callable[[dict[str, Any]], dict[str, Any]],
    repo: str,
) -> int:
    copied = 0
    async with (
        source.cursor(name="issuebot_import", row_factory=dict_row) as reader,
        target.cursor() as writer,
    ):
        reader.itersize = BATCH
        await reader.execute(read)
        while batch := await reader.fetchmany(BATCH):
            await writer.executemany(write, [{"repo": repo, **adapt(row)} for row in batch])
            copied += len(batch)
    return copied
