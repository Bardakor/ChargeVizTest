from __future__ import annotations

from pathlib import Path

import pytest

from chargeviz.database import Database
from chargeviz.models import EVSEObservation, PollTimings
from chargeviz.sessions import analyze


def ingest(
    db: Database,
    *,
    observed_at: str,
    states: dict[str, tuple[str, str]],
) -> None:
    observations = [
        EVSEObservation(
            source="mfg",
            location_id="LOC-1",
            evse_uid=uid,
            evse_id=f"GB*MFG*E{uid}",
            status=status,
            source_last_updated=updated,
        )
        for uid, (status, updated) in states.items()
    ]
    poll_id = db.start_poll(scheduled_at=observed_at, started_at=observed_at)
    db.complete_poll(
        poll_id=poll_id,
        observations=observations,
        observed_at=observed_at,
        http_status=200,
        payload_sha256="b" * 64,
        response_bytes=100,
        location_count=1,
        connector_count=len(observations),
        duplicate_count=0,
        unknown_status_count=0,
        timings=PollTimings(fetch_ms=1.0, parse_ms=1.0, persist_ms=0.0),
    )


def test_only_fully_observed_charging_intervals_enter_the_average(tmp_path: Path) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={
            "COMPLETE": ("AVAILABLE", "2026-07-27T09:00:00.000000Z"),
            "LEFT": ("CHARGING", "2026-07-27T08:55:00.000000Z"),
            "OPEN": ("AVAILABLE", "2026-07-27T09:00:00.000000Z"),
        },
    )
    ingest(
        db,
        observed_at="2026-07-27T09:02:30.000000Z",
        states={
            "COMPLETE": ("CHARGING", "2026-07-27T09:01:00.000000Z"),
            "LEFT": ("AVAILABLE", "2026-07-27T09:02:00.000000Z"),
            "OPEN": ("CHARGING", "2026-07-27T09:02:00.000000Z"),
        },
    )
    ingest(
        db,
        observed_at="2026-07-27T09:14:30.000000Z",
        states={
            "COMPLETE": ("AVAILABLE", "2026-07-27T09:11:00.000000Z"),
            "LEFT": ("AVAILABLE", "2026-07-27T09:02:00.000000Z"),
            "OPEN": ("CHARGING", "2026-07-27T09:02:00.000000Z"),
        },
    )

    report = analyze(db.path)

    assert report.completed_session_count == 1
    assert report.average_duration_seconds == 600.0
    assert report.observation_time_average_duration_seconds == 720.0
    assert report.median_duration_seconds == 600.0
    assert report.left_censored_session_count == 1
    assert report.right_censored_session_count == 1
    assert report.invalid_duration_count == 0


def test_regressive_source_timestamp_falls_back_to_observation_time(tmp_path: Path) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:00:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:02:30.000000Z",
        states={"EVSE-1": ("CHARGING", "2026-07-27T09:02:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:06:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T08:59:00.000000Z")},
    )

    report = analyze(db.path)

    assert report.completed_session_count == 1
    assert report.average_duration_seconds == 240.0
    assert report.observation_time_fallback_count == 1
    assert report.invalid_duration_count == 0


def test_unchanged_source_timestamp_falls_back_to_observation_time(tmp_path: Path) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:00:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:02:30.000000Z",
        states={"EVSE-1": ("CHARGING", "2026-07-27T09:02:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:08:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:02:00.000000Z")},
    )

    report = analyze(db.path)

    assert report.average_duration_seconds == 360.0
    assert report.observation_time_fallback_count == 1


def test_gap_free_sensitivity_excludes_a_session_spanning_a_failed_poll(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:00:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:02:30.000000Z",
        states={"EVSE-1": ("CHARGING", "2026-07-27T09:02:00.000000Z")},
    )
    failed_poll_id = db.start_poll(
        scheduled_at="2026-07-27T09:04:30.000000Z",
        started_at="2026-07-27T09:04:30.000000Z",
    )
    db.fail_poll(
        poll_id=failed_poll_id,
        completed_at="2026-07-27T09:04:31.000000Z",
        outcome="NETWORK_ERROR",
        next_delay_seconds=120,
        error="timeout",
    )
    ingest(
        db,
        observed_at="2026-07-27T09:06:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:06:00.000000Z")},
    )

    report = analyze(db.path)

    assert report.completed_session_count == 1
    assert report.average_duration_seconds == 240.0
    assert report.gap_affected_session_count == 1
    assert report.gap_free_session_count == 0
    assert report.gap_free_average_duration_seconds is None


@pytest.mark.parametrize("ambiguous_status", ["UNKNOWN", "VENDOR_MAINTENANCE"])
def test_ambiguous_status_does_not_create_a_completed_session(
    tmp_path: Path,
    ambiguous_status: str,
) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:00:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:02:30.000000Z",
        states={"EVSE-1": ("CHARGING", "2026-07-27T09:02:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:04:30.000000Z",
        states={"EVSE-1": (ambiguous_status, "2026-07-27T09:04:00.000000Z")},
    )

    report = analyze(db.path)

    assert report.completed_session_count == 0
    assert report.ambiguous_session_count == 1


def test_source_boundary_before_previous_observation_uses_observation_time(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:00:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:02:30.000000Z",
        states={"EVSE-1": ("CHARGING", "2026-07-27T08:59:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:06:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:06:00.000000Z")},
    )

    report = analyze(db.path)

    assert report.average_duration_seconds == 240.0
    assert report.observation_time_fallback_count == 1


def test_failure_before_detected_start_marks_session_as_gap_affected(tmp_path: Path) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:00:00.000000Z")},
    )
    failed_poll_id = db.start_poll(
        scheduled_at="2026-07-27T09:02:30.000000Z",
        started_at="2026-07-27T09:02:30.000000Z",
    )
    db.fail_poll(
        poll_id=failed_poll_id,
        completed_at="2026-07-27T09:02:31.000000Z",
        outcome="NETWORK_ERROR",
        next_delay_seconds=120,
        error="timeout",
    )
    ingest(
        db,
        observed_at="2026-07-27T09:04:30.000000Z",
        states={"EVSE-1": ("CHARGING", "2026-07-27T09:04:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:06:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:06:00.000000Z")},
    )

    report = analyze(db.path)

    assert report.completed_session_count == 1
    assert report.gap_affected_session_count == 1


def test_another_sources_failure_does_not_mark_session_as_gap_affected(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:00:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:02:30.000000Z",
        states={"EVSE-1": ("CHARGING", "2026-07-27T09:02:00.000000Z")},
    )
    failed_poll_id = db.start_poll(
        source="other",
        scheduled_at="2026-07-27T09:03:30.000000Z",
        started_at="2026-07-27T09:03:30.000000Z",
    )
    db.fail_poll(
        poll_id=failed_poll_id,
        completed_at="2026-07-27T09:03:31.000000Z",
        outcome="NETWORK_ERROR",
        next_delay_seconds=120,
        error="timeout",
    )
    ingest(
        db,
        observed_at="2026-07-27T09:04:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:04:00.000000Z")},
    )

    report = analyze(db.path)

    assert report.completed_session_count == 1
    assert report.gap_affected_session_count == 0


def test_missing_evse_between_boundaries_marks_session_as_gap_affected(tmp_path: Path) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={
            "EVSE-1": ("AVAILABLE", "2026-07-27T09:00:00.000000Z"),
            "CONTROL": ("AVAILABLE", "2026-07-27T09:00:00.000000Z"),
        },
    )
    ingest(
        db,
        observed_at="2026-07-27T09:02:30.000000Z",
        states={
            "EVSE-1": ("CHARGING", "2026-07-27T09:02:00.000000Z"),
            "CONTROL": ("AVAILABLE", "2026-07-27T09:00:00.000000Z"),
        },
    )
    ingest(
        db,
        observed_at="2026-07-27T09:04:30.000000Z",
        states={"CONTROL": ("AVAILABLE", "2026-07-27T09:00:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:06:30.000000Z",
        states={
            "EVSE-1": ("AVAILABLE", "2026-07-27T09:06:00.000000Z"),
            "CONTROL": ("AVAILABLE", "2026-07-27T09:00:00.000000Z"),
        },
    )

    report = analyze(db.path)

    assert report.completed_session_count == 1
    assert report.gap_affected_session_count == 1


def test_long_poll_pause_marks_session_as_gap_affected(tmp_path: Path) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:00:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:02:30.000000Z",
        states={"EVSE-1": ("CHARGING", "2026-07-27T09:02:00.000000Z")},
    )
    ingest(
        db,
        observed_at="2026-07-27T09:10:30.000000Z",
        states={"EVSE-1": ("AVAILABLE", "2026-07-27T09:10:00.000000Z")},
    )

    report = analyze(db.path)

    assert report.completed_session_count == 1
    assert report.gap_affected_session_count == 1


def test_open_baseline_charging_episode_is_not_also_right_censored(tmp_path: Path) -> None:
    db = Database(tmp_path / "sessions.sqlite3")
    ingest(
        db,
        observed_at="2026-07-27T09:00:30.000000Z",
        states={"EVSE-1": ("CHARGING", "2026-07-27T08:55:00.000000Z")},
    )

    report = analyze(db.path)

    assert report.left_censored_session_count == 1
    assert report.right_censored_session_count == 0
