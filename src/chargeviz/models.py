from __future__ import annotations

from dataclasses import dataclass

OCPI_STATUSES = frozenset(
    {
        "AVAILABLE",
        "BLOCKED",
        "CHARGING",
        "INOPERATIVE",
        "OUTOFORDER",
        "PLANNED",
        "REMOVED",
        "RESERVED",
        "UNKNOWN",
    }
)
KNOWN_TERMINAL_STATUSES = OCPI_STATUSES - {"CHARGING", "UNKNOWN"}


@dataclass(frozen=True, slots=True)
class EVSEObservation:
    source: str
    location_id: str
    evse_uid: str
    evse_id: str | None
    status: str
    source_last_updated: str


@dataclass(frozen=True, slots=True)
class PollTimings:
    """Timings measured before persistence; the write itself is timed by the database."""

    fetch_ms: float
    parse_ms: float


@dataclass(frozen=True, slots=True)
class ParsedSnapshot:
    observations: tuple[EVSEObservation, ...]
    location_count: int
    connector_count: int
    duplicate_count: int
    unknown_status_count: int
    unrecognized_status_count: int


@dataclass(frozen=True, slots=True)
class SnapshotStats:
    initial_count: int
    change_count: int
    unchanged_count: int
    missing_count: int
    regressive_timestamp_count: int
    persist_ms: float


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    attempt_count: int
    success_count: int
    rate_limited_count: int
    failure_count: int
    initial_count: int
    change_count: int


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    completed_session_count: int
    average_duration_seconds: float | None
    observation_time_average_duration_seconds: float | None
    median_duration_seconds: float | None
    p90_duration_seconds: float | None
    minimum_duration_seconds: float | None
    maximum_duration_seconds: float | None
    left_censored_session_count: int
    right_censored_session_count: int
    ambiguous_session_count: int
    invalid_duration_count: int
    observation_time_fallback_count: int
    gap_affected_session_count: int
    gap_free_session_count: int
    gap_free_average_duration_seconds: float | None
    terminal_status_counts: dict[str, int]
    poll_attempt_count: int
    successful_poll_count: int
    rate_limited_poll_count: int
    failed_poll_count: int
    running_poll_count: int
    distinct_evse_count: int
    status_change_count: int
    first_poll_started_at: str | None
    last_poll_completed_at: str | None
    minimum_poll_start_gap_seconds: float | None
    median_poll_start_gap_seconds: float | None
    maximum_success_gap_seconds: float | None
    average_fetch_ms: float | None
    average_parse_ms: float | None
    average_persist_ms: float | None
    total_response_bytes: int
