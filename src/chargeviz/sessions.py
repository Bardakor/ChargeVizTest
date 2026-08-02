from __future__ import annotations

import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from chargeviz.models import KNOWN_TERMINAL_STATUSES, AnalysisReport


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _seconds(start: str, end: str) -> float:
    return (_parse(end) - _parse(start)).total_seconds()


def _start_gaps(rows: Sequence[sqlite3.Row]) -> list[float]:
    """Seconds between the starts of consecutive polls."""
    times = [_parse(str(row["started_at"])) for row in rows]
    return [
        (later - earlier).total_seconds() for earlier, later in zip(times, times[1:], strict=False)
    ]


def _average_column(rows: Sequence[sqlite3.Row], column: str) -> float | None:
    return _mean([float(row[column]) for row in rows if row[column] is not None])


@dataclass(slots=True)
class _ActiveSession:
    source_started_at: str
    observed_started_at: str
    source_start_trusted: bool
    uncertainty_window_start_poll_id: int
    eligible: bool


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def analyze(
    path: str | Path,
    *,
    expected_interval_seconds: float = 120.0,
) -> AnalysisReport:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        events = connection.execute(
            """
            SELECT event_id, source, location_id, evse_uid, poll_id, previous_poll_id,
                   event_kind, previous_status, status, source_last_updated,
                   previous_observed_at, observed_at
            FROM status_events
            ORDER BY source, location_id, evse_uid, event_id
            """
        ).fetchall()
        polls = connection.execute(
            """
            SELECT poll_id, source, started_at, completed_at, outcome, fetch_ms,
                   parse_ms, persist_ms, response_bytes
            FROM poll_runs ORDER BY poll_id
            """
        ).fetchall()
        absence_rows = connection.execute(
            "SELECT poll_id, source, location_id, evse_uid FROM evse_absences"
        ).fetchall()
        connection.rollback()
    finally:
        connection.close()

    absences = {
        (str(row["source"]), str(row["location_id"]), str(row["evse_uid"]), int(row["poll_id"]))
        for row in absence_rows
    }
    failed_poll_ids: dict[str, set[int]] = defaultdict(set)
    polls_by_source: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for poll in polls:
        source = str(poll["source"])
        polls_by_source[source].append(poll)
        if poll["outcome"] != "SUCCESS":
            failed_poll_ids[source].add(int(poll["poll_id"]))

    long_gap_ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    long_gap_threshold = expected_interval_seconds * 1.5
    for source, source_polls in polls_by_source.items():
        for previous, current in zip(source_polls, source_polls[1:], strict=False):
            gap = _seconds(str(previous["started_at"]), str(current["started_at"]))
            if gap > long_gap_threshold:
                long_gap_ranges[source].append((int(previous["poll_id"]), int(current["poll_id"])))

    durations: list[float] = []
    observation_durations: list[float] = []
    gap_free_durations: list[float] = []
    terminal_status_counts: Counter[str] = Counter()
    left_censored = 0
    right_censored = 0
    ambiguous = 0
    invalid = 0
    fallback = 0
    gap_affected = 0
    active: _ActiveSession | None = None
    current_entity: tuple[str, str, str] | None = None

    def finish_entity() -> None:
        nonlocal active, right_censored
        if active is not None and active.eligible:
            right_censored += 1
        active = None

    for row in events:
        entity = (str(row["source"]), str(row["location_id"]), str(row["evse_uid"]))
        if entity != current_entity:
            finish_entity()
            current_entity = entity

        status = str(row["status"])
        if row["event_kind"] == "INITIAL":
            if status == "CHARGING":
                left_censored += 1
                active = _ActiveSession(
                    source_started_at=str(row["source_last_updated"]),
                    observed_started_at=str(row["observed_at"]),
                    source_start_trusted=False,
                    uncertainty_window_start_poll_id=int(row["poll_id"]),
                    eligible=False,
                )
            continue

        if status == "CHARGING":
            source_started_at = str(row["source_last_updated"])
            observed_started_at = str(row["observed_at"])
            previous_observed_at = row["previous_observed_at"]
            source_start_trusted = bool(
                previous_observed_at
                and _parse(str(previous_observed_at))
                < _parse(source_started_at)
                <= _parse(observed_started_at)
            )
            active = _ActiveSession(
                source_started_at=source_started_at,
                observed_started_at=observed_started_at,
                source_start_trusted=source_start_trusted,
                uncertainty_window_start_poll_id=int(row["previous_poll_id"] or row["poll_id"]),
                eligible=True,
            )
            continue

        if active is None:
            continue
        if not active.eligible:
            active = None
            continue
        if status not in KNOWN_TERMINAL_STATUSES:
            ambiguous += 1
            active = None
            continue

        end_source = str(row["source_last_updated"])
        end_observed = str(row["observed_at"])
        previous_observed_at = row["previous_observed_at"]
        source_duration = _seconds(active.source_started_at, end_source)
        source_timestamps_plausible = (
            active.source_start_trusted
            and source_duration > 0
            and bool(previous_observed_at)
            and _parse(str(previous_observed_at)) < _parse(end_source) <= _parse(end_observed)
        )
        observation_duration = _seconds(active.observed_started_at, end_observed)
        if source_timestamps_plausible:
            duration = source_duration
        else:
            duration = observation_duration
            fallback += 1

        if duration < 0 or observation_duration < 0:
            invalid += 1
            active = None
            continue

        end_poll_id = int(row["poll_id"])
        window_start = active.uncertainty_window_start_poll_id
        has_failed_poll = any(
            window_start < item < end_poll_id for item in failed_poll_ids[entity[0]]
        )
        has_absence = any(
            (entity[0], entity[1], entity[2], poll_id) in absences
            for poll_id in range(window_start + 1, end_poll_id)
        )
        has_long_gap = any(
            window_start <= previous_poll and current_poll <= end_poll_id
            for previous_poll, current_poll in long_gap_ranges[entity[0]]
        )
        is_gap_affected = has_failed_poll or has_absence or has_long_gap
        gap_affected += int(is_gap_affected)
        durations.append(duration)
        observation_durations.append(observation_duration)
        terminal_status_counts[status] += 1
        if not is_gap_affected:
            gap_free_durations.append(duration)
        active = None

    finish_entity()

    durations.sort()
    percentile_90 = durations[max(0, math.ceil(0.9 * len(durations)) - 1)] if durations else None
    successful = [row for row in polls if row["outcome"] == "SUCCESS"]
    start_gaps = _start_gaps(polls)
    success_gaps = _start_gaps(successful)

    return AnalysisReport(
        completed_session_count=len(durations),
        average_duration_seconds=_mean(durations),
        observation_time_average_duration_seconds=_mean(observation_durations),
        median_duration_seconds=statistics.median(durations) if durations else None,
        p90_duration_seconds=percentile_90,
        minimum_duration_seconds=min(durations) if durations else None,
        maximum_duration_seconds=max(durations) if durations else None,
        left_censored_session_count=left_censored,
        right_censored_session_count=right_censored,
        ambiguous_session_count=ambiguous,
        invalid_duration_count=invalid,
        observation_time_fallback_count=fallback,
        gap_affected_session_count=gap_affected,
        gap_free_session_count=len(gap_free_durations),
        gap_free_average_duration_seconds=_mean(gap_free_durations),
        terminal_status_counts=dict(sorted(terminal_status_counts.items())),
        poll_attempt_count=len(polls),
        successful_poll_count=len(successful),
        rate_limited_poll_count=sum(row["outcome"] == "RATE_LIMITED" for row in polls),
        failed_poll_count=sum(
            row["outcome"] not in {"SUCCESS", "RATE_LIMITED", "RUNNING"} for row in polls
        ),
        running_poll_count=sum(row["outcome"] == "RUNNING" for row in polls),
        distinct_evse_count=sum(row["event_kind"] == "INITIAL" for row in events),
        status_change_count=sum(row["event_kind"] == "CHANGE" for row in events),
        first_poll_started_at=str(successful[0]["started_at"]) if successful else None,
        last_poll_completed_at=(
            str(next(row["completed_at"] for row in reversed(successful) if row["completed_at"]))
            if any(row["completed_at"] for row in successful)
            else None
        ),
        minimum_poll_start_gap_seconds=min(start_gaps) if start_gaps else None,
        median_poll_start_gap_seconds=statistics.median(start_gaps) if start_gaps else None,
        maximum_success_gap_seconds=max(success_gaps) if success_gaps else None,
        average_fetch_ms=_average_column(successful, "fetch_ms"),
        average_parse_ms=_average_column(successful, "parse_ms"),
        average_persist_ms=_average_column(successful, "persist_ms"),
        total_response_bytes=sum(int(row["response_bytes"] or 0) for row in polls),
    )
