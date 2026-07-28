from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chargeviz.database import Database
from chargeviz.models import EVSEObservation, PollTimings


def observation(
    *,
    uid: str = "EVSE-1",
    status: str = "AVAILABLE",
    source_last_updated: str = "2026-07-27T09:00:00.000000Z",
) -> EVSEObservation:
    return EVSEObservation(
        source="mfg",
        location_id="LOC-1",
        evse_uid=uid,
        evse_id=f"GB*MFG*E{uid}",
        status=status,
        source_last_updated=source_last_updated,
    )


def complete(
    db: Database,
    observations: list[EVSEObservation],
    *,
    observed_at: str,
) -> object:
    poll_id = db.start_poll(scheduled_at=observed_at, started_at=observed_at)
    return db.complete_poll(
        poll_id=poll_id,
        observations=observations,
        observed_at=observed_at,
        http_status=200,
        payload_sha256="a" * 64,
        response_bytes=100,
        location_count=1,
        connector_count=len(observations),
        duplicate_count=0,
        unknown_status_count=0,
        timings=PollTimings(fetch_ms=10.0, parse_ms=2.0, persist_ms=0.0),
    )


def scalar(db_path: Path, sql: str) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute(sql).fetchone()[0])


def test_initial_snapshot_establishes_state_without_inventing_changes(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    db = Database(path)

    stats = complete(
        db,
        [observation(), observation(uid="EVSE-2", status="CHARGING")],
        observed_at="2026-07-27T09:00:30.000000Z",
    )

    assert stats.initial_count == 2
    assert stats.change_count == 0
    assert stats.unchanged_count == 0
    assert scalar(path, "SELECT COUNT(*) FROM current_evse_state") == 2
    assert scalar(path, "SELECT COUNT(*) FROM status_events WHERE event_kind = 'INITIAL'") == 2
    assert scalar(path, "SELECT COUNT(*) FROM status_events WHERE event_kind = 'CHANGE'") == 0


def test_replayed_snapshot_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    db = Database(path)
    snapshot = [observation()]
    complete(db, snapshot, observed_at="2026-07-27T09:00:30.000000Z")

    stats = complete(db, snapshot, observed_at="2026-07-27T09:02:30.000000Z")

    assert stats.initial_count == 0
    assert stats.change_count == 0
    assert stats.unchanged_count == 1
    assert scalar(path, "SELECT COUNT(*) FROM status_events") == 1
    assert scalar(path, "SELECT COUNT(*) FROM poll_runs WHERE outcome = 'SUCCESS'") == 2


def test_status_change_is_recorded_once_and_updates_current_state(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    db = Database(path)
    complete(db, [observation()], observed_at="2026-07-27T09:00:30.000000Z")

    stats = complete(
        db,
        [
            observation(
                status="CHARGING",
                source_last_updated="2026-07-27T09:01:50.000000Z",
            )
        ],
        observed_at="2026-07-27T09:02:30.000000Z",
    )

    assert stats.change_count == 1
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT previous_status, status, source_last_updated
            FROM status_events WHERE event_kind = 'CHANGE'
            """
        ).fetchone()
        state = connection.execute(
            "SELECT status FROM current_evse_state WHERE evse_uid = 'EVSE-1'"
        ).fetchone()
    assert row == ("AVAILABLE", "CHARGING", "2026-07-27T09:01:50.000000Z")
    assert state == ("CHARGING",)


def test_missing_evse_is_counted_without_synthesizing_a_transition(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    db = Database(path)
    complete(
        db,
        [observation(uid="EVSE-1"), observation(uid="EVSE-2", status="CHARGING")],
        observed_at="2026-07-27T09:00:30.000000Z",
    )

    stats = complete(
        db,
        [observation(uid="EVSE-1")],
        observed_at="2026-07-27T09:02:30.000000Z",
    )

    assert stats.missing_count == 1
    assert scalar(path, "SELECT COUNT(*) FROM status_events") == 2
    with sqlite3.connect(path) as connection:
        missing = connection.execute(
            "SELECT consecutive_missing_polls FROM current_evse_state WHERE evse_uid = 'EVSE-2'"
        ).fetchone()
    assert missing == (1,)


def test_snapshot_business_updates_are_atomic(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    db = Database(path)
    poll_id = db.start_poll(
        scheduled_at="2026-07-27T09:00:00.000000Z",
        started_at="2026-07-27T09:00:00.000000Z",
    )
    invalid = observation(uid="EVSE-2")
    object.__setattr__(invalid, "status", None)

    with pytest.raises(sqlite3.IntegrityError):
        db.complete_poll(
            poll_id=poll_id,
            observations=[observation(), invalid],
            observed_at="2026-07-27T09:00:30.000000Z",
            http_status=200,
            payload_sha256="a" * 64,
            response_bytes=100,
            location_count=1,
            connector_count=2,
            duplicate_count=0,
            unknown_status_count=0,
            timings=PollTimings(fetch_ms=10.0, parse_ms=2.0, persist_ms=0.0),
        )

    assert scalar(path, "SELECT COUNT(*) FROM current_evse_state") == 0
    assert scalar(path, "SELECT COUNT(*) FROM status_events") == 0
    assert scalar(path, "SELECT COUNT(*) FROM poll_runs WHERE outcome = 'RUNNING'") == 1
