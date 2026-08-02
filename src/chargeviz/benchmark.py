from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chargeviz.archive import payload_sha256
from chargeviz.database import Database
from chargeviz.mfg import MFGAdapter
from chargeviz.models import PollTimings


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _payload(evse_count: int, iteration: int) -> bytes:
    changed_status = "CHARGING" if iteration % 2 else "AVAILABLE"
    updated_at = (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=iteration)).isoformat()
    evses = [
        {
            "uid": f"EVSE-{index:06d}",
            "evse_id": f"GB*MFG*E{index:06d}",
            "status": changed_status if iteration > 0 and index % 100 == 0 else "AVAILABLE",
            "connectors": [{"id": "1"}],
            "last_updated": updated_at,
        }
        for index in range(evse_count)
    ]
    return json.dumps(
        {
            "data": [
                {
                    "id": "BENCHMARK-LOCATION",
                    "evses": evses,
                }
            ]
        },
        separators=(",", ":"),
    ).encode()


def run_benchmark(
    *,
    evse_count: int,
    iterations: int,
    database_path: Path,
) -> dict[str, int | float]:
    if evse_count <= 0:
        raise ValueError("evse_count must be positive")
    if iterations < 2:
        raise ValueError("iterations must be at least 2")
    if database_path.exists():
        raise ValueError(f"benchmark database already exists: {database_path}")

    database = Database(database_path)
    adapter = MFGAdapter()
    parse_samples: list[float] = []
    persist_samples: list[float] = []
    payload_bytes = 0

    for iteration in range(iterations):
        body = _payload(evse_count, iteration)
        payload_bytes = len(body)
        parse_started = time.perf_counter()
        snapshot = adapter.parse(body)
        parse_ms = (time.perf_counter() - parse_started) * 1000
        observed = (
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=iteration, seconds=30)
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        poll_id = database.start_poll(scheduled_at=observed, started_at=observed)
        stats = database.complete_poll(
            poll_id=poll_id,
            observations=snapshot.observations,
            observed_at=observed,
            http_status=200,
            payload_sha256=payload_sha256(body),
            response_bytes=len(body),
            location_count=snapshot.location_count,
            connector_count=snapshot.connector_count,
            duplicate_count=snapshot.duplicate_count,
            unknown_status_count=snapshot.unknown_status_count,
            unrecognized_status_count=snapshot.unrecognized_status_count,
            timings=PollTimings(fetch_ms=0.0, parse_ms=parse_ms),
        )
        parse_samples.append(parse_ms)
        persist_samples.append(stats.persist_ms)

    parse_p95 = _percentile(parse_samples, 0.95)
    persist_p95 = _percentile(persist_samples, 0.95)
    end_to_end_samples = [
        parse_ms + persist_ms
        for parse_ms, persist_ms in zip(parse_samples, persist_samples, strict=True)
    ]
    end_to_end_p95 = _percentile(end_to_end_samples, 0.95)
    return {
        "evse_count": evse_count,
        "iterations": iterations,
        "payload_bytes": payload_bytes,
        "parse_p50_ms": _percentile(parse_samples, 0.50),
        "parse_p95_ms": parse_p95,
        "persist_p50_ms": _percentile(persist_samples, 0.50),
        "persist_p95_ms": persist_p95,
        "end_to_end_p95_ms": end_to_end_p95,
        "evses_per_second_at_p95": evse_count / (end_to_end_p95 / 1000),
        "poll_interval_budget_used_percent": end_to_end_p95 / 120_000 * 100,
        "database_bytes": database_path.stat().st_size,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark snapshot parsing and SQLite reduction.")
    parser.add_argument("--evses", type=int, default=30_000)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="chargeviz-benchmark-") as directory:
        result = run_benchmark(
            evse_count=args.evses,
            iterations=args.iterations,
            database_path=Path(directory) / "benchmark.sqlite3",
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
