from __future__ import annotations

import math
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from chargeviz.archive import archive_payload, payload_sha256
from chargeviz.database import Database
from chargeviz.http import (
    HTTPClient,
    HTTPStatusFailure,
    NetworkFailure,
    RateLimited,
    RequestFailure,
)
from chargeviz.lock import RunLock
from chargeviz.mfg import MFGAdapter, PayloadError
from chargeviz.models import CollectionSummary, PollTimings


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def source_lock_path(source_url: str) -> Path:
    digest = payload_sha256(source_url.strip().encode())
    return Path(tempfile.gettempdir()) / "chargeviz-source-locks" / digest


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Client(Protocol):
    def fetch(self, url: str) -> Any: ...


class Adapter(Protocol):
    source: str

    def parse(self, payload: bytes) -> Any: ...


@dataclass(frozen=True)
class CollectorConfig:
    interval_seconds: float = 120.0
    duration_seconds: float = 7200.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.interval_seconds) or not math.isfinite(self.duration_seconds):
            raise ValueError("poll interval and collection duration must be finite")
        if self.interval_seconds < 120:
            raise ValueError("poll interval must be at least 120 seconds")
        if self.duration_seconds <= 0:
            raise ValueError("collection duration must be positive")


def compute_retry_delay(
    *,
    interval_seconds: float,
    retry_after_seconds: float | None,
    consecutive_rate_limits: int,
) -> float:
    attempts = max(1, consecutive_rate_limits)
    exponential = min(900.0, interval_seconds * (2 ** (attempts - 1)))
    return max(interval_seconds, exponential, retry_after_seconds or 0.0)


@dataclass(frozen=True, slots=True)
class _Failure:
    """Everything the attempt ledger needs in order to record one failed poll."""

    outcome: str
    error: str
    status: int | None = None
    retry_after_seconds: float | None = None
    response_bytes: int | None = None
    fetch_ms: float | None = None
    payload_sha256: str | None = None


# Ordered most specific first: RateLimited is a subclass of HTTPStatusFailure.
_REQUEST_FAILURE_OUTCOMES: tuple[tuple[type[RequestFailure], str], ...] = (
    (RateLimited, "RATE_LIMITED"),
    (HTTPStatusFailure, "HTTP_ERROR"),
    (NetworkFailure, "NETWORK_ERROR"),
)


def _describe_failure(error: Exception, response: Any | None) -> _Failure:
    """Map any exception raised during a poll onto the row that will record it."""
    if isinstance(error, RequestFailure):
        # The request itself failed, so the transport carries the diagnostic facts.
        return _Failure(
            outcome=next(
                name for kind, name in _REQUEST_FAILURE_OUTCOMES if isinstance(error, kind)
            ),
            error=str(error),
            status=error.status,
            retry_after_seconds=error.retry_after_seconds,
            response_bytes=error.response_bytes,
            fetch_ms=error.elapsed_ms,
        )

    # A body arrived but could not be turned into a snapshot. Keep what the response
    # reported and hash the bytes so the failure stays replayable from the archive.
    rejected_payload = isinstance(error, PayloadError)
    return _Failure(
        outcome="INVALID_PAYLOAD" if rejected_payload else "INTERNAL_ERROR",
        error=str(error) if rejected_payload else f"{type(error).__name__}: {error}",
        status=200 if rejected_payload else getattr(response, "status", None),
        response_bytes=getattr(response, "response_bytes", None),
        fetch_ms=getattr(response, "elapsed_ms", None),
        payload_sha256=payload_sha256(response.body) if response is not None else None,
    )


class Collector:
    def __init__(
        self,
        *,
        database: Database,
        source_url: str,
        config: CollectorConfig,
        client: Client | None = None,
        adapter: Adapter | None = None,
        archive_dir: str | Path | None = None,
        clock: Clock | None = None,
        emit: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.database = database
        self.source_url = source_url
        self.config = config
        self.client = client or HTTPClient()
        self.adapter = adapter or MFGAdapter()
        self.archive_dir = Path(archive_dir) if archive_dir is not None else None
        self.clock = clock or SystemClock()
        self.emit = emit or (lambda event: None)

    def _emit(self, event: str, **values: object) -> None:
        self.emit({"event": event, **values})

    def run(self) -> CollectionSummary:
        with RunLock(source_lock_path(self.source_url)), RunLock(self.database.path):
            return self._run()

    def _run(self) -> CollectionSummary:
        deadline = self.clock.monotonic() + self.config.duration_seconds
        persisted_boundary = self.database.next_request_at(
            source=self.adapter.source,
            interval_seconds=self.config.interval_seconds,
        )
        wall_wait = (
            max(0.0, (persisted_boundary - self.clock.now()).total_seconds())
            if persisted_boundary is not None
            else 0.0
        )
        next_due = self.clock.monotonic() + wall_wait
        consecutive_failures = self.database.trailing_unsuccessful_count(source=self.adapter.source)
        attempts = successes = rate_limits = failures = initials = changes = 0

        while self.clock.monotonic() < deadline:
            remaining_to_due = next_due - self.clock.monotonic()
            if remaining_to_due > 0:
                remaining_run = deadline - self.clock.monotonic()
                self.clock.sleep(min(remaining_to_due, remaining_run))
                if self.clock.monotonic() >= deadline:
                    break

            # Any wait is already served, so the poll is scheduled and started at once.
            request_started_mono = self.clock.monotonic()
            started_at = _timestamp(self.clock.now())
            poll_id = self.database.start_poll(
                scheduled_at=started_at,
                started_at=started_at,
                source=self.adapter.source,
            )
            attempts += 1
            self._emit(
                "poll_started",
                poll_id=poll_id,
                source=self.adapter.source,
                started_at=started_at,
            )
            delay = self.config.interval_seconds
            response: Any | None = None

            try:
                response = self.client.fetch(self.source_url)
                digest = payload_sha256(response.body)
                if self.archive_dir is not None:
                    archive_payload(response.body, self.archive_dir, digest)
                parse_started = time.perf_counter()
                snapshot = self.adapter.parse(response.body)
                parse_ms = (time.perf_counter() - parse_started) * 1000
                observed_at = _timestamp(self.clock.now())
                stats = self.database.complete_poll(
                    poll_id=poll_id,
                    observations=snapshot.observations,
                    observed_at=observed_at,
                    http_status=response.status,
                    payload_sha256=digest,
                    response_bytes=response.response_bytes,
                    location_count=snapshot.location_count,
                    connector_count=snapshot.connector_count,
                    duplicate_count=snapshot.duplicate_count,
                    unknown_status_count=snapshot.unknown_status_count,
                    unrecognized_status_count=snapshot.unrecognized_status_count,
                    timings=PollTimings(fetch_ms=response.elapsed_ms, parse_ms=parse_ms),
                )
                successes += 1
                initials += stats.initial_count
                changes += stats.change_count
                consecutive_failures = 0
                self._emit(
                    "poll_succeeded",
                    poll_id=poll_id,
                    evse_count=len(snapshot.observations),
                    initial_count=stats.initial_count,
                    change_count=stats.change_count,
                    unchanged_count=stats.unchanged_count,
                    missing_count=stats.missing_count,
                    fetch_ms=round(response.elapsed_ms, 3),
                    parse_ms=round(parse_ms, 3),
                    persist_ms=round(stats.persist_ms, 3),
                    payload_sha256=digest,
                )
            except Exception as error:
                failure = _describe_failure(error, response)
                rate_limited = failure.outcome == "RATE_LIMITED"
                rate_limits += int(rate_limited)
                failures += int(not rate_limited)
                consecutive_failures += 1
                delay = compute_retry_delay(
                    interval_seconds=self.config.interval_seconds,
                    retry_after_seconds=failure.retry_after_seconds,
                    consecutive_rate_limits=consecutive_failures,
                )
                try:
                    self.database.fail_poll(
                        poll_id=poll_id,
                        completed_at=_timestamp(self.clock.now()),
                        outcome=failure.outcome,
                        http_status=failure.status,
                        retry_after_seconds=failure.retry_after_seconds,
                        next_delay_seconds=delay,
                        payload_sha256=failure.payload_sha256,
                        response_bytes=failure.response_bytes,
                        fetch_ms=failure.fetch_ms,
                        error=failure.error,
                    )
                except Exception:
                    # The ledger is the only record that this attempt happened; if it
                    # cannot be written, the cadence guarantee no longer holds on restart.
                    self._emit(
                        "poll_failed",
                        poll_id=poll_id,
                        outcome="UNRECOVERABLE_INTERNAL_ERROR",
                        error=failure.error,
                    )
                    raise
                if rate_limited:
                    self._emit(
                        "poll_rate_limited",
                        poll_id=poll_id,
                        retry_after_seconds=failure.retry_after_seconds,
                        next_delay_seconds=delay,
                    )
                else:
                    self._emit(
                        "poll_failed",
                        poll_id=poll_id,
                        outcome=failure.outcome,
                        error=failure.error,
                        next_delay_seconds=delay,
                    )

            next_due = request_started_mono + delay

        summary = CollectionSummary(
            attempt_count=attempts,
            success_count=successes,
            rate_limited_count=rate_limits,
            failure_count=failures,
            initial_count=initials,
            change_count=changes,
        )
        self._emit(
            "collection_finished",
            attempt_count=attempts,
            success_count=successes,
            rate_limited_count=rate_limits,
            failure_count=failures,
            initial_count=initials,
            change_count=changes,
        )
        return summary
