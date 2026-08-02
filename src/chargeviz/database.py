from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from chargeviz.models import EVSEObservation, PollTimings, SnapshotStats

# Outcomes a poll can end in. Failure outcomes are the ones `fail_poll` accepts;
# the CHECK constraint below is generated from the same values so the two cannot drift.
FAILURE_OUTCOMES = frozenset(
    {"RATE_LIMITED", "HTTP_ERROR", "NETWORK_ERROR", "INVALID_PAYLOAD", "INTERNAL_ERROR"}
)
POLL_OUTCOMES = frozenset({"RUNNING", "SUCCESS"}) | FAILURE_OUTCOMES

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS poll_runs (
    poll_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT NOT NULL CHECK (
        outcome IN ({", ".join(repr(name) for name in sorted(POLL_OUTCOMES))})
    ),
    http_status INTEGER,
    retry_after_seconds REAL,
    next_delay_seconds REAL,
    payload_sha256 TEXT,
    response_bytes INTEGER,
    location_count INTEGER,
    evse_count INTEGER,
    connector_count INTEGER,
    duplicate_count INTEGER,
    unknown_status_count INTEGER,
    unrecognized_status_count INTEGER,
    initial_count INTEGER,
    change_count INTEGER,
    unchanged_count INTEGER,
    missing_count INTEGER,
    regressive_timestamp_count INTEGER,
    fetch_ms REAL,
    parse_ms REAL,
    persist_ms REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS current_evse_state (
    source TEXT NOT NULL,
    location_id TEXT NOT NULL,
    evse_uid TEXT NOT NULL,
    evse_id TEXT,
    status TEXT NOT NULL,
    source_last_updated TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_poll_id INTEGER NOT NULL REFERENCES poll_runs(poll_id),
    consecutive_missing_polls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, location_id, evse_uid)
);

CREATE TABLE IF NOT EXISTS status_events (
    event_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    location_id TEXT NOT NULL,
    evse_uid TEXT NOT NULL,
    evse_id TEXT,
    poll_id INTEGER NOT NULL REFERENCES poll_runs(poll_id),
    previous_poll_id INTEGER REFERENCES poll_runs(poll_id),
    event_kind TEXT NOT NULL CHECK (event_kind IN ('INITIAL', 'CHANGE')),
    previous_status TEXT,
    status TEXT NOT NULL,
    source_last_updated TEXT NOT NULL,
    previous_observed_at TEXT,
    observed_at TEXT NOT NULL,
    CHECK (
        (
            event_kind = 'INITIAL'
            AND previous_status IS NULL
            AND previous_poll_id IS NULL
            AND previous_observed_at IS NULL
        )
        OR
        (
            event_kind = 'CHANGE'
            AND previous_status IS NOT NULL
            AND previous_status <> status
            AND previous_poll_id IS NOT NULL
            AND previous_observed_at IS NOT NULL
        )
    ),
    UNIQUE (source, location_id, evse_uid, poll_id),
    FOREIGN KEY (source, location_id, evse_uid)
        REFERENCES current_evse_state(source, location_id, evse_uid)
);

CREATE TABLE IF NOT EXISTS evse_absences (
    poll_id INTEGER NOT NULL REFERENCES poll_runs(poll_id),
    source TEXT NOT NULL,
    location_id TEXT NOT NULL,
    evse_uid TEXT NOT NULL,
    PRIMARY KEY (poll_id, source, location_id, evse_uid)
);

CREATE INDEX IF NOT EXISTS status_events_entity_time
ON status_events(source, location_id, evse_uid, observed_at, event_id);

CREATE INDEX IF NOT EXISTS poll_runs_outcome_started
ON poll_runs(outcome, started_at);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(SCHEMA)

    def start_poll(
        self,
        *,
        scheduled_at: str,
        started_at: str,
        source: str = "mfg",
    ) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO poll_runs(source, scheduled_at, started_at, outcome)
                VALUES (?, ?, ?, 'RUNNING')
                """,
                (source, scheduled_at, started_at),
            )
            return int(cursor.lastrowid)

    def next_request_at(
        self,
        *,
        source: str,
        interval_seconds: float,
    ) -> datetime | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT started_at, retry_after_seconds, next_delay_seconds
                FROM poll_runs
                WHERE source = ?
                ORDER BY poll_id DESC
                LIMIT 1
                """,
                (source,),
            ).fetchone()
        if row is None:
            return None
        started_at = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
        persisted_wait = float(row["next_delay_seconds"] or row["retry_after_seconds"] or 0.0)
        if not math.isfinite(persisted_wait):
            persisted_wait = interval_seconds
        wait_seconds = max(interval_seconds, persisted_wait)
        return started_at + timedelta(seconds=wait_seconds)

    def trailing_unsuccessful_count(self, *, source: str) -> int:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT outcome FROM poll_runs
                WHERE source = ?
                ORDER BY poll_id DESC
                """,
                (source,),
            ).fetchall()
        count = 0
        for row in rows:
            if row["outcome"] == "SUCCESS":
                break
            count += 1
        return count

    def complete_poll(
        self,
        *,
        poll_id: int,
        observations: Sequence[EVSEObservation],
        observed_at: str,
        http_status: int,
        payload_sha256: str,
        response_bytes: int,
        location_count: int,
        connector_count: int,
        duplicate_count: int,
        unknown_status_count: int,
        timings: PollTimings,
        unrecognized_status_count: int = 0,
    ) -> SnapshotStats:
        persist_started = time.perf_counter()
        incoming = {(item.source, item.location_id, item.evse_uid): item for item in observations}
        if len(incoming) != len(observations):
            raise ValueError("observations contain duplicate EVSE identities")

        initial_count = 0
        change_count = 0
        unchanged_count = 0
        regressive_count = 0

        with self.connection() as connection:
            poll = connection.execute(
                "SELECT source, outcome FROM poll_runs WHERE poll_id = ?",
                (poll_id,),
            ).fetchone()
            if poll is None:
                raise ValueError(f"unknown poll_id {poll_id}")
            if poll["outcome"] != "RUNNING":
                raise ValueError(f"poll_id {poll_id} is already complete")
            source = str(poll["source"])
            if any(item.source != source for item in observations):
                raise ValueError("observation source does not match poll source")

            existing_rows = connection.execute(
                """
                SELECT source, location_id, evse_uid, status, source_last_updated,
                       last_seen_at, last_poll_id
                FROM current_evse_state WHERE source = ?
                """,
                (source,),
            ).fetchall()
            existing = {
                (row["source"], row["location_id"], row["evse_uid"]): row for row in existing_rows
            }

            for key, item in incoming.items():
                previous = existing.get(key)
                if previous is None:
                    connection.execute(
                        """
                        INSERT INTO current_evse_state(
                            source, location_id, evse_uid, evse_id, status,
                            source_last_updated, first_seen_at, last_seen_at, last_poll_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.source,
                            item.location_id,
                            item.evse_uid,
                            item.evse_id,
                            item.status,
                            item.source_last_updated,
                            observed_at,
                            observed_at,
                            poll_id,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO status_events(
                            source, location_id, evse_uid, evse_id, poll_id, previous_poll_id,
                            event_kind, previous_status, status, source_last_updated,
                            previous_observed_at, observed_at
                        ) VALUES (?, ?, ?, ?, ?, NULL, 'INITIAL', NULL, ?, ?, NULL, ?)
                        """,
                        (
                            item.source,
                            item.location_id,
                            item.evse_uid,
                            item.evse_id,
                            poll_id,
                            item.status,
                            item.source_last_updated,
                            observed_at,
                        ),
                    )
                    initial_count += 1
                    continue

                regressive_count += int(item.source_last_updated < previous["source_last_updated"])
                if item.status != previous["status"]:
                    connection.execute(
                        """
                        INSERT INTO status_events(
                            source, location_id, evse_uid, evse_id, poll_id, previous_poll_id,
                            event_kind, previous_status, status, source_last_updated,
                            previous_observed_at, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'CHANGE', ?, ?, ?, ?, ?)
                        """,
                        (
                            item.source,
                            item.location_id,
                            item.evse_uid,
                            item.evse_id,
                            poll_id,
                            previous["last_poll_id"],
                            previous["status"],
                            item.status,
                            item.source_last_updated,
                            previous["last_seen_at"],
                            observed_at,
                        ),
                    )
                    change_count += 1
                else:
                    unchanged_count += 1

                connection.execute(
                    """
                    UPDATE current_evse_state
                    SET evse_id = ?, status = ?, source_last_updated = ?,
                        last_seen_at = ?, last_poll_id = ?, consecutive_missing_polls = 0
                    WHERE source = ? AND location_id = ? AND evse_uid = ?
                    """,
                    (
                        item.evse_id,
                        item.status,
                        item.source_last_updated,
                        observed_at,
                        poll_id,
                        item.source,
                        item.location_id,
                        item.evse_uid,
                    ),
                )

            missing_keys = set(existing) - set(incoming)
            for missing_source, location_id, evse_uid in missing_keys:
                connection.execute(
                    """
                    UPDATE current_evse_state
                    SET consecutive_missing_polls = consecutive_missing_polls + 1
                    WHERE source = ? AND location_id = ? AND evse_uid = ?
                    """,
                    (missing_source, location_id, evse_uid),
                )
                connection.execute(
                    """
                    INSERT INTO evse_absences(poll_id, source, location_id, evse_uid)
                    VALUES (?, ?, ?, ?)
                    """,
                    (poll_id, missing_source, location_id, evse_uid),
                )

            persist_ms = (time.perf_counter() - persist_started) * 1000
            connection.execute(
                """
                UPDATE poll_runs
                SET completed_at = ?, outcome = 'SUCCESS', http_status = ?,
                    payload_sha256 = ?, response_bytes = ?, location_count = ?,
                    evse_count = ?, connector_count = ?, duplicate_count = ?,
                    unknown_status_count = ?, unrecognized_status_count = ?,
                    initial_count = ?, change_count = ?, unchanged_count = ?, missing_count = ?,
                    regressive_timestamp_count = ?, fetch_ms = ?, parse_ms = ?,
                    persist_ms = ?
                WHERE poll_id = ?
                """,
                (
                    observed_at,
                    http_status,
                    payload_sha256,
                    response_bytes,
                    location_count,
                    len(observations),
                    connector_count,
                    duplicate_count,
                    unknown_status_count,
                    unrecognized_status_count,
                    initial_count,
                    change_count,
                    unchanged_count,
                    len(missing_keys),
                    regressive_count,
                    timings.fetch_ms,
                    timings.parse_ms,
                    persist_ms,
                    poll_id,
                ),
            )

        return SnapshotStats(
            initial_count=initial_count,
            change_count=change_count,
            unchanged_count=unchanged_count,
            missing_count=len(missing_keys),
            regressive_timestamp_count=regressive_count,
            persist_ms=persist_ms,
        )

    def fail_poll(
        self,
        *,
        poll_id: int,
        completed_at: str,
        outcome: str,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        next_delay_seconds: float | None = None,
        response_bytes: int | None = None,
        fetch_ms: float | None = None,
        payload_sha256: str | None = None,
        error: str,
    ) -> None:
        if outcome not in FAILURE_OUTCOMES:
            raise ValueError(f"unsupported failure outcome {outcome!r}")
        with self.connection() as connection:
            updated = connection.execute(
                """
                UPDATE poll_runs
                SET completed_at = ?, outcome = ?, http_status = ?,
                    retry_after_seconds = ?, next_delay_seconds = ?,
                    response_bytes = ?, fetch_ms = ?,
                    payload_sha256 = ?, error = ?
                WHERE poll_id = ? AND outcome = 'RUNNING'
                """,
                (
                    completed_at,
                    outcome,
                    http_status,
                    retry_after_seconds,
                    next_delay_seconds,
                    response_bytes,
                    fetch_ms,
                    payload_sha256,
                    error[:2000],
                    poll_id,
                ),
            ).rowcount
            if updated != 1:
                raise ValueError(f"poll_id {poll_id} is missing or already complete")
