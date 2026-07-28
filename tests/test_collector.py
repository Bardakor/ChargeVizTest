from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chargeviz.collector import Collector, CollectorConfig, source_lock_path
from chargeviz.database import Database
from chargeviz.http import HTTPResponse, HTTPStatusFailure, RateLimited
from chargeviz.mfg import MFGAdapter


def payload(status: str = "AVAILABLE") -> bytes:
    return json.dumps(
        {
            "data": [
                {
                    "id": "LOC-1",
                    "evses": [
                        {
                            "uid": "EVSE-1",
                            "evse_id": "GB*MFG*E1",
                            "status": status,
                            "connectors": [{"id": "1"}],
                            "last_updated": "2026-07-27T09:00:00Z",
                        }
                    ],
                }
            ]
        }
    ).encode()


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.origin = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self.origin + timedelta(seconds=self.elapsed)

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.elapsed += seconds


class FakeClient:
    def __init__(self, clock: FakeClock, outcomes: list[bytes | Exception]) -> None:
        self.clock = clock
        self.outcomes = iter(outcomes)
        self.started_at: list[float] = []
        self.in_flight = False

    def fetch(self, url: str) -> HTTPResponse:
        assert url == "https://example.test/locations"
        assert not self.in_flight
        self.in_flight = True
        self.started_at.append(self.clock.monotonic())
        try:
            outcome = next(self.outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return HTTPResponse(
                status=200,
                body=outcome,
                response_bytes=len(outcome),
                elapsed_ms=5.0,
            )
        finally:
            self.in_flight = False


def test_collection_is_sequential_and_start_to_start_cadence_is_respected(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    client = FakeClient(clock, [payload(), payload(), payload()])
    db = Database(tmp_path / "collector.sqlite3")
    collector = Collector(
        database=db,
        client=client,
        adapter=MFGAdapter(),
        config=CollectorConfig(interval_seconds=120, duration_seconds=250),
        source_url="https://example.test/locations",
        archive_dir=tmp_path / "raw",
        clock=clock,
        emit=lambda event: None,
    )

    summary = collector.run()

    assert client.started_at == [0.0, 120.0, 240.0]
    assert summary.attempt_count == 3
    assert summary.success_count == 3
    assert len(list((tmp_path / "raw").glob("*.json.gz"))) == 1


def test_rate_limit_honors_retry_after_and_records_attempt(tmp_path: Path) -> None:
    clock = FakeClock()
    limited = RateLimited(
        status=429,
        retry_after_seconds=8.0,
        response_bytes=10,
        elapsed_ms=5.0,
        message="rate limited",
    )
    client = FakeClient(clock, [limited, payload()])
    db = Database(tmp_path / "collector.sqlite3")
    collector = Collector(
        database=db,
        client=client,
        adapter=MFGAdapter(),
        config=CollectorConfig(interval_seconds=120, duration_seconds=130),
        source_url="https://example.test/locations",
        archive_dir=None,
        clock=clock,
        emit=lambda event: None,
    )

    summary = collector.run()

    assert client.started_at == [0.0, 120.0]
    assert summary.rate_limited_count == 1
    assert summary.success_count == 1
    with sqlite3.connect(db.path) as connection:
        rows = connection.execute(
            """
            SELECT outcome, retry_after_seconds, next_delay_seconds
            FROM poll_runs ORDER BY poll_id
            """
        ).fetchall()
    assert rows == [("RATE_LIMITED", 8.0, 120.0), ("SUCCESS", None, None)]


def test_restart_waits_for_the_persisted_rate_limit_boundary(tmp_path: Path) -> None:
    clock = FakeClock()
    db = Database(tmp_path / "collector.sqlite3")
    poll_id = db.start_poll(
        scheduled_at="2026-07-27T09:00:00.000000Z",
        started_at="2026-07-27T09:00:00.000000Z",
    )
    db.fail_poll(
        poll_id=poll_id,
        completed_at="2026-07-27T09:00:01.000000Z",
        outcome="RATE_LIMITED",
        http_status=429,
        retry_after_seconds=8.0,
        next_delay_seconds=180.0,
        response_bytes=10,
        fetch_ms=5.0,
        error="rate limited",
    )
    client = FakeClient(clock, [payload()])
    collector = Collector(
        database=db,
        client=client,
        adapter=MFGAdapter(),
        config=CollectorConfig(interval_seconds=120, duration_seconds=200),
        source_url="https://example.test/locations",
        archive_dir=None,
        clock=clock,
        emit=lambda event: None,
    )

    collector.run()

    assert client.started_at == [180.0]


def test_restart_restores_the_consecutive_rate_limit_backoff(tmp_path: Path) -> None:
    clock = FakeClock()
    db = Database(tmp_path / "collector.sqlite3")
    poll_id = db.start_poll(
        scheduled_at="2026-07-27T09:00:00.000000Z",
        started_at="2026-07-27T09:00:00.000000Z",
    )
    db.fail_poll(
        poll_id=poll_id,
        completed_at="2026-07-27T09:00:01.000000Z",
        outcome="RATE_LIMITED",
        http_status=429,
        retry_after_seconds=8.0,
        next_delay_seconds=120.0,
        response_bytes=10,
        fetch_ms=5.0,
        error="rate limited",
    )
    limited_again = RateLimited(
        status=429,
        retry_after_seconds=10.0,
        response_bytes=10,
        elapsed_ms=5.0,
        message="rate limited",
    )
    client = FakeClient(clock, [limited_again])
    collector = Collector(
        database=db,
        client=client,
        adapter=MFGAdapter(),
        config=CollectorConfig(interval_seconds=120, duration_seconds=200),
        source_url="https://example.test/locations",
        archive_dir=None,
        clock=clock,
        emit=lambda event: None,
    )

    collector.run()

    with sqlite3.connect(db.path) as connection:
        latest = connection.execute(
            """
            SELECT outcome, retry_after_seconds, next_delay_seconds
            FROM poll_runs ORDER BY poll_id DESC LIMIT 1
            """
        ).fetchone()
    assert client.started_at == [120.0]
    assert latest == ("RATE_LIMITED", 10.0, 240.0)


def test_restart_restores_the_consecutive_transient_failure_backoff(tmp_path: Path) -> None:
    clock = FakeClock()
    db = Database(tmp_path / "collector.sqlite3")
    poll_id = db.start_poll(
        scheduled_at="2026-07-27T09:00:00.000000Z",
        started_at="2026-07-27T09:00:00.000000Z",
    )
    db.fail_poll(
        poll_id=poll_id,
        completed_at="2026-07-27T09:00:01.000000Z",
        outcome="HTTP_ERROR",
        http_status=503,
        next_delay_seconds=120.0,
        response_bytes=10,
        fetch_ms=5.0,
        error="unavailable",
    )
    unavailable_again = HTTPStatusFailure(
        status=503,
        retry_after_seconds=None,
        response_bytes=10,
        elapsed_ms=5.0,
        message="unavailable",
    )
    client = FakeClient(clock, [unavailable_again])
    collector = Collector(
        database=db,
        client=client,
        adapter=MFGAdapter(),
        config=CollectorConfig(interval_seconds=120, duration_seconds=200),
        source_url="https://example.test/locations",
        archive_dir=None,
        clock=clock,
        emit=lambda event: None,
    )

    collector.run()

    with sqlite3.connect(db.path) as connection:
        latest = connection.execute(
            """
            SELECT outcome, next_delay_seconds
            FROM poll_runs ORDER BY poll_id DESC LIMIT 1
            """
        ).fetchone()
    assert client.started_at == [120.0]
    assert latest == ("HTTP_ERROR", 240.0)


def test_restart_keeps_backoff_streak_across_failure_types(tmp_path: Path) -> None:
    clock = FakeClock()
    db = Database(tmp_path / "collector.sqlite3")
    poll_id = db.start_poll(
        scheduled_at="2026-07-27T09:00:00.000000Z",
        started_at="2026-07-27T09:00:00.000000Z",
    )
    db.fail_poll(
        poll_id=poll_id,
        completed_at="2026-07-27T09:00:01.000000Z",
        outcome="HTTP_ERROR",
        http_status=503,
        next_delay_seconds=120.0,
        error="unavailable",
    )
    limited = RateLimited(
        status=429,
        retry_after_seconds=10.0,
        response_bytes=10,
        elapsed_ms=5.0,
        message="rate limited",
    )
    client = FakeClient(clock, [limited])
    collector = Collector(
        database=db,
        client=client,
        adapter=MFGAdapter(),
        config=CollectorConfig(interval_seconds=120, duration_seconds=200),
        source_url="https://example.test/locations",
        archive_dir=None,
        clock=clock,
        emit=lambda event: None,
    )

    collector.run()

    with sqlite3.connect(db.path) as connection:
        latest = connection.execute(
            """
            SELECT outcome, next_delay_seconds
            FROM poll_runs ORDER BY poll_id DESC LIMIT 1
            """
        ).fetchone()
    assert latest == ("RATE_LIMITED", 240.0)


def test_source_lock_is_independent_of_database_path(tmp_path: Path) -> None:
    first_database = tmp_path / "first.sqlite3"
    second_database = tmp_path / "second.sqlite3"
    url = "https://example.test/locations"

    assert first_database != second_database
    assert source_lock_path(url) == source_lock_path(url)
    assert source_lock_path(url) != source_lock_path(f"{url}?other=true")


def test_archive_failure_is_recorded_without_mutating_state(tmp_path: Path) -> None:
    clock = FakeClock()
    client = FakeClient(clock, [payload()])
    archive_path = tmp_path / "raw"
    archive_path.write_text("not a directory", encoding="utf-8")
    db = Database(tmp_path / "collector.sqlite3")
    collector = Collector(
        database=db,
        client=client,
        adapter=MFGAdapter(),
        config=CollectorConfig(interval_seconds=120, duration_seconds=1),
        source_url="https://example.test/locations",
        archive_dir=archive_path,
        clock=clock,
        emit=lambda event: None,
    )

    summary = collector.run()

    assert summary.failure_count == 1
    with sqlite3.connect(db.path) as connection:
        outcome = connection.execute("SELECT outcome FROM poll_runs").fetchone()
        states = connection.execute("SELECT COUNT(*) FROM current_evse_state").fetchone()
    assert outcome == ("INTERNAL_ERROR",)
    assert states == (0,)


def test_invalid_success_body_is_archived_for_replay(tmp_path: Path) -> None:
    clock = FakeClock()
    client = FakeClient(clock, [b"not-json"])
    archive_path = tmp_path / "raw"
    db = Database(tmp_path / "collector.sqlite3")
    collector = Collector(
        database=db,
        client=client,
        adapter=MFGAdapter(),
        config=CollectorConfig(interval_seconds=120, duration_seconds=1),
        source_url="https://example.test/locations",
        archive_dir=archive_path,
        clock=clock,
        emit=lambda event: None,
    )

    summary = collector.run()

    assert summary.failure_count == 1
    assert len(list(archive_path.glob("*.json.gz"))) == 1
