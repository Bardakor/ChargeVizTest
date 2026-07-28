from __future__ import annotations

from pathlib import Path

from chargeviz.benchmark import run_benchmark


def test_benchmark_reports_processing_headroom(tmp_path: Path) -> None:
    result = run_benchmark(
        evse_count=100,
        iterations=2,
        database_path=tmp_path / "benchmark.sqlite3",
    )

    assert result["evse_count"] == 100
    assert result["iterations"] == 2
    assert result["payload_bytes"] > 0
    assert result["parse_p95_ms"] > 0
    assert result["persist_p95_ms"] > 0
    assert result["end_to_end_p95_ms"] > 0
    assert result["evses_per_second_at_p95"] > 0
    assert 0 < result["poll_interval_budget_used_percent"] < 100
